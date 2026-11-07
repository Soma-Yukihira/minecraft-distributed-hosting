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

log = logging.getLogger("mdh.tunnel")

CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 10.0


async def run_tunnel(data_url: str, relay_token: str, local_port: int,
                     ssl_context: ssl.SSLContext | None = None) -> tuple[int, int]:
    """Carry one connection until either side closes. Returns (bytes to the server, bytes back)."""
    sent = received = 0
    # No per-message compression: Minecraft already compresses its packets, deflate would only
    # cost CPU on both ends.
    async with websockets.connect(data_url, open_timeout=CONNECT_TIMEOUT_S, max_size=4 * CHUNK,
                                  compression=None, ssl=ssl_context) as ws:
        await ws.send(json.dumps({"relay_token": relay_token}))
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", local_port),
                                                CONNECT_TIMEOUT_S)

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
