"""``pclapp-cleanup``: remove files the request path deliberately leaves behind.

Three kinds, all older than a grace period (default 24 h) so nothing in flight is touched:

1. files of soft-deleted clouds (``DELETE /clouds/{id}`` only sets ``deleted_at``);
2. orphans in ``clouds/`` — a file with no row, left by a crash between rename and commit;
3. leftovers in ``tmp/`` — uploads or downsample outputs whose process died mid-way.

Rows are never deleted: a soft-deleted cloud keeps its row so jobs and lineage still resolve.
Run it from cron, or ``docker compose run --rm api pclapp-cleanup``. ``--dry-run`` only lists.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import Cloud
from app.storage import Storage, get_storage

log = logging.getLogger("pclapp.cleanup")


@dataclass
class Report:
    deleted_clouds: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    tmp: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.deleted_clouds) + len(self.orphans) + len(self.tmp)


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def _remove(path: Path, dry_run: bool) -> None:
    if not dry_run:
        path.unlink(missing_ok=True)


def run(session: Session, storage: Storage, grace: timedelta, *, dry_run: bool = False) -> Report:
    report = Report()
    now = datetime.now(UTC)
    cutoff_ts = time.time() - grace.total_seconds()

    # 1. soft-deleted clouds whose file is still there
    stale = session.scalars(
        select(Cloud).where(Cloud.deleted_at.is_not(None), Cloud.deleted_at < now - grace)
    )
    for cloud in stale:
        if storage.exists(cloud.path):
            _remove(storage.abs_path(cloud.path), dry_run)
            report.deleted_clouds.append(cloud.path)

    # 2. files in clouds/ with no row at all
    known = {row for row in session.scalars(select(Cloud.path))}
    for rel in storage.iter_cloud_files():
        if rel in known:
            continue
        path = storage.abs_path(rel)
        if not _is_cloud_filename(path.stem) or _older_than(path, cutoff_ts):
            _remove(path, dry_run)
            report.orphans.append(rel)

    # 3. tmp/ leftovers
    tmp_dir = storage.root / "tmp"
    if tmp_dir.is_dir():
        for path in sorted(tmp_dir.iterdir()):
            if path.is_file() and _older_than(path, cutoff_ts):
                _remove(path, dry_run)
                report.tmp.append(f"tmp/{path.name}")

    return report


def _is_cloud_filename(stem: str) -> bool:
    """Anything not named ``<uuid>.<fmt>`` was never written by us; remove it regardless of age."""
    try:
        uuid.UUID(stem)
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="remove files left behind by soft deletes and crashes"
    )
    parser.add_argument(
        "--grace-hours", type=float, default=24.0, help="leave anything younger alone"
    )
    parser.add_argument("--dry-run", action="store_true", help="list, do not delete")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    with get_sessionmaker()() as session:
        report = run(
            session, get_storage(), timedelta(hours=args.grace_hours), dry_run=args.dry_run
        )
    verb = "would remove" if args.dry_run else "removed"
    for kind, items in (
        ("soft-deleted cloud file", report.deleted_clouds),
        ("orphan file", report.orphans),
        ("tmp file", report.tmp),
    ):
        for rel in items:
            log.info("%s %s %s", verb, kind, rel)
    log.info("%s %d file(s)", verb, report.total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
