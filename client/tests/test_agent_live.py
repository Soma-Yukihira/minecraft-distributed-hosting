"""The real host agent against a real control plane (uvicorn in a thread) and a fake Minecraft.

Lease timings are shortened (heartbeat 2 s, TTL 6 s, grace 10 s) so outages play out in seconds.
"""

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from mdh_client.minecraft.installer import PreparedServer
from mdh_client.minecraft.sources import Artifact, ServerDistribution
from mdh_client.monitoring.resources import SystemResources
from mdh_server.app import create_app
from mdh_server.config import Settings
from mdh_server.coordination.lease import LeasePolicy
from mdh_shared.models.enums import Loader

FAKE = Path(__file__).with_name("fake_minecraft.py")
PW = "correct horse battery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveBackend:
    def __init__(self, tmp: Path) -> None:
        self.db = tmp / "vps" / "mdh.sqlite3"
        #: Where install.sh publish-client puts the installer and release.json.
        self.updates = tmp / "vps" / "updates"
        self.port = free_port()
        self.relay_port = free_port()   # players connect here (domain "mc.test", no DNS needed)
        self.url = f"http://127.0.0.1:{self.port}"
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        settings = Settings(database_path=str(self.db), storage_dir=str(self.db.parent / "storage"),
                            updates_dir=str(self.updates), registration_mode="open", heartbeat_interval_s=2,
                            lease=LeasePolicy(ttl_s=6, grace_s=10), sweep_interval_s=0.2,
                            preemption_stable_s=1.0, relay_bind="127.0.0.1", relay_port=self.relay_port,
                            relay_domain="mc.test")
        app = create_app(settings, password_hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.server.started

    def stop(self) -> None:
        assert self.server and self.thread
        self.server.should_exit = True
        self.thread.join(10)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, /, **payload) -> None:
        self.events.append((name, payload))

    def phases(self) -> list[str]:
        return [p["phase"] for n, p in self.events if n == "hosting"]

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


async def until(predicate, timeout: float = 20, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def plenty_of_resources(path: Path) -> SystemResources:
    """Deterministic machine: tests must not depend on what else runs on the PC."""
    return SystemResources(total_ram_mb=32768, available_ram_mb=24000, logical_cores=16,
                           physical_cores=8, disk_free_mb=500_000, os_name="Windows 11")


async def fake_prepare(loader, mc_version, status) -> PreparedServer:
    status("Préparation (test)", None)
    return PreparedServer(java=Path(sys.executable), jar=FAKE, distribution=ServerDistribution(
        Loader.PAPER, mc_version, 25, Artifact(url="https://fill-data.papermc.io/x.jar", filename="x.jar",
                                               size=None, sha256="0" * 64), "1"))


@pytest.fixture
def backend(tmp_path: Path):
    b = LiveBackend(tmp_path)
    b.start()
    yield b
    if b.server and not b.server.should_exit:
        b.stop()


def make_agent(tmp_path: Path, backend: LiveBackend, recorder: Recorder, *, eula: bool = True,
               ask_offer=None, **settings_overrides) -> HostAgent:
    layout = Layout(tmp_path / "client")
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name="PC test", server_port=25599,
                              max_ram_mb=2048, **settings_overrides)
    if eula:
        settings.eula_accepted_at = "2026-10-31T10:00:00+00:00"
    return HostAgent(layout, settings, emit=recorder, ask_offer=ask_offer, prepare=fake_prepare,
                     command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)],
                     store_credentials=False, ready_timeout_s=20, stop_timeout_s=10,
                     detect_resources=plenty_of_resources)


async def setup_group(agent: HostAgent, backend: LiveBackend) -> str:
    async with httpx.AsyncClient(base_url=backend.url + "/api/v1") as c:
        await c.post("/auth/register", json={"username": "oscar", "password": PW})
    await agent.login("oscar", PW)
    group = await agent.api.create_group("Les copains")
    server = await agent.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                           loader="paper", memory_mb=1024)
    await agent.api.set_priority(server["server_id"], agent.settings.host_id, 100)
    await until(lambda: agent.channel is not None and agent.channel.connected, what="websocket")
    return server["server_id"]


def server_dir(agent: HostAgent, sid: str) -> Path:
    return agent.layout.server_dir(sid)


def test_host_runs_and_stops_cleanly(tmp_path, backend) -> None:
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        assert rec.phases()[:3] == ["preparing", "starting", "running"]
        props = (server_dir(agent, sid) / "server.properties").read_text("latin-1")
        assert "online-mode=true" in props and "server-port=25599" in props

        await agent.send_console("list")
        await until(lambda: any("There are 1 of a max" in p.get("line", "") for n, p in rec.events
                                if n == "log"), what="list output")
        await agent.send_console("chat")
        await until(lambda: any("<Léa> Ça marche, à tout à l'heure !" in p.get("line", "")
                                for n, p in rec.events if n == "log"), what="accented log line")
        status = await agent.api.server(sid)
        assert status["active_host_id"] == agent.settings.host_id and status["lease_epoch"] == 1

        await agent.api.stop(sid, countdown_s=5)
        await until(lambda: _is(agent, sid, "offline"), what="offline")
        assert agent.hosted is None
        commands = (server_dir(agent, sid) / "commands.log").read_text("utf-8").splitlines()
        assert "say Le serveur s'arrête dans 5 s (arrêt demandé)." in commands
        assert commands[-1] == "stop"
        events = [e["type"] for e in await agent.api.events(sid)]
        assert "lease_released" in events and "host_running" in events
        await agent.close()

    asyncio.run(scenario())


async def _is(agent: HostAgent, sid: str, wanted: str) -> bool:
    return (await agent.api.server(sid))["state"] == wanted


def test_offline_accounts_with_group_whitelist(tmp_path, backend) -> None:
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        await agent.api.update_server(sid, allow_offline_accounts=True, allowed_players=["oscar", "Lea_2"])
        directory = server_dir(agent, sid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".mdh-auth-mode").write_text("offline", "utf-8")
        (directory / "whitelist.json").write_text('[{"uuid": "x", "name": "eve"}]', "utf-8")

        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        props = (directory / "server.properties").read_text("latin-1")
        assert "online-mode=false" in props and "enforce-whitelist=true" in props
        import json

        from mdh_client.minecraft.server_files import offline_uuid
        whitelist = json.loads((directory / "whitelist.json").read_text("utf-8"))
        assert {(e["name"], e["uuid"]) for e in whitelist} == {
            ("oscar", offline_uuid("oscar")), ("Lea_2", offline_uuid("Lea_2"))}   # "eve" gone
        commands = (directory / "commands.log").read_text("utf-8")
        assert "whitelist add" not in commands  # would store official UUIDs (see server_files)

        # A typo fixed while the game runs: the relay lets the player in at once, so Minecraft must
        # follow at once too, without a restart (it used to refuse them until the next start).
        await agent.api.update_server(sid, allowed_players=["oscar", "AARON_PickOus"])

        def names() -> set[str]:
            return {e["name"] for e in json.loads((directory / "whitelist.json").read_text("utf-8"))}

        await until(lambda: names() == {"oscar", "AARON_PickOus"}, what="live whitelist")
        await until(lambda: "whitelist reload" in (directory / "commands.log").read_text("utf-8"),
                    what="whitelist reload")
        assert agent.hosted is not None and agent.hosted.phase == "running"  # no restart
        assert "whitelist add" not in (directory / "commands.log").read_text("utf-8")

        await agent.api.stop(sid, countdown_s=0)
        await until(lambda: _is(agent, sid, "offline"), what="offline")
        await agent.close()

    asyncio.run(scenario())


def test_verified_accounts_whitelist_follows_the_group_list_live(tmp_path, backend) -> None:
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        await agent.api.update_server(sid, properties={"white-list": True}, allowed_players=["oscar"])
        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        log = server_dir(agent, sid) / "commands.log"
        await until(lambda: "whitelist add oscar" in log.read_text("utf-8"), what="initial whitelist")

        await agent.api.update_server(sid, allowed_players=["Lea_2"])
        await until(lambda: "whitelist remove oscar" in log.read_text("utf-8"), what="live update")
        assert "whitelist add Lea_2" in log.read_text("utf-8")

        # Turning the whitelist off waits for the restart: nothing is sent to Minecraft now.
        before = log.read_text("utf-8")
        await agent.api.update_server(sid, properties={"white-list": False}, allowed_players=[])
        await asyncio.sleep(1.5)
        assert log.read_text("utf-8") == before and agent.hosted.phase == "running"

        await agent.api.stop(sid, countdown_s=0)
        await until(lambda: _is(agent, sid, "offline"), what="offline")
        await agent.close()

    asyncio.run(scenario())


def test_vps_outage_self_fences_then_resumes(tmp_path, backend) -> None:
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        proc = agent.hosted.process

        backend.stop()                       # the VPS disappears
        t0 = time.monotonic()
        await until(lambda: "fenced" in rec.phases(), timeout=15, what="self-fencing")
        fenced_after = time.monotonic() - t0
        await until(lambda: not proc.alive, timeout=20, what="minecraft stopped")
        # TTL 6 s - margin 2 s, counted from the last heartbeat sent (every 2 s).
        assert fenced_after < 6.5, fenced_after
        assert (server_dir(agent, sid) / "commands.log").read_text().splitlines()[-1] == "stop"

        backend.start()                      # the VPS comes back with its database
        await until(lambda: agent.channel.connected, timeout=40, what="reconnection")
        # If the VPS is back before the old lease expires, it shows "running" (epoch 1) for up to
        # TTL seconds: wait for the real resumption under a new epoch.
        async def resumed() -> bool:
            s = await agent.api.server(sid)
            return s["state"] == "running" and s["lease_epoch"] == 2
        await until(resumed, timeout=40, what="resumed under epoch 2")
        status = await agent.api.server(sid)
        assert status["active_host_id"] == agent.settings.host_id
        events = [e["type"] for e in await agent.api.events(sid, limit=100)]
        assert "resume_offered" in events and "lease_expired" in events
        await agent.close()

    asyncio.run(scenario())


def test_crash_loop_gives_up_and_releases(tmp_path, backend, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MC_MODE", "crash")
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec, max_restarts=1)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: any(n == "error" and "abandonné" in p["message"] for n, p in rec.events),
                    timeout=30, what="give up")
        await until(lambda: _is(agent, sid, "offline"), what="released")
        assert rec.phases().count("running") == 2  # first start + one automatic restart
        events = [e["type"] for e in await agent.api.events(sid, limit=100)]
        assert events.count("host_crashed") >= 2 and "lease_released" in events
        await agent.close()

    asyncio.run(scenario())


def test_missing_eula_is_never_accepted_silently(tmp_path, backend) -> None:
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec, eula=False)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: "eula_required" in rec.names(), what="eula prompt")
        await until(lambda: _is(agent, sid, "offline"), what="released")
        assert not (server_dir(agent, sid) / "eula.txt").exists()
        await agent.close()

    asyncio.run(scenario())


def test_user_can_decline_an_offer(tmp_path, backend) -> None:
    rec = Recorder()
    asked: list[dict] = []

    async def ask(info: dict) -> bool:
        asked.append(info)
        return False

    async def scenario():
        agent = make_agent(tmp_path, backend, rec, ask_offer=ask, auto_accept_offers=False)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: asked, what="question to the user")
        assert asked[0]["name"] == "Survie" and asked[0]["memory_mb"] == 1024
        await until(lambda: _has_event(agent, sid, "offer_declined"), what="decline recorded")
        assert agent.hosted is None
        await agent.close()

    asyncio.run(scenario())


async def _has_event(agent: HostAgent, sid: str, kind: str) -> bool:
    return kind in [e["type"] for e in await agent.api.events(sid)]
