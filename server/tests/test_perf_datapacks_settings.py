"""0.7.0 on the control plane: performance graphs, datapacks, settings files, spark."""

import pytest

from mdh_harness import Closed
from mdh_server import perf
from mdh_server.database.db import MIGRATIONS
from mdh_shared.protocol.messages import HostMetrics
from test_api_coordination import start_on_oscar
from test_api_migration import METRICS
from test_mods_config import patch


def test_performance_minute_by_minute(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    t0 = h.clock.t
    for i, tps in enumerate((20.0, 20.0, 12.0, 19.0, 20.0, 20.0)):   # one heartbeat every 10 s: one minute
        h.advance(10)
        pc_o.heartbeat(metrics={**METRICS, "cpu_percent": 30.0 + i, "ram_used_mb": 4000 + 100 * i,
                                "players_online": 2, "tps": tps, "mspt": 1000 / tps})
    r = h.get(world.lea, f"/servers/{world.server_id}/performance?hours=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hours"] == 1 and body["step_s"] == 60
    points = body["points"]
    assert 1 <= len(points) <= 2
    tps_min = min(p["tps_min"] for p in points)
    assert tps_min == 12.0 and max(p["mspt_max"] for p in points) == pytest.approx(1000 / 12, rel=1e-3)
    assert max(p["ram_used_mb"] for p in points) == 4500 and all(p["players"] == 2 for p in points)
    # Flushed twice (here, then by the sweep): still the same minutes, nothing counted twice.
    h.tick()
    again = h.get(world.lea, f"/servers/{world.server_id}/performance?hours=1").json()["points"]
    assert sum(p["players"] for p in again) == sum(p["players"] for p in points)
    assert h.get(world.lea, f"/servers/{world.server_id}/performance?hours=169").status_code == 422
    assert t0 < h.clock.t


def test_points_are_spread_over_long_periods(h) -> None:
    rec = perf.PerfRecorder()
    conn = h.ctx.conn
    oscar = h.register("oscar")
    sid = h.create_server(oscar, h.create_group(oscar))
    now = 1_800_000_000.0
    for minute in range(0, 7 * 24 * 60, 30):   # a sample every 30 min over a week
        rec.observe(sid, HostMetrics(cpu_percent=10, ram_used_mb=1000, ram_free_mb=1, disk_free_mb=1, tps=20.0),
                    now - minute * 60)
    rec.flush(conn, now)
    step, points = perf.query(conn, sid, now, 168)
    assert step == 1680 and len(points) <= perf.MAX_POINTS and points[0].tps == 20.0
    rec.flush(conn, now + 8 * 24 * 3600)     # a week later: old minutes are gone
    assert perf.query(conn, sid, now + 8 * 24 * 3600, 168)[1] == []


PACK = {"project_id": "8oi3bsk5", "version_id": "TerrAAAA", "slug": "terralith", "name": "Terralith",
        "version_number": "2.6.5+26.3", "url": "https://cdn.modrinth.com/data/8oi3bsk5/versions/TerrAAAA/Terralith.zip",
        "sha512": "d" * 128, "size": 2_000_000, "mc_version": "1.21.4"}


def test_datapacks_in_the_configuration(world) -> None:
    mc = world.h.status(world.oscar, world.server_id)["mc_version"]
    r = patch(world, datapacks=[{**PACK, "mc_version": mc}])
    assert r.status_code == 200, r.text
    assert r.json()["datapacks"][0]["slug"] == "terralith"
    assert world.h.status(world.lea, world.server_id)["datapacks"] == r.json()["datapacks"]
    assert patch(world, world.lea, datapacks=[]).status_code == 403
    other = "1.21.5" if mc != "1.21.5" else "1.21.6"
    r = patch(world, mc_version=other)      # the datapack was chosen for the old version
    assert r.status_code == 422 and "prévu pour Minecraft" in r.json()["error"]["details"]["problems"][0]
    assert patch(world, mc_version=other, datapacks=[{**PACK, "mc_version": other}]).status_code == 200


@pytest.mark.parametrize("bad", [{"url": "https://evil.example/Terralith.zip"}, {"url": PACK["url"][:-4] + ".jar"},
                                 {"size": 65 * 1024**2}, {"slug": "a b"}])
def test_datapack_entries_are_validated(world, bad) -> None:
    assert patch(world, datapacks=[{**PACK, **bad}]).status_code == 422


def test_settings_files(world) -> None:
    h = world.h
    files = {"config/lithium.properties": "mixin.ai=false\n", "plugins/LuckPerms/config.yml": "server: survie\n",
             "bukkit.yml": "settings:\n  allow-end: true\n"}
    r = patch(world, config_files=files)
    assert r.status_code == 200, r.text
    assert r.json()["config_file_paths"] == sorted(files)
    assert h.get(world.lea, f"/servers/{world.server_id}/config-files").json() == files   # the host reads them
    assert patch(world, world.lea, config_files={}).status_code == 403
    assert patch(world, config_files={}).json()["config_file_paths"] == []
    outsider = h.register("eve")
    assert h.get(outsider, f"/servers/{world.server_id}/config-files").status_code == 404


@pytest.mark.parametrize("files", [
    {"server.properties": "online-mode=false"}, {"mods/x.jar": "PK"}, {"config/../../etc.toml": "x"},
    {"kubejs/server_scripts/a.js": "x"}, {"config/a.toml": "a\x00b"}, {"config/big.toml": "x" * (257 * 1024)},
    {f"config/f{i}.toml": "x" for i in range(201)}, {"world/level.dat": "x"}, {"/etc/passwd.txt": "x"},
])
def test_settings_files_are_validated(world, files) -> None:
    assert patch(world, config_files=files).status_code == 422


def test_spark_profile_is_an_admin_action(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    act = {"kind": "profile_start", "seconds": 60}
    assert h.post(world.oscar, f"/servers/{world.server_id}/actions", {"action": act}).status_code == 202
    assert pc_o.expect("action")["action"] == act
    assert h.post(world.oscar, f"/servers/{world.server_id}/actions",
                  {"action": {"kind": "profile_start", "seconds": 5}}).status_code == 422
    assert h.post(world.lea, f"/servers/{world.server_id}/actions", {"action": act}).status_code == 403
    pc_o.heartbeat(metrics={**METRICS, "profile": {"state": "done", "url": "https://spark.lucko.me/AbCdEf1234"}})
    assert h.status(world.lea, world.server_id)["profile"]["url"] == "https://spark.lucko.me/AbCdEf1234"
    # Any other link breaks the protocol: the VPS closes that PC's session (the application checks first).
    with pytest.raises(Closed):
        pc_o.heartbeat(metrics={**METRICS, "profile": {"state": "done", "url": "https://evil.example/x"}})


def test_schema_version() -> None:
    assert max(MIGRATIONS) >= 11
