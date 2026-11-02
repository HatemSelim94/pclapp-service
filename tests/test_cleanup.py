"""The cleanup command removes exactly the files the request path leaves behind."""

from __future__ import annotations

import io
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import cleanup
from app.models import Cloud
from app.storage import get_storage
from tests.conftest import MakeActor

GRACE = timedelta(hours=1)


def _upload(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post(
        "/clouds", headers=headers, files={"file": ("a.txt", io.BytesIO(b"0 0 0\n"), "text/plain")}
    )
    assert r.status_code == 201
    cid: str = r.json()["id"]
    return cid


def _age(path: str | os.PathLike[str], hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_cleanup_removes_only_what_it_should(
    committed_client: TestClient, make_committed_actor: MakeActor, committed_db: Session
) -> None:
    alice = make_committed_actor("alice")
    storage = get_storage()
    storage.ensure_dirs()

    live = _upload(committed_client, alice.headers)
    deleted_recently = _upload(committed_client, alice.headers)
    deleted_long_ago = _upload(committed_client, alice.headers)
    for cid in (deleted_recently, deleted_long_ago):
        assert committed_client.delete(f"/clouds/{cid}", headers=alice.headers).status_code == 204
    committed_db.execute(
        update(Cloud)
        .where(Cloud.id == uuid.UUID(deleted_long_ago))
        .values(deleted_at=datetime.now(UTC) - GRACE * 2)
    )
    committed_db.commit()
    paths = {
        cid: committed_db.get_one(Cloud, uuid.UUID(cid)).path
        for cid in (live, deleted_recently, deleted_long_ago)
    }

    old_orphan = storage.abs_path(f"clouds/{uuid.uuid4()}.pcd")
    old_orphan.write_bytes(b"x")
    _age(old_orphan, 2)
    young_orphan = storage.abs_path(f"clouds/{uuid.uuid4()}.pcd")
    young_orphan.write_bytes(b"x")  # an upload between rename and commit looks exactly like this
    stray = storage.abs_path("clouds/notes.txt")  # not ours; removed regardless of age
    stray.write_bytes(b"x")

    old_tmp = storage.root / "tmp" / "old.part"
    old_tmp.write_bytes(b"x")
    _age(old_tmp, 2)
    young_tmp = storage.root / "tmp" / "young.part"
    young_tmp.write_bytes(b"x")

    # dry run changes nothing
    report = cleanup.run(committed_db, storage, GRACE, dry_run=True)
    assert report.total == 4
    assert all(storage.abs_path(p).exists() for p in paths.values())
    assert old_orphan.exists() and old_tmp.exists()

    report = cleanup.run(committed_db, storage, GRACE)
    assert report.deleted_clouds == [paths[deleted_long_ago]]
    assert sorted(report.orphans) == sorted([f"clouds/{old_orphan.name}", "clouds/notes.txt"])
    assert report.tmp == ["tmp/old.part"]

    assert storage.exists(paths[live])
    assert storage.exists(paths[deleted_recently]), "inside the grace period"
    assert not storage.exists(paths[deleted_long_ago])
    assert not old_orphan.exists() and young_orphan.exists() and not stray.exists()
    assert not old_tmp.exists() and young_tmp.exists()

    # the row of the soft-deleted cloud is kept (lineage, job history)
    assert committed_db.get(Cloud, uuid.UUID(deleted_long_ago)) is not None

    # idempotent
    assert cleanup.run(committed_db, storage, GRACE).total == 0

    young_orphan.unlink()
    young_tmp.unlink()


def test_cleanup_cli_runs(committed_db: Session) -> None:
    assert cleanup.main(["--dry-run", "--grace-hours", "48"]) == 0
