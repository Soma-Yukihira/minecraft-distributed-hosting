"""Persistent WebSocket to the control plane: hello, heartbeats, incoming orders, reconnection."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from pydantic import ValidationError

from mdh_shared import __version__
from mdh_shared.protocol.messages import HeartbeatAck, Welcome, parse_server_message

log = logging.getLogger("mdh.channel")

#: Close code of a control plane that no longer accepts this version of the application.
CLIENT_TOO_OLD = 4426

BACKOFF_S = (1, 2, 5, 10, 20, 30)


class ControlChannel:
    """Keeps one session open and calls back the agent.

    * ``heartbeat_payload()`` builds the body of each heartbeat (leases, availability, metrics).
    * ``on_ack(ack, sent_at)`` receives each acknowledgement with the *local monotonic time at
      which the heartbeat was sent*, which is what lease self-fencing needs.
    """

    def __init__(
        self,
        ws_url: str,
        *,
        access_token: Callable[[], Awaitable[str]],
        host_id: str | None,
        heartbeat_payload: Callable[[], dict[str, Any]],
        on_ack: Callable[[HeartbeatAck, float], Awaitable[None]],
        on_message: Callable[[Any], Awaitable[None]],
        on_connection: Callable[[bool], None] = lambda _: None,
        clock: Callable[[], float] = time.monotonic,
        ssl_context: ssl.SSLContext | None = None,
        on_too_old: Callable[[], None] = lambda: None,
    ) -> None:
        self.ws_url = ws_url
        self._on_too_old = on_too_old
        self._ssl = ssl_context
        self._access_token = access_token
        self.host_id = host_id
        self._payload = heartbeat_payload
        self._on_ack = on_ack
        self._on_message = on_message
        self._on_connection = on_connection
        self._clock = clock
        self._ws: Any = None
        self._seq = int(time.time() * 1000)  # strictly increasing even across reconnects
        self._sent: dict[int, float] = {}
        self.connected = False
        self.interval_s = 10.0
        self._wakeup = asyncio.Event()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as exc:
                log.info("control channel lost: %s", exc)
                if exc.rcvd is not None and exc.rcvd.code == CLIENT_TOO_OLD:
                    self._on_too_old()
            except Exception as exc:  # noqa: BLE001 - any failure means "reconnect later"
                log.info("control channel lost: %s", exc)
            finally:
                if self.connected:
                    self.connected = False
                    self._on_connection(False)
                self._ws = None
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    async def _session(self) -> None:
        token = await self._access_token()
        async with websockets.connect(self.ws_url, open_timeout=10, ping_interval=20,
                                      max_size=256 * 1024, ssl=self._ssl) as ws:
            self._ws = ws
            await ws.send(json.dumps({"type": "hello", "access_token": token, "host_id": self.host_id,
                                      "client_version": __version__}))
            welcome = parse_server_message(await asyncio.wait_for(ws.recv(), 10))
            if not isinstance(welcome, Welcome):
                raise ConnectionError("unexpected first message")
            self.interval_s = welcome.heartbeat_interval_s
            self.connected = True
            self._on_connection(True)
            sender = asyncio.create_task(self._heartbeats()) if self.host_id else None
            try:
                async for raw in ws:
                    try:
                        msg = parse_server_message(raw)
                    except ValidationError:
                        log.warning("ignoring malformed message from control plane")
                        continue
                    if isinstance(msg, HeartbeatAck):
                        sent_at = self._sent.pop(msg.seq, None)
                        if sent_at is not None:
                            await self._on_ack(msg, sent_at)
                    else:
                        await self._on_message(msg)
            finally:
                if sender:
                    sender.cancel()

    def beat_now(self) -> None:
        """Send a heartbeat right away (e.g. just after acquiring a lease)."""
        self._wakeup.set()

    async def _heartbeats(self) -> None:
        while True:
            self._seq += 1
            body = {"type": "heartbeat", "seq": self._seq, **self._payload()}
            self._sent[self._seq] = self._clock()
            # Bound memory if acks are lost.
            for old in [s for s in self._sent if s < self._seq - 20]:
                del self._sent[old]
            await self._ws.send(json.dumps(body))
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), self.interval_s)
            except TimeoutError:
                pass

    async def send(self, payload: dict[str, Any]) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(payload))
            return True
        except Exception:  # noqa: BLE001
            return False
