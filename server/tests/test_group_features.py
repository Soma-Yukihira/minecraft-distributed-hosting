"""Moderators, access requests, statistics, and the v7 schema migration."""

from datetime import UTC, datetime

from mdh_server import access
from mdh_server.database.db import MIGRATIONS, connect, immediate, migrate
from mdh_server.stats import StatsRecorder


def set_role(world, user, member, role):
    return world.h.client.patch(f"/api/v1/groups/{world.group_id}/members/{member.user_id}",
                                headers=user.headers, json={"role": role})


def action(world, user, **act):
    return world.h.post(user, f"/servers/{world.server_id}/actions", {"action": act})


# ------------------------------------------------------------------ moderators

def test_moderators_run_the_game_but_change_nothing(world) -> None:
    h = world.h
    assert action(world, world.lea, kind="kick", player="Steve").status_code == 403  # member
    assert set_role(world, world.oscar, world.lea, "moderator").status_code == 204
    roles = {m["username"]: m["role"] for m in h.get(world.oscar, f"/groups/{world.group_id}/members").json()}
    assert roles["lea"] == "moderator"
    assert h.get(world.lea, f"/groups/{world.group_id}").json()["role"] == "moderator"
    # Allowed actions get past the permission check (the server is not running: 409).
    for act in ({"kind": "kick", "player": "Steve"}, {"kind": "ban", "player": "Steve"},
                {"kind": "pardon", "player": "Steve"}, {"kind": "announce", "message": "Salut"}):
        assert action(world, world.lea, **act).status_code == 409, act
    for act in ({"kind": "op", "player": "Steve"}, {"kind": "deop", "player": "Steve"},
                {"kind": "whitelist_add", "player": "Steve"}, {"kind": "restart"}):
        assert action(world, world.lea, **act).status_code == 403, act
    # No configuration, no backups, no invitations, no roles.
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.lea.headers, json={"memory_mb": 2048})
    assert r.status_code == 403
    assert h.post(world.lea, f"/groups/{world.group_id}/invites", {}).status_code == 403
    assert h.post(world.lea, f"/servers/{world.server_id}/restore", {"version_id": "0" * 32}).status_code == 403
    assert set_role(world, world.lea, world.oscar, "member").status_code == 403


def test_who_names_whom(world) -> None:
    h = world.h
    tom = h.register("tom", invite=h.invite(world.oscar, world.group_id))
    assert set_role(world, world.oscar, world.lea, "admin").status_code == 204
    assert set_role(world, world.lea, tom, "moderator").status_code == 204   # admins name moderators
    assert set_role(world, world.lea, tom, "admin").status_code == 403       # only the owner names admins
    assert set_role(world, world.lea, tom, "member").status_code == 204
    assert set_role(world, world.oscar, tom, "admin").status_code == 204
    assert set_role(world, world.lea, tom, "member").status_code == 403      # nor demotes them
    assert set_role(world, world.oscar, tom, "member").status_code == 204
    assert set_role(world, world.oscar, tom, "superuser").status_code == 422


def test_v7_migration_keeps_members_and_allows_moderators(tmp_path) -> None:
    conn = connect(tmp_path / "old.sqlite3")
    # A database as 0.3.0 left it (schema v6).
    real = dict(MIGRATIONS)
    try:
        MIGRATIONS.pop(7)
        import mdh_server.database.db as db

        db.SCHEMA_VERSION = 6
        migrate(conn)
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(real)
        db.SCHEMA_VERSION = max(MIGRATIONS)
    with immediate(conn):
        conn.execute("INSERT INTO users VALUES ('u1', 'oscar', 'x', 0, 0)")
        conn.execute("INSERT INTO users VALUES ('u2', 'lea', 'x', 0, 0)")
        conn.execute("INSERT INTO groups VALUES ('g1', 'amis', 'u1', 0, 1000)")
        conn.execute("INSERT INTO memberships VALUES ('g1', 'u1', 'owner', 1, 5)")
        conn.execute("INSERT INTO memberships VALUES ('g1', 'u2', 'member', 0, 6)")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
    rows = [tuple(r) for r in conn.execute("SELECT * FROM memberships ORDER BY user_id")]
    assert rows == [("g1", "u1", "owner", 1, 5.0), ("g1", "u2", "member", 0, 6.0)]
    conn.execute("UPDATE memberships SET role = 'moderator' WHERE user_id = 'u2'")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


# ------------------------------------------------------------------ access requests

def server_row(world):
    return world.h.ctx.conn.execute("SELECT * FROM servers WHERE id = ?", (world.server_id,)).fetchone()


def test_access_requests(world) -> None:
    h = world.h
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                       json={"properties": {"white-list": True}, "allowed_players": ["Oscar"]})
    assert r.status_code == 200
    srv = server_row(world)
    assert access.whitelist_on(srv) and access.allowed(srv, "OSCAR") and not access.allowed(srv, "Steve")
    message, new = access.record(h.ctx.conn, srv, "Steve", h.ctx.now())
    assert new and message == access.ASKED
    _, new = access.record(h.ctx.conn, srv, "steve", h.ctx.now() + 5)  # same player, other case
    assert not new
    assert h.status(world.oscar, world.server_id)["pending_access_requests"] == 1
    assert h.get(world.lea, f"/servers/{world.server_id}/access-requests").status_code == 403
    listed = h.get(world.oscar, f"/servers/{world.server_id}/access-requests").json()
    assert [(x["player"], x["attempts"], x["status"]) for x in listed] == [("Steve", 2, "pending")]

    r = h.post(world.oscar, f"/servers/{world.server_id}/access-requests/Steve/accept")
    assert r.status_code == 200, r.text
    assert r.json()["allowed_players"] == ["Oscar", "Steve"] and r.json()["pending_access_requests"] == 0
    assert r.json()["config_revision"] == 3  # applied at once by the host (server_update)
    assert h.post(world.oscar, f"/servers/{world.server_id}/access-requests/Steve/accept").status_code == 404
    assert "access_accepted" in h.event_types(world.oscar, world.server_id)

    access.record(h.ctx.conn, server_row(world), "Eve", h.ctx.now())
    assert h.post(world.oscar, f"/servers/{world.server_id}/access-requests/Eve/refuse").status_code == 200
    message, new = access.record(h.ctx.conn, server_row(world), "Eve", h.ctx.now())
    assert message == access.REFUSED and not new
    assert h.status(world.oscar, world.server_id)["pending_access_requests"] == 0
    assert h.post(world.oscar, f"/servers/{world.server_id}/access-requests/a%20b/accept").status_code == 422


def test_a_flood_of_names_stops_at_the_cap(world) -> None:
    h = world.h
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"allow_offline_accounts": True, "allowed_players": ["Oscar"]})
    srv = server_row(world)
    news = [access.record(h.ctx.conn, srv, f"bot{i}", h.ctx.now())[1] for i in range(access.MAX_PENDING + 10)]
    assert news.count(True) == access.MAX_PENDING
    assert access.record(h.ctx.conn, srv, "late", h.ctx.now())[0] == access.ASKED_PERSONAL
    # Forgotten after 30 days.
    access.purge(h.ctx.conn, h.ctx.now() + access.KEEP_S + 1)
    assert h.ctx.conn.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0] == 0


def test_places_of_players_with_unverified_names(world) -> None:
    h = world.h
    sid = world.server_id
    r = h.client.patch(f"/api/v1/servers/{sid}", headers=world.oscar.headers,
                       json={"allow_offline_accounts": True, "personal_addresses": False,
                             "allowed_players": ["Oscar", "Krystal"]})
    assert r.status_code == 200, r.text
    srv = server_row(world)
    assert access.checks_places(srv)
    assert access.place_of("86.247.12.34") == "86.247.12.0/24"
    assert access.place_of("::ffff:86.247.12.34") == "86.247.12.0/24"
    assert access.place_of("2a01:cb00:1234:5678::1") == "2a01:cb00:1234::/48"
    assert access.place_of("garbage") == "?"
    now = h.ctx.now()

    # First connection: trusted and remembered; same network later: let in.
    assert access.check_place(h.ctx.conn, srv, "Krystal", "86.247.12.0/24", now) == (None, False)
    assert access.check_place(h.ctx.conn, srv, "krystal", "86.247.12.0/24", now + 1) == (None, False)
    # Somewhere else: a request, once, until an admin answers.
    assert access.check_place(h.ctx.conn, srv, "Krystal", "5.6.7.0/24", now + 2) == (access.NEW_PLACE, True)
    assert access.check_place(h.ctx.conn, srv, "Krystal", "5.6.7.0/24", now + 3) == (access.NEW_PLACE, False)
    assert access.check_place(h.ctx.conn, srv, "Krystal", "9.9.9.0/24", now + 4) == (access.NEW_PLACE, True)
    assert h.status(world.oscar, sid)["pending_access_requests"] == 2
    listed = h.get(world.oscar, f"/servers/{sid}/access-requests").json()
    assert {(x["player"], x["kind"], x["place"], x["attempts"]) for x in listed} == {
        ("Krystal", "new_place", "5.6.7.0/24", 2), ("Krystal", "new_place", "9.9.9.0/24", 1)}

    # Accepted: that place is hers; refused: that one stays closed, the others are not affected.
    url = f"/servers/{sid}/access-requests/Krystal"
    assert h.post(world.oscar, f"{url}/accept", {"place": "5.6.7.0/24"}).status_code == 200
    assert h.post(world.oscar, f"{url}/refuse", {"place": "9.9.9.0/24"}).status_code == 200
    assert h.post(world.oscar, f"{url}/accept", {"place": "1.1.1.0/24"}).status_code == 404
    assert h.post(world.oscar, f"{url}/accept").status_code == 404   # no request to join
    assert access.check_place(h.ctx.conn, srv, "Krystal", "5.6.7.0/24", now + 5) == (None, False)
    assert access.check_place(h.ctx.conn, srv, "Krystal", "9.9.9.0/24", now + 6) == (access.PLACE_REFUSED, False)
    types = h.event_types(world.oscar, sid)
    assert {"new_place_requested", "new_place_accepted", "new_place_refused"} <= set(types)

    # A request to join carries its place: accepting the player trusts it.
    access.record(h.ctx.conn, srv, "Steve", now, "7.7.7.0/24")
    r = h.post(world.oscar, f"/servers/{sid}/access-requests/Steve/accept")    # 0.4.x application
    assert r.status_code == 200 and "Steve" in r.json()["allowed_players"]
    srv = server_row(world)
    assert access.check_place(h.ctx.conn, srv, "Steve", "7.7.7.0/24", now + 7) == (None, False)
    assert access.check_place(h.ctx.conn, srv, "Steve", "8.8.8.0/24", now + 8) == (access.NEW_PLACE, True)

    # Taken off the list: places and requests from new places go with them.
    h.client.patch(f"/api/v1/servers/{sid}", headers=world.oscar.headers, json={"allowed_players": ["Oscar"]})
    left = h.ctx.conn.execute("SELECT COUNT(*) FROM known_places WHERE server_id = ?", (sid,)).fetchone()[0]
    assert left == 0
    assert h.get(world.oscar, f"/servers/{sid}/access-requests").json() == []

    # At most MAX_PLACES per player, the least recently used forgotten; unused ones expire.
    for i in range(access.MAX_PLACES + 3):
        with immediate(h.ctx.conn):
            access._learn(h.ctx.conn, sid, "Oscar", f"10.0.{i}.0/24", now + i)
    places = {r[0] for r in h.ctx.conn.execute("SELECT place FROM known_places WHERE player_name = 'Oscar'")}
    assert len(places) == access.MAX_PLACES and "10.0.0.0/24" not in places
    access.purge(h.ctx.conn, now + access.MAX_PLACES + access.PLACE_KEEP_S + 10)
    assert h.ctx.conn.execute("SELECT COUNT(*) FROM known_places").fetchone()[0] == 0


def test_personal_addresses_or_official_accounts_check_no_place(world) -> None:
    h = world.h
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"allow_offline_accounts": True, "allowed_players": ["Oscar"]})
    assert not access.checks_places(server_row(world))       # personal addresses (default)
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"allow_offline_accounts": False, "personal_addresses": False})
    assert not access.checks_places(server_row(world))       # Microsoft verifies the name


def test_v9_migration_keeps_access_requests(tmp_path) -> None:
    conn = connect(tmp_path / "old.sqlite3")
    real = dict(MIGRATIONS)
    try:
        MIGRATIONS.pop(9)
        import mdh_server.database.db as db

        db.SCHEMA_VERSION = 8
        migrate(conn)
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(real)
        db.SCHEMA_VERSION = max(MIGRATIONS)
    with immediate(conn):
        conn.execute("INSERT INTO users VALUES ('u1', 'oscar', 'x', 0, 0)")
        conn.execute("INSERT INTO groups VALUES ('g1', 'amis', 'u1', 0, 1000)")
        conn.execute("INSERT INTO servers (id, group_id, name, mc_version, loader, memory_mb, created_at)"
                     " VALUES ('s1', 'g1', 'Survie', '26.3', 'fabric', 2048, 0)")
        conn.execute("INSERT INTO access_requests (server_id, player_name, status, first_at, last_at, attempts)"
                     " VALUES ('s1', 'Steve', 'refused', 1, 2, 3)")
    migrate(conn)
    row = conn.execute("SELECT * FROM access_requests").fetchone()
    assert (row["player_name"], row["place"], row["kind"], row["status"], row["attempts"]) == (
        "Steve", "", "join", "refused", 3)


# ------------------------------------------------------------------ statistics

def test_stats_count_play_hosting_and_hours(world) -> None:
    h = world.h
    conn = h.ctx.conn
    rec = StatsRecorder()
    t0 = datetime(2026, 11, 2, 20, 0, tzinfo=UTC).timestamp()  # a Monday, 20:00 UTC
    for i in range(7):  # one minute, a sample every 10 s
        rec.observe(world.server_id, world.pc_oscar, ["Oscar", "Lea"], 2, t0 + 10 * i)
    rec.observe(world.server_id, world.pc_oscar, ["Oscar"], 1, t0 + 400)  # late: counts 30 s at most
    rec.observe(world.server_id, world.pc_lea, ["Oscar"], 1, t0 + 410)    # new host: clock restarts
    rec.observe(world.server_id, world.pc_lea, ["Oscar"], 1, t0 + 420)
    rec.observe(world.server_id, world.pc_lea, [], 0, t0 + 430)
    rec.flush(conn, t0 + 431)
    rec.observe(world.server_id, world.pc_lea, [], 0, t0 + 440)  # added to the same rows later
    rec.flush(conn, t0 + 441)

    h.clock.t = t0 + 500
    st = h.get(world.lea, f"/servers/{world.server_id}/stats?days=7&utc_offset_min=60").json()
    players = {p["player"]: p["seconds"] for p in st["players"]}
    assert players == {"Oscar": 60 + 30 + 10, "Lea": 60}
    hosts = {x["display_name"]: x["seconds"] for x in st["hosts"]}
    assert hosts == {"PC Oscar": 90, "PC Lea": 30}
    assert st["online_seconds"] == 120
    # 20:00 UTC is 21:00 in France (UTC+1): Monday, 21h.
    busy = [x for x in st["hours"] if x["online_seconds"]]
    assert [(x["weekday"], x["hour"]) for x in busy] == [(0, 21)]
    assert busy[0]["average_players"] == round((2 * 60 + 30 + 10) / 120, 2)
    assert len(st["hours"]) == 168
    outsider = h.register("eve")
    assert h.get(outsider, f"/servers/{world.server_id}/stats").status_code == 404


def test_a_stopped_server_restarts_the_clock(world) -> None:
    h = world.h
    rec = h.ctx.coordinator.stats
    rec.observe(world.server_id, world.pc_oscar, ["Oscar"], 1, 1000.0)
    rec.observe(world.server_id, world.pc_oscar, ["Oscar"], 1, 1010.0)
    rec.forget(world.server_id)  # the server stopped
    rec.observe(world.server_id, world.pc_oscar, ["Oscar"], 1, 5000.0)
    rec.flush(h.ctx.conn, 5001.0)
    total = h.ctx.conn.execute("SELECT SUM(seconds) FROM play_stats").fetchone()[0]
    assert total == 10.0  # nothing counted for the time the server was stopped
