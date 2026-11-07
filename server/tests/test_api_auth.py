"""Authentication, sessions and request hygiene."""

import uuid

import pytest
from argon2 import PasswordHasher

PASSWORD = "correct horse battery"  # same as the harness


def test_register_login_me(h) -> None:
    oscar = h.register("Oscar")
    me = h.get(oscar, "/me").json()
    assert me["username"] == "Oscar" and me["groups"] == []
    stored = h.ctx.conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored.startswith("$argon2id$")


def test_wrong_password_and_unknown_user_look_the_same(h) -> None:
    h.register("oscar")
    a = h.client.post("/api/v1/auth/login", json={"username": "oscar", "password": "wrong password!"})
    b = h.client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong password!"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_usernames_are_case_insensitive_unique(h) -> None:
    h.register("oscar")
    r = h.client.post("/api/v1/auth/register", json={"username": "OSCAR", "password": "another password"})
    assert r.status_code == 409


def test_login_is_case_insensitive(h) -> None:
    h.register("Oscar")
    assert h.login("oscar").username == "oscar"


def test_requests_without_valid_token_are_rejected(h) -> None:
    assert h.client.get("/api/v1/me").status_code == 401
    assert h.client.get("/api/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_access_token_expires_after_15_minutes(h) -> None:
    oscar = h.register("oscar")
    h.advance(14 * 60)
    assert h.get(oscar, "/me").status_code == 200
    h.advance(2 * 60)
    assert h.get(oscar, "/me").status_code == 401


def test_refresh_rotates_tokens(h) -> None:
    oscar = h.register("oscar")
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": oscar.refresh_token})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != oscar.refresh_token
    assert h.client.get("/api/v1/me", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 200


def test_refresh_token_reuse_revokes_the_whole_family(h) -> None:
    oscar = h.register("oscar")
    stolen = oscar.refresh_token
    legit = h.client.post("/api/v1/auth/refresh", json={"refresh_token": stolen}).json()
    # The attacker replays the old token: detected, and the legitimate session dies too.
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_reused"
    assert h.client.post("/api/v1/auth/refresh",
                         json={"refresh_token": legit["refresh_token"]}).status_code == 401
    assert h.client.get("/api/v1/me",
                        headers={"Authorization": f"Bearer {legit['access_token']}"}).status_code == 401


def test_logout_revokes_session(h) -> None:
    oscar = h.register("oscar")
    assert h.client.post("/api/v1/auth/logout", json={"refresh_token": oscar.refresh_token}).status_code == 204
    assert h.get(oscar, "/me").status_code == 401
    assert h.client.post("/api/v1/auth/refresh", json={"refresh_token": oscar.refresh_token}).status_code == 401


def test_tokens_are_not_stored_in_clear(h) -> None:
    oscar = h.register("oscar")
    dump = "\n".join(h.ctx.conn.iterdump())
    assert oscar.access_token not in dump
    assert oscar.refresh_token not in dump


def test_login_is_rate_limited(h) -> None:
    h.register("oscar")
    codes = [h.client.post("/api/v1/auth/login", json={"username": "oscar", "password": "wrong password!"}).status_code
             for _ in range(7)]
    assert codes[:4] == [401] * 4
    assert codes[-1] == 429


def test_invite_mode_requires_a_valid_invitation(harness_factory) -> None:
    h = harness_factory(registration_mode="invite")
    r = h.client.post("/api/v1/auth/register", json={"username": "mallory", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invite_required"
    r = h.client.post("/api/v1/auth/register",
                      json={"username": "mallory", "password": PASSWORD, "invite_code": "x" * 16})
    assert r.status_code == 403


def test_invitation_joins_the_group_and_is_single_use(harness_factory) -> None:
    h = harness_factory(registration_mode="invite")
    # Bootstrap account, as `python -m mdh_server create-user` would do.
    fast = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    h.ctx.conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, 'oscar', ?, 0)",
        (uuid.uuid4().hex, fast.hash(PASSWORD)))
    oscar = h.login("oscar")
    group_id = h.create_group(oscar)
    code = h.invite(oscar, group_id)
    lea = h.register("lea", invite=code)
    assert [g["group_id"] for g in h.get(lea, "/me").json()["groups"]] == [group_id]
    r = h.client.post("/api/v1/auth/register", json={"username": "tom", "password": PASSWORD, "invite_code": code})
    assert r.status_code == 403


def test_idempotency_key_replays_and_rejects_reuse(h) -> None:
    oscar = h.register("oscar")
    headers = {**oscar.headers, "Idempotency-Key": "key-0001-abc"}
    first = h.client.post("/api/v1/groups", headers=headers, json={"name": "A"})
    again = h.client.post("/api/v1/groups", headers=headers, json={"name": "A"})
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert again.headers.get("idempotent-replay") == "true"
    assert len(h.get(oscar, "/groups").json()) == 1
    other = h.client.post("/api/v1/groups", headers=headers, json={"name": "B"})
    assert other.status_code == 422
    bad = h.client.post("/api/v1/groups", headers={**oscar.headers, "Idempotency-Key": "x"}, json={"name": "C"})
    assert bad.status_code == 400


def test_health_and_no_public_docs(h) -> None:
    assert h.client.get("/api/v1/health").json()["status"] == "ok"
    assert h.client.get("/api/v1/docs").status_code == 404


@pytest.mark.parametrize("password", ["short", "x" * 300])
def test_password_policy(h, password: str) -> None:
    r = h.client.post("/api/v1/auth/register", json={"username": "oscar", "password": password})
    assert r.status_code == 422


def test_forgetting_the_invitation_never_locks_registration(harness_factory) -> None:
    """Regression (real use): a few tries without the invitation code, then "too many attempts"
    for 12 minutes, with a message saying one minute."""
    h = harness_factory(registration_mode="invite")
    for _ in range(20):
        r = h.client.post("/api/v1/auth/register", json={"username": "aaron", "password": PASSWORD})
        assert r.json()["error"]["code"] == "invite_required"
    fast = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    h.ctx.conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (?, 'oscar', ?, 0)",
                       (uuid.uuid4().hex, fast.hash(PASSWORD)))
    oscar = h.login("oscar")
    code = h.invite(oscar, h.create_group(oscar))
    assert h.register("aaron", invite=code)               # still works right away


def test_rate_limit_says_how_long_to_wait(harness_factory) -> None:
    h = harness_factory(registration_mode="open")
    statuses = [h.client.post("/api/v1/auth/register",
                              json={"username": f"user{i}", "password": PASSWORD}).status_code for i in range(6)]
    assert statuses == [201] * 5 + [429]
    r = h.client.post("/api/v1/auth/register", json={"username": "user9", "password": PASSWORD})
    wait = r.json()["error"]["details"]["retry_after_s"]
    assert 1 <= wait <= 30 and r.headers["Retry-After"] == str(wait)     # then 1 account per 30 s


def test_guessing_invitation_codes_is_throttled(harness_factory) -> None:
    h = harness_factory(registration_mode="invite")
    codes = [h.client.post("/api/v1/auth/register", json={"username": "mallory", "password": PASSWORD,
                                                          "invite_code": f"guess{i:011d}"}).json()["error"]["code"]
             for i in range(12)]
    assert codes[:10] == ["invalid_invite"] * 10 and codes[-1] == "rate_limited"
