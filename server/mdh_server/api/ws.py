"""WebSocket endpoint: host heartbeats (lease renewal), offers, live updates for the GUI."""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from mdh_server.api.ratelimit import ws_client_ip
from mdh_server.auth.tokens import authenticate
from mdh_server.context import AppContext
from mdh_server.coordination.service import ProtocolError
from mdh_server.realtime import Session
from mdh_shared import versions
from mdh_shared.constants import MIN_CLIENT_VERSION
from mdh_shared.protocol.messages import (
    Heartbeat,
    Hello,
    MessageTooLarge,
    OfferReply,
    PrefetchDone,
    Welcome,
    parse_client_message,
)

log = logging.getLogger("mdh.ws")
router = APIRouter()

HELLO_TIMEOUT_S = 10.0
POLICY_VIOLATION = 1008
REPLACED = 4000  # application close code: another session of the same host took over
#: The application is older than MIN_CLIENT_VERSION: it must update before hosting again.
CLIENT_TOO_OLD = 4426


async def _authenticate(ctx: AppContext, websocket: WebSocket) -> Session | int:
    """The session, or the close code to answer with."""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_S)
        hello = parse_client_message(raw)
    except (asyncio.TimeoutError, ValidationError, MessageTooLarge, WebSocketDisconnect, KeyError):
        return POLICY_VIOLATION
    if not isinstance(hello, Hello):
        return POLICY_VIOLATION
    user = authenticate(ctx.conn, hello.access_token, ctx.now())
    if user is None:
        return POLICY_VIOLATION
    if hello.host_id is not None:
        owned = ctx.conn.execute("SELECT 1 FROM hosts WHERE id = ? AND user_id = ?",
                                 (hello.host_id, user.user_id)).fetchone()
        if not owned:
            return POLICY_VIOLATION
    if versions.older_than(hello.client_version, MIN_CLIENT_VERSION):
        log.info("refused application %r of user %s (minimum %s)", hello.client_version,
                 user.username, MIN_CLIENT_VERSION)
        return CLIENT_TOO_OLD
    return Session(uuid.uuid4().hex, user.user_id, hello.host_id, websocket)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx
    if not ctx.limiter.hit(f"ws:{ws_client_ip(websocket, ctx.settings.trusted_proxies)}",
                           per_minute=60, burst=20):
        await websocket.close(code=POLICY_VIOLATION)  # reconnect storm or flood: refuse early
        return
    await websocket.accept()
    # The token travels in the first frame, never in the URL (URLs end up in Nginx logs).
    session = await _authenticate(ctx, websocket)
    if isinstance(session, int):
        await websocket.close(code=session)
        return

    previous = ctx.hub.register(session)
    if previous is not None:
        previous.closed = True
        try:
            await previous.websocket.close(code=REPLACED)
        except Exception:  # noqa: BLE001 - the old socket is probably already dead
            pass
    await session.send(Welcome(session_id=session.session_id, server_time=ctx.now(),
                               heartbeat_interval_s=ctx.settings.heartbeat_interval_s))
    if session.host_id is not None:
        await ctx.coordinator.host_connected(session)

    idle_timeout = 3 * ctx.settings.heartbeat_interval_s
    close_code = 1000
    try:
        while not session.closed:
            raw = await asyncio.wait_for(websocket.receive_text(), idle_timeout)
            msg = parse_client_message(raw)
            # Long-lived session: a disabled account is cut off at its next frame.
            if ctx.conn.execute("SELECT disabled FROM users WHERE id = ?",
                                (session.user_id,)).fetchone()["disabled"]:
                raise ProtocolError("account disabled")
            if isinstance(msg, Heartbeat):
                if session.host_id is None:
                    raise ProtocolError("heartbeats are only for host sessions")
                ack = await ctx.coordinator.heartbeat(session, msg)
                await session.send(ack)
            elif isinstance(msg, OfferReply):
                if session.host_id is None:
                    raise ProtocolError("offer replies are only for host sessions")
                await ctx.coordinator.offer_reply(session, msg)
            elif isinstance(msg, PrefetchDone):
                if session.host_id is None:
                    raise ProtocolError("prefetch results are only for host sessions")
                await ctx.coordinator.prefetch_done(session, msg)
            else:
                raise ProtocolError(f"unexpected {msg.type} message")
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except (ValidationError, MessageTooLarge, ProtocolError) as exc:
        log.info("closing session %s: %s", session.session_id, exc)
        close_code = POLICY_VIOLATION
    finally:
        ctx.hub.unregister(session)
        if session.host_id is not None:
            ctx.coordinator.host_disconnected(session)
        if not session.closed:
            session.closed = True
            try:
                await websocket.close(code=close_code)
            except Exception:  # noqa: BLE001
                pass