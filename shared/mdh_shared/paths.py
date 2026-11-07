"""Strict validation of relative paths received from the network.

Every path inside a manifest is attacker-controlled from the point of view of the receiver
(a compromised group member, a bug, a tampered upload). Paths are therefore accepted only in a
single canonical form that is safe on both Linux (VPS) and Windows (hosts):

* relative, POSIX separators (``/``), NFC-normalised Unicode;
* no ``.``/``..``/empty components, no backslash, no colon (drive letters, NTFS streams);
* no control characters nor characters Windows forbids (``<>:"|?*``);
* no Windows reserved device names (``CON``, ``NUL``, ``COM1``...), even with an extension;
* no component ending with a dot or a space (Windows silently strips them).
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from mdh_shared.constants import MAX_PATH_COMPONENT_LENGTH, MAX_PATH_LENGTH

_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*\\')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
)


class UnsafePathError(ValueError):
    """Raised when a path is not in the accepted canonical form."""


def validate_relative_path(path: str) -> str:
    """Return ``path`` unchanged if it is a safe canonical relative path, else raise."""
    if not isinstance(path, str) or not path:
        raise UnsafePathError("path must be a non-empty string")
    if len(path) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"path longer than {MAX_PATH_LENGTH} characters")
    if unicodedata.normalize("NFC", path) != path:
        raise UnsafePathError("path is not NFC-normalised")
    if path.startswith("/"):
        raise UnsafePathError("absolute path")
    for ch in path:
        if ord(ch) < 32 or ord(ch) == 127:
            raise UnsafePathError("control character in path")
        if ch in _WINDOWS_FORBIDDEN_CHARS:
            raise UnsafePathError(f"forbidden character {ch!r} in path")
    for part in path.split("/"):
        _validate_component(part)
    return path


def _validate_component(part: str) -> None:
    if part in ("", ".", ".."):
        raise UnsafePathError("empty, '.' or '..' path component")
    if len(part) > MAX_PATH_COMPONENT_LENGTH:
        raise UnsafePathError("path component too long")
    if part.endswith((".", " ")) or part.startswith(" "):
        raise UnsafePathError("path component starts with a space or ends with a dot/space")
    stem = part.split(".", 1)[0].rstrip(" ").upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise UnsafePathError(f"reserved Windows device name {part!r}")


def is_safe_relative_path(path: str) -> bool:
    try:
        validate_relative_path(path)
    except UnsafePathError:
        return False
    return True


def safe_join(root: Path, relative: str) -> Path:
    """Join a validated relative path to ``root`` and check the result stays inside ``root``.

    The check is done on resolved paths, so a symlink or junction placed inside ``root`` that
    points outside of it is also rejected.
    """
    validate_relative_path(relative)
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*relative.split("/")).resolve()
    if not candidate.is_relative_to(root_resolved) or candidate == root_resolved:
        raise UnsafePathError("path escapes the destination directory")
    return candidate


def to_relative_posix(root: Path, path: Path) -> str:
    """Express ``path`` (inside ``root``) in the canonical manifest form."""
    rel = os.path.relpath(path, root)
    return unicodedata.normalize("NFC", rel.replace(os.sep, "/"))


def find_case_collisions(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Return pairs of paths that would map to the same file on a case-insensitive filesystem."""
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for p in paths:
        key = p.casefold()
        if key in seen:
            collisions.append((seen[key], p))
        else:
            seen[key] = p
    return collisions
