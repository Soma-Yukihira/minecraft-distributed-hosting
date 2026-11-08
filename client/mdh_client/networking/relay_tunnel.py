"""Host end of a relay tunnel: one player connection, carried over an outgoing WebSocket.

The VPS relay asks the host (``relay_open`` on the control session) to connect back; the host
opens ``/relay/data/{conn_id}`` -- an outgoing HTTPS connection, so nothing has to be opened on
the PC's box -- and glues it to the local Minecraft server on ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl

import websockets

from mdh_shared.protocol import minecraft as mc

log = logging.getLogger("mdh.tunnel")

CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 10.0


async def run_tunnel(data_url: str, relay_token: str, local_port: int,
                     ssl_context: ssl.SSLContext | None = None, *, first: bytes = b"",
                     refuse: str | None = None) -> tuple[int, int]:
    """Carry one connection until either side closes. Returns (bytes to the server, bytes back).

    ``first`` is written to the local server before anything else (the PROXY protocol header).
    ``refuse``: never reach Minecraft; a login gets this message on screen, a ping just ends."""
    sent = received = 0
    # No per-message compression: Minecraft already compresses its packets, deflate would only
    # cost CPU on both ends.
    async with websockets.connect(data_url, open_timeout=CONNECT_TIMEOUT_S, max_size=4 * CHUNK,
                                  compression=None, ssl=ssl_context) as ws:
        await ws.send(json.dumps({"relay_token": relay_token}))
        if refuse is not None:
            await _refuse(ws, refuse)
            return 0, 0
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", local_port),
                                                CONNECT_TIMEOUT_S)
        if first:
            writer.write(first)

        async def to_server() -> None:
            nonlocal sent
            async for data in ws:
                if isinstance(data, str):
                    raise ValueError("unexpected text frame on a data tunnel")
                sent += len(data)
                writer.write(data)
                await writer.drain()

        async def to_player() -> None:
            nonlocal received
            while data := await reader.read(CHUNK):
                received += len(data)
                await ws.send(data)

        tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_player())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    return sent, received


async def _refuse(ws, message: str) -> None:
    """The relay sends the player's handshake (and login) first: answer a login with a refusal."""
    with contextlib.suppress(Exception):  # whatever arrives, the connection ends here
        data = await asyncio.wait_for(ws.recv(), CONNECT_TIMEOUT_S)
        reader = asyncio.StreamReader()
        reader.feed_data(data if isinstance(data, bytes) else b"")
        reader.feed_eof()
        _, hs = await mc.read_handshake(reader)
        if hs.next_state in (mc.LOGIN, mc.TRANSFER):
            await ws.send(mc.login_disconnect(message))
