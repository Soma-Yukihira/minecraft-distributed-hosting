"""World manifest: the exact, verifiable description of one world version."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from mdh_shared.constants import (
    BLOCK_SIZE,
    FORBIDDEN_EXTENSIONS,
    MAX_FILE_SIZE,
    MAX_MANIFEST_ENTRIES,
)
from mdh_shared.paths import find_case_collisions, validate_relative_path

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

MANIFEST_FORMAT_VERSION = 1


class FileEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0, le=MAX_FILE_SIZE)
    sha256: Sha256Hex
    blocks: tuple[Sha256Hex, ...]

    @field_validator("path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        validate_relative_path(value)
        suffix = value.rsplit("/", 1)[-1].rpartition(".")[2].lower()
        if f".{suffix}" in FORBIDDEN_EXTENSIONS:
            raise ValueError(f"executable content is never synchronised: {value}")
        return value

    @model_validator(mode="after")
    def _blocks_match_size(self) -> FileEntry:
        expected = math.ceil(self.size / BLOCK_SIZE)
        if len(self.blocks) != expected:
            raise ValueError(
                f"{self.path}: {len(self.blocks)} blocks for {self.size} bytes, expected {expected}"
            )
        return self


class Manifest(BaseModel):
    """Sorted list of files. Its canonical JSON hash identifies the world version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: int = MANIFEST_FORMAT_VERSION
    block_size: int = BLOCK_SIZE
    files: tuple[FileEntry, ...] = Field(max_length=MAX_MANIFEST_ENTRIES)

    @model_validator(mode="after")
    def _consistent(self) -> Manifest:
        if self.format_version != MANIFEST_FORMAT_VERSION:
            raise ValueError(f"unsupported manifest format {self.format_version}")
        if self.block_size != BLOCK_SIZE:
            raise ValueError(f"unsupported block size {self.block_size}")
        paths = [f.path for f in self.files]
        if paths != sorted(paths):
            raise ValueError("manifest entries must be sorted by path")
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate path in manifest")
        collisions = find_case_collisions(paths)
        if collisions:
            raise ValueError(f"paths collide on a case-insensitive filesystem: {collisions[0]}")
        _reject_file_directory_conflicts(paths)
        return self

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def block_hashes(self) -> set[str]:
        return {b for f in self.files for b in f.blocks}

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _reject_file_directory_conflicts(paths: list[str]) -> None:
    """A path cannot be both a file and a parent directory of another file."""
    files = {p.casefold() for p in paths}
    for p in paths:
        parts = p.casefold().split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in files:
                raise ValueError(f"{p!r} is inside a path that is also a file")
