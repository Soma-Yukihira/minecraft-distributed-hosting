"""Release numbers of the application and of the control plane: "major.minor.patch"."""

from __future__ import annotations


def parse(version: str) -> tuple[int, int, int] | None:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def older_than(version: str | None, minimum: str) -> bool:
    """True if ``version`` is below ``minimum``. Missing or unreadable counts as older."""
    have, need = parse(version or ""), parse(minimum)
    assert need is not None, minimum
    return have is None or have < need
