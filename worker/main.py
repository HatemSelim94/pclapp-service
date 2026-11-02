"""The worker: claim one job, run it, record the outcome; repeat until told to stop.

The queue is the ``jobs`` table. Claiming is one short transaction::

    SELECT * FROM jobs WHERE status = 'queued'
    ORDER BY created_at, id LIMIT 1
    FOR UPDATE SKIP LOCKED;
    UPDATE ... SET status = 'running', attempts = attempts + 1, worker_id = ..., started_at = now()

``FOR UPDATE`` locks the row we are about to take; ``SKIP LOCKED`` makes a second worker step
over it instead of waiting, so N workers share the queue with no coordinator and no job is
claimed twice (``tests/test_worker.py`` runs two workers against twenty jobs to prove it).

The job itself runs outside any transaction. A second short transaction writes the result
and ``done``, or the error and either ``queued`` again (attempts left) or ``failed``.

SIGTERM/SIGINT set a flag; the loop finishes the job in hand and exits.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import Cloud, Job, JobStatus
from app.storage import Storage, get_storage
from worker import runners
from worker.processor import Processor
from worker.runners import JobSnapshot, PermanentJobError

log = logging.getLogger("pclapp.worker")


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        processor: Processor,
        storage: Storage,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self.sessions = session_factory
        self.processor = processor
        self.storage = storage
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stop = threading.Event()

    # --- the three transactions --------------------------------------------------------------

    def claim(self) -> JobSnapshot | None:
        """Take the oldest queued job, or None. One short transaction."""
        with self.sessions() as session, session.begin():
            job = session.scalars(
                select(Job)
                .where(Job.status == JobStatus.queued)
                .order_by(Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if job is None:
                return None
            cloud = session.get_one(Cloud, job.cloud_id)
            job.status = JobStatus.running
            job.attempts += 1
            job.worker_id = self.worker_id
            job.started_at = datetime.now(UTC)
            job.error = None
            return JobSnapshot.of(job, cloud)

    def _finish(self, snap: JobSnapshot, result: runners.Result) -> None:
        produced: str | None = None
        try:
            with self.sessions() as session, session.begin():
                job = session.get_one(Job, snap.id)
                produced = runners.apply(session, job, result, self.storage)
                job.status = JobStatus.done
                job.finished_at = datetime.now(UTC)
        except Exception:
            if produced:
                self.storage.delete(produced)
            raise

    def _fail(self, snap: JobSnapshot, error: str, *, permanent: bool) -> None:
        with self.sessions() as session, session.begin():
            job = session.get_one(Job, snap.id)
            job.error = error[:2000]
            if not permanent and job.attempts < job.max_attempts:
                job.status = JobStatus.queued  # back in the queue; attempts is already counted
                job.started_at = None
                job.worker_id = None
            else:
                job.status = JobStatus.failed
                job.finished_at = datetime.now(UTC)

    # --- the loop ------------------------------------------------------------------------------

    def run_one(self) -> bool:
        """Claim and run a single job. Returns False if the queue was empty."""
        snap = self.claim()
        if snap is None:
            return False
        log.info("job %s %s attempt %d/%d", snap.id, snap.type, snap.attempts, snap.max_attempts)
        try:
            result = runners.compute(snap, self.processor, self.storage)
            self._finish(snap, result)
        except PermanentJobError as exc:
            log.warning("job %s failed permanently: %s", snap.id, exc)
            self._fail(snap, str(exc), permanent=True)
        except Exception as exc:
            log.error("job %s failed: %s", snap.id, exc)
            self._fail(snap, f"{exc}\n{traceback.format_exc()}", permanent=False)
        return True

    def run(self, *, max_jobs: int | None = None) -> int:
        """Run until ``stop`` is set (or ``max_jobs`` done). Returns the number of jobs run."""
        done = 0
        while not self.stop.is_set() and (max_jobs is None or done < max_jobs):
            if self.run_one():
                done += 1
            else:
                self.stop.wait(self.settings.worker_poll_seconds)
        return done

    def drain(self) -> int:
        """Run until the queue is empty. For tests and one-shot invocations."""
        done = 0
        while self.run_one():
            done += 1
        return done


# --- entry point -------------------------------------------------------------------------------


def load_processor(spec: str) -> Processor:
    """``pclapp`` → the real one; ``module:Class`` → anything else (used by tests)."""
    if spec == "pclapp":
        from worker.pclapp_processor import PclappProcessor

        return PclappProcessor()
    module_name, _, cls_name = spec.partition(":")
    module: Any = __import__(module_name, fromlist=[cls_name])
    processor: Processor = getattr(module, cls_name)()
    return processor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pclapp-service job worker")
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument(
        "--processor",
        default=os.environ.get("PCLAPP_PROCESSOR", "pclapp"),
        help="'pclapp' or 'module:Class' implementing worker.processor.Processor",
    )
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("PCLAPP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    worker = Worker(
        get_sessionmaker(),
        load_processor(args.processor),
        get_storage(),
        get_settings(),
        worker_id=args.worker_id,
    )

    def _stop(signum: int, _frame: Any) -> None:
        log.info("signal %s: finishing current job, then exiting", signal.Signals(signum).name)
        worker.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("worker %s started (processor=%s)", worker.worker_id, args.processor)
    started = time.monotonic()
    n = worker.drain() if args.once else worker.run()
    log.info(
        "worker %s exiting after %d jobs in %.1fs", worker.worker_id, n, time.monotonic() - started
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
