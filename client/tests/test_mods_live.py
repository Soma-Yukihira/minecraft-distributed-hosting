"""0.4.0 end to end: mods from Modrinth on whichever PC hosts, allowed PC by PC; access requests
through the relay; the world map through the status page; statistics.

Real control plane and relay, real agents, fake Minecraft, fake Modrinth (API and CDN), and a
fake BlueMap web server on the hosting PC.
"""

import asyncio
import hashlib
import sys
from pathlib import Path

import httpx

from fake_modrinth import FakeModrinth
from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from mdh_client.minecraft.mods import file_name
from mdh_shared.models.api import Mod
from test_agent_live import PW, LiveBackend, Recorder, fake_prepare, free_port, plenty_of_resources, until
from test_relay_live import Player, join, wait_state
from test_world_handover import backend, state  # noqa: F401

FAPI, LITH, BLUE = "P7dR8mSH", "gvQqBUqZ", "swbUV1cr"


def modrinth() -> FakeModrinth:
    f = FakeModrinth()
    f.add(FAPI, "fabric-api", version_id="FapiAAAA")
    f.add(LITH, "lithium", deps=[FAPI], version_id="LithBBBB")
    f.add(BLUE, "bluemap", deps=[FAPI], version_id="BlueCCCC")
    return f


def pc(tmp_path: Path, backend: LiveBackend, name: str, cdn: FakeModrinth) -> HostAgent:
    layout = Layout(tmp_path / name)
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name=name, max_ram_mb=2048, server_port=free_port(),
                              map_port=free_port(), eula_accepted_at="2026-10-31T10:00:00+00:00")
    return HostAgent(layout, settings, emit=Recorder(), prepare=fake_prepare, store_credentials=False,
                     command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)],
                     detect_resources=plenty_of_resources, ready_timeout_s=20, stop_timeout_s=10,
                     downloads_transport=cdn.transport())


async def two_pcs(tmp_path, backend, cdn, **server):
    oscar, lea = pc(tmp_path, backend, "pc_oscar", cdn), pc(tmp_path, backend, "pc_lea", cdn)
    await oscar.api.register("oscar", PW, None)
    await oscar.login("oscar", PW)
    group = await oscar.api.create_group("Les copains")
    sid = (await oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3", loader="fabric",
                                         memory_mb=1024, **server))["server_id"]
    code = (await oscar.api.create_invite(group["group_id"]))["code"]
    await lea.register("lea", PW, code)
    await oscar.api.set_priority(sid, oscar.settings.host_id, 100)
    await oscar.api.set_priority(sid, lea.settings.host_id, 70)
    await until(lambda: oscar.channel.connected and lea.channel.connected, what="both online")
    return oscar, lea, sid, group["group_id"]


def jars(agent: HostAgent, sid: str) -> dict[str, str]:
    folder = agent.layout.server_dir(sid) / "mods"
    return {p.name: hashlib.sha512(p.read_bytes()).hexdigest() for p in folder.glob("*.jar")}


async def stop(agent, sid) -> None:
    await agent.api.stop(sid, 0)
    await wait_state(agent, sid, "offline")


def test_mods_are_installed_verified_and_allowed_pc_by_pc(tmp_path, backend) -> None:
    async def scenario():
        cdn = modrinth()
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, cdn)
        server = await oscar.api.server(sid)
        mods = await oscar.resolve_mod(server, LITH, [])
        assert [m.slug for m in mods] == ["lithium", "fabric-api"]
        await oscar.save_mods(sid, mods)
        assert oscar.unapproved_mods(await oscar.api.server(sid)) == []       # chosen here: allowed here
        assert len(lea.unapproved_mods(await lea.api.server(sid))) == 2       # not on Léa's PC
        stray = oscar.layout.server_dir(sid) / "mods" / "found-on-a-forum.jar"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"not declared")

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert jars(oscar, sid) == {file_name(m): m.sha512 for m in mods}
        assert not stray.exists()
        assert list((oscar.layout.server_dir(sid) / ".mdh" / "removed-jars").rglob("found-on-a-forum.jar"))
        assert any(n == "error" and "mis de côté" in p["message"] for n, p in oscar._listener.events)
        await stop(oscar, sid)

        # Oscar's PC stops hosting: Léa's would be next, but she has not allowed the mods.
        await oscar.update_settings(hosting_allowed=False)
        await oscar.api.start(sid)

        async def declined():
            return "offer_declined" in [e["type"] for e in await lea.api.events(sid, limit=20)]
        await until(declined, timeout=30, what="Léa's PC declines")
        assert ("mods_approval" in lea._listener.names() and lea.hosted is None
                and not (lea.layout.server_dir(sid) / "mods").exists())
        event = next(e for e in await lea.api.events(sid, limit=20) if e["type"] == "offer_declined")
        assert event["payload"]["reason"] == "mods à autoriser sur ce PC"
        await oscar.api.stop(sid, 0)

        lea.approve_mods(lea.unapproved_mods(await lea.api.server(sid)))
        assert lea.settings.approved_mods == [m.sha512 for m in mods]
        await lea.api.start(sid)
        await wait_state(lea, sid, "running")
        assert jars(lea, sid) == {file_name(m): m.sha512 for m in mods}
        # Léa's PC downloaded them itself from Modrinth's CDN (the VPS never holds a JAR).
        assert sum(1 for u in cdn.requests if "cdn.modrinth.com" in u) >= 4
        await stop(lea, sid)

        # A file changed on the CDN after it was chosen: refused, the server does not start with it.
        cdn.files[mods[0].url] = b"Y" * mods[0].size
        for m in mods:
            for cached in lea.layout.mod_cache.glob("*.jar"):
                cached.unlink()
        for p in (lea.layout.server_dir(sid) / "mods").glob("*.jar"):
            p.unlink()
        await lea.api.start(sid)

        async def gave_up():
            return any(n == "error" or (n == "hosting" and p["phase"] == "released")
                       for n, p in lea._listener.events[-30:]) and lea.hosted is None
        await until(gave_up, timeout=40, what="Léa gives up")
        assert file_name(mods[0]) not in jars(lea, sid)
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_access_requests_through_the_relay(tmp_path, backend) -> None:
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid, gid = await two_pcs(tmp_path, backend, modrinth(), allowed_players=["Oscar"],
                                             properties={"white-list": True})
        await lea.close()
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")

        refusal = await join(port, "mc.test", "Steve")
        assert isinstance(refusal, str) and "demande est envoyée" in refusal
        assert "demande est envoyée" in await join(port, "mc.test", "Steve")      # asked again: same answer
        s = await state(oscar, sid)
        assert s["pending_access_requests"] == 1
        requests = await oscar.api.access_requests(sid)
        assert [(r["player"], r["attempts"]) for r in requests] == [("Steve", 2)]

        s = await oscar.api.answer_access_request(sid, "Steve", True)
        assert s["allowed_players"] == ["Oscar", "Steve"]
        # The host applies the new list without restarting: whitelist add.
        cmds = oscar.layout.server_dir(sid) / "commands.log"
        await until(lambda: cmds.exists() and "whitelist add Steve" in cmds.read_text("utf-8"), timeout=20,
                    what="whitelist add Steve")
        steve = await join(port, "mc.test", "Steve")
        assert isinstance(steve, Player)
        steve.close()

        assert "demande est envoyée" in await join(port, "mc.test", "Eve")
        await oscar.api.answer_access_request(sid, "Eve", False)
        assert "refusé" in await join(port, "mc.test", "Eve")
        assert "Oscar" in [p for p in (await state(oscar, sid))["allowed_players"]]
        assert "Eve" not in (await state(oscar, sid))["allowed_players"]
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


async def fake_bluemap(port: int, seen: list[bytes]) -> asyncio.base_events.Server:
    """Answers like BlueMap's web server: a page and files, keep-alive."""
    async def handle(reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                seen.append(head)
                path = head.split(b" ")[1].decode()
                body = f"BLUEMAP {path}".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)


def test_world_map_through_the_status_page(tmp_path, backend) -> None:
    async def scenario():
        port = backend.relay_port
        cdn = modrinth()
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, cdn)
        await lea.close()
        server = await oscar.api.server(sid)
        s = await oscar.save_mods(sid, await oscar.resolve_mod(server, "bluemap", []), map_enabled=True)
        assert s["public_status"] and s["map_url"] == f"http://mc.test:{port}/map/"
        seen: list[bytes] = []
        web = await fake_bluemap(oscar.settings.map_port, seen)
        base = f"http://127.0.0.1:{port}"
        headers = {"Host": f"mc.test:{port}"}
        async with httpx.AsyncClient(base_url=base, headers=headers) as http:
            r = await http.get("/map/")
            assert r.status_code == 503 and "en ligne" in r.text           # server stopped

            await oscar.api.start(sid)
            await wait_state(oscar, sid, "running")
            conf = oscar.layout.server_dir(sid) / "config" / "bluemap"
            assert 'ip: "127.0.0.1"' in (conf / "webserver.conf").read_text("utf-8")
            assert f"port: {oscar.settings.map_port}" in (conf / "webserver.conf").read_text("utf-8")
            assert "accept-download: true" in (conf / "core.conf").read_text("utf-8")

            page = await http.get("/")
            assert "Voir la carte du monde" in page.text
            assert (await http.get("/map", follow_redirects=False)).headers["location"] == "/map/"
            r1 = await http.get("/map/", headers={"Cookie": "secret=1", "Accept-Encoding": "gzip"})
            r2 = await http.get("/map/maps/world/tiles/0/x1/z2.json?v=3")      # same connection
            assert (r1.status_code, r1.text) == (200, "BLUEMAP /")
            assert (r2.status_code, r2.text) == (200, "BLUEMAP /maps/world/tiles/0/x1/z2.json?v=3")
            assert b"Cookie" not in seen[0] and b"Accept-Encoding: gzip" in seen[0]
            assert seen[0].startswith(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            assert len(seen) == 2

        # Anything but plain GET/HEAD of the map is never passed on.
        for raw in (b"GET /map/../../etc/passwd HTTP/1.1\r\nHost: mc.test\r\n\r\n",
                    b"GET /map/%2e%2e/secret HTTP/1.1\r\nHost: mc.test\r\n\r\n",
                    b"POST /map/x HTTP/1.1\r\nHost: mc.test\r\nContent-Length: 0\r\n\r\n"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(raw)
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), 15)
            writer.close()
            assert b"BLUEMAP" not in answer
        assert len(seen) == 2
        # The map does not count as a player.
        assert (await state(oscar, sid))["relay_connections"] == 0

        await oscar.save_mods(sid, [], map_enabled=False)
        async with httpx.AsyncClient(base_url=base, headers=headers) as http:
            assert (await http.get("/map/")).status_code == 404
        web.close()
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


def test_statistics_from_a_real_session(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, modrinth())
        await lea.close()
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        alex = await join(backend.relay_port, "mc.test", "Alex")
        assert isinstance(alex, Player)
        await asyncio.sleep(9)  # a few heartbeats (every 2 s in these tests)
        alex.close()

        st = await oscar.api.stats(sid, days=7, utc_offset_min=60)
        assert st["online_seconds"] >= 4
        assert [h["display_name"] for h in st["hosts"]] == ["pc_oscar"]
        assert "Alex" in {p["player"] for p in st["players"]}
        assert any(h["average_players"] for h in st["hours"])
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


def test_mod_model_roundtrip() -> None:
    entry = modrinth().versions[LITH][0]
    assert Mod.model_validate({"project_id": LITH, "version_id": entry["id"], "slug": "lithium", "name": "Lithium",
                               "version_number": "1", "url": entry["files"][0]["url"],
                               "sha512": entry["files"][0]["hashes"]["sha512"], "size": entry["files"][0]["size"],
                               "loader": "fabric", "mc_version": "26.3"})


def test_map_turned_on_while_running_needs_the_restart_it_gets(tmp_path, backend) -> None:
    """Regression (real test): the map was turned on two minutes after the server started; the
    page said "shows when the server is online" although it was, and Restart did not apply it."""
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, modrinth())
        await lea.close()
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        server = await oscar.api.server(sid)
        await oscar.save_mods(sid, await oscar.resolve_mod(server, "bluemap", []), map_enabled=True)
        await oscar.api.update_server(sid, properties={"motd": "Avec carte"})
        seen: list[bytes] = []
        web = await fake_bluemap(oscar.settings.map_port, seen)
        headers = {"Host": f"mc.test:{port}"}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30) as http:
            r = await http.get("/map/")
            assert r.status_code == 503 and "redémarrer le serveur" in r.text and not seen
            assert not (oscar.layout.server_dir(sid) / "mods").glob("*.jar").__next__.__self__ \
                or not list((oscar.layout.server_dir(sid) / "mods").glob("*.jar"))

            first = oscar.hosted.process
            await oscar.restart_hosted()             # keeps the lease, applies the new configuration
            assert oscar.hosted.process is not first and oscar.hosted.phase == "running"
            assert (await state(oscar, sid))["state"] == "running"
            assert [p.name for p in (oscar.layout.server_dir(sid) / "mods").glob("bluemap-*.jar")]
            props = (oscar.layout.server_dir(sid) / "server.properties").read_text("latin-1")
            assert "motd=Avec carte" in props
            r = await http.get("/map/")
            assert (r.status_code, r.text) == (200, "BLUEMAP /")

            web.close()                              # BlueMap not answering (yet)
            await web.wait_closed()
            r = await http.get("/map/", headers={"Connection": "close"})
            assert r.status_code == 503 and "démarre" in r.text
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


def test_restart_waits_for_mods_this_pc_has_not_allowed(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, modrinth())
        await oscar.update_settings(hosting_allowed=False)
        await lea.api.start(sid)
        await wait_state(lea, sid, "running")
        server = await oscar.api.server(sid)
        await oscar.save_mods(sid, await oscar.resolve_mod(server, LITH, []))   # chosen on Oscar's PC
        first = lea.hosted.process
        await lea.restart_hosted()
        assert lea.hosted.process is first and first.alive                      # nothing stopped
        assert "mods_approval" in lea._listener.names()
        await stop(lea, sid)
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ 0.6.0

def lithium_jar() -> bytes:
    import io
    import json
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "lithium"}))
        z.writestr("net/caffeinemc/mods/lithium/ai/Pathing.class", b"")
    return buf.getvalue()


def test_a_modpack_from_modrinth_with_its_configuration(tmp_path, backend) -> None:
    from test_modpack import PACK, entry, mrpack

    async def scenario():
        cdn = modrinth()
        cdn.add(PACK, "cobblemon-pack", project_type="modpack", version_id="PackDDDD", title="Cobblemon Pack",
                content=mrpack([entry(cdn, FAPI), entry(cdn, LITH)], {
                    "overrides/config/lithium.properties": b"mixin.ai=false",
                    "overrides/mods/bundled.jar": b"PK"}, mc="26.3"))
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, cdn)
        await lea.close()
        server = await oscar.api.server(sid)
        hits = await oscar.search_modpacks("cobblemon")
        assert [h.project_id for h in hits] == [PACK]
        pack = await oscar.import_modpack(PACK)
        s = await oscar.save_modpack(server, pack)
        assert s["modpack"]["name"] == "Cobblemon Pack" and [m["slug"] for m in s["mods"]] == ["fabric-api", "lithium"]
        assert oscar.unapproved_mods(s) == []

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        directory = oscar.layout.server_dir(sid)
        assert (directory / "config" / "lithium.properties").read_text() == "mixin.ai=false"
        assert not (directory / "mods" / "bundled.jar").exists()
        assert set(jars(oscar, sid)) == {file_name(Mod.model_validate(m)) for m in s["mods"]}
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


def test_pregeneration_and_an_explained_crash(tmp_path, backend) -> None:
    async def scenario():
        cdn = FakeModrinth()
        cdn.add(FAPI, "fabric-api", version_id="FapiAAAA")
        cdn.add(LITH, "lithium", deps=[FAPI], version_id="LithBBBB", content=lithium_jar())
        cdn.add("fALzjamp", "chunky", version_id="ChunEEEE")
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, cdn)
        await lea.close()
        server = await oscar.api.server(sid)
        mods = await oscar.resolve_mod(server, LITH, [])
        mods += await oscar.resolve_mod(server, "chunky", mods)
        await oscar.save_mods(sid, mods)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        directory = oscar.layout.server_dir(sid)
        assert "pause-when-empty-seconds=0" in (directory / "server.properties").read_text("latin-1")

        await oscar.api.send_action(sid, {"kind": "pregen_start", "dimension": "overworld", "radius": 1000})

        async def progress():
            return ((await oscar.api.server(sid)).get("pregen") or {}).get("percent") == 12.5
        await until(progress, timeout=30, what="pre-generation progress")
        s = await oscar.api.server(sid)
        assert s["pregen"] == {"dimension": "overworld", "radius": 1000, "state": "running", "percent": 12.5,
                               "eta_s": 90}
        commands = (directory / "commands.log").read_text("utf-8").splitlines()
        assert commands[-7:] == ["chunky world minecraft:overworld", "chunky spawn", "chunky shape square",
                                 "chunky radius 1000", "chunky quiet 30", "chunky start", "chunky confirm"]
        await oscar.api.send_action(sid, {"kind": "pregen_cancel"})

        async def cancelled():
            return ((await oscar.api.server(sid)).get("pregen") or {}).get("state") == "cancelled"
        await until(cancelled, timeout=30, what="pre-generation cancelled")
        s = await oscar.api.server(sid)
        assert (s["pregen"]["dimension"], s["pregen"]["radius"]) == ("overworld", 1000)

        # Lithium crashes the server: the hosting PC names it, the admins see it.
        await oscar.send_console("crashmod")

        async def explained():
            return (await oscar.api.server(sid)).get("last_crash")
        await until(explained, timeout=30, what="crash explained")
        crash = (await oscar.api.server(sid))["last_crash"]
        assert (crash["cause"], crash["mod_project_id"], crash["mod_name"]) == ("mod", LITH, "Lithium")
        assert any(n == "error" and "Lithium" in p["message"] for n, p in oscar._listener.events)
        await wait_state(oscar, sid, "running")          # restarted by itself, as before
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())


def test_datapacks_settings_files_spark_and_performance(tmp_path, backend) -> None:
    from mdh_client.minecraft import datapacks as datapack_files

    async def scenario():
        cdn = modrinth()
        cdn.add("8oi3bsk5", "terralith", loaders=("datapack",), project_type="datapack", version_id="TerrAAAA")
        cdn.add("l6YH9Als", "spark", version_id="SparkAAA")
        oscar, lea, sid, _ = await two_pcs(tmp_path, backend, cdn)
        await lea.close()
        server = await oscar.api.server(sid)
        await oscar.save_mods(sid, await oscar.resolve_mod(server, "spark", []))
        terra = await oscar.datapack(server, "terralith")
        s = await oscar.save_datapacks(sid, [terra])
        assert oscar.unapproved_mods(s) == [] and len(lea.unapproved_mods(s)) == 2   # spark and Terralith
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        directory = oscar.layout.server_dir(sid)
        assert (directory / "world" / "datapacks" / datapack_files.file_name(terra)).read_bytes() == \
            cdn.files[terra.url]

        # A settings file written by a mod, saved to the VPS with the world.
        (directory / "config").mkdir(exist_ok=True)
        (directory / "config" / "lithium.properties").write_bytes(b"mixin.ai=true\n")
        await oscar.backup_now()
        server = await oscar.api.server(sid)
        listed = await oscar.config_file_list(server)
        assert ("config/lithium.properties", 14, False) in listed
        assert all(not p.startswith(("world/", "mods/")) for p, _, _ in listed)
        assert await oscar.read_config_file(server, "config/lithium.properties") == "mixin.ai=true\n"
        s = await oscar.save_config_files(sid, {"config/lithium.properties": "mixin.ai=false\n"})
        assert s["config_file_paths"] == ["config/lithium.properties"]
        assert ("config/lithium.properties", 15, True) in await oscar.config_file_list(s)

        # spark: one minute of profile (a moment with the fake Minecraft), then its link.
        await oscar.api.send_action(sid, {"kind": "profile_start", "seconds": 60})

        async def report():
            return ((await oscar.api.server(sid)).get("profile") or {}).get("url")
        await until(report, timeout=30, what="spark report")
        assert (await oscar.api.server(sid))["profile"] == {"state": "done", "url": "https://spark.lucko.me/AbCdEf1234"}
        assert "spark profiler start --timeout 60" in (directory / "commands.log").read_text("utf-8")

        # The settings file applies at the next start, whatever PC hosts.
        await stop(oscar, sid)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert (directory / "config" / "lithium.properties").read_text("utf-8") == "mixin.ai=false\n"
        perf = await oscar.api.performance(sid, 1)
        assert perf["points"] and perf["points"][-1]["ram_used_mb"] is not None
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())
