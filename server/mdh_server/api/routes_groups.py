"""Groups, invitations and members."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Response

from mdh_server import events
from mdh_server.api.errors import ApiError, conflict, forbidden
from mdh_server.api.routes_auth import consume_invite, find_valid_invite
from mdh_server.auth import tokens
from mdh_server.auth.deps import Ctx, CurrentUser, membership
from mdh_server.database.db import immediate
from mdh_shared.models.api import (
    GroupCreate,
    GroupInfo,
    HostInfo,
    InviteCreate,
    InviteInfo,
    JoinRequest,
    MemberInfo,
    MemberUpdate,
)
from mdh_shared.models.enums import Role

router = APIRouter(tags=["groups"])

MAX_GROUPS_PER_USER = 20


@router.post("/groups", status_code=201)
async def create_group(body: GroupCreate, user: CurrentUser, ctx: Ctx) -> GroupInfo:
    owned = ctx.conn.execute("SELECT COUNT(*) FROM groups WHERE owner_id = ?",
                             (user.user_id,)).fetchone()[0]
    if owned >= MAX_GROUPS_PER_USER:
        raise ApiError(403, "limit_reached", f"at most {MAX_GROUPS_PER_USER} groups per user")
    group_id = tokens.new_id()
    now = ctx.now()
    with immediate(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO groups (id, name, owner_id, created_at, storage_quota_bytes) VALUES (?, ?, ?, ?, ?)",
            (group_id, body.name, user.user_id, now, ctx.settings.default_group_quota_bytes))
        ctx.conn.execute(
            "INSERT INTO memberships (group_id, user_id, role, can_host, joined_at) VALUES (?, ?, 'owner', 1, ?)",
            (group_id, user.user_id, now))
        events.record(ctx.conn, group_id=group_id, type="group_created", now=now,
                      actor_user_id=user.user_id, name=body.name)
    return GroupInfo(group_id=group_id, name=body.name, role=Role.OWNER, can_host=True)


@router.get("/groups")
async def list_groups(user: CurrentUser, ctx: Ctx) -> list[GroupInfo]:
    rows = ctx.conn.execute(
        "SELECT g.id, g.name, m.role, m.can_host FROM memberships m JOIN groups g ON g.id = m.group_id"
        " WHERE m.user_id = ? ORDER BY g.name", (user.user_id,)).fetchall()
    return [GroupInfo(group_id=r["id"], name=r["name"], role=Role(r["role"]),
                      can_host=bool(r["can_host"])) for r in rows]


@router.get("/groups/{group_id}")
async def get_group(group_id: str, user: CurrentUser, ctx: Ctx) -> GroupInfo:
    m = membership(ctx.conn, group_id, user.user_id)
    name = ctx.conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()["name"]
    return GroupInfo(group_id=group_id, name=name, role=m.role, can_host=m.can_host)


@router.post("/groups/{group_id}/invites", status_code=201)
async def create_invite(group_id: str, body: InviteCreate, user: CurrentUser, ctx: Ctx) -> InviteInfo:
    membership(ctx.conn, group_id, user.user_id).require_admin()
    code = secrets.token_urlsafe(12)
    now = ctx.now()
    expires_at = now + body.expires_in_s
    with immediate(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO invites (code_hash, group_id, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?, ?)",
            (tokens.token_hash(code), group_id, user.user_id, body.max_uses, expires_at))
        events.record(ctx.conn, group_id=group_id, type="invite_created", now=now,
                      actor_user_id=user.user_id, max_uses=body.max_uses)
    return InviteInfo(code=code, expires_at=datetime.fromtimestamp(expires_at, UTC),
                      max_uses=body.max_uses)


@router.post("/groups/join")
async def join_group(body: JoinRequest, user: CurrentUser, ctx: Ctx) -> GroupInfo:
    now = ctx.now()
    with immediate(ctx.conn):
        invite = find_valid_invite(ctx.conn, body.code, now)
        if invite is None:
            raise ApiError(403, "invalid_invite", "invitation code invalid, expired or used up")
        already = ctx.conn.execute("SELECT 1 FROM memberships WHERE group_id = ? AND user_id = ?",
                                   (invite["group_id"], user.user_id)).fetchone()
        if already:
            raise conflict("already_member", "you are already a member of this group")
        consume_invite(ctx.conn, invite, user.user_id, now)
    await ctx.hub.group_changed(ctx.conn, invite["group_id"])
    return await get_group(invite["group_id"], user, ctx)


@router.get("/groups/{group_id}/members")
async def list_members(group_id: str, user: CurrentUser, ctx: Ctx) -> list[MemberInfo]:
    membership(ctx.conn, group_id, user.user_id)
    members = ctx.conn.execute(
        "SELECT u.id, u.username, m.role, m.can_host FROM memberships m JOIN users u ON u.id = m.user_id"
        " WHERE m.group_id = ? ORDER BY u.username", (group_id,)).fetchall()
    result = []
    for m in members:
        hosts = ctx.conn.execute("SELECT * FROM hosts WHERE user_id = ? ORDER BY display_name",
                                 (m["id"],)).fetchall()
        result.append(MemberInfo(
            user_id=m["id"], username=m["username"], role=Role(m["role"]), can_host=bool(m["can_host"]),
            hosts=[host_info(ctx, h) for h in hosts]))
    return result


def host_info(ctx, row) -> HostInfo:
    return HostInfo(
        host_id=row["id"], display_name=row["display_name"], online=ctx.hub.is_online(row["id"]),
        hosting_allowed=bool(row["hosting_allowed"]), auto_accept_offers=bool(row["auto_accept_offers"]),
        max_ram_mb=row["max_ram_mb"], cpu_cores=row["cpu_cores"], total_ram_mb=row["total_ram_mb"])


@router.patch("/groups/{group_id}/members/{member_id}")
async def update_member(group_id: str, member_id: str, body: MemberUpdate, user: CurrentUser,
                        ctx: Ctx) -> Response:
    caller = membership(ctx.conn, group_id, user.user_id)
    caller.require_admin()
    target = membership(ctx.conn, group_id, member_id)
    if target.role == Role.OWNER and member_id != user.user_id:
        raise forbidden("only the owner can change the owner's membership")
    if body.role == Role.OWNER:
        raise forbidden("ownership transfer is not supported yet")
    if target.role == Role.OWNER and body.role is not None:
        raise forbidden("the owner cannot demote themselves; the group needs an owner")
    if body.role == Role.ADMIN and caller.role != Role.OWNER:
        raise forbidden("only the owner can promote admins")
    if (target.role == Role.ADMIN and body.role not in (None, Role.ADMIN) and caller.role != Role.OWNER
            and member_id != user.user_id):
        raise forbidden("only the owner can demote an admin")
    with immediate(ctx.conn):
        if body.role is not None:
            ctx.conn.execute("UPDATE memberships SET role = ? WHERE group_id = ? AND user_id = ?",
                             (body.role.value, group_id, member_id))
        if body.can_host is not None:
            ctx.conn.execute("UPDATE memberships SET can_host = ? WHERE group_id = ? AND user_id = ?",
                             (int(body.can_host), group_id, member_id))
        events.record(ctx.conn, group_id=group_id, type="member_updated", now=ctx.now(),
                      actor_user_id=user.user_id, member_id=member_id,
                      **body.model_dump(exclude_none=True, mode="json"))
    await ctx.hub.group_changed(ctx.conn, group_id)  # the member's application shows the new rights
    return Response(status_code=204)


@router.delete("/groups/{group_id}/members/{member_id}", status_code=204)
async def remove_member(group_id: str, member_id: str, user: CurrentUser, ctx: Ctx) -> Response:
    caller = membership(ctx.conn, group_id, user.user_id)
    if member_id != user.user_id:
        caller.require_admin()
    target = membership(ctx.conn, group_id, member_id)
    if target.role == Role.OWNER:
        raise forbidden("the owner cannot leave or be removed from the group")
    if target.role == Role.ADMIN and caller.role != Role.OWNER and member_id != user.user_id:
        raise forbidden("only the owner can remove an admin")
    hosting = ctx.conn.execute(
        "SELECT 1 FROM leases l JOIN servers s ON s.id = l.server_id JOIN hosts h ON h.id = l.holder_host_id"
        " WHERE s.group_id = ? AND h.user_id = ?", (group_id, member_id)).fetchone()
    if hosting:
        # Phase 5 will migrate automatically; until then, never cut a running host off.
        raise conflict("member_is_hosting", "this member is hosting a server: stop or transfer it first")
    with immediate(ctx.conn):
        ctx.conn.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?",
                         (group_id, member_id))
        events.record(ctx.conn, group_id=group_id, type="member_removed", now=ctx.now(),
                      actor_user_id=user.user_id, member_id=member_id)
    await ctx.hub.group_changed(ctx.conn, group_id, member_id)
    return Response(status_code=204)
