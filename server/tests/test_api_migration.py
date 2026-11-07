"""Planned migrations (docs/ARCHITECTURE.md 3.5 and 3.6) through the real control plane.

Two simulated PCs: Oscar (owner, priority 100) and Léa (member, priority 70). Time is controlled
(FakeClock); the periodic sweep is triggered with ``tick()``.
"""

from mdh_harness import DEFAULT_AVAILABILITY

METRICS = {"cpu_percent": 10.0, "ram_used_mb": 4000, "ram_free_mb": 20000, "disk_free_mb": 500000}


def run_on(world, first: str):
    """The server runs on ``first`` ("oscar" or "lea"), the other PC is online but idle."""
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    pc_l = h.connect_host(world.lea, world.pc_lea)
    if first == "lea":
        pc_o.heartbeat(availability={**DEFAULT_AVAILABILITY, "hosting_allowed": False})
    assert h.post(world.oscar, f"/servers/{world.server_id}/start").status_code == 200
    host = pc_o if first == "oscar" else pc_l
    host.accept_next_offer()
    assert host.report(world.server_id, "running").status_code == 204
    if first == "lea":
        pc_o.heartbeat(availability=DEFAULT_AVAILABILITY)
    return pc_o, pc_l


def transfer(world, user, **body):
    return world.h.post(user, f"/servers/{world.server_id}/transfer", body)


def migration(world) -> dict | None:
    return world.h.status(world.oscar, world.server_id)["migration"]


def hand_over(world, source, target, countdown_s: int = 60) -> dict:
    """Drive a transfer up to the moment the target receives its hosting offer."""
    prefetch = target.expect("prefetch_request")
    target.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    request = source.expect("migration_request")
    assert request["countdown_s"] == countdown_s
    assert source.release(world.server_id).status_code == 204
    offer = target.expect("hosting_offer")
    assert offer["reason"] == "migration"
    return offer


# --------------------------------------------------------------------------- voluntary transfer


def test_voluntary_transfer_hands_the_world_over_step_by_step(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    r = transfer(world, world.oscar, target_host_id=world.pc_lea, countdown_s=30)
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "migrating"
    assert r.json()["migration"]["status"] == "prefetching"

    # 2. The target downloads the world while the game goes on (no lease involved).
    prefetch = pc_l.expect("prefetch_request")
    assert prefetch["version_id"] is None and prefetch["server_id"] == world.server_id
    pc_o.expect_none("migration_request")              # nothing is stopped before the prefetch
    pc_o.report(world.server_id, "running")            # a late report keeps "migrating"
    assert h.status(world.oscar, world.server_id)["state"] == "migrating"
    pc_l.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})

    # 3-6. The current host announces, stops, commits, releases.
    request = pc_o.expect("migration_request")
    assert (request["target_host_id"], request["countdown_s"]) == (world.pc_lea, 30)
    assert migration(world)["status"] == "stopping"
    assert pc_o.report(world.server_id, "stopping").status_code == 204
    assert pc_o.release(world.server_id).status_code == 204

    # 7. Only then is the target offered the server, with the next epoch.
    offer = pc_l.expect("hosting_offer")
    assert offer["reason"] == "migration"
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["migration"]["status"], s["active_host_id"]) == ("migrating", "handover", None)
    assert pc_l.acquire(world.server_id, offer["offer_id"]).json()["epoch"] == 2
    pc_l.report(world.server_id, "starting")
    assert migration(world)["status"] == "handover"
    pc_l.report(world.server_id, "running")

    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["active_host_id"], s["migration"]) == ("running", world.pc_lea, None)
    pc_o.expect_none("hosting_offer")
    events = h.event_types(world.oscar, world.server_id)
    assert events[events.index("migration_started"):] == [
        "migration_started", "migration_prefetched", "host_stopping", "lease_released",
        "migration_handover", "lease_granted", "host_starting", "host_running", "migration_completed"]


def test_transfer_picks_the_best_other_pc_when_no_target_is_given(world) -> None:
    pc_o, pc_l = run_on(world, "oscar")
    assert transfer(world, world.oscar).status_code == 202
    assert migration(world)["target_host_id"] == world.pc_lea
    assert migration(world)["countdown_s"] == 60


def test_transfer_is_refused_when_it_cannot_work(world) -> None:
    h = world.h
    pc_o = h.connect_host(world.oscar, world.pc_oscar)
    pc_l = h.connect_host(world.lea, world.pc_lea)
    r = transfer(world, world.oscar, target_host_id=world.pc_lea)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_running")

    h.post(world.oscar, f"/servers/{world.server_id}/start")
    pc_o.accept_next_offer()
    pc_o.report(world.server_id, "running")
    r = transfer(world, world.oscar, target_host_id=world.pc_oscar)
    assert r.json()["error"]["code"] == "already_hosting"

    pc_l.heartbeat(availability={**DEFAULT_AVAILABILITY, "free_ram_mb": 1000})
    r = transfer(world, world.oscar, target_host_id=world.pc_lea)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "target_not_eligible")
    assert r.json()["error"]["details"]["reasons"] == ["not_enough_ram"]
    r = transfer(world, world.oscar)
    assert r.json()["error"]["code"] == "no_eligible_host"
    assert r.json()["error"]["details"]["rejected"][world.pc_lea] == ["not_enough_ram"]
    assert h.status(world.oscar, world.server_id)["state"] == "running"   # nothing changed

    pc_l.heartbeat(availability=DEFAULT_AVAILABILITY)
    assert transfer(world, world.oscar, target_host_id=world.pc_lea).status_code == 202
    r = transfer(world, world.oscar, target_host_id=world.pc_lea)
    assert r.json()["error"]["code"] == "migration_in_progress"


def test_who_may_move_the_server(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    # Léa is a plain member: she cannot push Oscar's PC around...
    assert transfer(world, world.lea).status_code == 403
    # ...but she may ask for the server to come to her own PC ("héberger sur mon PC").
    assert transfer(world, world.lea, target_host_id=world.pc_lea).status_code == 202
    outsider = h.register("mallory")
    assert transfer(world, outsider, target_host_id=world.pc_lea).status_code == 404
    assert h.post(outsider, f"/servers/{world.server_id}/transfer/cancel").status_code == 404


# --------------------------------------------------------------------------- cancellations


def test_cancel_during_countdown_keeps_the_current_host(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    prefetch = pc_l.expect("prefetch_request")
    pc_l.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    pc_o.expect("migration_request")

    r = h.post(world.lea, f"/servers/{world.server_id}/transfer/cancel")   # the target's owner
    assert r.status_code == 200, r.text
    assert pc_o.expect("migration_cancel")["reason"] == "cancelled_by_user"
    assert pc_l.expect("migration_cancel")["migration_id"] == prefetch["migration_id"]
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["active_host_id"], s["migration"]) == ("running", world.pc_oscar, None)
    assert pc_o.heartbeat()["leases"][0]["ok"] is True                    # still the host
    r = h.post(world.oscar, f"/servers/{world.server_id}/transfer/cancel")
    assert r.json()["error"]["code"] == "no_migration"


def test_cancel_is_refused_once_the_world_was_handed_over(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    hand_over(world, pc_o, pc_l, countdown_s=60)
    r = h.post(world.oscar, f"/servers/{world.server_id}/transfer/cancel")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "too_late")


def test_failed_prefetch_cancels_without_touching_the_running_server(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    prefetch = pc_l.expect("prefetch_request")
    pc_l.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": False,
               "reason": "disque plein"})
    pc_o.expect("migration_cancel")
    pc_o.expect_none("migration_request")
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["active_host_id"]) == ("running", world.pc_oscar)
    assert h.event_types(world.oscar, world.server_id)[-1] == "migration_cancelled"
    # A late or forged result for that migration is ignored.
    pc_l.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    pc_o.expect_none("migration_request")


def test_target_disconnecting_cancels_the_migration(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    pc_l.expect("prefetch_request")
    pc_l.close()
    h.tick()
    assert pc_o.expect("migration_cancel")["reason"] == "target_offline"
    assert h.status(world.oscar, world.server_id)["state"] == "running"


def test_prefetch_that_never_finishes_times_out(harness_factory) -> None:
    h = harness_factory(migration_prefetch_timeout_s=120)
    oscar = h.register("oscar")
    gid = h.create_group(oscar)
    lea = h.register("lea", invite=h.invite(oscar, gid))
    pc_oscar, pc_lea = h.register_host(oscar), h.register_host(lea)
    sid = h.create_server(oscar, gid)
    h.set_priority(oscar, sid, pc_oscar, 100)
    pc_o, pc_l = h.connect_host(oscar, pc_oscar), h.connect_host(lea, pc_lea)
    h.post(oscar, f"/servers/{sid}/start")
    pc_o.accept_next_offer()
    pc_o.report(sid, "running")
    h.post(oscar, f"/servers/{sid}/transfer", {"target_host_id": pc_lea})
    for _ in range(5):                       # keep both PCs alive for 125 s
        h.advance(25)
        pc_o.heartbeat()
        pc_l.heartbeat()
        h.tick()
    assert pc_o.expect("migration_cancel")["reason"] == "prefetch_timeout"
    assert h.status(oscar, sid)["state"] == "running"


def test_stop_during_a_migration_wins(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    prefetch = pc_l.expect("prefetch_request")
    pc_l.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    pc_o.expect("migration_request")
    assert h.post(world.oscar, f"/servers/{world.server_id}/stop", {"countdown_s": 0}).status_code == 200
    pc_o.expect("stop_request")
    assert pc_l.expect("migration_cancel")["reason"] == "stop_requested"
    assert h.status(world.oscar, world.server_id)["state"] == "stopping"
    assert pc_o.release(world.server_id).status_code == 204
    s = h.status(world.oscar, world.server_id)
    assert (s["state"], s["migration"]) == ("offline", None)
    pc_l.expect_none("hosting_offer")


def test_source_lease_expiring_mid_migration_is_an_ordinary_crash(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    pc_l.expect("prefetch_request")
    h.advance(31)                            # Oscar's PC froze: no renewal
    pc_l.heartbeat()
    h.tick()
    assert pc_l.expect("migration_cancel")["reason"] in ("source_unreachable", "source_lost")
    assert h.status(world.oscar, world.server_id)["state"] == "orphaned"
    pc_l.expect_none("hosting_offer")        # never handed over without a final commit


# --------------------------------------------------------------------------- failed handover


def test_target_declining_at_handover_gives_the_server_back(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    offer = hand_over(world, pc_o, pc_l)
    pc_l.send({"type": "offer_reply", "offer_id": offer["offer_id"], "accept": False, "reason": "parti"})
    back = pc_o.expect("hosting_offer")
    assert back["reason"] == "election"
    assert pc_o.acquire(world.server_id, back["offer_id"]).json()["epoch"] == 2
    assert pc_l.acquire(world.server_id, offer["offer_id"]).status_code == 410
    assert "migration_failed" in h.event_types(world.oscar, world.server_id)


def test_target_vanishing_at_handover_gives_the_server_back(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    hand_over(world, pc_o, pc_l)
    h.advance(31)                            # the migration offer expires unanswered
    pc_o.heartbeat()
    h.tick()
    assert pc_o.expect("hosting_offer")["reason"] == "election"
    assert h.status(world.oscar, world.server_id)["migration"] is None


def test_target_failing_to_start_gives_the_server_back(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea)
    offer = hand_over(world, pc_o, pc_l)
    pc_l.acquire(world.server_id, offer["offer_id"])
    pc_l.report(world.server_id, "crashed")
    assert pc_l.release(world.server_id).status_code == 204
    assert pc_o.expect("hosting_offer")["reason"] == "election"
    events = h.event_types(world.oscar, world.server_id)
    assert "migration_failed" in events and "migration_completed" not in events


# --------------------------------------------------------------------------- preemption (3.6)


def keep_alive(world, pc_o, pc_l, seconds: float, players: int | None = None) -> None:
    """Let time pass while both PCs keep their sessions and leases alive."""
    while seconds > 0:
        step = min(seconds, 20)
        world.h.advance(step)
        seconds -= step
        metrics = None if players is None else {**METRICS, "players_online": players}
        for pc in (pc_o, pc_l):
            pc.heartbeat(metrics=metrics if pc.leases else None)
        world.h.tick()


def test_priority_pc_coming_back_takes_over_once_the_server_is_empty(world) -> None:
    h = world.h
    pc_o, pc_l = run_on(world, "lea")
    keep_alive(world, pc_o, pc_l, 30, players=2)
    pc_o.expect_none("prefetch_request")     # not stable yet, and players are on
    keep_alive(world, pc_o, pc_l, 60, players=2)
    pc_o.expect_none("prefetch_request")     # stable, but "when_empty" waits for nobody online
    keep_alive(world, pc_o, pc_l, 10, players=0)
    prefetch = pc_o.expect("prefetch_request")
    assert migration(world)["reason"] == "preemption"
    pc_o.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    assert pc_l.expect("migration_request")["countdown_s"] == 0
    assert pc_l.release(world.server_id).status_code == 204
    offer = pc_o.expect("hosting_offer")
    pc_o.acquire(world.server_id, offer["offer_id"])
    pc_o.report(world.server_id, "running")
    assert h.status(world.oscar, world.server_id)["active_host_id"] == world.pc_oscar


def test_countdown_preemption_warns_players_first(world) -> None:
    h = world.h
    r = h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                       json={"preemption": "countdown", "preemption_countdown_s": 120})
    assert r.status_code == 200 and r.json()["preemption_countdown_s"] == 120
    pc_o, pc_l = run_on(world, "lea")
    keep_alive(world, pc_o, pc_l, 70, players=3)
    prefetch = pc_o.expect("prefetch_request")
    pc_o.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": True})
    assert pc_l.expect("migration_request")["countdown_s"] == 120


def test_never_preempt_policy_and_lower_priority_pcs_change_nothing(world) -> None:
    h = world.h
    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"preemption": "never"})
    pc_o, pc_l = run_on(world, "lea")
    keep_alive(world, pc_o, pc_l, 90, players=0)
    pc_o.expect_none("prefetch_request")

    h.client.patch(f"/api/v1/servers/{world.server_id}", headers=world.oscar.headers,
                   json={"preemption": "when_empty"})
    h.set_priority(world.oscar, world.server_id, world.pc_oscar, 70)    # same priority as Léa
    keep_alive(world, pc_o, pc_l, 30, players=0)
    pc_o.expect_none("prefetch_request")


def test_failed_preemption_is_not_retried_in_a_loop(world) -> None:
    pc_o, pc_l = run_on(world, "lea")
    keep_alive(world, pc_o, pc_l, 70, players=0)
    prefetch = pc_o.expect("prefetch_request")
    pc_o.send({"type": "prefetch_done", "migration_id": prefetch["migration_id"], "ok": False,
               "reason": "refusé"})
    pc_l.expect("migration_cancel")
    keep_alive(world, pc_o, pc_l, 300, players=0)
    pc_o.expect_none("prefetch_request")
    keep_alive(world, pc_o, pc_l, 320, players=0)                        # retry delay is 600 s
    assert pc_o.expect("prefetch_request")["migration_id"] != prefetch["migration_id"]


def test_voluntary_transfer_away_from_the_priority_pc_is_not_undone(world) -> None:
    """Found with the real server: Oscar (priority 100) moved the server to Léa's PC on purpose,
    and the empty server came straight back to Oscar's PC."""
    h = world.h
    pc_o, pc_l = run_on(world, "oscar")
    transfer(world, world.oscar, target_host_id=world.pc_lea, countdown_s=0)
    offer = hand_over(world, pc_o, pc_l, countdown_s=0)
    pc_l.acquire(world.server_id, offer["offer_id"])
    pc_l.report(world.server_id, "running")
    keep_alive(world, pc_o, pc_l, 120, players=0)
    pc_o.expect_none("prefetch_request")
    assert h.status(world.oscar, world.server_id)["active_host_id"] == world.pc_lea

    # The choice holds for that hosting session only: on Léa's next session (new lease, e.g. after
    # a restart while Oscar's PC was busy), Oscar's PC is the priority one again.
    h.post(world.oscar, f"/servers/{world.server_id}/stop", {"countdown_s": 0})
    assert pc_l.release(world.server_id).status_code == 204
    pc_o.heartbeat(availability={**DEFAULT_AVAILABILITY, "hosting_allowed": False})
    h.post(world.oscar, f"/servers/{world.server_id}/start")
    assert pc_l.accept_next_offer() == 3
    pc_l.report(world.server_id, "running")
    pc_o.heartbeat(availability=DEFAULT_AVAILABILITY)
    keep_alive(world, pc_o, pc_l, 10, players=0)
    assert pc_o.expect("prefetch_request")["server_id"] == world.server_id