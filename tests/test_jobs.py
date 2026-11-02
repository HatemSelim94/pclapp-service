"""Job lifecycle: validation at the API, queued → running → done/failed through the worker.

These tests commit for real (``committed_client``) because the worker reads through its own
connections and must see the rows.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Cluster, Job, JobStatus
from tests.conftest import Actor, MakeActor
from tests.fake_processor import FailingProcessor
from worker.main import Worker

MakeWorker = Callable[..., Worker]

FOUR_POINTS = b"0 0 0\n1 0 0\n0 1 0\n0 0 1\n"
# two tight clusters 10 units apart plus one stray point
TWO_CLUSTERS = (
    b"".join(f"{0.1 * i:.2f} 0 0\n".encode() for i in range(6))
    + b"".join(f"{10 + 0.1 * i:.2f} 0 0\n".encode() for i in range(4))
    + b"50 50 50\n"
)


def upload_txt(client: TestClient, actor: Actor, content: bytes, name: str = "pts") -> str:
    r = client.post(
        "/clouds",
        headers=actor.headers,
        files={"file": (f"{name}.txt", io.BytesIO(content), "text/plain")},
        data={"name": name},
    )
    assert r.status_code == 201, r.text
    cid: str = r.json()["id"]
    return cid


def get_job(client: TestClient, actor: Actor, job_id: str) -> dict[str, Any]:
    r = client.get(f"/jobs/{job_id}", headers=actor.headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def create_job(client: TestClient, actor: Actor, cloud_id: str, body: dict[str, Any]) -> str:
    r = client.post(f"/clouds/{cloud_id}/jobs", json=body, headers=actor.headers)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    jid: str = r.json()["id"]
    return jid


# --- validation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "explode", "params": {}},
        {"type": "downsample", "params": {}},
        {"type": "downsample", "params": {"leaf_size": 0}},
        {"type": "downsample", "params": {"leaf_size": 0.1, "extra": 1}},
        {"type": "cluster", "params": {"tolerance": -1}},
        {"type": "cluster", "params": {"tolerance": 0.5, "min_size": 10, "max_size": 5}},
        {"type": "cluster", "params": {"tolerance": 0.5, "min_size": 0}},
        {"type": "summarize", "params": {"leaf_size": 1}},
        {"params": {}},
    ],
)
def test_bad_job_requests_are_422(
    committed_client: TestClient, make_committed_actor: MakeActor, body: dict[str, Any]
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    r = committed_client.post(f"/clouds/{cid}/jobs", json=body, headers=alice.headers)
    assert r.status_code == 422, r.text


def test_cluster_defaults_are_filled(
    committed_client: TestClient, make_committed_actor: MakeActor
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    jid = create_job(committed_client, alice, cid, {"type": "cluster", "params": {"tolerance": 1}})
    assert get_job(committed_client, alice, jid)["params"] == {
        "tolerance": 1.0,
        "min_size": 20,
        "max_size": 3000,
    }


def test_jobs_of_a_foreign_cloud_are_404(
    committed_client: TestClient, make_committed_actor: MakeActor
) -> None:
    alice, bob = make_committed_actor("alice"), make_committed_actor("bob")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    jid = get_jobs_of(committed_client, alice, cid)[0]["id"]
    body = {"type": "summarize"}
    assert (
        committed_client.post(f"/clouds/{cid}/jobs", json=body, headers=bob.headers).status_code
        == 404
    )
    assert committed_client.get(f"/jobs/{jid}", headers=bob.headers).status_code == 404
    assert committed_client.get(f"/clouds/{cid}/jobs", headers=bob.headers).status_code == 404
    assert committed_client.get(f"/jobs/{uuid.uuid4()}", headers=alice.headers).status_code == 404


def get_jobs_of(client: TestClient, actor: Actor, cloud_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/clouds/{cloud_id}/jobs", headers=actor.headers)
    assert r.status_code == 200, r.text
    items: list[dict[str, Any]] = r.json()["items"]
    return items


# --- lifecycle -----------------------------------------------------------------------------


def test_summarize_fills_the_cloud(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)

    (job,) = get_jobs_of(committed_client, alice, cid)
    assert job["type"] == "summarize"
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["started_at"] is None

    assert make_worker().drain() == 1

    job = get_job(committed_client, alice, job["id"])
    assert job["status"] == "done"
    assert job["attempts"] == 1
    assert job["worker_id"] == "test-worker"
    assert job["started_at"] and job["finished_at"]
    assert job["error"] is None

    cloud = committed_client.get(f"/clouds/{cid}", headers=alice.headers).json()
    assert cloud["point_count"] == 4
    assert cloud["bbox_min"] == [0, 0, 0]
    assert cloud["bbox_max"] == [1, 1, 1]
    assert cloud["centroid"] == [0.25, 0.25, 0.25]


def test_downsample_creates_a_derived_cloud(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    # 8 points in two voxels of edge 1: four around (0.2, 0.2, 0.2), four around (5.2, 0.2, 0.2)
    pts = b"".join(f"{dx + 0.1 * k:.1f} 0.2 0.2\n".encode() for dx in (0.0, 5.0) for k in range(4))
    cid = upload_txt(committed_client, alice, pts, name="scene")
    jid = create_job(
        committed_client, alice, cid, {"type": "downsample", "params": {"leaf_size": 1}}
    )

    assert make_worker().drain() == 2  # the automatic summarize + ours

    job = get_job(committed_client, alice, jid)
    assert job["status"] == "done"
    assert job["result_cloud_id"]

    derived = committed_client.get(
        f"/clouds/{job['result_cloud_id']}", headers=alice.headers
    ).json()
    assert derived["derived_from_cloud_id"] == cid
    assert derived["derived_by_job_id"] == jid
    assert derived["owner_id"] == alice.id
    assert derived["format"] == "pcd"
    assert derived["name"] == "scene (downsampled 1)"
    assert derived["point_count"] == 2
    assert derived["size_bytes"] > 0

    # it is a normal cloud: listed, downloadable, and it can be processed again
    ids = [c["id"] for c in committed_client.get("/clouds", headers=alice.headers).json()["items"]]
    assert ids == [derived["id"], cid]
    r = committed_client.get(f"/clouds/{derived['id']}/download", headers=alice.headers)
    assert r.status_code == 200 and len(r.content) == derived["size_bytes"]

    source = committed_client.get(f"/clouds/{cid}", headers=alice.headers).json()
    assert source["derived_by_job_id"] is None


def test_cluster_job_stores_clusters_largest_first(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, TWO_CLUSTERS)
    jid = create_job(
        committed_client,
        alice,
        cid,
        {"type": "cluster", "params": {"tolerance": 0.5, "min_size": 2, "max_size": 100}},
    )
    make_worker().drain()

    job = get_job(committed_client, alice, jid)
    assert job["status"] == "done"
    assert [c["size"] for c in job["clusters"]] == [6, 4]  # the stray point is below min_size
    assert [c["index"] for c in job["clusters"]] == [0, 1]
    assert job["clusters"][0]["bbox_min"] == [0, 0, 0]
    assert job["clusters"][1]["centroid"][0] == pytest.approx(10.15)

    # the list endpoint carries them too
    jobs = {j["id"]: j for j in get_jobs_of(committed_client, alice, cid)}
    assert len(jobs[jid]["clusters"]) == 2


# --- failure -------------------------------------------------------------------------------


def test_failing_job_is_retried_then_failed(
    committed_client: TestClient,
    make_committed_actor: MakeActor,
    make_worker: MakeWorker,
    committed_db: Session,
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    (job,) = get_jobs_of(committed_client, alice, cid)
    committed_db.execute(update(Job).where(Job.id == uuid.UUID(job["id"])).values(max_attempts=2))
    committed_db.commit()

    worker = make_worker(FailingProcessor())

    assert worker.run_one() is True
    job = get_job(committed_client, alice, job["id"])
    assert job["status"] == "queued", "attempts left → back in the queue"
    assert job["attempts"] == 1
    assert "boom" in job["error"]
    assert job["finished_at"] is None

    assert worker.run_one() is True
    job = get_job(committed_client, alice, job["id"])
    assert job["status"] == "failed"
    assert job["attempts"] == 2
    assert job["finished_at"] is not None

    assert worker.run_one() is False, "failed jobs are not picked up again"


def test_unreadable_file_fails_with_error_text(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, b"this is not a point cloud\n")
    (job,) = get_jobs_of(committed_client, alice, cid)

    worker = make_worker()
    worker.drain()  # retried up to max_attempts (3), all failing

    job = get_job(committed_client, alice, job["id"])
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    assert "could not convert" in job["error"]


def test_bad_params_in_the_table_fail_permanently(
    committed_client: TestClient,
    make_committed_actor: MakeActor,
    make_worker: MakeWorker,
    committed_db: Session,
) -> None:
    """The API would never store this, but the worker must not trust the table blindly."""
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    jid = create_job(
        committed_client, alice, cid, {"type": "downsample", "params": {"leaf_size": 1}}
    )
    committed_db.execute(
        update(Job).where(Job.id == uuid.UUID(jid)).values(params={"leaf_size": -1})
    )
    committed_db.commit()

    make_worker().drain()
    job = get_job(committed_client, alice, jid)
    assert job["status"] == "failed"
    assert job["attempts"] == 1, "permanent failures are not retried"
    assert "invalid params" in job["error"]


def test_job_on_deleted_cloud_fails_permanently(
    committed_client: TestClient,
    make_committed_actor: MakeActor,
    make_worker: MakeWorker,
    committed_db: Session,
) -> None:
    alice = make_committed_actor("alice")
    cid = upload_txt(committed_client, alice, FOUR_POINTS)
    (job,) = get_jobs_of(committed_client, alice, cid)
    assert committed_client.delete(f"/clouds/{cid}", headers=alice.headers).status_code == 204

    make_worker().drain()
    row = committed_db.scalars(select(Job).where(Job.id == uuid.UUID(job["id"]))).one()
    assert row.status == JobStatus.failed
    assert row.attempts == 1
    assert row.error == "cloud was deleted"
    assert committed_db.scalars(select(Cluster)).all() == []
