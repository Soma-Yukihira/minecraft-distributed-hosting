"""Content-addressed storage of world blocks and manifests on the VPS filesystem.

Layout (one isolated tree per group, no cross-group deduplication on purpose)::

    <root>/<group_id>/blobs/ab/cd/<sha256>.zst        one block, zstd-compressed
    <root>/<group_id>/manifests/<sha256>.json.zst     one world version description

Every block is decompressed with a hard size cap and re-hashed before being stored: the VPS never
trusts the hash announced by a client. Files are written to a temporary name, fsync'ed, then
renamed, so a crash never leaves a truncated block under a valid name.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

import zstandard

from mdh_shared.constants import BLOCK_SIZE
from mdh_shared.models.manifest import Manifest

#: zstd never expands incompressible data by more than a few hundred bytes per MiB.
MAX_COMPRESSED_BLOCK = BLOCK_SIZE + 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class StorageError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _check(pattern: re.Pattern, value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise StorageError("invalid_id", f"invalid {what}")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def group_dir(self, group_id: str) -> Path:
        return self.root / _check(_HEX32, group_id, "group id")

    def blob_path(self, group_id: str, sha256: str) -> Path:
        _check(_HEX64, sha256, "block hash")
        return self.group_dir(group_id) / "blobs" / sha256[:2] / sha256[2:4] / f"{sha256}.zst"

    def manifest_path(self, group_id: str, digest: str) -> Path:
        _check(_HEX64, digest, "manifest hash")
        return self.group_dir(group_id) / "manifests" / f"{digest}.json.zst"

    # ------------------------------------------------------------------ blocks

    def put_blob(self, group_id: str, sha256: str, compressed: bytes) -> tuple[int, int]:
        """Verify and store one block. Returns (uncompressed size, stored size). Idempotent."""
        path = self.blob_path(group_id, sha256)
        if len(compressed) > MAX_COMPRESSED_BLOCK:
            raise StorageError("too_large", "compressed block too large")
        try:
            # Read at most BLOCK_SIZE + 1 bytes: a decompression bomb is cut short, never inflated.
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed)) as reader:
                data = reader.read(BLOCK_SIZE + 1)
        except zstandard.ZstdError as exc:
            raise StorageError("invalid_block", f"not valid zstd data: {exc}") from exc
        if not data or len(data) > BLOCK_SIZE:
            raise StorageError("invalid_block", "block is empty or larger than the block size")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError("hash_mismatch", "block content does not match its hash")
        if not path.exists():
            _atomic_write(path, compressed)
        return len(data), len(compressed)

    def has_blob(self, group_id: str, sha256: str) -> bool:
        return self.blob_path(group_id, sha256).is_file()

    def delete_blob(self, group_id: str, sha256: str) -> None:
        self.blob_path(group_id, sha256).unlink(missing_ok=True)

    # ------------------------------------------------------------------ manifests

    def put_manifest(self, group_id: str, manifest: Manifest) -> str:
        digest = manifest.digest()
        path = self.manifest_path(group_id, digest)
        if not path.exists():
            _atomic_write(path, zstandard.ZstdCompressor(level=6).compress(manifest.canonical_bytes()))
        return digest

    def read_manifest(self, group_id: str, digest: str) -> Manifest:
        raw = self.manifest_path(group_id, digest).read_bytes()
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
            data = reader.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise StorageError("too_large", "manifest too large")
        manifest = Manifest.model_validate_json(data)
        if manifest.digest() != digest:  # bit rot or tampering on the VPS disk
            raise StorageError("corrupted", f"stored manifest {digest} is corrupted")
        return manifest

    def delete_manifest(self, group_id: str, digest: str) -> None:
        self.manifest_path(group_id, digest).unlink(missing_ok=True)
