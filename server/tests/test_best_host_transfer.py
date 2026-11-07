"""0.8.0 on the control plane: the automatic choice of the host, speed tests, transfer addresses."""

import pytest

from mdh_harness import DEFAULT_AVAILABILITY
from mdh_server.database.db import MIGRATIONS
from mdh_server.election.scoring import Candidate, Requirements, performance_score, rank_candidates
from mdh_shared.protocol import minecraft as mc
from test_api_coordination import start_on_oscar
from test_mods_config import patch

REQ = Requirements(memory_mb=4096, world_size_mb=100, head_version_id=None)


def cand(host_id: str, priority: int = 50, **kw) -> Candidate:
    base = dict(online=True, hosting_allowed=True, can_host=True, enabled_for_server=True, free_ram_mb=16000,
                free_disk_mb=100_000, cpu_cores=8, latency_ms=20.0, relay_ok=True)
    return Candidate(host_id=host_id, priority=priority, **{**base, **kw})


def test_best_mode_prefers_the_fast_pc_over_the_priority() -> None:
    fast = cand("fast", priority=10, cpu_score=1.4, upload_kbps=40_000, latency_ms=15.0)
    slow = cand("slow", priority=100, cpu_score=0.5, upload_kbps=2_000, latency_ms=90.0)
    assert rank_candidates([slow, fast], REQ).winner == "slow"              # priority (default)
    assert rank_candidates([slow, fast], REQ, "best").winner == "fast"
    assert performance_score(fast, REQ) > performance_score(slow, REQ) + 0.3
    # Unknown measures count as average, never as zero.
    blank = cand("blank")
    assert 0.3 < performance_score(blank, REQ) < performance_score(fast, REQ)
    # The current host's memory already goes to Minecraft: counted back for it.
    busy = cand("busy", free_ram_mb=5000)
    assert performance_score(busy, REQ, extra_ram_mb=4096) > performance_score(busy, REQ)


def test_login_queries_and_transfer_handshake() -> None:
    q = mc.login_plugin_request(7)
    length, pos = mc._read_varint(q, 0)
    packet_id, pos = mc._read_varint(q, pos)
    assert packet_id == mc.LOGIN_PLUGIN_REQUEST and mc._read_varint(q, pos)[0] == 7
    assert mc.parse_plugin_response(mc.LOGIN_PLUGIN_RESPONSE, mc.varint(7) + b"\x00") == 7
    with pytest.raises(mc.ProtocolError):
        mc.parse_plugin_response(0x00, b"")
    hs = mc.Handshake(protocol=774, address="tabc.mc.test\0FML3\0", port=25565, next_state=mc.TRANSFER)
    raw = mc.login_handshake(hs)
    length, pos = mc._read_varint(raw, 0)
    pid, pos = mc._read_varint(raw, pos)
    again = mc.parse_handshake(pid, raw[pos:])
    assert again == mc.Handshake(774, "tabc.mc.test\0FML3\0", 25565, mc.LOGIN)


def test_host_choice_in_the_configuration(world) -> None:
    assert world.h.status(world.oscar, world.server_id)["host_choice"] == "priority"
    assert patch(world, host_choice="best").json()["host_choice"] == "best"
    assert patch(world, host_choice="random").status_code == 422
    assert patch(world, world.lea, host_choice="priority").status_code == 403
    assert max(MIGRATIONS) >= 12


def test_measures_reach_the_election(world) -> None:
    h = world.h
    patch(world, host_choice="best")
    pc_o = h.connect_host(world.oscar, world.pc_oscar, availability={**DEFAULT_AVAILABILITY, "rtt_ms": 120.0,
                                                                     "upload_kbps": 1500, "cpu_score": 0.4})
    pc_l = h.connect_host(world.lea, world.pc_lea, availability={**DEFAULT_AVAILABILITY, "rtt_ms": 12.0,
                                                                 "upload_kbps": 50_000, "cpu_score": 1.3})
    hosts = {x["host_id"]: x for x in h.get(world.oscar, f"/servers/{world.server_id}/hosts").json()}
    assert hosts[world.pc_lea]["upload_kbps"] == 50_000 and hosts[world.pc_lea]["score"] > hosts[world.pc_oscar]["score"]
    # Oscar's PC has the priority (100 vs 70), but Léa's runs the server better.
    assert h.post(world.oscar, f"/servers/{world.server_id}/start").status_code == 200
    pc_l.expect("hosting_offer")
    pc_o.expect_none("hosting_offer")


def test_speed_tests(world) -> None:
    h = world.h
    url = f"/api/v1/hosts/{world.pc_oscar}/speedtest"
    r = h.client.post(url, headers=world.oscar.headers, content=b"x" * 100_000)
    assert r.status_code == 200 and r.json() == {"bytes": 100_000}
    assert h.client.post(f"/api/v1/hosts/{world.pc_lea}/speedtest", headers=world.oscar.headers,
                         content=b"x").status_code == 404                      # not Oscar's PC
    assert h.client.post(url, headers=world.oscar.headers, content=b"x" * (5 * 1024**2)).status_code == 413
    codes = [h.client.post(url, headers=world.oscar.headers, content=b"x").status_code for _ in range(4)]
    assert codes[-1] == 429                                                    # a few every few minutes


def test_transfer_addresses_only_for_the_lease_holder(world) -> None:
    h = world.h
    _, _, epoch = start_on_oscar(world)
    url = f"/servers/{world.server_id}/transfer-addresses"
    r = h.post(world.oscar, url, {"host_id": world.pc_oscar, "epoch": epoch})
    assert r.status_code == 200 and r.json() == []                              # nobody through the relay
    assert h.post(world.oscar, url, {"host_id": world.pc_oscar, "epoch": epoch + 1}).status_code == 409
    assert h.post(world.lea, url, {"host_id": world.pc_lea, "epoch": epoch}).status_code == 409
    assert h.post(world.lea, url, {"host_id": world.pc_oscar, "epoch": epoch}).status_code == 404
