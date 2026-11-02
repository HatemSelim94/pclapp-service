"""Upload, list (keyset pagination, filter), get, download, soft delete."""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cloud, Job, JobStatus, JobType
from app.storage import get_storage
from tests.conftest import Actor, MakeActor

SAMPLE_PCD = Path(__file__).parent / "data" / "sample_objects.pcd"
SAMPLE_TXT = b"0 0 0\n1 0 0\n0 1 0\n0 0 1\n"


def upload(
    client: TestClient,
    actor: Actor,
    filename: str = "scan.pcd",
    content: bytes | None = None,
    **form: Any,
) -> dict[str, Any]:
    data = content if content is not None else SAMPLE_PCD.read_bytes()
    r = client.post(
        "/clouds",
        headers=actor.headers,
        files={"file": (filename, io.BytesIO(data), "application/octet-stream")},
        data=form,
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


# --- upload --------------------------------------------------------------------------------


def test_upload_pcd_creates_row_file_and_summarize_job(
    client: TestClient, make_actor: MakeActor, db: Session
) -> None:
    alice = make_actor("alice")
    body = upload(client, alice, "scene.pcd", name="my scene")

    assert body["name"] == "my scene"
    assert body["format"] == "pcd"
    assert body["size_bytes"] == SAMPLE_PCD.stat().st_size
    assert body["owner_id"] == alice.id
    assert body["point_count"] is None  # filled by the summarize job later
    assert body["derived_from_cloud_id"] is None
    assert body["derived_by_job_id"] is None

    cloud = db.get(Cloud, uuid.UUID(body["id"]))
    assert cloud is not None
    assert cloud.path == f"clouds/{cloud.id}.pcd"
    assert get_storage().abs_path(cloud.path).read_bytes() == SAMPLE_PCD.read_bytes()

    jobs = list(db.scalars(select(Job).where(Job.cloud_id == cloud.id)))
    assert [(j.type, j.status, j.requested_by) for j in jobs] == [
        (JobType.summarize, JobStatus.queued, alice.user.id)
    ]


def test_upload_txt_with_intensity_flag(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    body = upload(client, alice, "points.txt", SAMPLE_TXT, intensity="true")
    assert body["format"] == "txt"
    assert body["has_intensity"] is True
    assert body["name"] == "points"  # defaults to the file stem


@pytest.mark.parametrize("filename", ["scan.ply", "scan", "scan.PCD.exe", ".pcd"])
def test_bad_extension_is_422(client: TestClient, make_actor: MakeActor, filename: str) -> None:
    alice = make_actor("alice")
    r = client.post(
        "/clouds", headers=alice.headers, files={"file": (filename, io.BytesIO(b"x"), "x")}
    )
    assert r.status_code == 422, r.text


def test_upload_case_insensitive_extension(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    assert upload(client, alice, "SCAN.PCD")["format"] == "pcd"


def test_upload_over_size_cap_is_413_and_leaves_no_file(
    client: TestClient, make_actor: MakeActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = make_actor("alice")
    storage = get_storage()
    monkeypatch.setattr(storage, "max_bytes", 100)
    r = client.post(
        "/clouds",
        headers=alice.headers,
        files={"file": ("big.pcd", io.BytesIO(b"x" * 101), "application/octet-stream")},
    )
    assert r.status_code == 413
    assert list((storage.root / "tmp").iterdir()) == []


def test_upload_needs_a_key(client: TestClient) -> None:
    r = client.post("/clouds", files={"file": ("a.pcd", io.BytesIO(b"x"), "x")})
    assert r.status_code == 401


# --- get / download / delete -------------------------------------------------------------------


def test_get_and_download(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    body = upload(client, alice, "scene.pcd", name="scene")

    r = client.get(f"/clouds/{body['id']}", headers=alice.headers)
    assert r.status_code == 200
    assert r.json() == body

    r = client.get(f"/clouds/{body['id']}/download", headers=alice.headers)
    assert r.status_code == 200
    assert r.content == SAMPLE_PCD.read_bytes()
    assert 'filename="scene.pcd"' in r.headers["content-disposition"]


def test_unknown_or_foreign_cloud_is_404(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    body = upload(client, alice)
    assert client.get(f"/clouds/{uuid.uuid4()}", headers=alice.headers).status_code == 404
    assert client.get(f"/clouds/{body['id']}", headers=bob.headers).status_code == 404
    assert client.get(f"/clouds/{body['id']}/download", headers=bob.headers).status_code == 404
    assert client.delete(f"/clouds/{body['id']}", headers=bob.headers).status_code == 404


def test_soft_delete_hides_cloud_but_keeps_row_and_file(
    client: TestClient, make_actor: MakeActor, db: Session
) -> None:
    alice = make_actor("alice")
    body = upload(client, alice)
    cid = body["id"]

    assert client.delete(f"/clouds/{cid}", headers=alice.headers).status_code == 204
    assert client.get(f"/clouds/{cid}", headers=alice.headers).status_code == 404
    assert client.get(f"/clouds/{cid}/download", headers=alice.headers).status_code == 404
    assert client.delete(f"/clouds/{cid}", headers=alice.headers).status_code == 404
    assert client.get("/clouds", headers=alice.headers).json()["items"] == []

    cloud = db.get(Cloud, uuid.UUID(cid))
    assert cloud is not None and cloud.deleted_at is not None
    assert get_storage().exists(cloud.path), "file removal is the cleanup command's job"


# --- list ----------------------------------------------------------------------------------


def _ids(page: dict[str, Any]) -> list[str]:
    return [c["id"] for c in page["items"]]


def test_list_only_own_clouds_newest_first(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    mine = [upload(client, alice, name=f"a{i}")["id"] for i in range(3)]
    upload(client, bob, name="bobs")

    page = client.get("/clouds", headers=alice.headers).json()
    assert _ids(page) == list(reversed(mine))
    assert page["next_cursor"] is None


def test_keyset_pagination_is_stable_across_inserts(
    client: TestClient, make_actor: MakeActor
) -> None:
    alice = make_actor("alice")
    ids = [upload(client, alice, name=f"c{i:02d}")["id"] for i in range(5)]
    newest_first = list(reversed(ids))

    p1 = client.get("/clouds", params={"limit": 2}, headers=alice.headers).json()
    assert _ids(p1) == newest_first[:2]
    assert p1["next_cursor"]

    # Two rows are inserted *while* the client is paging. With OFFSET, page 2 would now
    # repeat rows from page 1. With a keyset cursor the new rows sort before the cursor
    # and simply do not appear in this walk.
    upload(client, alice, name="late1")
    upload(client, alice, name="late2")

    p2 = client.get(
        "/clouds", params={"limit": 2, "cursor": p1["next_cursor"]}, headers=alice.headers
    ).json()
    assert _ids(p2) == newest_first[2:4]

    p3 = client.get(
        "/clouds", params={"limit": 2, "cursor": p2["next_cursor"]}, headers=alice.headers
    ).json()
    assert _ids(p3) == newest_first[4:]
    assert p3["next_cursor"] is None

    assert set(_ids(p1) + _ids(p2) + _ids(p3)) == set(ids), "every row exactly once"


def test_exact_multiple_of_page_size_has_no_empty_trailing_page(
    client: TestClient, make_actor: MakeActor
) -> None:
    alice = make_actor("alice")
    for i in range(4):
        upload(client, alice, name=f"c{i}")
    p1 = client.get("/clouds", params={"limit": 2}, headers=alice.headers).json()
    p2 = client.get(
        "/clouds", params={"limit": 2, "cursor": p1["next_cursor"]}, headers=alice.headers
    ).json()
    assert len(p2["items"]) == 2
    assert p2["next_cursor"] is None


def test_limit_is_capped_and_bad_cursor_is_422(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    assert client.get("/clouds", params={"limit": 0}, headers=alice.headers).status_code == 422
    assert client.get("/clouds", params={"limit": 99999}, headers=alice.headers).status_code == 200
    r = client.get("/clouds", params={"cursor": "not-a-cursor"}, headers=alice.headers)
    assert r.status_code == 422


def test_q_filters_by_name_case_insensitively(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    upload(client, alice, name="Bridge scan")
    upload(client, alice, name="bridge_detail")
    upload(client, alice, name="tunnel")
    upload(client, alice, name="100% done")

    def names(q: str) -> list[str]:
        r = client.get("/clouds", params={"q": q}, headers=alice.headers)
        return [c["name"] for c in r.json()["items"]]

    assert sorted(names("bridge")) == ["Bridge scan", "bridge_detail"]
    assert names("TUNNEL") == ["tunnel"]
    assert names("%") == ["100% done"], "LIKE wildcards in q are escaped"
    assert names("_") == ["bridge_detail"]
    assert names("nothing") == []
