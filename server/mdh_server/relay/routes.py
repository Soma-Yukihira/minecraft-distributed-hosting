"""Relay endpoints: the host's data WebSocket, and personal addresses of players."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket

from mdh_server.api.errors import not_found
from mdh_server.auth.deps import Ctx, CurrentUser, server_membership
from mdh_server.context import AppContext
from mdh_server.database.db import immediate
from mdh_server.relay import addresses
from mdh_shared.models.api import PlayerAddress

router = APIRouter(tags=["relay"])

TOKEN_TIMEOUT_S = 10.0
POLICY_VIOLATION = 1008


@router.websocket("/relay/data/{conn_id}")
async def relay_data(websocket: WebSocket, conn_id: str) -> None:
    """One player connection, opened by the host on request of the relay (``relay_open``).

    Authenticated by the one-time token sent to the lease holder over its control session; the
    token travels in the first frame, never in the URL (URLs end up in Nginx logs).
    """
    ctx: AppContext = websocket.app.state.ctx
    if conn_id not in ctx.relay.tunnels:  # nothing waits for this id: do not even wait for a token
        await websocket.close(code=POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), TOKEN_TIMEOUT_S)
        token = json.loads(raw)["relay_token"]
        if not isinstance(token, str):
            raise TypeError
    except Exception:  # noqa: BLE001 - anything unexpected: refuse
        await websocket.close(code=POLICY_VIOLATION)
        return
    tunnel = ctx.relay.claim(conn_id, token)
    if tunnel is None:
        await websocket.close(code=POLICY_VIOLATION)
        return
    tunnel.attached.set_result(websocket)
    await tunnel.finished.wait()  # the relay pumps through this socket until the player leaves
    with contextlib.suppress(Exception):
        await websocket.close()


def _addresses(ctx: AppContext, server_id: str) -> list[PlayerAddress]:
    rows = ctx.conn.execute("SELECT player_name, token FROM player_access WHERE server_id = ?"
                            " ORDER BY player_name COLLATE NOCASE", (server_id,)).fetchall()
    return [PlayerAddress(player=r["player_name"],
                          address=addresses.player_address(ctx.settings, r["token"], ctx.relay.port))
            for r in rows]


@router.get("/servers/{server_id}/player-addresses")
async def player_addresses(server_id: str, user: CurrentUser, ctx: Ctx) -> list[PlayerAddress]:
    """Admin: the personal address of each allowed player, to send to them."""
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    return _addresses(ctx, server_id)


@router.post("/servers/{server_id}/player-addresses/{player}/regenerate")
async def regenerate_address(server_id: str, player: str, user: CurrentUser, ctx: Ctx) -> PlayerAddress:
    """Admin: the old address stops working at once (e.g. it was shared by mistake)."""
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    with immediate(ctx.conn):
        token = addresses.regenerate_token(ctx.conn, server_id, player)
    if token is None:
        raise not_found("player")
    ctx.relay.cut_server_player(server_id, player)
    return next(a for a in _addresses(ctx, server_id) if a.player.lower() == player.lower())
