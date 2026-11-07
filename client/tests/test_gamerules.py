"""0.8.1: options Minecraft ignores in server.properties since 1.21.9 (PvP...) are game rules
(docs/ARCHITECTURE.md 3.16)."""

import asyncio

from mdh_client.minecraft import gamerules
from test_agent_live import until
from test_relay_live import two_pcs, wait_state
from test_world_handover import backend  # noqa: F401


def test_rule_names_follow_the_version() -> None:
    assert gamerules.rule_names("1.21.8") == {}                         # server.properties still works
    assert gamerules.rule_names("1.21.10")["allow-nether"] == "allowEnteringNetherUsingPortals"
    assert gamerules.rule_names("1.21.11-pre-2")["spawn-monsters"] == "spawn_monsters"
    assert gamerules.rule_names("26.3")["enable-command-block"] == "command_blocks_work"
    assert gamerules.rule_names("26w05a")["pvp"] == "pvp"                # weekly snapshot: newest names
    assert gamerules.rule_names(None) == {}


def test_commands() -> None:
    props = {"pvp": False, "allow-nether": "true", "motd": "Salut", "difficulty": "hard"}
    assert gamerules.commands(props, "26.3", "fabric") == [
        "gamerule pvp false", "gamerule allow_entering_nether_using_portals true"]
    assert gamerules.commands(props, "1.20.4", "fabric") == []
    paper = gamerules.commands({"pvp": False}, "26.3", "paper")
    assert paper == ["gamerule pvp false", "execute in minecraft:the_nether run gamerule pvp false",
                     "execute in minecraft:the_end run gamerule pvp false"]      # per world on Paper


def test_pvp_applied_at_start_and_without_restart(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar, lea, sid = await two_pcs(tmp_path, backend, properties={"pvp": False})
        await lea.close()
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        commands = oscar.layout.server_dir(sid) / "commands.log"

        def sent() -> list[str]:
            return commands.read_text("utf-8").splitlines() if commands.exists() else []
        await until(lambda: "gamerule pvp false" in sent(), what="PvP off once ready")
        process = oscar.hosted.process

        await oscar.api.update_server(sid, properties={"pvp": True})
        await until(lambda: "gamerule pvp true" in sent(), timeout=20, what="PvP on without restart")
        assert oscar.hosted.process is process and "stop" not in sent()
        await oscar.close()

    asyncio.run(scenario())
