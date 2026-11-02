"""The access table from app/access.py, exercised end to end through the HTTP API.

action        owner  editor  viewer  stranger
read            200    200     200     404
download        200    200     200     404
list jobs       200    200     200     404
create job      202    202     403     404
list shares     200    403     403     404
grant share     201    403     403     404
delete          204    403     403     404
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Actor, MakeActor

Request = Callable[[TestClient, Actor, str, Actor], Any]


def _upload(client: TestClient, actor: Actor) -> str:
    r = client.post(
        "/clouds",
        headers=actor.headers,
        files={"file": ("pts.txt", io.BytesIO(b"0 0 0\n1 1 1\n"), "text/plain")},
    )
    assert r.status_code == 201, r.text
    cid: str = r.json()["id"]
    return cid


# (name, request, expected status per role: owner, editor, viewer, stranger)
CASES: list[tuple[str, Request, tuple[int, int, int, int]]] = [
    (
        "read",
        lambda c, a, cid, _: c.get(f"/clouds/{cid}", headers=a.headers),
        (200, 200, 200, 404),
    ),
    (
        "download",
        lambda c, a, cid, _: c.get(f"/clouds/{cid}/download", headers=a.headers),
        (200, 200, 200, 404),
    ),
    (
        "list jobs",
        lambda c, a, cid, _: c.get(f"/clouds/{cid}/jobs", headers=a.headers),
        (200, 200, 200, 404),
    ),
    (
        "create job",
        lambda c, a, cid, _: c.post(
            f"/clouds/{cid}/jobs", json={"type": "summarize"}, headers=a.headers
        ),
        (202, 202, 403, 404),
    ),
    (
        "list shares",
        lambda c, a, cid, _: c.get(f"/clouds/{cid}/shares", headers=a.headers),
        (200, 403, 403, 404),
    ),
    (
        "grant share",
        lambda c, a, cid, other: c.post(
            f"/clouds/{cid}/shares",
            json={"user_id": other.id, "role": "viewer"},
            headers=a.headers,
        ),
        (201, 403, 403, 404),
    ),
    (
        "delete",
        lambda c, a, cid, _: c.delete(f"/clouds/{cid}", headers=a.headers),
        (204, 403, 403, 404),
    ),
]

ROLES = ["owner", "editor", "viewer", "stranger"]


@pytest.mark.parametrize("role_index,role", list(enumerate(ROLES)))
@pytest.mark.parametrize("name,call,expected", CASES, ids=[c[0] for c in CASES])
def test_access_table(
    client: TestClient,
    make_actor: MakeActor,
    name: str,
    call: Request,
    expected: tuple[int, int, int, int],
    role_index: int,
    role: str,
) -> None:
    owner, editor, viewer, stranger = (make_actor(r) for r in ROLES)
    extra = make_actor("extra")  # the grantee in the "grant share" case
    cid = _upload(client, owner)
    for who, r in ((editor, "editor"), (viewer, "viewer")):
        assert (
            client.post(
                f"/clouds/{cid}/shares", json={"user_id": who.id, "role": r}, headers=owner.headers
            ).status_code
            == 201
        )

    actor = {"owner": owner, "editor": editor, "viewer": viewer, "stranger": stranger}[role]
    response = call(client, actor, cid, extra)
    assert response.status_code == expected[role_index], (
        f"{role} {name}: expected {expected[role_index]}, "
        f"got {response.status_code}: {response.text}"
    )


# --- sharing behaviour beyond the table ----------------------------------------------------------


def test_shared_clouds_appear_in_the_list(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    cid = _upload(client, alice)
    _upload(client, alice)  # not shared

    assert client.get("/clouds", headers=bob.headers).json()["items"] == []
    client.post(
        f"/clouds/{cid}/shares", json={"user_id": bob.id, "role": "viewer"}, headers=alice.headers
    )
    assert [c["id"] for c in client.get("/clouds", headers=bob.headers).json()["items"]] == [cid]

    assert client.delete(f"/clouds/{cid}/shares/{bob.id}", headers=alice.headers).status_code == 204
    assert client.get("/clouds", headers=bob.headers).json()["items"] == []
    assert client.get(f"/clouds/{cid}", headers=bob.headers).status_code == 404


def test_regranting_changes_the_role(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    cid = _upload(client, alice)
    job = {"type": "summarize"}

    client.post(
        f"/clouds/{cid}/shares", json={"user_id": bob.id, "role": "viewer"}, headers=alice.headers
    )
    assert client.post(f"/clouds/{cid}/jobs", json=job, headers=bob.headers).status_code == 403

    r = client.post(
        f"/clouds/{cid}/shares", json={"user_id": bob.id, "role": "editor"}, headers=alice.headers
    )
    assert r.status_code == 201 and r.json()["role"] == "editor"
    assert client.post(f"/clouds/{cid}/jobs", json=job, headers=bob.headers).status_code == 202

    shares = client.get(f"/clouds/{cid}/shares", headers=alice.headers).json()
    assert [s["user_id"] for s in shares] == [bob.id], "one row per (cloud, user)"


def test_share_edge_cases(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    cid = _upload(client, alice)

    def post(body: dict[str, str]) -> Any:
        return client.post(f"/clouds/{cid}/shares", json=body, headers=alice.headers)

    nobody = "00000000-0000-0000-0000-000000000000"
    assert post({"user_id": alice.id, "role": "viewer"}).status_code == 409, "owner"
    assert post({"user_id": nobody, "role": "viewer"}).status_code == 404
    assert post({"user_id": bob.id, "role": "admin"}).status_code == 422
    assert client.delete(f"/clouds/{cid}/shares/{bob.id}", headers=alice.headers).status_code == 404


def test_shared_user_sees_jobs_and_results(client: TestClient, make_actor: MakeActor) -> None:
    alice, bob = make_actor("alice"), make_actor("bob")
    cid = _upload(client, alice)
    client.post(
        f"/clouds/{cid}/shares", json={"user_id": bob.id, "role": "viewer"}, headers=alice.headers
    )
    (job,) = client.get(f"/clouds/{cid}/jobs", headers=bob.headers).json()["items"]
    assert client.get(f"/jobs/{job['id']}", headers=bob.headers).status_code == 200
