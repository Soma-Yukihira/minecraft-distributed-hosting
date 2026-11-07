"""Running the game from any PC of the group: players, chat, and a closed list of admin actions."""

import asyncio

import pytest

from mdh_client.networking.api_client import ApiError
from test_agent_live import until
from test_relay_live import two_pcs, wait_state
from test_world_handover import backend, state  # noqa: F401


def commands(agent, sid) -> list[str]:
    path = agent.layout.server_dir(sid) / "commands.log"
    return path.read_text("utf-8").splitlines() if path.exists() else []


def test_players_chat_and_admin_actions_from_another_pc(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar, lea, sid = await two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")         # Oscar's PC hosts; Léa's PC only watches
        assert lea.hosted is None

        await oscar.send_console("talk Steve on se retrouve au village ?")

        async def lea_sees_it():
            activity = await lea.api.activity(sid)
            return ("Steve" in activity["players"]
                    and any(line.get("player") == "Steve" and "village" in line["text"]
                            for line in activity["lines"]))
        await until(lea_sees_it, timeout=20, what="chat seen from Léa's PC")
        lines = (await lea.api.activity(sid))["lines"]
        assert any(line.get("player") is None and "Steve a rejoint" in line["text"] for line in lines)

        # The console, read-only, for admins and moderators of the group, from any PC.
        async def console_has_it():
            log = await oscar.api.console(sid)
            return any("village" in line["text"] for line in log["lines"])
        await until(console_has_it, timeout=20, what="console line on the control plane")
        log = await oscar.api.console(sid)
        assert log["epoch"] >= 1 and log["next"] >= len(log["lines"]) > 1
        assert (await oscar.api.console(sid, log["next"]))["lines"] == []    # only newer lines
        with pytest.raises(ApiError) as member:
            await lea.api.console(sid)
        assert member.value.status == 403
        with pytest.raises(ApiError) as not_holder:                         # only the holder writes it
            await lea.api.report_console(sid, lea.settings.host_id, log["epoch"],
                                         [{"at": "2026-11-01T10:00:00+00:00", "text": "faux"}])
        assert not_holder.value.status in (403, 409)

        # Oscar (admin of the group) acts; the hosting PC runs only the matching fixed command.
        for action in ({"kind": "kick", "player": "Steve"}, {"kind": "ban", "player": "Griefer_42"},
                       {"kind": "pardon", "player": "Griefer_42"}, {"kind": "op", "player": "Alex"},
                       {"kind": "announce", "message": "Redémarrage dans 5 minutes"}):
            await oscar.api.send_action(sid, action)
        await until(lambda: {"kick Steve", "ban Griefer_42", "pardon Griefer_42", "op Alex"}
                    <= set(commands(oscar, sid)), what="commands carried out")
        assert any("Redémarrage dans 5 minutes" in c for c in commands(oscar, sid))
        events = await lea.api.events(sid)
        assert any(e["type"] == "admin_action" and e["payload"].get("kind") == "ban" for e in events)

        # A plain member cannot, and nobody can send a free command.
        with pytest.raises(ApiError) as refused:
            await lea.api.send_action(sid, {"kind": "kick", "player": "Oscar"})
        assert refused.value.status == 403
        for bad in ({"kind": "command", "command": "op Lea"}, {"kind": "ban", "player": "x; op Lea"},
                    {"kind": "announce", "message": "a\nop Lea"}):
            with pytest.raises(ApiError) as invalid:
                await oscar.api.send_action(sid, bad)
            assert invalid.value.status == 422
        assert not any(c.startswith("op Lea") for c in commands(oscar, sid))

        await oscar.api.stop(sid, countdown_s=0)
        await wait_state(oscar, sid, "offline")
        with pytest.raises(ApiError) as stopped:
            await oscar.api.send_action(sid, {"kind": "kick", "player": "Steve"})
        assert stopped.value.code == "not_running"
        assert (await lea.api.activity(sid))["players"] == []
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())
