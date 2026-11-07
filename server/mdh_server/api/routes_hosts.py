"""Machines able to host servers, and their per-server priority."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mdh_server.api.errors import ApiError, conflict, not_found
from mdh_server.api.routes_groups import host_info
from mdh_server.auth import tokens
from mdh_server.auth.deps import Ctx, CurrentUser, owned_host, server_membership
from mdh_server.api.ratelimit import RateLimiter
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.database.db import immediate
from mdh_server.election.scoring import performance_score
from mdh_shared.models.api import HostInfo, HostPriority, HostRegister, HostUpdate, ServerHostInfo

router = APIRouter(tags=["hosts"])


@router.post("/hosts", status_code=201)
async def register_host(body: HostRegister, user: CurrentUser, ctx: Ctx) -> HostInfo:
    count = ctx.conn.execute("SELECT COUNT(*) FROM hosts WHERE user_id = ?", (user.user_id,)).fetchone()[0]
    if count >= ctx.settings.max_hosts_per_user:
        raise ApiError(403, "limit_reached", f"at most {ctx.settings.max_hosts_per_user} machines per user")
    host_id = tokens.new_id()
    ctx.conn.execute(
        "INSERT INTO hosts (id, user_id, display_name, os, cpu_cores, total_ram_mb, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (host_id, user.user_id, body.display_name, body.os, body.cpu_cores, body.total_ram_mb, ctx.now()))
    return host_info(ctx, owned_host(ctx.conn, host_id, user.user_id))


@router.get("/hosts")
async def list_hosts(user: CurrentUser, ctx: Ctx) -> list[HostInfo]:
    rows = ctx.conn.execute("SELECT * FROM hosts WHERE user_id = ? ORDER BY display_name",
                            (user.user_id,)).fetchall()
    return [host_info(ctx, r) for r in rows]


@router.patch("/hosts/{host_id}")
async def update_host(host_id: str, body: HostUpdate, user: CurrentUser, ctx: Ctx) -> HostInfo:
    owned_host(ctx.conn, host_id, user.user_id)
    changes = body.model_dump(exclude_unset=True)
    columns = {"display_name", "hosting_allowed", "auto_accept_offers", "max_ram_mb"}
    with immediate(ctx.conn):
        for key, value in changes.items():
            assert key in columns  # guaranteed by the model; keeps the f-string below safe
            ctx.conn.execute(f"UPDATE hosts SET {key} = ? WHERE id = ?",
                             (int(value) if isinstance(value, bool) else value, host_id))
    return host_info(ctx, owned_host(ctx.conn, host_id, user.user_id))


@router.delete("/hosts/{host_id}", status_code=204)
async def delete_host(host_id: str, user: CurrentUser, ctx: Ctx) -> Response:
    owned_host(ctx.conn, host_id, user.user_id)
    referenced = ctx.conn.execute(
        "SELECT 1 FROM leases WHERE holder_host_id = ?"
        " UNION SELECT 1 FROM world_versions WHERE host_id = ?", (host_id, host_id)).fetchone()
    if referenced:
        raise conflict("host_in_use", "this machine holds a lease or authored world versions")
    ctx.conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
    return Response(status_code=204)


@router.get("/servers/{server_id}/hosts")
async def server_hosts(server_id: str, user: CurrentUser, ctx: Ctx) -> list[ServerHostInfo]:
    """Every machine of the group's members, with its priority for this server."""
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    rows = ctx.conn.execute(
        "SELECT h.id, h.display_name, h.hosting_allowed, u.username, m.can_host,"
        " COALESCE(p.priority, 50) AS priority, COALESCE(p.enabled, 1) AS enabled"
        " FROM hosts h JOIN users u ON u.id = h.user_id"
        " JOIN memberships m ON m.user_id = h.user_id AND m.group_id = ?"
        " LEFT JOIN server_host_prefs p ON p.host_id = h.id AND p.server_id = ?"
        " ORDER BY priority DESC, h.display_name", (srv["group_id"], server_id)).fetchall()
    coordinator = ctx.coordinator
    candidates = {c.host_id: c for c in coordinator._candidates(srv)}
    req = coordinator._requirements(srv)
    lease = load_lease(ctx.conn, server_id)
    result = []
    for r in rows:
        c = candidates.get(r["id"])
        online = ctx.hub.is_online(r["id"])
        avail = coordinator.presence[r["id"]].availability if online and r["id"] in coordinator.presence else None
        extra = srv["memory_mb"] if r["id"] == lease.holder and lease.is_active(ctx.now()) else 0
        result.append(ServerHostInfo(
            host_id=r["id"], display_name=r["display_name"], owner=r["username"], online=online,
            hosting_allowed=bool(r["hosting_allowed"]), can_host=bool(r["can_host"]), priority=r["priority"],
            enabled=bool(r["enabled"]), rtt_ms=avail.rtt_ms if avail else None,
            upload_kbps=avail.upload_kbps if avail else None, cpu_score=avail.cpu_score if avail else None,
            score=round(100 * performance_score(c, req, extra), 1) if c is not None and avail else None))
    return result


#: Speed tests: one every few minutes per PC (each one sends a couple of megabytes).
_speedtests = RateLimiter()
SPEEDTEST_MAX_BYTES = 4 * 1024**2


@router.post("/hosts/{host_id}/speedtest")
async def speedtest(host_id: str, request: Request, user: CurrentUser, ctx: Ctx) -> dict[str, int]:
    """The PC sends a few random megabytes and times it: its upload speed to the VPS, which is
    what players get through the relay (docs/ARCHITECTURE.md 3.15). Nothing is kept."""
    owned_host(ctx.conn, host_id, user.user_id)
    _speedtests.check(f"speedtest:{host_id}", per_minute=0.2, burst=3)
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > SPEEDTEST_MAX_BYTES:
            raise ApiError(413, "too_large", "speed tests send at most 4 MiB")
    return {"bytes": received}


@router.put("/servers/{server_id}/hosts/{host_id}/priority", status_code=204)
async def set_priority(server_id: str, host_id: str, body: HostPriority, user: CurrentUser,
                       ctx: Ctx) -> Response:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    in_group = ctx.conn.execute(
        "SELECT 1 FROM hosts h JOIN memberships m ON m.user_id = h.user_id"
        " WHERE h.id = ? AND m.group_id = ?", (host_id, srv["group_id"])).fetchone()
    if not in_group:
        raise not_found("host")
    ctx.conn.execute(
        "INSERT INTO server_host_prefs (server_id, host_id, priority, enabled) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (server_id, host_id) DO UPDATE SET priority = excluded.priority,"
        " enabled = excluded.enabled",
        (server_id, host_id, body.priority, int(body.enabled)))
    return Response(status_code=204)
