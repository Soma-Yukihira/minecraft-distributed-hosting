"""Wake-up by the first player, sleep once empty (docs/ARCHITECTURE.md 4.7): nobody has to open
the application to play."""

import asyncio
import sqlite3

from test_agent_live import until
from test_relay_live import Player, join, server_list, two_pcs, wait_state
from test_world_handover import backend, state  # noqa: F401


def test_first_player_wakes_the_server_and_it_sleeps_once_empty(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid = await two_pcs(tmp_path, backend, start_on_join=True,
                                        properties={"white-list": True}, allowed_players=["Alex", "Steve"])
        await lea.close()
        assert "En veille" in await server_list(port, "mc.test")   # a ping never wakes it
        assert (await state(oscar, sid))["state"] == "offline"

        # Someone not on the list cannot make a PC host: the attempt only asks the admins.
        assert "demande est envoyée" in await join(port, "mc.test", "Mallory")
        s = await state(oscar, sid)
        assert s["desired_running"] is False and s["pending_access_requests"] == 1

        # Alex tries to join: the server starts, and he waits on "Connexion…" until he is in
        # (0.8.0; before, he was told to come back in a minute).
        waited: list[int] = []
        alex = await join(port, "mc.test", "Alex", waited=waited)
        assert isinstance(alex, Player) and await alex.echo(b"hi") == b"hi"
        assert waited, "the relay kept Alex waiting with its login queries"
        assert (await state(oscar, sid))["state"] == "running"
        events = await oscar.api.events(sid)
        assert any(e["type"] == "start_requested" and e["payload"].get("player") == "Alex" for e in events)

        # "Stop when empty" (shortened from the 5-minute minimum for the test).
        with sqlite3.connect(backend.db) as db:
            db.execute("UPDATE servers SET stop_when_empty_after_s = 3 WHERE id = ?", (sid,))
        await asyncio.sleep(5)
        assert (await state(oscar, sid))["state"] == "running"        # Alex is still playing
        alex.close()
        await oscar.send_console("leave")                             # Steve (fake server) leaves
        await until(lambda: not oscar.hosted.process.players, what="nobody in game")

        async def stop_announced():
            return (await state(oscar, sid))["stops_when_empty_at"] is not None
        await until(stop_announced, timeout=15, what="stop time shown")
        await wait_state(oscar, sid, "offline", timeout=60)
        s = await state(oscar, sid)
        assert s["desired_running"] is False
        versions = await oscar.api.versions(sid)
        assert versions[0]["kind"] == "final"                         # stopped cleanly, saved
        events = await oscar.api.events(sid)
        assert any(e["type"] == "stop_requested" and e["payload"].get("reason") == "empty" for e in events)
        assert "En veille" in await server_list(port, "mc.test")      # ready to wake up again
        await oscar.close()

    asyncio.run(scenario())
