"""0.8.0 end to end: a change of PC the players should not notice (docs/ARCHITECTURE.md 4.18),
and the measures each PC gives for the automatic choice of the host (3.15).

Real control plane and relay, real agents, fake Minecraft. The players are the tests' own
Minecraft client: it answers the relay's login queries like the real game, and does what the
game does on /transfer (connects to the given address with the "transfer" intent). Whether the
real game behaves exactly so still has to be tried with a real client.
"""

import asyncio
import re

from mdh_shared.protocol import minecraft as mc
from test_agent_live import until
from test_relay_live import Player, join, two_pcs, wait_state
from test_world_handover import backend, state  # noqa: F401


def logged(agent, text: str) -> bool:
    return any(n == "log" and text in p["line"] for n, p in agent._listener.events)


def test_players_follow_the_server_to_the_new_pc(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid = await two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        alex = await join(port, "mc.test", "Alex")
        assert isinstance(alex, Player) and await alex.echo(b"on Oscar") == b"on Oscar"

        await oscar.api.transfer(sid, lea.settings.host_id, countdown_s=0)
        commands = oscar.layout.server_dir(sid) / "commands.log"

        def transfer_line():
            text = commands.read_text("utf-8") if commands.exists() else ""
            return re.search(r"^transfer (\S+) (\d+) Alex$", text, re.M)
        await until(transfer_line, timeout=30, what="Alex sent away by /transfer")
        host, target_port = transfer_line().group(1), int(transfer_line().group(2))
        assert host.endswith(".mc.test") and target_port == port
        assert not host.startswith("mc.test")                     # a one-time address, not the server's

        # What the game does on /transfer: connect there at once, with the "transfer" intent.
        assert "celle de Alex" in await join(port, host, "Eve", next_state=mc.TRANSFER)   # Alex's only
        waited: list[int] = []
        back = await join(port, host, "Alex", next_state=mc.TRANSFER, waited=waited)
        assert isinstance(back, Player), back
        assert waited, "the relay kept Alex waiting while the server changed PC"
        assert await back.echo(b"on Lea") == b"on Lea"
        s = await state(oscar, sid)
        assert s["active_host_id"] == lea.settings.host_id and s["state"] == "running"
        # Léa's server saw a plain login: it does not need accepts-transfers.
        assert logged(lea, "Alex joined the game") and not logged(lea, "transfers are disabled")
        assert await alex.closed()                                 # the old connection is gone
        back.close()
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_each_pc_measures_itself(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar, lea, sid = await two_pcs(tmp_path, backend)

        async def measured():
            hosts = await oscar.api.server_hosts(sid)
            return all(h["upload_kbps"] and h["cpu_score"] and h["rtt_ms"] is not None for h in hosts)
        await until(measured, timeout=40, what="both PCs measured")
        hosts = {h["host_id"]: h for h in await oscar.api.server_hosts(sid)}
        assert all(0 < h["score"] <= 100 for h in hosts.values())
        assert lea.upload_kbps and lea.upload_kbps > 1000            # 2 MiB to the local VPS
        s = await oscar.api.update_server(sid, host_choice="best")
        assert s["host_choice"] == "best"
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())
