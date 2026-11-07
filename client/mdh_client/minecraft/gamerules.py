"""Options Minecraft moved from server.properties to game rules (docs/ARCHITECTURE.md 3.16).

Since 1.21.9, ``pvp``, ``allow-nether``, ``spawn-monsters`` and ``enable-command-block`` are
ignored in server.properties: they are game rules, stored in the world. The host sets them with
``/gamerule`` once the server is ready, and again as soon as the group changes them (no restart).
"""

from __future__ import annotations

import re
from typing import Any

#: 1.21.11 and later (26.x): names checked in the 26.3 server jar (GameRules, GameRuleCommand).
SNAKE_RULES = {
    "pvp": "pvp",
    "allow-nether": "allow_entering_nether_using_portals",
    "spawn-monsters": "spawn_monsters",
    "enable-command-block": "command_blocks_work",
}
#: 1.21.9 and 1.21.10, before game rules were renamed.
CAMEL_RULES = {
    "pvp": "pvp",
    "allow-nether": "allowEnteringNetherUsingPortals",
    "spawn-monsters": "spawnMonsters",
    "enable-command-block": "commandBlocksEnabled",
}
#: Paper keeps game rules per world: the other dimensions are set too.
PAPER_DIMENSIONS = ("minecraft:the_nether", "minecraft:the_end")

_RELEASE = re.compile(r"^(\d+(?:\.\d+)*)")


def _version(mc_version: str | None) -> tuple[int, ...] | None:
    """(1, 21, 11) for "1.21.11" or "1.21.11-pre-2"; None for a weekly snapshot or nothing."""
    m = _RELEASE.match(mc_version or "")
    if m is None or "w" in (mc_version or ""):
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def rule_names(mc_version: str | None) -> dict[str, str]:
    """server.properties key -> game rule, for the options this version only takes as rules."""
    v = _version(mc_version)
    if v is None:
        return SNAKE_RULES if re.fullmatch(r"\d{2}w\d{2}[a-z]", mc_version or "") else {}
    if v >= (1, 21, 11):
        return SNAKE_RULES
    if v >= (1, 21, 9):
        return CAMEL_RULES
    return {}


def _value(value: Any) -> str:
    if isinstance(value, str):
        return "true" if value.strip().lower() == "true" else "false"
    return "true" if value else "false"


def commands(properties: dict[str, Any], mc_version: str | None, loader: str | None) -> list[str]:
    """The /gamerule commands that apply these options (only those present in ``properties``)."""
    names = rule_names(mc_version)
    result = []
    for key, rule in names.items():
        if key not in properties:
            continue
        command = f"gamerule {rule} {_value(properties[key])}"
        result.append(command)
        if loader == "paper":
            result += [f"execute in {dim} run {command}" for dim in PAPER_DIMENSIONS]
    return result
