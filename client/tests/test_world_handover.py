"""The world travels between two real agents through a real control plane, without loss."""

import asyncio
import sys
from pathlib import Path

from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from test_agent_live import PW, LiveBackend, Recorder, fake_prepare, plenty_of_resources, until

import pytest


@pytest.fixture
def backend(tmp_path):
    b = LiveBackend(tmp_path)
    b.start()
    yield b
    if b.server and not b.server.should_exit:
        b.stop()


def pc(tmp_path: Path, backend: LiveBackend, name: str, rec: Recorder, **kw) -> HostAgent:
    layout = Layout(tmp_path / name)
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name=name, max_ram_mb=2048,
                              eula_accepted_at="2026-10-31T10:00:00+00:00")
    return HostAgent(layout, settings, emit=rec, prepare=fake_prepare, store_credentials=False,
                     command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)],
                     detect_resources=plenty_of_resources, ready_timeout_s=20, stop_timeout_s=10, **kw)


async def state(agent: HostAgent, sid: str) -> dict:
    return await agent.api.server(sid)


async def wait_state(agent, sid, wanted, timeout=30):
    async def ok():
        return (await state(agent, sid))["state"] == wanted
    await until(ok, timeout=timeout, what=f"server {wanted}")


def world_file(agent: HostAgent, sid: str, name: str) -> str | None:
    path = agent.layout.server_dir(sid) / "world" / "data" / name
    return path.read_text("utf-8") if path.exists() else None


async def setup_two_pcs(tmp_path, backend):
    rec_o, rec_l = Recorder(), Recorder()
    oscar = pc(tmp_path, backend, "pc_oscar", rec_o)
    await oscar.api.register("oscar", PW, None)
    await oscar.login("oscar", PW)
    group = await oscar.api.create_group("Les copains")
    server = await oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3", memory_mb=1024)
    sid = server["server_id"]
    code = (await oscar.api.create_invite(group["group_id"]))["code"]
    lea = pc(tmp_path, backend, "pc_lea", rec_l, hot_backup_interval_s=2)
    await lea.register("lea", PW, code)
    await oscar.api.set_priority(sid, oscar.settings.host_id, 100)
    await oscar.api.set_priority(sid, lea.settings.host_id, 70)
    await until(lambda: oscar.channel.connected and lea.channel.connected, what="both online")
    return oscar, lea, sid, rec_o, rec_l


def test_world_moves_between_two_pcs(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, rec_l = await setup_two_pcs(tmp_path, backend)

        # Situation A: Oscar's PC hosts, Oscar plays, then stops: final backup to the VPS.
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.send_console("mark chest=diamonds")
        await oscar.api.stop(sid, countdown_s=0)
        await wait_state(oscar, sid, "offline")
        v1 = (await oscar.api.versions(sid))[0]
        assert v1["kind"] == "final" and (await state(oscar, sid))["head_version_id"] == v1["version_id"]
        oscar_region = (oscar.layout.server_dir(sid) / "world/region/r.0.0.mca").read_bytes()

        # Situation B: Oscar's PC is off. Léa starts the server: she gets Oscar's world.
        await oscar.close()
        await lea.api.start(sid)
        await wait_state(lea, sid, "running")
        assert world_file(lea, sid, "chest") == "diamonds"
        assert (lea.layout.server_dir(sid) / "world/region/r.0.0.mca").read_bytes() == oscar_region
        assert (await state(lea, sid))["active_host_id"] == lea.settings.host_id

        # Léa plays; the automatic (hot) backup kicks in while the game runs.
        await lea.send_console("mark chest=emeralds")
        async def hot_backup_done():
            return any(v["kind"] == "hot" for v in await lea.api.versions(sid))
        await until(hot_backup_done, timeout=20, what="hot backup")
        commands = (lea.layout.server_dir(sid) / "commands.log").read_text("utf-8").splitlines()
        i = commands.index("save-off")
        assert commands[i:i + 3] == ["save-off", "save-all flush", "save-on"]

        # Léa closes the app: the server stops cleanly and the final world goes to the VPS.
        await lea.send_console("mark sign=bye")
        await lea.close()
        oscar2 = pc(tmp_path, backend, "pc_oscar", Recorder())
        await oscar2.login("oscar", PW)
        await wait_state(oscar2, sid, "offline")
        kinds = [v["kind"] for v in await oscar2.api.versions(sid)]
        assert kinds[0] == "final" and "hot" in kinds

        # Situation C: Oscar comes back and gets everything Léa did.
        await until(lambda: oscar2.channel.connected, what="oscar online")
        await oscar2.api.start(sid)
        await wait_state(oscar2, sid, "running")
        assert world_file(oscar2, sid, "chest") == "emeralds"
        assert world_file(oscar2, sid, "sign") == "bye"
        await oscar2.close()

    asyncio.run(scenario())


def test_vps_outage_during_play_loses_nothing(tmp_path, backend) -> None:
    async def scenario():
        rec = Recorder()
        oscar = pc(tmp_path, backend, "pc_oscar", rec)
        await oscar.api.register("oscar", PW, None)
        await oscar.login("oscar", PW)
        group = await oscar.api.create_group("g")
        sid = (await oscar.api.create_server(group["group_id"], name="s", mc_version="26.3",
                                             memory_mb=1024))["server_id"]
        await until(lambda: oscar.channel.connected, what="online")
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.send_console("mark house=built")

        backend.stop()                          # the VPS disappears in the middle of the game
        await until(lambda: "fenced" in rec.phases(), timeout=15, what="self-fencing")
        await until(lambda: world_file(oscar, sid, "house") == "built", timeout=20,
                    what="world saved locally on the safety stop")

        backend.start()
        async def resumed():
            s = await oscar.api.server(sid)
            return s["state"] == "running" and s["lease_epoch"] == 2
        await until(resumed, timeout=60, what="resumed")
        versions = await oscar.api.versions(sid)
        assert versions[0]["label"] == "Reprise des données de ce PC"   # uploaded, not discarded
        assert world_file(oscar, sid, "house") == "built"
        await oscar.close()

    asyncio.run(scenario())


def test_divergent_local_copy_is_set_aside_never_overwritten(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, _ = await setup_two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.api.stop(sid, countdown_s=0)
        await wait_state(oscar, sid, "offline")
        await oscar.close()

        await lea.api.start(sid)
        await wait_state(lea, sid, "running")
        await lea.send_console("mark chest=lea")
        await lea.api.stop(sid, countdown_s=0)
        await wait_state(lea, sid, "offline")
        await lea.close()

        # Meanwhile Oscar's copy changed without ever reaching the VPS.
        local = oscar.layout.server_dir(sid) / "world" / "data"
        local.mkdir(parents=True, exist_ok=True)
        (local / "chest").write_text("oscar-offline", "utf-8")

        rec = Recorder()
        oscar2 = pc(tmp_path, backend, "pc_oscar", rec)
        await oscar2.login("oscar", PW)
        await until(lambda: oscar2.channel.connected, what="online")
        await oscar2.api.start(sid)
        await wait_state(oscar2, sid, "running")
        assert world_file(oscar2, sid, "chest") == "lea"          # the group's world wins...
        archived = list((oscar2.layout.server_dir(sid) / "divergent").glob("*/world/data/chest"))
        assert [p.read_text("utf-8") for p in archived] == ["oscar-offline"]   # ...nothing is lost
        assert any(n == "error" and "conservée" in p["message"] for n, p in rec.events)
        await oscar2.close()

    asyncio.run(scenario())
