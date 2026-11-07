"""FastAPI dependencies: context, current user, group permissions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from mdh_server.api.errors import ApiError, forbidden, not_found
from mdh_server.auth.tokens import AuthenticatedUser, authenticate
from mdh_server.context import AppContext
from mdh_shared.models.enums import Role


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


async def current_user(request: Request, ctx: Ctx) -> AuthenticatedUser:
    token = bearer_token(request)
    user = authenticate(ctx.conn, token, ctx.now()) if token else None
    if user is None:
        raise ApiError(401, "unauthorized", "missing, invalid or expired access token",
                       headers={"WWW-Authenticate": "Bearer"})
    # Generous per-user limit; abusive clients get throttled before hurting the VPS.
    if "/blobs" in request.url.path:
        # World blocks: one request per MiB, so a 2 GB world is 2000 requests. Already bounded by
        # the lease (uploads), the group quota and the block size; found by the load test.
        ctx.limiter.check(f"blocks:{user.user_id}", per_minute=6000, burst=600)
    else:
        ctx.limiter.check(f"user:{user.user_id}", per_minute=600, burst=120)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]


@dataclass(frozen=True)
class Membership:
    group_id: str
    user_id: str
    role: Role
    can_host: bool

    def require_admin(self) -> None:
        if not self.role.is_admin:
            raise forbidden("group admin rights required")

    def require_moderator(self) -> None:
        if not self.role.can_moderate:
            raise forbidden("group moderator rights required")


def membership(conn: sqlite3.Connection, group_id: str, user_id: str) -> Membership:
    """Return the caller's membership, or 404 (never reveal groups the caller is not in)."""
    row = conn.execute(
        "SELECT role, can_host FROM memberships WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    if row is None:
        raise not_found("group")
    return Membership(group_id, user_id, Role(row["role"]), bool(row["can_host"]))


def server_membership(conn: sqlite3.Connection, server_id: str, user_id: str) -> tuple[sqlite3.Row, Membership]:
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    if row is None:
        raise not_found("server")
    try:
        return row, membership(conn, row["group_id"], user_id)
    except ApiError:
        raise not_found("server") from None


def owned_host(conn: sqlite3.Connection, host_id: str, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM hosts WHERE id = ? AND user_id = ?", (host_id, user_id)).fetchone()
    if row is None:
        raise not_found("host")
    return row
