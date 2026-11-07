"""Registration, login, token rotation."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request, Response

from mdh_server import events
from mdh_server.api.errors import ApiError
from mdh_server.api.ratelimit import client_ip
from mdh_server.auth import tokens
from mdh_server.auth.deps import Ctx, CurrentUser, bearer_token
from mdh_server.database.db import immediate
from mdh_shared.models.api import (
    GroupInfo,
    LoginRequest,
    PasswordChange,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserInfo,
)
from mdh_shared.models.enums import Role

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])


def _pair(issued: tokens.IssuedTokens) -> TokenPair:
    return TokenPair(access_token=issued.access_token, refresh_token=issued.refresh_token,
                     expires_in=issued.expires_in)


def _token_error(exc: tokens.TokenError) -> ApiError:
    return ApiError(401, exc.code, str(exc))


def find_valid_invite(conn: sqlite3.Connection, code: str, now: float) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invites WHERE code_hash = ? AND expires_at > ? AND uses < max_uses",
        (tokens.token_hash(code), now),
    ).fetchone()


def consume_invite(conn: sqlite3.Connection, invite: sqlite3.Row, user_id: str, now: float) -> None:
    """Join the invite's group. Must run inside a transaction."""
    conn.execute("UPDATE invites SET uses = uses + 1 WHERE code_hash = ?", (invite["code_hash"],))
    conn.execute(
        "INSERT INTO memberships (group_id, user_id, role, can_host, joined_at) VALUES (?, ?, ?, 1, ?)",
        (invite["group_id"], user_id, Role.MEMBER.value, now),
    )
    events.record(conn, group_id=invite["group_id"], type="member_joined", now=now,
                  actor_user_id=user_id)


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, request: Request, ctx: Ctx) -> dict[str, str]:
    ip = client_ip(request, ctx.settings.trusted_proxies)
    now = ctx.now()
    # Refusals that cost nothing and reveal nothing do not count against the limits below:
    # forgetting the invitation code a few times must not lock someone out for an hour.
    if ctx.settings.registration_mode == "invite" and not body.invite_code:
        raise ApiError(403, "invite_required", "an invitation code is required to register")
    if body.invite_code:
        ctx.limiter.check(f"invite-guess:{ip}", per_minute=10, burst=10)  # no code guessing
        if find_valid_invite(ctx.conn, body.invite_code, now) is None:
            raise ApiError(403, "invalid_invite", "invitation code invalid, expired or used up")
    # Only an attempt that may create an account counts (and costs a password hash).
    ctx.limiter.check(f"register:{ip}", per_minute=2, burst=5)  # then one every 30 s
    password_hash = await ctx.passwords.hash(body.password)
    user_id = tokens.new_id()
    # Re-check everything after the await: state may have changed meanwhile.
    with immediate(ctx.conn):
        invite = find_valid_invite(ctx.conn, body.invite_code, now) if body.invite_code else None
        if body.invite_code and invite is None:
            raise ApiError(403, "invalid_invite", "invitation code invalid, expired or used up")
        try:
            ctx.conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, body.username, password_hash, now),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "username_taken", "this username is already taken") from None
        if invite is not None:
            consume_invite(ctx.conn, invite, user_id, now)
    if invite is not None:
        await ctx.hub.group_changed(ctx.conn, invite["group_id"])
    return {"user_id": user_id}


@router.post("/login")
async def login(body: LoginRequest, request: Request, ctx: Ctx) -> TokenPair:
    ip = client_ip(request, ctx.settings.trusted_proxies)
    ctx.limiter.check(f"login-ip:{ip}", per_minute=10, burst=5)
    ctx.limiter.check(f"login-user:{body.username.lower()}", per_minute=10, burst=5)
    row = ctx.conn.execute(
        "SELECT id, password_hash FROM users WHERE lower(username) = lower(?) AND disabled = 0",
        (body.username,),
    ).fetchone()
    if not await ctx.passwords.verify(row["password_hash"] if row else None, body.password):
        raise ApiError(401, "invalid_credentials", "wrong username or password")
    if ctx.passwords.needs_rehash(row["password_hash"]):
        new_hash = await ctx.passwords.hash(body.password)
        ctx.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row["id"]))
    issued = tokens.login(ctx.conn, row["id"], ctx.now(),
                          access_ttl_s=ctx.settings.access_token_ttl_s,
                          refresh_ttl_s=ctx.settings.refresh_token_ttl_s)
    return _pair(issued)


@router.post("/refresh")
async def refresh(body: RefreshRequest, request: Request, ctx: Ctx) -> TokenPair:
    ctx.limiter.check(f"refresh:{client_ip(request, ctx.settings.trusted_proxies)}",
                      per_minute=30, burst=10)
    try:
        issued = tokens.rotate(ctx.conn, body.refresh_token, ctx.now(),
                               access_ttl_s=ctx.settings.access_token_ttl_s,
                               refresh_ttl_s=ctx.settings.refresh_token_ttl_s)
    except tokens.TokenError as exc:
        raise _token_error(exc) from None
    return _pair(issued)


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest, ctx: Ctx) -> Response:
    tokens.logout(ctx.conn, body.refresh_token, ctx.now())
    return Response(status_code=204)


@me_router.post("/me/password", status_code=204)
async def change_password(body: PasswordChange, request: Request, user: CurrentUser, ctx: Ctx) -> Response:
    """New password; every other session of this account (other PCs) must log in again."""
    ctx.limiter.check(f"login-user:{user.username.lower()}", per_minute=10, burst=5)
    row = ctx.conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.user_id,)).fetchone()
    if not await ctx.passwords.verify(row["password_hash"], body.current_password):
        raise ApiError(401, "invalid_credentials", "wrong current password")
    new_hash = await ctx.passwords.hash(body.new_password)
    keep = tokens.family_of(ctx.conn, bearer_token(request) or "")
    with immediate(ctx.conn):
        ctx.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user.user_id))
        tokens.revoke_user(ctx.conn, user.user_id, ctx.now(), keep_family=keep)
    return Response(status_code=204)


@me_router.get("/me")
async def me(user: CurrentUser, ctx: Ctx) -> UserInfo:
    rows = ctx.conn.execute(
        "SELECT g.id, g.name, m.role, m.can_host FROM memberships m JOIN groups g ON g.id = m.group_id"
        " WHERE m.user_id = ? ORDER BY g.name",
        (user.user_id,),
    ).fetchall()
    return UserInfo(
        user_id=user.user_id,
        username=user.username,
        groups=[GroupInfo(group_id=r["id"], name=r["name"], role=Role(r["role"]),
                          can_host=bool(r["can_host"])) for r in rows],
    )
