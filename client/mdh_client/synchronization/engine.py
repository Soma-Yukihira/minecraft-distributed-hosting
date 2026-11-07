"""World synchronisation between this PC and the VPS.

Invariants (docs/ARCHITECTURE.md section 3):

* **Upload** sends only the blocks the VPS lacks, then commits a version with
  ``parent = head`` under the current lease epoch. A commit is the only thing that moves the
  head; an interrupted upload simply resumes (the VPS reports what is still missing).
* **Download** never touches the live world until every file to replace has been rebuilt in a
  staging area and verified (SHA-256 of each block and of each whole file). Blocks already on
  this PC are reused; only the others are fetched.
* **Swap** is journaled: replaced files go to ``.mdh/previous``; an interrupted swap is rolled
  back at the next start (:meth:`SyncEngine.recover`).
* Local data the VPS does not have is never discarded: it is either uploaded (if it is the
  newest state) or archived in ``divergent/`` (excluded from synchronisation).
* **Prefetch** (planned migration target) rebuilds and verifies files in ``.mdh/prefetch``
  without a lease and without touching the live world; the final download then only fetches
  the last delta and moves the prefetched files into place.

Heavy file work runs in worker threads: blocking the agent's event loop would stop heartbeats
and make this PC self-fence.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import zstandard

from mdh_client.networking.api_client import ApiClient, ApiError, ApiUnavailable
from mdh_shared.constants import BLOCK_SIZE
from mdh_shared.models.manifest import FileEntry, Manifest
from mdh_shared.paths import safe_join
from mdh_shared.sync.manifest_builder import build_file_entry, build_manifest, scan

log = logging.getLogger("mdh.sync")

STATE_DIR = ".mdh"
PREFETCH_DIR = "prefetch"
DIVERGENT_DIR = "divergent"
DISK_MARGIN_BYTES = 512 * 1024 * 1024
RETRIES = 4
THROTTLE_RETRIES = 8

Progress = Callable[[str, float | None], None]


class SyncError(Exception):
    pass


class LeaseLost(SyncError):
    """The VPS refused a write because this PC no longer holds the lease."""


class NotEnoughSpace(SyncError):
    pass


@dataclass(frozen=True)
class LeaseContext:
    group_id: str
    server_id: str
    host_id: str
    epoch: int


@dataclass(frozen=True)
class MaterializeResult:
    changed_files: int
    deleted_files: int
    downloaded_blocks: int
    reused_blocks: int


def _read_block(path: Path, index: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(index * BLOCK_SIZE)
        return fh.read(BLOCK_SIZE)


def _replace(src: Path, dst: Path) -> None:
    """os.replace with a few retries (antivirus or indexer may hold a file for a moment)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    _replace(tmp, path)


class SyncEngine:
    def __init__(self, api: ApiClient, *, concurrency: int = 4, progress: Progress | None = None) -> None:
        self.api = api
        self.concurrency = concurrency
        self.progress = progress or (lambda message, fraction: None)

    # ------------------------------------------------------------------ local state

    @staticmethod
    def state_dir(live: Path) -> Path:
        return live / STATE_DIR

    def local_version(self, live: Path) -> str | None:
        try:
            return json.loads((self.state_dir(live) / "local_version.json").read_text("utf-8"))["version_id"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def prefetched_version(self, live: Path) -> str | None:
        """Version fully ready in the prefetch area (standby copy or migration), if any."""
        try:
            return json.loads((self.state_dir(live) / "prefetched.json").read_text("utf-8"))["version_id"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def local_manifest(self, live: Path) -> Manifest | None:
        try:
            return Manifest.model_validate_json((self.state_dir(live) / "local_manifest.json").read_bytes())
        except (FileNotFoundError, ValueError):
            return None

    def _record_local(self, live: Path, version_id: str, manifest: Manifest) -> None:
        state = self.state_dir(live)
        _write_json(state / "local_manifest.json", json.loads(manifest.canonical_bytes()))
        _write_json(state / "local_version.json", {"version_id": version_id, "manifest": manifest.digest()})

    async def scan_entries(self, root: Path) -> dict[str, FileEntry]:
        manifest, _ = await asyncio.to_thread(build_manifest, root)
        return {f.path: f for f in manifest.files}

    async def has_local_changes(self, live: Path) -> bool:
        """True when the live world differs from the last version this PC synced."""
        current = await self.scan_entries(live)
        known = self.local_manifest(live)
        if known is None:
            return bool(current)
        return current != {f.path: f for f in known.files}

    # ------------------------------------------------------------------ upload

    async def upload(self, ctx: LeaseContext, root: Path, *, parent: str | None, kind: str,
                     live: Path | None = None, label: str | None = None) -> dict:
        """Upload ``root`` (a stopped server or a consistent snapshot) as a new version."""
        manifest, report = await asyncio.to_thread(build_manifest, root)
        if report.skipped_unsafe_names:
            log.warning("files with unsafe names were not synchronised: %s", report.skipped_unsafe_names[:5])
        locations: dict[str, tuple[str, int]] = {}
        for f in manifest.files:
            for i, b in enumerate(f.blocks):
                locations.setdefault(b, (f.path, i))
        missing = await self._call(lambda: self.api.missing_blobs(ctx.group_id, sorted(locations)))
        total = len(missing)
        done = 0
        self.progress(f"Envoi du monde : {total} blocs à envoyer", 0.0 if total else None)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def send(sha: str) -> None:
            nonlocal done
            rel, index = locations[sha]
            async with semaphore:
                data = await asyncio.to_thread(_read_block, root.joinpath(*rel.split("/")), index)
                if hashlib.sha256(data).hexdigest() != sha:
                    raise SyncError(f"{rel} changed while being uploaded")
                compressed = await asyncio.to_thread(zstandard.ZstdCompressor(level=3).compress, data)
                await self._call(lambda: self.api.put_blob(ctx.group_id, sha, compressed, server_id=ctx.server_id,
                                                           host_id=ctx.host_id, epoch=ctx.epoch))
            done += 1
            self.progress(f"Envoi du monde : {done}/{total} blocs", done / total)

        await _gather_all(send(sha) for sha in missing)
        # Same key on every retry: if the VPS committed but the answer was lost, the retry gets the
        # original answer instead of a "head moved" refusal caused by our own commit.
        key = uuid.uuid4().hex
        body = {"host_id": ctx.host_id, "epoch": ctx.epoch, "parent_version_id": parent, "kind": kind,
                "label": label, "manifest": json.loads(manifest.canonical_bytes())}
        version = await self._call(lambda: self.api.commit_version(ctx.server_id, idempotency_key=key, **body))
        await asyncio.to_thread(self._record_local, live or root, version["version_id"], manifest)
        log.info("uploaded version %s (%s, %d blocks sent, %d files)", version["version_id"], kind, total,
                 len(manifest.files))
        return version

    # ------------------------------------------------------------------ download

    async def files(self, server_id: str, version: dict) -> list[FileEntry]:
        """What a version of the world holds (its manifest, verified)."""
        return list((await self._manifest(server_id, version)).files)

    async def read_file(self, group_id: str, server_id: str, version: dict, path: str, max_bytes: int) -> bytes:
        """One file of a version, rebuilt in memory from its blocks and checked."""
        entry = next((f for f in await self.files(server_id, version) if f.path == path), None)
        if entry is None:
            raise SyncError(f"{path} is not in this version of the world")
        if entry.size > max_bytes:
            raise SyncError(f"{path} is too big to be read here")
        parts = []
        for sha in entry.blocks:
            compressed = await self._call(lambda sha=sha: self.api.get_blob(group_id, sha))
            data = await asyncio.to_thread(_decompress_block, compressed)
            if hashlib.sha256(data).hexdigest() != sha:
                raise SyncError(f"block {sha[:12]} received from the VPS is corrupted")
            parts.append(data)
        content = b"".join(parts)
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise SyncError(f"{path} does not match its fingerprint")
        return content

    async def _manifest(self, server_id: str, version: dict) -> Manifest:
        raw = await self._call(lambda: self.api.manifest(server_id, version["version_id"]))
        manifest = Manifest.model_validate(raw)
        if manifest.digest() != version["manifest_sha256"]:
            raise SyncError("the manifest received does not match the version's fingerprint")
        return manifest

    def prefetch_dir(self, live: Path) -> Path:
        return self.state_dir(live) / PREFETCH_DIR

    async def prefetch(self, group_id: str, server_id: str, version: dict, live: Path) -> MaterializeResult:
        """Download ``version`` ahead of a migration, without a lease and without touching ``live``.

        Files that differ from the live copy are rebuilt and verified in ``.mdh/prefetch``.
        Calling it again with a newer version only fetches what changed in between.
        """
        manifest = await self._manifest(server_id, version)
        live.mkdir(parents=True, exist_ok=True)
        area = self.prefetch_dir(live)
        local = await self.scan_entries(live)
        prefetched = await self.scan_entries(area) if area.exists() else {}
        missing = [f for f in manifest.files
                   if (f.path not in local or local[f.path].sha256 != f.sha256)
                   and (f.path not in prefetched or prefetched[f.path].sha256 != f.sha256)]
        _check_space(area if area.exists() else live, sum(f.size for f in missing))
        # The prefetch area may hold files of an older version: build the new ones aside first.
        staging = self.state_dir(live) / "staging" / f"prefetch-{version['version_id']}"
        counters = await self._stage(group_id, missing, staging, [(live, local), (area, prefetched)],
                                     "Préchargement du monde")
        for entry in missing:
            _replace(safe_join(staging, entry.path), safe_join(area, entry.path))
        wanted = {f.path for f in manifest.files}
        for rel in prefetched:
            if rel not in wanted:
                safe_join(area, rel).unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        _write_json(self.state_dir(live) / "prefetched.json", {"version_id": version["version_id"]})
        log.info("prefetched %s: %d files, %d blocks downloaded, %d reused", version["version_id"],
                 len(missing), counters["downloaded"], counters["reused"])
        return MaterializeResult(len(missing), 0, counters["downloaded"], counters["reused"])

    async def rebuild(self, group_id: str, server_id: str, version: dict, target: Path,
                      reuse: Path | None = None) -> MaterializeResult:
        """Rebuild ``version`` in the empty folder ``target`` (an export), verified, reusing the
        blocks of ``reuse`` (this PC's copy of the server) where they match."""
        manifest = await self._manifest(server_id, version)
        sources = []
        if reuse is not None and reuse.is_dir():
            sources.append((reuse, await self.scan_entries(reuse)))
        target.mkdir(parents=True, exist_ok=True)
        _check_space(target, manifest.total_size)
        counters = await self._stage(group_id, list(manifest.files), target, sources, "Préparation du monde")
        return MaterializeResult(len(manifest.files), 0, counters["downloaded"], counters["reused"])

    def discard_prefetch(self, live: Path) -> None:
        (self.state_dir(live) / "prefetched.json").unlink(missing_ok=True)
        shutil.rmtree(self.prefetch_dir(live), ignore_errors=True)

    async def materialize(self, group_id: str, server_id: str, version: dict, live: Path) -> MaterializeResult:
        """Make ``live`` contain exactly ``version`` (verified), reusing local and prefetched blocks."""
        manifest = await self._manifest(server_id, version)
        live.mkdir(parents=True, exist_ok=True)
        local = await self.scan_entries(live)
        area = self.prefetch_dir(live)
        prefetched = await self.scan_entries(area) if area.exists() else {}
        wanted = {f.path: f for f in manifest.files}
        to_write = [f for f in manifest.files if local.get(f.path) is None or local[f.path].sha256 != f.sha256]
        to_delete = [p for p in local if p not in wanted]
        if not to_write and not to_delete:
            await asyncio.to_thread(self._record_local, live, version["version_id"], manifest)
            self.discard_prefetch(live)
            return MaterializeResult(0, 0, 0, 0)

        staging = self.state_dir(live) / "staging" / version["version_id"]
        # Prefetched files that are already exactly right are moved, not copied (no extra space).
        ready = [f for f in to_write if f.path in prefetched and prefetched[f.path].sha256 == f.sha256]
        _check_space(live, sum(f.size for f in to_write) - sum(f.size for f in ready))
        for entry in ready:
            _replace(safe_join(area, entry.path), safe_join(staging, entry.path))
        moved = {f.path for f in ready}
        rest = [f for f in to_write if f.path not in moved]
        counters = await self._stage(group_id, rest, staging, [(live, local), (area, prefetched)],
                                     "Récupération du monde")
        await asyncio.to_thread(self._swap, live, staging, [f.path for f in to_write], to_delete,
                                version["version_id"], manifest)
        self.discard_prefetch(live)
        return MaterializeResult(len(to_write), len(to_delete), counters["downloaded"],
                                 counters["reused"] + sum(len(f.blocks) for f in ready))

    async def _stage(self, group_id: str, entries: list[FileEntry], staging: Path,
                     sources: list[tuple[Path, dict[str, FileEntry]]], label: str) -> dict[str, int]:
        """Rebuild ``entries`` in ``staging``, verified, from local blocks first, then the VPS."""
        reusable: dict[str, tuple[Path, str, int]] = {}
        for root, found in sources:
            for entry in found.values():
                for i, b in enumerate(entry.blocks):
                    reusable.setdefault(b, (root, entry.path, i))
        counters = {"downloaded": 0, "reused": 0}
        total_blocks = sum(len(f.blocks) for f in entries)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(sha: str) -> bytes:
            if sha in reusable:
                root, rel, index = reusable[sha]
                try:
                    data = await asyncio.to_thread(_read_block, root.joinpath(*rel.split("/")), index)
                except OSError:
                    data = b""
                if hashlib.sha256(data).hexdigest() == sha:
                    counters["reused"] += 1
                    return data
            async with semaphore:
                compressed = await self._call(lambda: self.api.get_blob(group_id, sha))
            data = await asyncio.to_thread(_decompress_block, compressed)
            if hashlib.sha256(data).hexdigest() != sha:
                raise SyncError(f"block {sha[:12]} received from the VPS is corrupted")
            counters["downloaded"] += 1
            return data

        async def stage(entry: FileEntry) -> None:
            target = safe_join(staging, entry.path)
            if target.is_file() and (await asyncio.to_thread(build_file_entry, staging, entry.path)).sha256 == entry.sha256:
                return  # already staged by an interrupted previous attempt
            blocks = await _gather_all(fetch(b) for b in entry.blocks)
            await asyncio.to_thread(_write_verified, target, blocks, entry)
            done = counters["downloaded"] + counters["reused"]
            self.progress(f"{label} : {done}/{total_blocks} blocs", done / total_blocks if total_blocks else None)

        self.progress(label, 0.0)
        for i in range(0, len(entries), 8):  # bounded memory: 8 files at a time
            await _gather_all(stage(f) for f in entries[i:i + 8])
        return counters

    def _swap(self, live: Path, staging: Path, to_write: list[str], to_delete: list[str],
              version_id: str, manifest: Manifest) -> None:
        state = self.state_dir(live)
        previous = state / "previous"
        shutil.rmtree(previous, ignore_errors=True)
        journal = state / "swap-journal.json"
        _write_json(journal, {"version_id": version_id, "to_write": to_write, "to_delete": to_delete,
                              "staging": str(staging)})
        for rel in [*to_write, *to_delete]:
            current = safe_join(live, rel)
            if current.exists():
                _replace(current, safe_join(previous, rel))
        for rel in to_write:
            _replace(safe_join(staging, rel), safe_join(live, rel))
        self._record_local(live, version_id, manifest)
        journal.unlink()
        shutil.rmtree(staging, ignore_errors=True)

    def recover(self, live: Path) -> bool:
        """Roll back a swap interrupted by a crash or power loss. Returns True if one was found."""
        journal_path = self.state_dir(live) / "swap-journal.json"
        try:
            journal = json.loads(journal_path.read_text("utf-8"))
        except FileNotFoundError:
            return False
        previous = self.state_dir(live) / "previous"
        for rel in [*journal["to_write"], *journal["to_delete"]]:
            saved = safe_join(previous, rel)
            current = safe_join(live, rel)
            if saved.exists():
                _replace(saved, current)            # the file as it was before the swap
            elif rel in journal["to_write"] and current.exists():
                current.unlink()                    # a new file the swap had added
        journal_path.unlink()
        log.warning("interrupted world swap to %s rolled back", journal["version_id"])
        return True

    def archive_divergent(self, live: Path) -> Path:
        """Copy this PC's unsynchronised world aside (never deleted) before replacing it."""
        target = live / DIVERGENT_DIR / time.strftime("%Y-%m-%d_%H-%M-%S")
        paths, _ = scan(live)
        for rel in paths:
            dst = safe_join(target, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_join(live, rel), dst)
        return target

    # ------------------------------------------------------------------ snapshots

    @staticmethod
    def refresh_snapshot(live: Path, snapshot: Path) -> None:
        """Mirror the synchronised files of ``live`` into ``snapshot`` (copy only what changed).

        Must run while the server is not writing (stopped, or between ``save-off`` and ``save-on``).
        """
        paths, _ = scan(live)
        existing, _ = scan(snapshot) if snapshot.exists() else ([], None)
        for rel in set(existing) - set(paths):
            safe_join(snapshot, rel).unlink()
        for rel in paths:
            src, dst = safe_join(live, rel), safe_join(snapshot, rel)
            s = src.stat()
            if dst.exists():
                d = dst.stat()
                if d.st_size == s.st_size and d.st_mtime_ns == s.st_mtime_ns:
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # ------------------------------------------------------------------ helpers

    async def _call(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Retry transient network failures (a fresh request each time); translate lease refusals.

        Being throttled (429) is not a failure: wait and try again, for up to about a minute.
        """
        attempt = throttled = 0
        while True:
            try:
                return await factory()
            except ApiUnavailable:
                attempt += 1
                if attempt == RETRIES:
                    raise
            except ApiError as exc:
                if exc.code in ("stale_epoch", "expired"):
                    raise LeaseLost(exc.message) from exc
                if exc.status == 429 and throttled < THROTTLE_RETRIES:
                    throttled += 1
                    await asyncio.sleep(min(2 ** throttled, 10))
                    continue
                attempt += 1
                if exc.status < 500 or attempt == RETRIES:
                    raise
            await asyncio.sleep(min(2 ** (attempt - 1), 8))


def _check_space(where: Path, size: int) -> None:
    needed = size + DISK_MARGIN_BYTES
    free = psutil.disk_usage(str(where)).free
    if free < needed:
        raise NotEnoughSpace(f"{needed // 2**20} Mo nécessaires, {free // 2**20} Mo libres")


def _decompress_block(compressed: bytes) -> bytes:
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed)) as reader:
        data = reader.read(BLOCK_SIZE + 1)
    if len(data) > BLOCK_SIZE:
        raise SyncError("block larger than the block size")
    return data


def _write_verified(target: Path, blocks: list[bytes], entry: FileEntry) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    with part.open("wb") as fh:
        for data in blocks:
            digest.update(data)
            fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if digest.hexdigest() != entry.sha256:
        part.unlink(missing_ok=True)
        raise SyncError(f"{entry.path}: reassembled file does not match its fingerprint")
    _replace(part, target)


async def _gather_all(coros) -> list:
    """Like gather(), but cancels the siblings as soon as one fails."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
