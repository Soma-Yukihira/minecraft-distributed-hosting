"""Logical Minecraft servers: configuration, lifecycle requests, journal, metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Query

from mdh_server import access, events
from mdh_server.api.errors import ApiError, conflict, forbidden
from mdh_server.auth import tokens
from mdh_server.auth.deps import Ctx, CurrentUser, membership, server_membership
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.database.db import immediate
from mdh_server.relay import addresses
from mdh_shared.models.api import (
    Datapack,
    DeleteServerRequest,
    EventInfo,
    MetricsPoint,
    Mod,
    ServerCreate,
    ServerPatch,
    ServerStatus,
    StopRequestBody,
    TransferRequest,
    datapack_problems,
    mod_problems,
)
from mdh_shared.models.enums import Loader, MigrationReason, Role, ServerState
from mdh_shared.protocol.messages import ServerUpdate

router = APIRouter(tags=["servers"])

MAX_SERVERS_PER_GROUP = 10


def stored_mods(srv) -> list[Mod]:
    return [Mod.model_validate(m) for m in json.loads(srv["mods_json"])]


def _check_mods(loader: Loader, mc_version: str, mods: list[Mod], map_enabled: bool,
                datapacks: list[Datapack] = ()) -> None:
    problems = mod_problems(loader, mc_version, mods, map_enabled) + datapack_problems(mc_version, list(datapacks))
    if problems:
        raise ApiError(422, "mods_incompatible", "these mods cannot run on this server",
                       details={"problems": problems})


@router.post("/groups/{group_id}/servers", status_code=201)
async def create_server(group_id: str, body: ServerCreate, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    membership(ctx.conn, group_id, user.user_id).require_admin()
    count = ctx.conn.execute("SELECT COUNT(*) FROM servers WHERE group_id = ?", (group_id,)).fetchone()[0]
    if count >= MAX_SERVERS_PER_GROUP:
        raise ApiError(403, "limit_reached", f"at most {MAX_SERVERS_PER_GROUP} servers per group")
    _check_mods(body.loader, body.mc_version, body.mods, body.map_enabled, body.datapacks)
    server_id = tokens.new_id()
    now = ctx.now()
    with immediate(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO servers (id, group_id, name, mc_version, loader, memory_mb, properties_json,"
            " preemption, preemption_countdown_s, auto_failover_after_s, hot_backup_interval_s,"
            " start_on_join, stop_when_empty_after_s, public_status,"
            " allow_offline_accounts, personal_addresses, allowed_players_json, address_label,"
            " mods_json, map_enabled, resource_pack_json, modpack_json, datapacks_json, host_choice, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (server_id, group_id, body.name, body.mc_version, body.loader.value, body.memory_mb,
             json.dumps(body.properties), body.preemption.value, body.preemption_countdown_s,
             body.auto_failover_after_s,
             body.hot_backup_interval_s, int(body.start_on_join), body.stop_when_empty_after_s,
             int(body.public_status),
             int(body.allow_offline_accounts), int(body.personal_addresses),
             json.dumps(body.allowed_players), addresses.unique_label(ctx.conn, body.name),
             json.dumps([m.model_dump(mode="json") for m in body.mods]), int(body.map_enabled),
             body.resource_pack.model_dump_json() if body.resource_pack else None,
             body.modpack.model_dump_json() if body.modpack else None,
             json.dumps([d.model_dump(mode="json") for d in body.datapacks]), body.host_choice, now))
        ctx.conn.execute("INSERT INTO leases (server_id) VALUES (?)", (server_id,))
        addresses.sync_player_tokens(ctx.conn, server_id, body.allowed_players)
        events.record(ctx.conn, group_id=group_id, server_id=server_id, type="server_created",
                      now=now, actor_user_id=user.user_id, name=body.name,
                      mc_version=body.mc_version, loader=body.loader.value,
                      allow_offline_accounts=body.allow_offline_accounts)
    await ctx.hub.group_changed(ctx.conn, group_id)  # the new server appears for everyone
    return ctx.coordinator.status(server_id)


@router.get("/groups/{group_id}/servers")
async def list_servers(group_id: str, user: CurrentUser, ctx: Ctx) -> list[ServerStatus]:
    membership(ctx.conn, group_id, user.user_id)
    rows = ctx.conn.execute("SELECT id FROM servers WHERE group_id = ? ORDER BY name", (group_id,))
    return [ctx.coordinator.status(r["id"]) for r in rows.fetchall()]


@router.get("/servers/{server_id}")
async def get_server(server_id: str, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    server_membership(ctx.conn, server_id, user.user_id)
    return ctx.coordinator.status(server_id)


@router.patch("/servers/{server_id}")
async def update_server(server_id: str, body: ServerPatch, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    changes = body.model_dump(exclude_unset=True, mode="json")
    if not changes:
        return ctx.coordinator.status(server_id)
    columns = {"name": "name", "mc_version": "mc_version", "loader": "loader",
               "memory_mb": "memory_mb", "properties": "properties_json",
               "preemption": "preemption", "preemption_countdown_s": "preemption_countdown_s",
               "auto_failover_after_s": "auto_failover_after_s",
               "hot_backup_interval_s": "hot_backup_interval_s",
               "start_on_join": "start_on_join",
               "stop_when_empty_after_s": "stop_when_empty_after_s",
               "public_status": "public_status",
               "allow_offline_accounts": "allow_offline_accounts",
               "personal_addresses": "personal_addresses",
               "allowed_players": "allowed_players_json",
               "mods": "mods_json", "map_enabled": "map_enabled", "resource_pack": "resource_pack_json",
               "modpack": "modpack_json", "datapacks": "datapacks_json",
               "config_files": "config_files_json", "host_choice": "host_choice"}
    if changes.keys() & {"loader", "mc_version", "mods", "map_enabled", "datapacks"}:
        # Checked on the result: a new Minecraft version must come with mods made for it.
        _check_mods(body.loader or Loader(srv["loader"]), body.mc_version or srv["mc_version"],
                    body.mods if body.mods is not None else stored_mods(srv),
                    body.map_enabled if body.map_enabled is not None else bool(srv["map_enabled"]),
                    body.datapacks if body.datapacks is not None else
                    [Datapack.model_validate(d) for d in json.loads(srv["datapacks_json"])])
    with immediate(ctx.conn):
        for key, value in changes.items():
            if key in ("properties", "allowed_players", "mods", "datapacks", "config_files"):
                value = json.dumps(value)
            elif key in ("resource_pack", "modpack"):
                value = None if value is None else json.dumps(value)
            elif isinstance(value, bool):
                value = int(value)
            ctx.conn.execute(f"UPDATE servers SET {columns[key]} = ? WHERE id = ?", (value, server_id))
        ctx.conn.execute("UPDATE servers SET config_revision = config_revision + 1 WHERE id = ?",
                         (server_id,))
        if "allowed_players" in changes:
            addresses.sync_player_tokens(ctx.conn, server_id, changes["allowed_players"])
            access.forget_removed(ctx.conn, server_id, changes["allowed_players"])
        events.record(ctx.conn, group_id=srv["group_id"], server_id=server_id, type="server_updated",
                      now=ctx.now(), actor_user_id=user.user_id, changes=changes)
    await ctx.coordinator.broadcast(server_id)
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/delete")
async def delete_server(server_id: str, body: DeleteServerRequest, user: CurrentUser, ctx: Ctx) -> dict[str, str]:
    """Delete a server and its whole history (docs/ARCHITECTURE.md 3.18). Owner only, server stopped.

    Its blocks shared with no other server of the group are freed by the next retention pass."""
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    if m.role != Role.OWNER:
        raise forbidden("only the group owner can delete a server")
    if body.name != srv["name"]:
        raise ApiError(422, "name_mismatch", "type the server's exact name to delete it")
    now = ctx.now()
    if (ServerState(srv["state"]) != ServerState.OFFLINE or srv["desired_running"]
            or load_lease(ctx.conn, server_id).is_active(now) or ctx.coordinator.active_migration(server_id)):
        raise conflict("server_running", "stop the server before deleting it")
    with immediate(ctx.conn):
        # Versions are protected (head, pinned) and point to each other: unhook them first.
        ctx.conn.execute("UPDATE servers SET head_version_id = NULL WHERE id = ?", (server_id,))
        ctx.conn.execute("UPDATE world_versions SET pinned = 0, parent_id = NULL WHERE server_id = ?", (server_id,))
        ctx.conn.execute("DELETE FROM world_versions WHERE server_id = ?", (server_id,))
        ctx.conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))  # the rest cascades
        events.record(ctx.conn, group_id=srv["group_id"], type="server_deleted", now=now,
                      actor_user_id=user.user_id, deleted_server_id=server_id, name=srv["name"])
    ctx.coordinator.forget_server(server_id)
    await ctx.hub.send_to_group(ctx.conn, srv["group_id"],
                                ServerUpdate(server_id=server_id, state="deleted", active_host_id=None))
    await ctx.hub.group_changed(ctx.conn, srv["group_id"])
    return {"deleted": server_id}


@router.post("/servers/{server_id}/start")
async def start_server(server_id: str, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    server_membership(ctx.conn, server_id, user.user_id)
    await ctx.coordinator.request_start(server_id, user.user_id)
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/stop")
async def stop_server(server_id: str, user: CurrentUser, ctx: Ctx,
                      body: StopRequestBody | None = None) -> ServerStatus:
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    if not (m.can_host or m.role.is_admin):
        raise forbidden("only hosts and admins can stop the server")
    await ctx.coordinator.request_stop(server_id, user.user_id, (body or StopRequestBody()).countdown_s)
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/transfer", status_code=202)
async def transfer_server(server_id: str, body: TransferRequest, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    """Planned change of host. Allowed to admins, to the owner of the current host, and to a
    member who may host and asks for the server to come to one of their own PCs."""
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    holder = ctx.conn.execute("SELECT holder_host_id FROM leases WHERE server_id = ?",
                              (server_id,)).fetchone()["holder_host_id"]
    owns = {r["id"] for r in ctx.conn.execute("SELECT id FROM hosts WHERE user_id = ?", (user.user_id,))}
    allowed = (m.role.is_admin or holder in owns
               or (m.can_host and body.target_host_id is not None and body.target_host_id in owns))
    if not allowed:
        raise forbidden("only admins, the current host or the destination PC's owner can move the server")
    await ctx.coordinator.request_transfer(server_id, target_host_id=body.target_host_id,
                                           countdown_s=body.countdown_s, reason=MigrationReason.TRANSFER,
                                           actor_user_id=user.user_id)
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/transfer/cancel")
async def cancel_transfer(server_id: str, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    mig = ctx.coordinator.active_migration(server_id)
    owns = {r["id"] for r in ctx.conn.execute("SELECT id FROM hosts WHERE user_id = ?", (user.user_id,))}
    involved = mig is not None and (mig["source_host_id"] in owns or mig["target_host_id"] in owns
                                    or mig["actor_user_id"] == user.user_id)
    if not (m.role.is_admin or involved):
        raise forbidden("only admins and the PCs involved can cancel a change of host")
    await ctx.coordinator.cancel_transfer(server_id, user.user_id)
    return ctx.coordinator.status(server_id)


@router.post("/servers/{server_id}/failover/confirm")
async def confirm_failover(server_id: str, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    _, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    await ctx.coordinator.confirm_failover(server_id, user.user_id)
    return ctx.coordinator.status(server_id)


@router.get("/servers/{server_id}/events")
async def server_events(server_id: str, user: CurrentUser, ctx: Ctx,
                        before: int | None = Query(default=None, ge=1),
                        limit: int = Query(default=50, ge=1, le=200)) -> list[EventInfo]:
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    rows = events.list_events(ctx.conn, group_id=srv["group_id"], server_id=server_id,
                              before=before, limit=limit)
    return [EventInfo(event_id=r["id"], type=r["type"], server_id=r["server_id"], host_id=r["host_id"],
                      actor_user_id=r["actor_user_id"], payload=json.loads(r["payload_json"]),
                      created_at=datetime.fromtimestamp(r["created_at"], UTC)) for r in rows]


@router.get("/servers/{server_id}/config-files")
async def config_files(server_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, str]:
    """The settings files the group manages, for the hosting PC and the admins' editor."""
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    return json.loads(srv["config_files_json"])


@router.get("/servers/{server_id}/metrics")
async def server_metrics(server_id: str, user: CurrentUser, ctx: Ctx) -> list[MetricsPoint]:
    server_membership(ctx.conn, server_id, user.user_id)
    return [
        MetricsPoint(at=datetime.fromtimestamp(t, UTC), host_id=host, cpu_percent=m.cpu_percent,
                     ram_used_mb=m.ram_used_mb, players_online=m.players_online, tps=m.tps, mspt=m.mspt)
        for t, host, m in ctx.coordinator.metrics.get(server_id, ())
    ]
