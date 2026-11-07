"""Access requests of players refused by the whitelist (docs/API.md section 4 ter)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Path

from mdh_server import access
from mdh_server.api.errors import ApiError, not_found
from mdh_server.auth.deps import Ctx, CurrentUser, server_membership
from mdh_server.database.db import immediate
from mdh_shared.models.api import AccessAnswer, AccessRequest, ServerStatus

router = APIRouter(tags=["access"])

Player = Path(pattern=r"^[A-Za-z0-9_]{3,16}$")


@router.get("/servers/{server_id}/access-requests")
async def list_requests(server_id: str, user: CurrentUser, ctx: Ctx) -> list[AccessRequest]:
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    with immediate(ctx.conn):
        access.purge(ctx.conn, ctx.now())
    rows = ctx.conn.execute("SELECT * FROM access_requests WHERE server_id = ?"
                            " ORDER BY status = 'refused', last_at DESC", (server_id,)).fetchall()
    return [AccessRequest(player=r["player_name"], status=r["status"], attempts=r["attempts"],
                          kind=r["kind"], place=r["place"],
                          first_at=datetime.fromtimestamp(r["first_at"], UTC),
                          last_at=datetime.fromtimestamp(r["last_at"], UTC)) for r in rows]


@router.post("/servers/{server_id}/access-requests/{player}/accept")
async def accept_request(server_id: str, user: CurrentUser, ctx: Ctx, player: str = Player,
                         body: AccessAnswer | None = None) -> ServerStatus:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    try:
        found = access.accept(ctx.conn, srv, player, user.user_id, ctx.now(), body.place if body else None)
    except access.ListFull:
        raise ApiError(409, "too_many_players", "the list of allowed players is full (200)") from None
    if not found:
        raise not_found("access request")
    await ctx.coordinator.broadcast(server_id)  # the host applies the new list at once
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/access-requests/{player}/refuse")
async def refuse_request(server_id: str, user: CurrentUser, ctx: Ctx, player: str = Player,
                         body: AccessAnswer | None = None) -> ServerStatus:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    if not access.refuse(ctx.conn, srv, player, user.user_id, ctx.now(), body.place if body else None):
        raise not_found("access request")
    await ctx.coordinator.broadcast(server_id)
    return ctx.coordinator.status(server_id)
