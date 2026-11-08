"""End-to-end coordination scenarios: two simulated PCs talking to the real control plane.

Time is controlled (FakeClock) and the periodic sweep is triggered explicitly with ``tick()``.
Lease TTL 30 s, grace 15 s, heartbeat 10 s, offers expire after 30 s.
"""

from datetime import datetime

from mdh_harness import DEFAULT_AVAILABILITY


def start_on_oscar(world):
    """Situation A: Oscar's PC (priority 100) becomes the host."""
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    pc_l = h.connect_host(world.lea, world.pc_lea)
    assert h.post(world.lea, f"/servers/{world.server_id}/start").status_code == 200
    epoch = pc_o.accept_next_offer()
    pc_l.expect_none("hosting_offer")
    assert pc_o.report(world.server_id, "running").status_code == 204
    return pc_o, pc_l, epoch


def test_situation_a_priority_host_runs_the_server(world) -> None:
    h = world.h
    pc_o, pc_l, epoch = start_on_oscar(world)
    s = h.status(world.lea, world.server_id)
    assert (s["state"], s["active_host_id"], s["lease_epoch"], epoch) == ("running", world.pc_oscar, 1, 1)
    ack = pc_o.heartbeat(metrics={"cpu_percent": 12.5, "ram_used_mb": 5000, "ram_free_mb": 20000,
                                  "disk_free_mb": 500000, "players_online": 3, "tps": 20.0})
    assert ack["leases"][0]["ok"] is True
    assert h.status(world.lea, world.server_id)["players_online"] == 3
    assert h.get(world.lea, f"/servers/{world.server_id}/metrics").json()[0]["tps"] == 20.0
    assert h.event_types(world.oscar, world.server_id)[-4:] == [
        "start_requested", "offer_sent", "lease_granted", "host_running"]


def test_live_updates_reach_group_members(world) -> None:
    h = world.h
    viewer = h.connect_viewer(world.lea)
    start_on_oscar(world)
    states = []
    while True:
        try:
            states.append(viewer.expect("server_update", timeout=0.5)["state"])
        except TimeoutError:
            break
    assert states[-1] == "running"
    assert "electing" in states and "assigned" in states


def test_clean_stop(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    assert h.post(world.lea, f"/servers/{world.server_id}/stop", {"countdown_s": 60}).status_code == 200
    stop = pc_o.expect("stop_request")
    assert stop["countdown_s"] == 60
    assert h.status(world.oscar, world.server_id)["state"] == "stopping"
    # A late "running" report while stopping must not flip the state back.
    pc_o.report(world.server_id, "running")
    assert h.status(world.oscar, world.server_id)["state"] == "stopping"
    assert pc_o.release(world.server_id).status_code == 204
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["active_host_id"], s["desired_running"]) == ("offline", None, False)
    pc_l.expect_none("hosting_offer")


def test_situation_b_host_leaves_and_another_takes_over(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    # Oscar closes the app: final snapshot (Phase 4), then release. The group still wants to play.
    assert pc_o.release(world.server_id).status_code == 204
    epoch = pc_l.accept_next_offer()
    assert epoch == 2
    assert h.status(world.oscar, world.server_id)["active_host_id"] == world.pc_lea
    pc_o.expect_none("hosting_offer")


def test_declined_offer_goes_to_next_candidate(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    pc_l = h.connect_host(world.lea, world.pc_lea)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    offer = pc_o.expect("hosting_offer")
    pc_o.send({"type": "offer_reply", "offer_id": offer["offer_id"], "accept": False, "reason": "busy"})
    assert pc_l.accept_next_offer() == 1
    # The declined offer can no longer be used.
    assert pc_o.acquire(world.server_id, offer["offer_id"]).status_code == 410


def test_unanswered_offer_expires(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    pc_l = h.connect_host(world.lea, world.pc_lea)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    offer = pc_o.expect("hosting_offer")
    h.advance(31)
    h.tick()
    assert pc_o.acquire(world.server_id, offer["offer_id"]).json()["error"]["code"] == "offer_expired"
    pc_l.accept_next_offer()


def test_offers_are_bound_to_their_host(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    h.connect_host(world.lea, world.pc_lea)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    offer = pc_o.expect("hosting_offer")
    # Léa tries to grab Oscar's offer with her own PC, then with Oscar's PC id.
    r = h.post(world.lea, f"/servers/{world.server_id}/lease/acquire",
               {"host_id": world.pc_lea, "offer_id": offer["offer_id"]})
    assert r.status_code == 410
    r = h.post(world.lea, f"/servers/{world.server_id}/lease/acquire",
               {"host_id": world.pc_oscar, "offer_id": offer["offer_id"]})
    assert r.status_code == 404


def test_ineligible_priority_host_is_skipped(world) -> None:
    h = world.h
    h.connect_host(world.oscar, world.pc_oscar, availability={**DEFAULT_AVAILABILITY, "free_ram_mb": 4000})
    pc_l = h.connect_host(world.lea, world.pc_lea)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    pc_l.accept_next_offer()


def test_no_host_available_then_one_appears(world) -> None:
    h = world.h
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["desired_running"]) == ("offline", True)
    events = h.get(world.oscar, f"/servers/{world.server_id}/events").json()
    no_host = next(e for e in events if e["type"] == "no_eligible_host")
    assert set(no_host["payload"]["rejected"][world.pc_oscar]) >= {"offline"}
    h.tick()  # no duplicate journal entry while waiting
    assert h.event_types(world.oscar, world.server_id).count("no_eligible_host") == 1
    pc_l = h.connect_host(world.lea, world.pc_lea)
    h.tick()
    pc_l.accept_next_offer()


def test_unreachable_host_recovers_without_failover(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    h.advance(26)  # 2.6 heartbeat intervals without renewal, lease still valid (30 s)
    h.tick()
    assert h.status(world.oscar, world.server_id)["state"] == "unreachable"
    assert pc_o.heartbeat()["leases"][0]["ok"] is True
    assert h.status(world.oscar, world.server_id)["state"] == "running"


def crash_oscar(world, pc_o) -> None:
    """Oscar's PC loses power: no release, no more heartbeats."""
    h = world.h
    pc_o.close()
    h.advance(26)
    h.tick()
    assert h.status(world.oscar, world.server_id)["state"] == "unreachable"
    h.advance(5)  # lease expired (30 s after the last renewal)
    h.tick()
    assert h.status(world.oscar, world.server_id)["state"] == "orphaned"


def test_brutal_crash_requires_confirmation_then_fails_over(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    crash_oscar(world, pc_o)
    pc_l.expect_none("hosting_offer")

    r = h.post(world.lea, f"/servers/{world.server_id}/start")
    assert r.json()["error"]["code"] == "needs_failover_confirmation"
    r = h.post(world.lea, f"/servers/{world.server_id}/failover/confirm")
    assert r.status_code == 403  # admins only

    r = h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_isolated_yet"
    # Lease expired 1 s ago (30 s after the grant); grace is 15 s.
    assert int(r.headers["Retry-After"]) == 14

    h.advance(14)
    assert h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm").status_code == 200
    assert pc_l.accept_next_offer() == 2
    assert "failover_confirmed" in h.event_types(world.oscar, world.server_id)


def test_returning_zombie_host_is_fenced(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    crash_oscar(world, pc_o)
    h.advance(15)
    h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm")
    pc_l.accept_next_offer()

    # Oscar's PC wakes up and still believes it holds epoch 1.
    zombie = h.connect_host(world.oscar, world.pc_oscar)
    ack = zombie.heartbeat(leases={world.server_id: 1})
    assert ack["leases"][0]["ok"] is False
    r = h.post(world.oscar, f"/servers/{world.server_id}/lease/renew", {"host_id": world.pc_oscar, "epoch": 1})
    assert r.json()["error"]["code"] == "stale_epoch"
    r = h.post(world.oscar, f"/servers/{world.server_id}/host-state",
               {"host_id": world.pc_oscar, "epoch": 1, "state": "running"})
    assert r.status_code == 409
    r = h.post(world.oscar, f"/servers/{world.server_id}/lease/release", {"host_id": world.pc_oscar, "epoch": 1})
    assert r.status_code == 409
    s = h.status(world.oscar, world.server_id)
    assert (s["active_host_id"], s["lease_epoch"]) == (world.pc_lea, 2)


def test_crashed_host_coming_back_resumes_without_loss(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    crash_oscar(world, pc_o)
    back = h.connect_host(world.oscar, world.pc_oscar)
    offer = back.expect("hosting_offer")
    assert offer["reason"] == "resume"
    r = back.acquire(world.server_id, offer["offer_id"])
    assert r.status_code == 200 and r.json()["epoch"] == 2
    assert h.status(world.oscar, world.server_id)["state"] == "assigned"
    assert "lease_granted" in h.event_types(world.oscar, world.server_id)
    pc_l.expect_none("hosting_offer")


def test_host_back_before_expiry_gets_resume_offer_when_lease_expires(world) -> None:
    """Regression: the holder reconnects while its lease is still valid (it already self-fenced
    and stopped renewing). When the lease then expires, it must be offered the resume."""
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    pc_o.close()
    h.advance(10)
    back = h.connect_host(world.oscar, world.pc_oscar)   # no lease in its heartbeats any more
    back.expect_none("hosting_offer")                    # lease still valid: nothing to resume yet
    h.advance(21)
    h.tick()
    assert h.status(world.oscar, world.server_id)["state"] == "orphaned"
    offer = back.expect("hosting_offer")
    assert offer["reason"] == "resume"
    assert back.acquire(world.server_id, offer["offer_id"]).json()["epoch"] == 2
    pc_l.expect_none("hosting_offer")


def test_automatic_failover_when_enabled(world) -> None:
    h = world.h
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"auto_failover_after_s": 300})
    pc_o, pc_l, _ = start_on_oscar(world)
    assert h.status(world.oscar, world.server_id)["auto_failover_at"] is None  # only once orphaned
    crash_oscar(world, pc_o)
    # The members see when it will happen: lease expiry + 300 s, exactly what the sweep uses.
    deadline = datetime.fromisoformat(h.status(world.lea, world.server_id)["auto_failover_at"]).timestamp()
    assert 290 < deadline - h.clock.t <= 300
    h.advance(deadline - h.clock.t - 1)
    h.tick()
    pc_l.expect_none("hosting_offer")
    h.advance(1)
    h.tick()
    assert pc_l.accept_next_offer() == 2
    assert "auto_failover" in h.event_types(world.oscar, world.server_id)
    assert h.status(world.oscar, world.server_id)["auto_failover_at"] is None


def test_no_failover_deadline_without_the_setting_or_once_stopped(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    crash_oscar(world, pc_o)
    assert h.status(world.oscar, world.server_id)["auto_failover_at"] is None  # admin decides
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"auto_failover_after_s": 600})
    assert h.status(world.oscar, world.server_id)["auto_failover_at"] is not None
    assert h.post(world.oscar, f"/servers/{world.server_id}/stop").status_code == 200
    assert h.status(world.oscar, world.server_id)["auto_failover_at"] is None  # nobody wants it up


def test_startup_quarantine_after_control_plane_restart(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    crash_oscar(world, pc_o)
    h.advance(15)
    # Simulate a VPS restart right now: the new process must wait TTL + grace.
    h.ctx.started_at = h.clock()
    r = h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm")
    assert r.json()["error"]["code"] == "not_isolated_yet"
    h.advance(45)
    assert h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm").status_code == 200


def start_on_lea(world, oscar_online: bool = True):
    """Léa's PC hosts (Oscar's PC, if connected, can take over)."""
    h = world.h
    h.set_priority(world.oscar, world.server_id, world.pc_lea, 100)
    h.set_priority(world.oscar, world.server_id, world.pc_oscar, 10)
    pc_o = h.connect_host(world.oscar, world.pc_oscar) if oscar_online else None
    pc_l = h.connect_host(world.lea, world.pc_lea)
    assert h.post(world.oscar, f"/servers/{world.server_id}/start").status_code == 200
    pc_l.accept_next_offer()
    assert pc_l.report(world.server_id, "running").status_code == 204
    return pc_o, pc_l


def members(world) -> dict[str, dict]:
    r = world.h.get(world.oscar, f"/groups/{world.group_id}/members")
    return {m["username"]: m for m in r.json()}


def test_removing_a_member_who_hosts_moves_the_server_first(world) -> None:
    h = world.h
    pc_o, pc_l = start_on_lea(world)
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers)
    assert r.status_code == 200 and r.json() == {"pending": True}
    # Still a member, no longer allowed to host; the server goes to Oscar's PC while players play on.
    assert members(world)["lea"]["leaving"] is True and members(world)["lea"]["can_host"] is False
    assert pc_o.expect("prefetch_request")["server_id"] == world.server_id
    s = h.status(world.oscar, world.server_id)
    assert s["state"] == "migrating" and s["migration"]["target_host_id"] == world.pc_oscar
    # Rights of a leaving member cannot be given back meanwhile.
    r = h.client.patch(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}",
                       headers=world.oscar.headers, json={"can_host": True})
    assert r.status_code == 409
    # Once her PC no longer holds the server, she is gone.
    assert pc_l.release(world.server_id).status_code == 204
    h.tick()
    assert "lea" not in members(world)
    assert h.get(world.lea, f"/servers/{world.server_id}").status_code == 404


def test_a_leaving_host_with_nobody_to_take_over_stops_the_server(world) -> None:
    h = world.h
    _, pc_l = start_on_lea(world, oscar_online=False)
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.lea.headers)
    assert r.json() == {"pending": True}  # she leaves by herself: same thing
    assert pc_l.expect("stop_request")["server_id"] == world.server_id
    s = h.status(world.oscar, world.server_id)
    assert s["state"] == "stopping" and s["desired_running"] is False
    h.tick()
    assert "lea" in members(world)  # still hosting: not yet
    pc_l.release(world.server_id)
    h.tick()
    assert "lea" not in members(world)


def test_a_leaving_host_that_ignores_everything_loses_the_lease(world) -> None:
    from mdh_server.coordination.service import DEPARTURE_DEADLINE_S

    h = world.h
    _, pc_l = start_on_lea(world, oscar_online=False)
    h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers)
    assert pc_l.heartbeat()["leases"][0]["ok"] is True
    h.advance(DEPARTURE_DEADLINE_S + 1)
    assert pc_l.heartbeat()["leases"][0]["ok"] is False  # it must stop: its lease is gone


def test_member_hosting_cannot_be_removed(world) -> None:
    h = world.h
    start_on_oscar(world)
    h.client.patch(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}",
                   headers=world.oscar.headers, json={"role": "admin"})
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.oscar.user_id}", headers=world.lea.headers)
    assert r.status_code == 403  # owner
    s = h.status(world.oscar, world.server_id)
    assert s["active_host_id"] == world.pc_oscar


def test_host_cannot_be_deleted_while_holding_a_lease(world) -> None:
    h = world.h
    start_on_oscar(world)
    r = h.client.delete(f"/api/v1/hosts/{world.pc_oscar}", headers=world.oscar.headers)
    assert r.status_code == 409


def test_restart_requested_while_electing_is_idempotent(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    h.post(world.lea, f"/servers/{world.server_id}/start")
    pc_o.expect("hosting_offer")
    pc_o.expect_none("hosting_offer")
    offers = h.ctx.conn.execute("SELECT COUNT(*) FROM offers WHERE status = 'pending'").fetchone()[0]
    assert offers == 1


def test_stop_while_electing_cancels_offer(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    offer = pc_o.expect("hosting_offer")
    h.post(world.oscar, f"/servers/{world.server_id}/stop")
    assert h.status(world.oscar, world.server_id)["state"] == "offline"
    assert pc_o.acquire(world.server_id, offer["offer_id"]).status_code == 410


def test_rights_and_servers_reach_the_members_applications_at_once(world) -> None:
    h = world.h
    viewer = h.connect_viewer(world.lea)
    r = h.client.patch(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}",
                       headers=world.oscar.headers, json={"role": "admin"})
    assert r.status_code == 204
    assert viewer.expect("group_update")["group_id"] == world.group_id
    # A new server shows up for everyone, without restarting the application.
    created = h.post(world.oscar, f"/groups/{world.group_id}/servers", {"name": "Créatif", "mc_version": "26.3"})
    assert created.status_code == 201
    assert viewer.expect("group_update")["group_id"] == world.group_id
    # A member who is removed learns it too (no longer in the group's list).
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers)
    assert r.status_code == 200 and r.json() == {"pending": False}
    assert viewer.expect("group_update")["group_id"] == world.group_id


def test_a_vanished_host_does_not_hold_its_owners_departure_up(world) -> None:
    """Its lease expired (the server is orphaned): that PC can no longer write, the member goes."""
    h = world.h
    _, pc_l = start_on_lea(world, oscar_online=False)
    pc_l.close()
    for _ in range(10):
        h.advance(30)
        h.tick()
    assert h.status(world.oscar, world.server_id)["state"] == "orphaned"
    r = h.client.delete(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers)
    assert r.json() == {"pending": False}
    assert "lea" not in members(world)


def test_removing_a_leaving_member_again_starts_nothing_new(world) -> None:
    h = world.h
    start_on_lea(world, oscar_online=False)
    url = f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}"
    assert h.client.delete(url, headers=world.oscar.headers).json() == {"pending": True}
    assert h.client.delete(url, headers=world.lea.headers).json() == {"pending": True}
    leaving = h.ctx.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'member_leaving'").fetchone()[0]
    assert leaving == 1
