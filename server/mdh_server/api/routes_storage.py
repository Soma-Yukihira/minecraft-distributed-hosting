"""Blocks, world versions, restore, storage usage (docs/API.md sections 6-7)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from mdh_server import events
from mdh_server.api.errors import ApiError, conflict, forbidden, not_found
from mdh_server.auth.deps import Ctx, CurrentUser, membership, owned_host, server_membership
from mdh_server.coordination import lease as lease_rules
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.database.db import immediate
from mdh_server.storage.store import MAX_COMPRESSED_BLOCK, StorageError
from mdh_shared.constants import WORLD_DIRS
from mdh_shared.models.api import (
    MissingBlobsRequest,
    MissingBlobsResponse,
    NewWorldRequest,
    PinRequest,
    RestoreRequest,
    ServerStatus,
    ServerStorage,
    StorageUsage,
    VersionCommit,
    VersionInfo,
    level_type_value,
)
from mdh_shared.models.enums import ServerState, VersionKind
from mdh_shared.models.manifest import Manifest

router = APIRouter(tags=["storage"])


def _storage_error(exc: StorageError) -> ApiError:
    status = {"hash_mismatch": 422, "invalid_block": 422, "too_large": 413, "invalid_id": 422,
              "corrupted": 500}.get(exc.code, 400)
    return ApiError(status, exc.code, str(exc))


def _require_host_member(ctx, group_id: str, user_id: str):
    m = membership(ctx.conn, group_id, user_id)
    if not (m.can_host or m.role.is_admin):
        raise forbidden("only members allowed to host can access world data")
    return m


def used_bytes(conn, group_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(stored_size), 0) FROM blobs WHERE group_id = ?",
                        (group_id,)).fetchone()[0]


def version_info(row) -> VersionInfo:
    return VersionInfo(version_id=row["id"], parent_version_id=row["parent_id"], kind=VersionKind(row["kind"]),
                       epoch=row["epoch"], host_id=row["host_id"], manifest_sha256=row["manifest_sha256"],
                       total_size=row["total_size"], created_at=datetime.fromtimestamp(row["created_at"], UTC),
                       pinned=bool(row["pinned"]), label=row["label"])


# ---------------------------------------------------------------------------- blocks

@router.post("/groups/{group_id}/blobs/missing")
async def missing_blobs(group_id: str, body: MissingBlobsRequest, user: CurrentUser, ctx: Ctx) -> MissingBlobsResponse:
    _require_host_member(ctx, group_id, user.user_id)
    wanted = list(dict.fromkeys(body.hashes))
    present: set[str] = set()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        present |= {r[0] for r in ctx.conn.execute(
            f"SELECT sha256 FROM blobs WHERE group_id = ? AND sha256 IN ({','.join('?' * len(chunk))})",
            (group_id, *chunk))}
    return MissingBlobsResponse(missing=[h for h in wanted if h not in present])


@router.put("/groups/{group_id}/blobs/{sha256}", status_code=204)
async def put_blob(group_id: str, sha256: str, request: Request, user: CurrentUser, ctx: Ctx,
                   server_id: str = Query(), host_id: str = Query(), epoch: int = Query(ge=1)) -> None:
    """Upload one block. Only the current lease holder of a server of this group may write."""
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    if srv["group_id"] != group_id:
        raise not_found("server")
    owned_host(ctx.conn, host_id, user.user_id)
    lease_rules.check_fence(load_lease(ctx.conn, server_id), host_id, epoch, ctx.now())
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_COMPRESSED_BLOCK:
        raise ApiError(413, "too_large", "compressed block too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_COMPRESSED_BLOCK:
            raise ApiError(413, "too_large", "compressed block too large")
    if ctx.conn.execute("SELECT 1 FROM blobs WHERE group_id = ? AND sha256 = ?", (group_id, sha256)).fetchone():
        return None
    quota = ctx.conn.execute("SELECT storage_quota_bytes FROM groups WHERE id = ?", (group_id,)).fetchone()[0]
    if used_bytes(ctx.conn, group_id) + len(body) > quota:
        raise ApiError(507, "quota_exceeded", "the group's storage quota on the VPS is full")
    try:
        # Decompression, hashing and fsync happen off the event loop.
        size, stored = await run_in_threadpool(ctx.store.put_blob, group_id, sha256, bytes(body))
    except StorageError as exc:
        raise _storage_error(exc) from None
    ctx.conn.execute("INSERT OR IGNORE INTO blobs (group_id, sha256, size, stored_size, created_at)"
                     " VALUES (?, ?, ?, ?, ?)", (group_id, sha256, size, stored, ctx.now()))
    return None


@router.get("/groups/{group_id}/blobs/{sha256}")
async def get_blob(group_id: str, sha256: str, user: CurrentUser, ctx: Ctx) -> FileResponse:
    _require_host_member(ctx, group_id, user.user_id)
    try:
        path = ctx.store.blob_path(group_id, sha256)
    except StorageError as exc:
        raise _storage_error(exc) from None
    if not path.is_file():
        raise not_found("block")
    return FileResponse(path, media_type="application/zstd")


# ---------------------------------------------------------------------------- versions

@router.post("/servers/{server_id}/versions", status_code=201)
async def commit_version(server_id: str, body: VersionCommit, user: CurrentUser, ctx: Ctx) -> VersionInfo:
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    owned_host(ctx.conn, body.host_id, user.user_id)
    if body.kind == VersionKind.RESTORE:
        raise ApiError(422, "invalid_kind", "restores go through /restore")
    gid = srv["group_id"]
    hashes = sorted(body.manifest.block_hashes())
    present = 0
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        present += ctx.conn.execute(
            f"SELECT COUNT(*) FROM blobs WHERE group_id = ? AND sha256 IN ({','.join('?' * len(chunk))})",
            (gid, *chunk)).fetchone()[0]
    if present != len(hashes):
        raise ApiError(422, "missing_blobs", f"{len(hashes) - present} blocks are not on the VPS yet")
    digest = await run_in_threadpool(ctx.store.put_manifest, gid, body.manifest)
    now = ctx.now()
    version_id = uuid.uuid4().hex
    with immediate(ctx.conn):
        # Fencing + compare-and-swap, in the same transaction as the head move.
        lease_rules.check_fence(load_lease(ctx.conn, server_id), body.host_id, body.epoch, now)
        head = ctx.conn.execute("SELECT head_version_id FROM servers WHERE id = ?", (server_id,)).fetchone()[0]
        if body.parent_version_id != head:
            raise conflict("parent_mismatch", "the world changed since this copy was taken (head moved)")
        ctx.conn.execute(
            "INSERT INTO world_versions (id, server_id, parent_id, epoch, host_id, kind, manifest_sha256,"
            " total_size, created_at, label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, server_id, head, body.epoch, body.host_id, body.kind.value, digest,
             body.manifest.total_size, now, body.label))
        ctx.conn.executemany("INSERT INTO version_blobs (version_id, sha256) VALUES (?, ?)",
                             [(version_id, h) for h in hashes])
        ctx.conn.execute("UPDATE servers SET head_version_id = ? WHERE id = ?", (version_id, server_id))
        events.record(ctx.conn, group_id=gid, server_id=server_id, type="version_committed", now=now,
                      host_id=body.host_id, version_id=version_id, kind=body.kind.value,
                      size=body.manifest.total_size, files=len(body.manifest.files))
    await ctx.coordinator.broadcast(server_id)
    row = ctx.conn.execute("SELECT * FROM world_versions WHERE id = ?", (version_id,)).fetchone()
    return version_info(row)


@router.get("/servers/{server_id}/versions")
async def list_versions(server_id: str, user: CurrentUser, ctx: Ctx,
                        limit: int = Query(default=100, ge=1, le=500)) -> list[VersionInfo]:
    server_membership(ctx.conn, server_id, user.user_id)
    rows = ctx.conn.execute("SELECT * FROM world_versions WHERE server_id = ? ORDER BY created_at DESC LIMIT ?",
                            (server_id, limit)).fetchall()
    return [version_info(r) for r in rows]


@router.get("/servers/{server_id}/versions/{version_id}/manifest")
async def get_manifest(server_id: str, version_id: str, user: CurrentUser, ctx: Ctx):
    srv, _ = server_membership(ctx.conn, server_id, user.user_id)
    _require_host_member(ctx, srv["group_id"], user.user_id)
    row = ctx.conn.execute("SELECT manifest_sha256 FROM world_versions WHERE id = ? AND server_id = ?",
                           (version_id, server_id)).fetchone()
    if row is None:
        raise not_found("version")
    try:
        manifest = await run_in_threadpool(ctx.store.read_manifest, srv["group_id"], row[0])
    except StorageError as exc:
        raise _storage_error(exc) from None
    return json.loads(manifest.canonical_bytes())


@router.post("/servers/{server_id}/versions/{version_id}/pin")
async def pin_version(server_id: str, version_id: str, body: PinRequest, user: CurrentUser, ctx: Ctx) -> VersionInfo:
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    with immediate(ctx.conn):
        cur = ctx.conn.execute("UPDATE world_versions SET pinned = ?, label = COALESCE(?, label)"
                               " WHERE id = ? AND server_id = ?", (int(body.pinned), body.label, version_id, server_id))
        if cur.rowcount == 0:
            raise not_found("version")
        events.record(ctx.conn, group_id=srv["group_id"], server_id=server_id, type="version_pinned",
                      now=ctx.now(), actor_user_id=user.user_id, version_id=version_id, pinned=body.pinned)
    return version_info(ctx.conn.execute("SELECT * FROM world_versions WHERE id = ?", (version_id,)).fetchone())


@router.post("/servers/{server_id}/restore", status_code=201)
async def restore(server_id: str, body: RestoreRequest, user: CurrentUser, ctx: Ctx) -> VersionInfo:
    """Make an older version the new head. History is never rewritten: a new version is added."""
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    now = ctx.now()
    lease = load_lease(ctx.conn, server_id)
    if ServerState(srv["state"]) != ServerState.OFFLINE or srv["desired_running"] or lease.is_active(now):
        raise conflict("server_running", "stop the server before restoring a backup")
    source = ctx.conn.execute("SELECT * FROM world_versions WHERE id = ? AND server_id = ?",
                              (body.version_id, server_id)).fetchone()
    if source is None:
        raise not_found("version")
    version_id = uuid.uuid4().hex
    with immediate(ctx.conn):
        head = ctx.conn.execute("SELECT head_version_id FROM servers WHERE id = ?", (server_id,)).fetchone()[0]
        ctx.conn.execute(
            "INSERT INTO world_versions (id, server_id, parent_id, epoch, host_id, kind, manifest_sha256,"
            " total_size, created_at, label) VALUES (?, ?, ?, ?, NULL, 'restore', ?, ?, ?, ?)",
            (version_id, server_id, head, lease.epoch, source["manifest_sha256"], source["total_size"], now,
             f"Restauration de {datetime.fromtimestamp(source['created_at'], UTC):%Y-%m-%d %H:%M} UTC"))
        ctx.conn.execute("INSERT INTO version_blobs (version_id, sha256)"
                         " SELECT ?, sha256 FROM version_blobs WHERE version_id = ?", (version_id, source["id"]))
        ctx.conn.execute("UPDATE servers SET head_version_id = ? WHERE id = ?", (version_id, server_id))
        events.record(ctx.conn, group_id=srv["group_id"], server_id=server_id, type="version_restored",
                      now=now, actor_user_id=user.user_id, version_id=version_id, source_version_id=source["id"])
    await ctx.coordinator.broadcast(server_id)
    return version_info(ctx.conn.execute("SELECT * FROM world_versions WHERE id = ?", (version_id,)).fetchone())


@router.post("/servers/{server_id}/new-world")
async def new_world(server_id: str, body: NewWorldRequest, user: CurrentUser, ctx: Ctx) -> ServerStatus:
    """Start over with a new world (docs/ARCHITECTURE.md 3.17).

    Like a restore, nothing is deleted: a new head is added that keeps every file except the world
    folders (ops, bans, mod and plugin settings stay), so the old world can be restored."""
    srv, m = server_membership(ctx.conn, server_id, user.user_id)
    m.require_admin()
    now = ctx.now()
    lease = load_lease(ctx.conn, server_id)
    if ServerState(srv["state"]) != ServerState.OFFLINE or srv["desired_running"] or lease.is_active(now):
        raise conflict("server_running", "stop the server before starting a new world")
    head_id = srv["head_version_id"]
    kept: Manifest | None = None
    if head_id:
        sha = ctx.conn.execute("SELECT manifest_sha256 FROM world_versions WHERE id = ?", (head_id,)).fetchone()[0]
        try:
            head = await run_in_threadpool(ctx.store.read_manifest, srv["group_id"], sha)
        except StorageError as exc:
            raise _storage_error(exc) from None
        kept = Manifest(files=tuple(f for f in head.files if f.path.split("/", 1)[0] not in WORLD_DIRS))
        digest = await run_in_threadpool(ctx.store.put_manifest, srv["group_id"], kept)
    properties = {**json.loads(srv["properties_json"]), "level-seed": body.seed,
                  "level-type": level_type_value(body.level_type, srv["mc_version"]),
                  "generate-structures": body.generate_structures}
    label = f"Nouveau monde (graine : {body.seed})" if body.seed else "Nouveau monde (graine au hasard)"
    with immediate(ctx.conn):
        if ctx.conn.execute("SELECT head_version_id FROM servers WHERE id = ?", (server_id,)).fetchone()[0] != head_id:
            raise conflict("parent_mismatch", "the world changed meanwhile, try again")
        if kept is not None:
            # Pinned, so the cleanup of old versions never removes the world that was replaced.
            ctx.conn.execute("UPDATE world_versions SET pinned = 1, label = COALESCE(label, ?) WHERE id = ?",
                             (f"Ancien monde (remplacé le {datetime.fromtimestamp(now, UTC):%Y-%m-%d %H:%M} UTC)",
                              head_id))
            version_id = uuid.uuid4().hex
            ctx.conn.execute(
                "INSERT INTO world_versions (id, server_id, parent_id, epoch, host_id, kind, manifest_sha256,"
                " total_size, created_at, label) VALUES (?, ?, ?, ?, NULL, 'manual', ?, ?, ?, ?)",
                (version_id, server_id, head_id, lease.epoch, digest, kept.total_size, now, label))
            ctx.conn.executemany("INSERT INTO version_blobs (version_id, sha256) VALUES (?, ?)",
                                 [(version_id, h) for h in sorted(kept.block_hashes())])
            ctx.conn.execute("UPDATE servers SET head_version_id = ? WHERE id = ?", (version_id, server_id))
        ctx.conn.execute("UPDATE servers SET properties_json = ?, config_revision = config_revision + 1"
                         " WHERE id = ?", (json.dumps(properties), server_id))
        events.record(ctx.conn, group_id=srv["group_id"], server_id=server_id, type="world_reset", now=now,
                      actor_user_id=user.user_id, previous_version_id=head_id, seed=body.seed or None,
                      level_type=body.level_type, generate_structures=body.generate_structures)
    await ctx.coordinator.broadcast(server_id)
    return ctx.coordinator.status(server_id)


@router.get("/groups/{group_id}/storage")
async def storage_usage(group_id: str, user: CurrentUser, ctx: Ctx) -> StorageUsage:
    membership(ctx.conn, group_id, user.user_id)
    quota = ctx.conn.execute("SELECT storage_quota_bytes FROM groups WHERE id = ?", (group_id,)).fetchone()[0]
    servers = []
    for s in ctx.conn.execute("SELECT id, name, head_version_id FROM servers WHERE group_id = ? ORDER BY name",
                              (group_id,)).fetchall():
        head_size = 0
        if s["head_version_id"]:
            head_size = ctx.conn.execute("SELECT total_size FROM world_versions WHERE id = ?",
                                         (s["head_version_id"],)).fetchone()[0]
        count = ctx.conn.execute("SELECT COUNT(*) FROM world_versions WHERE server_id = ?", (s["id"],)).fetchone()[0]
        servers.append(ServerStorage(server_id=s["id"], name=s["name"], head_size=head_size, versions=count))
    return StorageUsage(used_bytes=used_bytes(ctx.conn, group_id), quota_bytes=quota, servers=servers)
