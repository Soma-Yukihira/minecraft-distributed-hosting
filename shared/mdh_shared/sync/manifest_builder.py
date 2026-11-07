"""Build, compare and verify manifests of a server directory.

Must only be run on a consistent snapshot: a stopped server, or a local copy taken between
``save-off``/``save-all flush`` and ``save-on`` (docs/ARCHITECTURE.md 3.4).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from mdh_shared.constants import (
    BLOCK_SIZE,
    EXCLUDED_FILE_PATTERNS,
    EXCLUDED_TOP_LEVEL_DIRS,
    FORBIDDEN_EXTENSIONS,
)
from mdh_shared.models.manifest import FileEntry, Manifest
from mdh_shared.paths import UnsafePathError, to_relative_posix, validate_relative_path


@dataclass(frozen=True)
class ExcludeRules:
    top_level_dirs: frozenset[str] = EXCLUDED_TOP_LEVEL_DIRS
    file_patterns: tuple[str, ...] = EXCLUDED_FILE_PATTERNS
    forbidden_extensions: frozenset[str] = FORBIDDEN_EXTENSIONS

    def excludes_dir(self, rel_path: str) -> bool:
        return "/" not in rel_path and rel_path.casefold() in {
            d.casefold() for d in self.top_level_dirs
        }

    def excludes_file(self, rel_path: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1].casefold()
        return any(fnmatch.fnmatchcase(name, pattern.casefold()) for pattern in self.file_patterns)

    def is_forbidden(self, rel_path: str) -> bool:
        return Path(rel_path).suffix.casefold() in self.forbidden_extensions


@dataclass
class ScanReport:
    """What was left out of a manifest, so the UI can explain it to the user."""

    skipped_links: list[str] = field(default_factory=list)
    skipped_executables: list[str] = field(default_factory=list)
    skipped_unsafe_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ManifestDiff:
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


class IntegrityError(Exception):
    """A file on disk does not match the manifest."""


def iter_blocks(path: Path, block_size: int = BLOCK_SIZE) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(block_size):
            yield chunk


def build_file_entry(root: Path, rel_path: str) -> FileEntry:
    """Hash one file block by block (single read pass)."""
    file_hash = hashlib.sha256()
    blocks: list[str] = []
    size = 0
    for chunk in iter_blocks(root.joinpath(*rel_path.split("/"))):
        file_hash.update(chunk)
        blocks.append(hashlib.sha256(chunk).hexdigest())
        size += len(chunk)
    return FileEntry(path=rel_path, size=size, sha256=file_hash.hexdigest(), blocks=tuple(blocks))


def scan(root: Path, rules: ExcludeRules = ExcludeRules()) -> tuple[list[str], ScanReport]:
    """List synchronisable files under ``root`` as canonical relative paths (sorted).

    Symlinks and junctions are never followed nor synchronised.
    """
    report = ScanReport()
    found: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                rel = to_relative_posix(root, Path(entry.path))
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction():
                    report.skipped_links.append(rel)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not rules.excludes_dir(rel):
                        stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False) or rules.excludes_file(rel):
                    continue
                if rules.is_forbidden(rel):
                    report.skipped_executables.append(rel)
                    continue
                try:
                    validate_relative_path(rel)
                except UnsafePathError:
                    report.skipped_unsafe_names.append(rel)
                    continue
                found.append(rel)
    found.sort()
    return found, report


def build_manifest(root: Path, rules: ExcludeRules = ExcludeRules()) -> tuple[Manifest, ScanReport]:
    paths, report = scan(root, rules)
    entries = tuple(build_file_entry(root, p) for p in paths)
    return Manifest(files=entries), report


def diff_manifests(old: Manifest | None, new: Manifest) -> ManifestDiff:
    old_files = {f.path: f for f in old.files} if old else {}
    new_files = {f.path: f for f in new.files}
    added = tuple(p for p in new_files if p not in old_files)
    deleted = tuple(p for p in old_files if p not in new_files)
    modified = tuple(
        p for p, f in new_files.items() if p in old_files and old_files[p].sha256 != f.sha256
    )
    return ManifestDiff(added=added, modified=modified, deleted=deleted)


def missing_blocks(manifest: Manifest, available: set[str]) -> set[str]:
    return manifest.block_hashes() - available


def verify_directory(root: Path, manifest: Manifest, rules: ExcludeRules = ExcludeRules()) -> None:
    """Check that ``root`` contains exactly the files of ``manifest`` with matching content.

    Raises :class:`IntegrityError` describing the first mismatches found.
    """
    paths, _ = scan(root, rules)
    expected = {f.path: f for f in manifest.files}
    problems: list[str] = []
    extra = sorted(set(paths) - expected.keys())
    missing = sorted(expected.keys() - set(paths))
    problems += [f"unexpected file {p}" for p in extra[:5]]
    problems += [f"missing file {p}" for p in missing[:5]]
    for p in sorted(expected.keys() & set(paths)):
        actual = build_file_entry(root, p)
        if actual.sha256 != expected[p].sha256 or actual.size != expected[p].size:
            problems.append(f"content mismatch {p}")
            if len(problems) >= 10:
                break
    if problems:
        raise IntegrityError("; ".join(problems))
