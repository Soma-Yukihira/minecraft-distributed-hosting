"""Running a game from any PC of the group (docs/API.md section 4 bis).

Admins send actions from a closed list (announce, kick, ban, pardon, op, deop, restart...):
the control plane hands them to the lease holder, which maps each one to a fixed Minecraft
command. There is no way to send a free command. The holder reports who is connected, the
last chat lines and the console (read-only for the others); all of it is kept in memory only,
never written to disk.
"""

from __future__ import annotations

import collections
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict

from mdh_server import events
from mdh_server.api.errors import conflict
from mdh_server.auth.deps import Ctx, CurrentUser, owned_host, server_membership
from mdh_server.coordination import lease as lease_rules
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.database.db import immediate
from mdh_shared.models.api import (ActivityReport, ChatLine, ConsoleLine, ConsoleLog, ConsoleReport, ServerActivity,
                                   TransferAddress, TransferAddressesRequest)
from mdh_shared.models.enums import HostRuntimeState
from mdh_shared.protocol.messages import ActionMessage, ServerAction

router = APIRouter(tags=["admin"])

MAX_LINES = 100
#: Console lines kept per server (a modded start writes a few hundred).
MAX_CONSOLE_LINES = 1000
#: What moderators may do: run the game, never change who is operator or allowed.
MODERATOR_ACTIONS = frozenset({"announce", "kick", "ban", "pardon", "ban_ip"})


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ServerAction


class Activity:
    def __init__(self) -> None:
        self.players: list[str] = []
        self.lines: collections.deque[ChatLine] = collections.deque(maxlen=MAX_LINES)
        self.updated_at: float | None = None


def _activity(ctx, server_id: str) -> Activity:
    return ctx.coordinator.activity.setdefault(server_id, Activity())


class Console:
    def __init__(self) -> None:
        #: (number, line); numbers keep growing across hosts, so ``after`` stays meaningful.
        self.lines: collections.deque[tuple[int, ConsoleLine]] = collections.deque(maxlen=MAX_CONSOLE_LINES)
        self.seq = 0
        self.epoch = 0


def _console(ctx, server_id: str) -> Console:
    return ctx.coordinator.console.setdefault(server_id, Console())


@router.post("/servers/{server_id}/actions", status_code=202)
async def send_action(server_id: str, body: ActionRequest, user: CurrentUser, ctx: Ctx) -> dict[str, str]:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    if body.action.kind in MODERATOR_ACTIONS:
        m.require_moderator()
    else:
        m.require_admin()  # op, whitelist and restart change who may do what, or the server itself
    lease = load_lease(ctx.conn, server_id)
    if (not lease.is_active(ctx.now()) or lease.holder is None
            or ctx.coordinator.runtime.get(server_id) != HostRuntimeState.RUNNING):
        raise conflict("not_running", "the server is not running")
    action_id = uuid.uuid4().hex
    if not await ctx.hub.send_to_host(lease.holder, ActionMessage(server_id=server_id, action_id=action_id,
                                                                 action=body.action)):
        raise conflict("host_unreachable", "the hosting PC cannot be reached right now")
    detail = body.action.model_dump(exclude={"message"})
    with immediate(ctx.conn):
        events.record(ctx.conn, group_id=srv["group_id"], server_id=server_id, type="admin_action",
                      now=ctx.now(), actor_user_id=user.user_id, host_id=lease.holder, **detail)
    return {"action_id": action_id}


@router.post("/servers/{server_id}/activity", status_code=204)
async def report_activity(server_id: str, body: ActivityReport, user: CurrentUser, ctx: Ctx) -> Response:
    server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    lease_rules.check_fence(load_lease(ctx.conn, server_id), body.host_id, body.epoch, ctx.now())
    activity = _activity(ctx, server_id)
    activity.players = sorted(set(body.players), key=str.lower)
    activity.lines.extend(body.lines)
    activity.updated_at = ctx.now()
    return Response(status_code=204)


@router.post("/servers/{server_id}/transfer-addresses")
async def transfer_addresses(server_id: str, body: TransferAddressesRequest, user: CurrentUser,
                             ctx: Ctx) -> list[TransferAddress]:
    """For the lease holder about to hand the server over: where to /transfer each player in game
    (docs/ARCHITECTURE.md 4.18). One-time relay addresses, never a player's personal address."""
    server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    lease_rules.check_fence(load_lease(ctx.conn, server_id), body.host_id, body.epoch, ctx.now())
    return ctx.relay.transfer_addresses(server_id)


@router.post("/servers/{server_id}/console", status_code=204)
async def report_console(server_id: str, body: ConsoleReport, user: CurrentUser, ctx: Ctx) -> Response:
    server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    lease_rules.check_fence(load_lease(ctx.conn, server_id), body.host_id, body.epoch, ctx.now())
    console = _console(ctx, server_id)
    if body.epoch != console.epoch:  # another host (or another lease): another console
        console.lines.clear()
        console.epoch = body.epoch
    for line in body.lines:
        console.seq += 1
        console.lines.append((console.seq, line))
    return Response(status_code=204)


@router.get("/servers/{server_id}/console")
async def get_console(server_id: str, user: CurrentUser, ctx: Ctx, after: int = Query(default=0, ge=0)) -> ConsoleLog:
    """Read-only: what the server printed, for those who run the game (admins, moderators)."""
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_moderator()
    console = _console(ctx, server_id)
    if after > console.seq:
        after = 0  # the control plane restarted: everything it has is new to this reader
    return ConsoleLog(lines=[line for n, line in console.lines if n > after], next=console.seq,
                      epoch=console.epoch)


@router.get("/servers/{server_id}/activity")
async def get_activity(server_id: str, user: CurrentUser, ctx: Ctx) -> ServerActivity:
    server_membership(ctx.conn, server_id, user.user_id)
    activity = _activity(ctx, server_id)
    running = load_lease(ctx.conn, server_id).is_active(ctx.now())
    return ServerActivity(players=activity.players if running else [], lines=list(activity.lines),
                          updated_at=datetime.fromtimestamp(activity.updated_at, UTC)
                          if activity.updated_at else None)
