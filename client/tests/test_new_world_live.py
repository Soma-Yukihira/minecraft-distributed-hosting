"""Starting over with a new world (docs/ARCHITECTURE.md 3.17), with the real agent and control plane."""

import asyncio

from mdh_client.minecraft import bluemap
from mdh_client.minecraft.server_files import read_properties
from test_agent_live import Recorder, _is, backend, make_agent, server_dir, setup_group, until  # noqa: F401


def test_new_world_drops_the_world_keeps_the_settings_and_the_old_one(tmp_path, backend) -> None:  # noqa: F811
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        live = server_dir(agent, sid)
        (live / "world" / "old-world.txt").write_text("built a house", "utf-8")
        (live / "ops.json").write_text('[{"name": "oscar"}]', "utf-8")
        await agent.api.stop(sid, countdown_s=0)
        await until(lambda: _is(agent, sid, "offline"), what="offline")
        await until(lambda: agent.hosted is None, what="released")

        status = await agent.api.new_world(sid, seed="Les copains", level_type="flat")
        assert status["properties"]["level-seed"] == "Les copains"
        versions = await agent.api.versions(sid)
        assert versions[0]["label"] == "Nouveau monde (graine : Les copains)" and versions[1]["pinned"]

        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running again")
        assert not (live / "world" / "old-world.txt").exists()
        assert (live / "world" / "level.dat").exists()  # generated again by "Minecraft"
        assert (live / "ops.json").read_text("utf-8") == '[{"name": "oscar"}]'
        props = read_properties(live / "server.properties")
        assert props["level-seed"] == "Les copains" and props["level-type"] == "minecraft:flat"
        assert props["generate-structures"] == "true"
        await agent.api.stop(sid, countdown_s=0)
        await until(lambda: _is(agent, sid, "offline"), what="offline again")
        await agent.close()

    asyncio.run(scenario())


def test_the_map_of_a_replaced_world_is_forgotten(tmp_path) -> None:
    from mdh_shared.models.enums import Loader

    data = tmp_path / "bluemap"
    (data / "web" / "maps" / "world").mkdir(parents=True)
    (data / "web" / "maps" / "world" / "tile.png").write_bytes(b"old")
    (data / "web" / "index.html").write_text("app", "utf-8")
    (data / "minecraft-client-26.3.jar").write_bytes(b"textures")
    (data / "pluginState.json").write_text("{}", "utf-8")
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"level")

    bluemap.configure(tmp_path, Loader.FABRIC, 8100, eula_accepted=True)
    assert (data / "web" / "maps" / "world" / "tile.png").exists()  # same world: the map stays

    (tmp_path / "world" / "level.dat").unlink()
    bluemap.configure(tmp_path, Loader.FABRIC, 8100, eula_accepted=True)
    assert not (data / "web" / "maps").exists() and not (data / "pluginState.json").exists()
    assert (data / "minecraft-client-26.3.jar").exists() and (data / "web" / "index.html").exists()


def test_deleting_a_server_removes_it_and_the_copy_on_this_pc(tmp_path, backend) -> None:  # noqa: F811
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        status = await agent.api.server(sid)
        await agent.api.start(sid)
        await until(lambda: _is(agent, sid, "running"), what="running")
        await agent.api.stop(sid, countdown_s=0)
        await until(lambda: _is(agent, sid, "offline"), what="offline")
        await until(lambda: agent.hosted is None, what="released")
        live = server_dir(agent, sid)
        assert (live / "world" / "level.dat").exists()

        await agent.api.delete_server(sid, status["name"])
        await until(lambda: not live.exists(), what="local copy removed")
        assert await agent.api.servers(status["group_id"]) == []
        assert any(n == "server_update" and p.get("state") == "deleted" for n, p in rec.events)
        await agent.close()

    asyncio.run(scenario())


def test_the_application_hears_when_its_group_changes(tmp_path, backend) -> None:  # noqa: F811
    rec = Recorder()

    async def scenario():
        agent = make_agent(tmp_path, backend, rec)
        sid = await setup_group(agent, backend)
        group_id = (await agent.api.server(sid))["group_id"]
        await agent.api.create_server(group_id, name="Créatif", mc_version="26.3", loader="paper", memory_mb=1024)
        await until(lambda: ("group_update", {"group_id": group_id}) in rec.events, what="group_update")
        await agent.close()

    asyncio.run(scenario())
