"""Users, API keys, and the authentication dependency."""

from fastapi.testclient import TestClient

from app import auth
from tests.conftest import MakeActor


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "data_dir": "ok"}


def test_admin_creates_user_and_key(client: TestClient, admin_headers: dict[str, str]) -> None:
    r = client.post("/users", json={"name": "alice"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    user = r.json()
    assert user["name"] == "alice"

    r = client.post(f"/users/{user['id']}/api-keys", json={"label": "cli"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    issued = r.json()
    assert issued["key"].startswith(auth.KEY_PREFIX)
    assert issued["key_prefix"] == auth.display_prefix(issued["key"])

    # the key works, and identifies the right user
    r = client.get("/me", headers={"Authorization": f"Bearer {issued['key']}"})
    assert r.status_code == 200
    assert r.json()["id"] == user["id"]

    # listing never shows the key itself
    r = client.get(f"/users/{user['id']}/api-keys", headers=admin_headers)
    assert r.status_code == 200
    assert "key" not in r.json()[0]
    assert r.json()[0]["key_prefix"] == issued["key_prefix"]


def test_duplicate_user_name_is_409(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.post("/users", json={"name": "bob"}, headers=admin_headers).status_code == 201
    assert client.post("/users", json={"name": "bob"}, headers=admin_headers).status_code == 409


def test_invalid_user_name_is_422(client: TestClient, admin_headers: dict[str, str]) -> None:
    r = client.post("/users", json={"name": "has spaces"}, headers=admin_headers)
    assert r.status_code == 422


def test_admin_routes_need_admin_key(client: TestClient, make_actor: MakeActor) -> None:
    alice = make_actor("alice")
    assert client.post("/users", json={"name": "x"}).status_code == 401
    assert client.post("/users", json={"name": "x"}, headers=alice.headers).status_code == 403


def test_bad_or_missing_key_is_401(client: TestClient) -> None:
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer pk_nope"}).status_code == 401
    assert client.get("/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_revoked_key_stops_working(
    client: TestClient, admin_headers: dict[str, str], make_actor: MakeActor
) -> None:
    alice = make_actor("alice")
    assert client.get("/me", headers=alice.headers).status_code == 200

    keys = client.get(f"/users/{alice.id}/api-keys", headers=admin_headers).json()
    r = client.delete(f"/users/{alice.id}/api-keys/{keys[0]['id']}", headers=admin_headers)
    assert r.status_code == 204

    assert client.get("/me", headers=alice.headers).status_code == 401
    # revoking twice is idempotent
    r = client.delete(f"/users/{alice.id}/api-keys/{keys[0]['id']}", headers=admin_headers)
    assert r.status_code == 204


def test_key_hash_is_sha256_and_keys_are_unique() -> None:
    a, b = auth.generate_key(), auth.generate_key()
    assert a != b
    assert len(auth.hash_key(a)) == 64
    assert auth.hash_key(a) != auth.hash_key(b)
