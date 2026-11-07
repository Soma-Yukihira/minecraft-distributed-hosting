"""Public status page served by the relay on the Minecraft port (docs/ARCHITECTURE.md 4.9)."""

import asyncio

import httpx

from test_agent_live import until
from test_relay_live import Player, join, server_list, two_pcs, wait_state
from test_world_handover import backend, state  # noqa: F401


async def page(port: int, host: str, method: str = "GET") -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.request(method, f"http://127.0.0.1:{port}/", headers={"Host": f"{host}:{port}"})


def test_status_page_in_a_browser(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid = await two_pcs(tmp_path, backend)
        await lea.close()
        assert (await page(port, "mc.test")).status_code == 404       # off unless an admin turns it on

        s = await oscar.api.update_server(sid, public_status=True)
        assert s["status_page_url"] == f"http://mc.test:{port}/"
        r = await page(port, "mc.test")
        assert r.status_code == 200 and "Survie" in r.text and "arrêté" in r.text.lower()
        assert "script-src" not in r.headers["content-security-policy"]
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
        assert f"mc.test:{port}" in r.text                             # the address to copy

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")

        async def shows_players():
            text = (await page(port, "mc.test")).text
            return "En ligne" in text and "Steve" in text             # the fake server's player
        await until(shows_players, timeout=20, what="players on the page")

        # Same port, Minecraft still works.
        assert await server_list(port, "mc.test") == "FAKE pc_oscar"
        alex = await join(port, "mc.test", "Alex")
        assert isinstance(alex, Player)
        alex.close()

        head = await page(port, "mc.test", "HEAD")
        assert head.status_code == 200 and head.content == b""
        assert (await page(port, "mc.test", "POST")).status_code == 405
        assert (await page(port, "nope.mc.test")).status_code == 404

        # A request that never ends is cut, like a slow Minecraft handshake.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: mc.test\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 15) == b""
        writer.close()

        # Unofficial accounts: the page never shows a personal address (a secret), only the
        # server's, for the first connection (0.8.1).
        await oscar.api.update_server(sid, allow_offline_accounts=True, allowed_players=["Krystal"],
                                      properties={"white-list": True})
        text = (await page(port, "mc.test")).text
        assert "propre adresse" in text and "Première connexion" in text and f"mc.test:{port}</p>" in text
        token_host = (await oscar.api.player_addresses(sid))[0]["address"].split(":")[0]
        assert token_host not in (await page(port, token_host)).text
        await oscar.close()

    asyncio.run(scenario())
