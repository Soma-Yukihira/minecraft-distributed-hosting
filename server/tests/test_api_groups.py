"""Groups, members, hosts and servers: permissions and validation."""

import pytest


def test_owner_creates_group_and_member_joins(world) -> None:
    h = world.h
    members = h.get(world.oscar, f"/groups/{world.group_id}/members").json()
    assert {(m["username"], m["role"]) for m in members} == {("oscar", "owner"), ("lea", "member")}
    assert sorted(x["display_name"] for m in members for x in m["hosts"]) == ["PC Lea", "PC Oscar"]


def test_outsiders_cannot_see_anything(world) -> None:
    h = world.h
    eve = h.register("eve")
    assert h.get(eve, f"/groups/{world.group_id}").status_code == 404
    assert h.get(eve, f"/servers/{world.server_id}").status_code == 404
    assert h.post(eve, f"/servers/{world.server_id}/start").status_code == 404
    # Same answer as for a group that does not exist: no existence oracle.
    assert h.get(eve, "/groups/" + "0" * 32).json() == h.get(eve, f"/groups/{world.group_id}").json()


def test_members_cannot_administer(world) -> None:
    h, lea = world.h, world.lea
    assert h.post(lea, f"/groups/{world.group_id}/invites", {}).status_code == 403
    assert h.post(lea, f"/groups/{world.group_id}/servers",
                  {"name": "x", "mc_version": "1.21.4"}).status_code == 403
    r = h.client.put(f"/api/v1/servers/{world.server_id}/hosts/{world.pc_lea}/priority",
                     headers=lea.headers, json={"priority": 100})
    assert r.status_code == 403
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=lea.headers, json={"memory_mb": 2048})
    assert r.status_code == 403


def test_owner_cannot_be_removed_and_admin_rules(world) -> None:
    h = world.h
    gid = world.group_id
    r = h.client.delete(f"/api/v1/groups/{gid}/members/{world.oscar.user_id}", headers=world.lea.headers)
    assert r.status_code == 403
    r = h.client.patch(f"/api/v1/groups/{gid}/members/{world.lea.user_id}", headers=world.oscar.headers,
                       json={"role": "admin"})
    assert r.status_code == 204
    r = h.client.patch(f"/api/v1/groups/{gid}/members/{world.oscar.user_id}", headers=world.lea.headers,
                       json={"can_host": False})
    assert r.status_code == 403
    assert h.post(world.lea, f"/groups/{gid}/invites", {}).status_code == 201


def test_member_can_leave(world) -> None:
    h = world.h
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.lea.headers)
    assert r.status_code == 200 and r.json() == {"pending": False}
    assert h.get(world.lea, f"/servers/{world.server_id}").status_code == 404


def test_hosts_belong_to_their_owner(world) -> None:
    h = world.h
    r = h.client.patch(f"/api/v1/hosts/{world.pc_oscar}", headers=world.lea.headers, json={"display_name": "mine"})
    assert r.status_code == 404
    r = h.client.patch(f"/api/v1/hosts/{world.pc_lea}", headers=world.lea.headers,
                       json={"hosting_allowed": False, "max_ram_mb": 4096})
    assert r.status_code == 200 and r.json()["hosting_allowed"] is False


def test_server_hosts_lists_priorities(world) -> None:
    h = world.h
    h.connect_host(world.lea, world.pc_lea)
    rows = h.get(world.lea, f"/servers/{world.server_id}/hosts").json()
    assert [(r["display_name"], r["priority"], r["online"]) for r in rows] == [
        ("PC Oscar", 100, False), ("PC Lea", 70, True)]
    assert h.get(h.register("eve"), f"/servers/{world.server_id}/hosts").status_code == 404


def test_priority_only_for_hosts_of_group_members(world) -> None:
    h = world.h
    eve = h.register("eve")
    pc_eve = h.register_host(eve)
    r = h.client.put(f"/api/v1/servers/{world.server_id}/hosts/{pc_eve}/priority",
                     headers=world.oscar.headers, json={"priority": 100})
    assert r.status_code == 404


@pytest.mark.parametrize("properties", [
    {"motd": "hello\nonline-mode=false"},          # newline injection
    {"online-mode": False},                          # not remotely configurable
    {"server-port": 1234},
    {"rcon.password": "x"},
    {"max-players": "20"},                           # wrong type
    {"view-distance": 100},                          # out of range
    {"pvp": 1},
])
def test_server_properties_are_whitelisted(world, properties) -> None:
    r = world.h.post(world.oscar, f"/groups/{world.group_id}/servers",
                     {"name": "x", "mc_version": "1.21.4", "properties": properties})
    assert r.status_code == 422, properties


def test_server_creation_and_patch(world) -> None:
    h = world.h
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["loader"], s["config_revision"], s["desired_running"]) == ("offline", "paper", 1, False)
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                       json={"memory_mb": 8192, "properties": {"view-distance": 12, "motd": "Salut"}})
    assert r.status_code == 200
    assert (r.json()["memory_mb"], r.json()["config_revision"]) == (8192, 2)
    assert r.json()["properties"] == {"view-distance": 12, "motd": "Salut"}


def test_offline_accounts_and_allowed_players(world) -> None:
    h = world.h
    s = h.status(world.oscar, world.server_id)
    assert (s["allow_offline_accounts"], s["allowed_players"]) == (False, [])
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                       json={"allow_offline_accounts": True, "allowed_players": ["oscar", "Lea_2", "OSCAR"]})
    assert r.status_code == 200
    assert (r.json()["allow_offline_accounts"], r.json()["allowed_players"]) == (True, ["oscar", "Lea_2"])
    for bad in (["a b"], ["x"], ["oscar;op eve"], ["n" * 17]):
        r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                           json={"allowed_players": bad})
        assert r.status_code == 422, bad
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.lea.headers,
                       json={"allow_offline_accounts": False})
    assert r.status_code == 403


@pytest.mark.parametrize("version", ["1.21","1.21.4", "1.20.5-rc1", "24w14a", "26.3", "26.1.2",
                                     "26.3-rc-3", "26.3-pre-1", "26.4-snapshot-6", "1.21.11-pre5"])
def test_minecraft_versions_accepted(world, version) -> None:
    r = world.h.post(world.oscar, f"/groups/{world.group_id}/servers", {"name": "v", "mc_version": version})
    assert r.status_code == 201


@pytest.mark.parametrize("version", ["latest", "1.21.4; rm -rf /", "../1.21", "1.14 Pre-Release 1",
                                     "1.21.11_unobfuscated"])
def test_bad_minecraft_versions_rejected(world, version) -> None:
    r = world.h.post(world.oscar, f"/groups/{world.group_id}/servers", {"name": "v", "mc_version": version})
    assert r.status_code == 422


def test_the_owner_hands_the_group_over(world) -> None:
    from mdh_harness import PASSWORD  # same password for every test user

    h = world.h
    gid = world.group_id
    url = f"/api/v1/groups/{gid}/owner"
    # Only the owner, with their password, to a member of the group.
    assert h.client.post(url, headers=world.lea.headers,
                         json={"user_id": world.lea.user_id, "current_password": PASSWORD}).status_code == 403
    assert h.client.post(url, headers=world.oscar.headers,
                         json={"user_id": world.lea.user_id, "current_password": "wrong password"}).status_code == 401
    assert h.client.post(url, headers=world.oscar.headers,
                         json={"user_id": "f" * 32, "current_password": PASSWORD}).status_code == 404
    assert h.client.post(url, headers=world.oscar.headers,
                         json={"user_id": world.oscar.user_id, "current_password": PASSWORD}).status_code == 409
    # Not through the role either.
    r = h.client.patch(f"/api/v1/groups/{gid}/members/{world.lea.user_id}", headers=world.oscar.headers,
                       json={"role": "owner"})
    assert r.status_code == 403

    r = h.client.post(url, headers=world.oscar.headers,
                      json={"user_id": world.lea.user_id, "current_password": PASSWORD})
    assert r.status_code == 204
    roles = {m["username"]: m["role"] for m in h.get(world.oscar, f"/groups/{gid}/members").json()}
    assert roles == {"lea": "owner", "oscar": "admin"}
    assert h.ctx.conn.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone()[0] == world.lea.user_id
    # The former owner can leave now; the new one cannot.
    r = h.client.delete(f"/api/v1/groups/{gid}/members/{world.lea.user_id}", headers=world.lea.headers)
    assert r.status_code == 403
    r = h.client.delete(f"/api/v1/groups/{gid}/members/{world.oscar.user_id}", headers=world.oscar.headers)
    assert r.json() == {"pending": False}


def test_the_handover_is_checked_again_after_the_password(world, monkeypatch) -> None:
    """The member is removed while the password is checked: the group keeps its owner."""
    from mdh_harness import PASSWORD

    h = world.h
    gid = world.group_id
    verify = h.ctx.passwords.verify

    async def slow_verify(encoded, password):
        h.ctx.conn.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?", (gid, world.lea.user_id))
        return await verify(encoded, password)

    monkeypatch.setattr(h.ctx.passwords, "verify", slow_verify)
    r = h.client.post(f"/api/v1/groups/{gid}/owner", headers=world.oscar.headers,
                      json={"user_id": world.lea.user_id, "current_password": PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "membership_changed"
    assert h.ctx.conn.execute("SELECT owner_id FROM groups WHERE id = ?", (gid,)).fetchone()[0] == world.oscar.user_id
    roles = {m["username"]: m["role"] for m in h.get(world.oscar, f"/groups/{gid}/members").json()}
    assert roles == {"oscar": "owner"}
