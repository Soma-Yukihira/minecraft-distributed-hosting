"""Lease endpoints, reserved to the host client (renewal normally goes through the WebSocket)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response

from mdh_server.api.errors import forbidden
from mdh_server.auth.deps import Ctx, CurrentUser, owned_host, server_membership
from mdh_server.coordination.lease import LeaseState
from mdh_shared.models.api import (
    HostStateReport,
    ImportRequest,
    LeaseAcquire,
    LeaseGrant,
    LeaseRelease,
    LeaseRenew,
)

router = APIRouter(tags=["leases"])


def _authorize(ctx, server_id: str, host_id: str, user_id: str) -> None:
    _, m = server_membership(ctx.conn, server_id, user_id)
    owned_host(ctx.conn, host_id, user_id)
    if not m.can_host:
        raise forbidden("you are not allowed to host servers of this group")


def _grant(ctx, state: LeaseState) -> LeaseGrant:
    assert state.expires_at is not None
    return LeaseGrant(server_id=state.server_id, epoch=state.epoch, ttl_s=ctx.settings.lease.ttl_s,
                      expires_at=datetime.fromtimestamp(state.expires_at, UTC))


@router.post("/servers/{server_id}/lease/acquire")
async def acquire(server_id: str, body: LeaseAcquire, user: CurrentUser, ctx: Ctx) -> LeaseGrant:
    _authorize(ctx, server_id, body.host_id, user.user_id)
    return _grant(ctx, await ctx.coordinator.acquire(server_id, body.host_id, body.offer_id))


@router.post("/servers/{server_id}/import")
async def start_import(server_id: str, body: ImportRequest, user: CurrentUser, ctx: Ctx) -> LeaseGrant:
    """An admin replaces the world by one from elsewhere: lease for the upload (stopped server)."""
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    owned_host(ctx.conn, body.host_id, user.user_id)
    return _grant(ctx, await ctx.coordinator.grant_import(server_id, body.host_id, user.user_id))


@router.post("/servers/{server_id}/lease/renew")
async def renew(server_id: str, body: LeaseRenew, user: CurrentUser, ctx: Ctx) -> LeaseGrant:
    _authorize(ctx, server_id, body.host_id, user.user_id)
    return _grant(ctx, await ctx.coordinator.renew(server_id, body.host_id, body.epoch))


@router.post("/servers/{server_id}/lease/release", status_code=204)
async def release(server_id: str, body: LeaseRelease, user: CurrentUser, ctx: Ctx) -> Response:
    # Releasing is always allowed to the holder, even if it just lost can_host.
    server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    await ctx.coordinator.release(server_id, body.host_id, body.epoch, body.final_version_id)
    return Response(status_code=204)


@router.post("/servers/{server_id}/host-state", status_code=204)
async def host_state(server_id: str, body: HostStateReport, user: CurrentUser, ctx: Ctx) -> Response:
    server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    await ctx.coordinator.report_state(server_id, body.host_id, body.epoch, body.state, body.detail,
                                       body.crash)
    return Response(status_code=204)
