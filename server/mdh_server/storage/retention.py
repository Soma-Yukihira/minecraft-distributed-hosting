"""Which world versions to keep, and garbage collection of what nothing references any more.

Policy (configurable): the head, every pinned version, the last N versions, the newest version of
each hour over the last 24 h, the newest of each day over the last 7 days. Everything else may go.

Blocks are only deleted when no remaining version references them **and** they are older than a
grace period, so a block uploaded for a commit that is still in progress is never collected.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from mdh_server.database.db import immediate
from mdh_server.storage.store import BlobStore

log = logging.getLogger("mdh.retention")


@dataclass(frozen=True)
class RetentionPolicy:
    keep_last: int = 5
    hourly_hours: int = 24
    daily_days: int = 7
    blob_grace_s: int = 24 * 3600


@dataclass(frozen=True)
class VersionRow:
    version_id: str
    created_at: float
    pinned: bool


def versions_to_keep(versions: list[VersionRow], head_id: str | None, now: float,
                     policy: RetentionPolicy) -> set[str]:
    newest_first = sorted(versions, key=lambda v: v.created_at, reverse=True)
    keep = {v.version_id for v in newest_first[: policy.keep_last]}
    keep |= {v.version_id for v in versions if v.pinned}
    if head_id:
        keep.add(head_id)
    hours: set[int] = set()
    days: set[int] = set()
    for v in newest_first:  # newest first: the first one seen in a bucket is the one kept
        age = now - v.created_at
        if 0 <= age < policy.hourly_hours * 3600:
            bucket = int(v.created_at // 3600)
            if bucket not in hours:
                hours.add(bucket)
                keep.add(v.version_id)
        if 0 <= age < policy.daily_days * 86400:
            bucket = int(v.created_at // 86400)
            if bucket not in days:
                days.add(bucket)
                keep.add(v.version_id)
    return keep


@dataclass
class CollectionReport:
    versions_deleted: int = 0
    manifests_deleted: int = 0
    blobs_deleted: int = 0
    bytes_freed: int = 0


def collect(conn: sqlite3.Connection, store: BlobStore, now: float, policy: RetentionPolicy) -> CollectionReport:
    report = CollectionReport()
    for srv in conn.execute("SELECT id, group_id, head_version_id FROM servers").fetchall():
        rows = [VersionRow(r["id"], r["created_at"], bool(r["pinned"])) for r in conn.execute(
            "SELECT id, created_at, pinned FROM world_versions WHERE server_id = ?", (srv["id"],))]
        doomed = [v.version_id for v in rows
                  if v.version_id not in versions_to_keep(rows, srv["head_version_id"], now, policy)]
        if doomed:
            with immediate(conn):
                for vid in doomed:
                    # Lineage is informative only: children of a deleted version lose their parent.
                    conn.execute("UPDATE world_versions SET parent_id = NULL WHERE parent_id = ?", (vid,))
                    conn.execute("DELETE FROM world_versions WHERE id = ?", (vid,))
            report.versions_deleted += len(doomed)

    for group in conn.execute("SELECT id FROM groups").fetchall():
        gid = group["id"]
        live_manifests = {r[0] for r in conn.execute(
            "SELECT DISTINCT v.manifest_sha256 FROM world_versions v JOIN servers s ON s.id = v.server_id"
            " WHERE s.group_id = ?", (gid,))}
        manifest_dir = store.group_dir(gid) / "manifests"
        if manifest_dir.is_dir():
            for path in manifest_dir.glob("*.json.zst"):
                digest = path.name.removesuffix(".json.zst")
                if digest not in live_manifests and now - path.stat().st_mtime > policy.blob_grace_s:
                    path.unlink(missing_ok=True)
                    report.manifests_deleted += 1

        orphans = conn.execute(
            "SELECT b.sha256, b.stored_size FROM blobs b WHERE b.group_id = ? AND b.created_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM version_blobs vb JOIN world_versions v ON v.id = vb.version_id"
            " JOIN servers s ON s.id = v.server_id WHERE vb.sha256 = b.sha256 AND s.group_id = b.group_id)",
            (gid, now - policy.blob_grace_s)).fetchall()
        for row in orphans:
            # Row first, file second: if we crash in between, the file is merely an orphan on disk.
            with immediate(conn):
                conn.execute("DELETE FROM blobs WHERE group_id = ? AND sha256 = ?", (gid, row["sha256"]))
            store.delete_blob(gid, row["sha256"])
            report.blobs_deleted += 1
            report.bytes_freed += row["stored_size"]
    if report.versions_deleted or report.blobs_deleted:
        log.info("retention: %s", report)
    return report
