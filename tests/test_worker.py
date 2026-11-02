"""The SKIP LOCKED proof: two workers, twenty jobs, every job runs exactly once.

Both workers run in threads with their own connections while the jobs are in the table.
``FakeProcessor(delay=...)`` makes each job take a few milliseconds so the two loops really
overlap instead of one draining the queue before the other starts.
"""

from __future__ import annotations

import io
import threading
from collections import Counter
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from tests.conftest import MakeActor
from tests.fake_processor import FakeProcessor
from worker.main import Worker

MakeWorker = Callable[..., Worker]
N_JOBS = 20


def test_two_workers_run_every_job_exactly_once(
    committed_client: TestClient,
    make_committed_actor: MakeActor,
    make_worker: MakeWorker,
    committed_db: Session,
) -> None:
    alice = make_committed_actor("alice")
    for i in range(N_JOBS):
        r = committed_client.post(
            "/clouds",
            headers=alice.headers,
            files={"file": (f"c{i}.txt", io.BytesIO(b"0 0 0\n1 1 1\n"), "text/plain")},
        )
        assert r.status_code == 201
    assert committed_db.scalar(select(Job.id).where(Job.status == JobStatus.queued).limit(1))

    workers = [
        make_worker(FakeProcessor(delay=0.01), worker_id="w1"),
        make_worker(FakeProcessor(delay=0.01), worker_id="w2"),
    ]
    counts: list[int] = [0, 0]

    def drain(i: int) -> None:
        counts[i] = workers[i].drain()

    threads = [threading.Thread(target=drain, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "worker thread hung"

    assert sum(counts) == N_JOBS, counts

    committed_db.expire_all()
    jobs = committed_db.scalars(select(Job)).all()
    assert len(jobs) == N_JOBS
    assert all(j.status == JobStatus.done for j in jobs)
    assert all(j.attempts == 1 for j in jobs), "a double claim would show as attempts == 2"
    by_worker = Counter(j.worker_id for j in jobs)
    assert set(by_worker) == {"w1", "w2"}, f"both workers should have taken jobs: {by_worker}"
    assert sum(by_worker.values()) == N_JOBS


def test_run_stops_on_signal_flag(make_worker: MakeWorker) -> None:
    """``run()`` polls until ``stop`` is set; nothing queued, so it just has to return."""
    worker = make_worker()
    threading.Timer(0.1, worker.stop.set).start()
    assert worker.run() == 0
