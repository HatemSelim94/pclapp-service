"""GET /jobs/{id}/events streams status changes as server-sent events until the job is final."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import MakeActor
from tests.fake_processor import FakeProcessor
from worker.main import Worker

MakeWorker = Callable[..., Worker]


def _events(client: TestClient, url: str, headers: dict[str, str]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    with client.stream("GET", url, headers=headers) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                out.append((event, json.loads(line[6:])))
                event = None
    return out


def test_events_follow_a_job_to_done(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    r = committed_client.post(
        "/clouds",
        headers=alice.headers,
        files={"file": ("a.txt", io.BytesIO(b"0 0 0\n1 1 1\n"), "text/plain")},
    )
    cid = r.json()["id"]
    job_id = committed_client.get(f"/clouds/{cid}/jobs", headers=alice.headers).json()["items"][0][
        "id"
    ]

    # the worker starts a little later, so the stream sees 'queued' first
    worker = make_worker(FakeProcessor(delay=0.2))
    threading.Timer(0.3, worker.drain).start()

    events = _events(committed_client, f"/jobs/{job_id}/events", alice.headers)

    statuses = [d["status"] for e, d in events if e == "status"]
    assert statuses[0] == "queued"
    assert statuses[-1] == "done"
    assert "running" in statuses, "the stream saw the job while it ran"
    assert statuses == sorted(statuses, key=["queued", "running", "done"].index)
    assert events[-1] == ("end", {"id": job_id, "status": "done"})


def test_events_end_immediately_for_a_finished_job(
    committed_client: TestClient, make_committed_actor: MakeActor, make_worker: MakeWorker
) -> None:
    alice = make_committed_actor("alice")
    r = committed_client.post(
        "/clouds",
        headers=alice.headers,
        files={"file": ("a.txt", io.BytesIO(b"0 0 0\n"), "text/plain")},
    )
    cid = r.json()["id"]
    job_id = committed_client.get(f"/clouds/{cid}/jobs", headers=alice.headers).json()["items"][0][
        "id"
    ]
    make_worker().drain()

    events = _events(committed_client, f"/jobs/{job_id}/events", alice.headers)
    assert [e for e, _ in events] == ["status", "end"]
    assert events[0][1]["status"] == "done"


def test_events_respect_access(
    committed_client: TestClient, make_committed_actor: MakeActor
) -> None:
    alice, bob = make_committed_actor("alice"), make_committed_actor("bob")
    r = committed_client.post(
        "/clouds",
        headers=alice.headers,
        files={"file": ("a.txt", io.BytesIO(b"0 0 0\n"), "text/plain")},
    )
    cid = r.json()["id"]
    job_id = committed_client.get(f"/clouds/{cid}/jobs", headers=alice.headers).json()["items"][0][
        "id"
    ]
    assert committed_client.get(f"/jobs/{job_id}/events", headers=bob.headers).status_code == 404
    assert committed_client.get(f"/jobs/{job_id}/events").status_code == 401


def test_events_time_out(committed_client: TestClient, make_committed_actor: MakeActor) -> None:
    """No worker running: after sse_max_seconds (5 s in tests) the stream ends with `timeout`,
    and keepalive comments were sent in between."""
    alice = make_committed_actor("alice")
    r = committed_client.post(
        "/clouds",
        headers=alice.headers,
        files={"file": ("a.txt", io.BytesIO(b"0 0 0\n"), "text/plain")},
    )
    cid = r.json()["id"]
    job_id = committed_client.get(f"/clouds/{cid}/jobs", headers=alice.headers).json()["items"][0][
        "id"
    ]
    with committed_client.stream("GET", f"/jobs/{job_id}/events", headers=alice.headers) as resp:
        lines = list(resp.iter_lines())
    assert "event: status" in lines
    assert ": keepalive" in lines
    assert lines[-3] == "event: timeout"
