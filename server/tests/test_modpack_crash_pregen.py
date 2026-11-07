"""0.6.0 on the control plane: the modpack in the configuration, the explained crash, and the
pre-generation that keeps an empty server running."""

import pytest

from test_api_coordination import start_on_oscar
from test_api_migration import METRICS
from test_mods_config import patch

PACK = {"project_id": "1KVo5zza", "version_id": "PackDDDD", "name": "Cobblemon Pack", "version_number": "1.6.1",
        "url": "https://cdn.modrinth.com/data/1KVo5zza/versions/PackDDDD/Cobblemon%20Pack%201.6.1.mrpack",
        "sha512": "c" * 128, "size": 5_000_000}


def test_modpack_in_the_configuration(world) -> None:
    r = patch(world, modpack=PACK)
    assert r.status_code == 200, r.text
    assert r.json()["modpack"] == PACK
    assert world.h.status(world.lea, world.server_id)["modpack"] == PACK
    assert patch(world, world.lea, modpack=None).status_code == 403
    assert patch(world, modpack=None).json()["modpack"] is None


@pytest.mark.parametrize("bad", [
    {"url": "https://evil.example/data/1KVo5zza/versions/PackDDDD/pack.mrpack"},
    {"url": "https://cdn.modrinth.com/data/1KVo5zza/versions/PackDDDD/pack.jar"},
    {"url": "https://cdn.modrinth.com/data/OTHERprj/versions/PackDDDD/pack.mrpack"},
    {"size": 300 * 1024**2}, {"sha512": "C" * 128}, {"name": "a\nb"},
])
def test_modpack_entries_are_validated(world, bad) -> None:
    assert patch(world, modpack={**PACK, **bad}).status_code == 422


CRASH = {"cause": "mod", "message": "Le plantage vient apparemment de Lithium 0.15.0 : mets-le à jour ou retire-le.",
         "mod_project_id": "gvQqBUqZ", "mod_name": "Lithium"}


def test_the_explained_crash_is_shown_until_the_configuration_changes(world) -> None:
    h = world.h
    pc_o, _, epoch = start_on_oscar(world)
    r = h.client.post(f"/api/v1/servers/{world.server_id}/host-state", headers=world.oscar.headers,
                      json={"host_id": world.pc_oscar, "epoch": epoch, "state": "crashed",
                            "detail": "code 1: Exception in server tick loop", "crash": CRASH})
    assert r.status_code == 204, r.text
    crash = h.status(world.lea, world.server_id)["last_crash"]
    assert {k: crash[k] for k in CRASH} == CRASH and crash["at"]
    event = next(e for e in h.get(world.oscar, f"/servers/{world.server_id}/events").json()
                 if e["type"] == "host_crashed")
    assert event["payload"]["explanation"] == CRASH["message"] and event["payload"]["mod_name"] == "Lithium"
    assert patch(world, memory_mb=4096).status_code == 200   # the admin did something about it
    assert h.status(world.lea, world.server_id)["last_crash"] is None


@pytest.mark.parametrize("bad", [{"cause": "boom"}, {"message": "a\nb"}, {"mod_project_id": "../x"},
                                 {"message": "x" * 301}])
def test_crash_reports_are_validated(world, bad) -> None:
    h = world.h
    _, _, epoch = start_on_oscar(world)
    r = h.client.post(f"/api/v1/servers/{world.server_id}/host-state", headers=world.oscar.headers,
                      json={"host_id": world.pc_oscar, "epoch": epoch, "state": "crashed", "crash": {**CRASH, **bad}})
    assert r.status_code == 422


def action(world, user, **act):
    return world.h.post(user, f"/servers/{world.server_id}/actions", {"action": act})


def test_pregeneration_is_an_admin_action_with_bounds(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    assert action(world, world.oscar, kind="pregen_start", dimension="the_nether", radius=2500).status_code == 202
    msg = pc_o.expect("action")
    assert msg["action"] == {"kind": "pregen_start", "dimension": "the_nether", "radius": 2500}
    assert action(world, world.oscar, kind="pregen_cancel").status_code == 202
    assert pc_o.expect("action")["action"] == {"kind": "pregen_cancel"}
    for bad in ({"radius": 50}, {"radius": 50_000}, {"dimension": "aether", "radius": 1000},
                {"radius": 1000, "command": "op Eve"}):
        assert action(world, world.oscar, kind="pregen_start", **bad).status_code == 422, bad
    h.client.patch(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers,
                   json={"role": "moderator"})
    assert action(world, world.lea, kind="pregen_start", radius=1000).status_code == 403
    assert action(world, world.lea, kind="pregen_cancel").status_code == 403


def test_an_empty_server_keeps_running_while_the_world_is_pre_generated(world) -> None:
    h = world.h
    assert patch(world, stop_when_empty_after_s=300).status_code == 200
    pc_o, _, _ = start_on_oscar(world)
    running = {"dimension": "overworld", "radius": 5000, "state": "running", "percent": 12.5, "eta_s": 3600}

    def beat(pregen, seconds):
        for _ in range(seconds // 10):
            h.advance(10)
            pc_o.heartbeat(metrics={**METRICS, "players_online": 0, "pregen": pregen})
            h.tick()

    beat(running, 400)
    s = h.status(world.oscar, world.server_id)
    assert s["state"] == "running" and s["pregen"] == running and s["stops_when_empty_at"] is None
    pc_o.expect_none("stop_request")
    beat({**running, "state": "done", "percent": 100.0, "eta_s": 0}, 310)
    pc_o.expect("stop_request")                              # done: empty again, it stops
    assert "stop_requested" in h.event_types(world.oscar, world.server_id)
