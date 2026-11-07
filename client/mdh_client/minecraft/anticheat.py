"""Anti-cheat protection (docs/ARCHITECTURE.md 4.19).

The switch is Grim (``ANTICHEAT_MOD_SLUG``) in the group's list of plugins/mods: an ordinary,
verified Modrinth file, approved on each PC like any other. Grim watches every player's movements
and hits on the server and cancels what the game does not allow (flying, speed, reach, kill aura,
no fall...). Against X-ray, which only shows what the server sends, the server must not send the
ores players cannot see:

* on Fabric, the ``anti-xray`` mod does it with its own defaults (added with Grim);
* on Paper, it is built in but off by default: :func:`configure_paper` turns it on, before each
  start, in ``config/paper-world-defaults.yml``. Only the ``anticheat.anti-xray`` block is
  rewritten (text edit, the rest of the file is left as Paper wrote it), marked so that removing
  the protection turns off only what this application turned on.

Nothing here can see a player's own mods: a Java server is never told which mods a client runs.
"""

from __future__ import annotations

import os
from pathlib import Path

MARK = "# Minecraft Distributed Hosting : protection anti-triche"
#: Engine mode 2 (fake ores in place of hidden blocks), Paper's recommended lists for the overworld
#: and the nether merged in one block, so it does not depend on where each Paper version keeps
#: the nether's own settings. No ``air`` (it costs players' frame rate).
_HIDDEN = ("copper_ore", "deepslate_copper_ore", "raw_copper_block", "diamond_ore", "deepslate_diamond_ore",
           "gold_ore", "deepslate_gold_ore", "iron_ore", "deepslate_iron_ore", "raw_iron_block", "lapis_ore",
           "deepslate_lapis_ore", "redstone_ore", "deepslate_redstone_ore", "ancient_debris", "nether_gold_ore",
           "nether_quartz_ore")
_REPLACEMENT = ("chest", "amethyst_block", "andesite", "budding_amethyst", "calcite", "coal_ore",
                "deepslate_coal_ore", "deepslate", "diorite", "dirt", "emerald_ore", "deepslate_emerald_ore",
                "granite", "gravel", "smooth_basalt", "stone", "tuff", "basalt", "blackstone", "netherrack",
                "soul_sand", "soul_soil")
#: Paper keeps its settings in config/paper-world-defaults.yml since Minecraft 1.19.
PAPER_MIN_VERSION = (1, 19)


def _block(enabled: bool) -> list[str]:
    if not enabled:
        return ["  anti-xray:", "    enabled: false"]
    return ["  anti-xray:", f"    {MARK}", "    enabled: true", "    engine-mode: 2", "    hidden-blocks:",
            *(f"    - {b}" for b in _HIDDEN), "    lava-obscures: false", "    max-block-height: 128",
            "    replacement-blocks:", *(f"    - {b}" for b in _REPLACEMENT), "    update-radius: 2",
            "    use-permission: false"]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _ends_block(line: str, indent: int) -> bool:
    """A line that closes a block opened at ``indent`` (blank lines and comments never do)."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#") and _indent(line) <= indent


def edit_defaults(text: str, enabled: bool) -> str:
    """``paper-world-defaults.yml`` with the anti-X-ray block set (``text`` if nothing changes)."""
    lines = text.splitlines()
    top = next((i for i, line in enumerate(lines) if line.rstrip() == "anticheat:"), None)
    if top is None:
        if not enabled:
            return text
        new = [*lines, "anticheat:", *_block(True)]
    else:
        end = next((i for i in range(top + 1, len(lines)) if _ends_block(lines[i], 0)), len(lines))
        start = next((i for i in range(top + 1, end) if lines[i].rstrip() == "  anti-xray:"), None)
        if start is None:
            if not enabled:
                return text
            new = [*lines[:top + 1], *_block(True), *lines[top + 1:]]
        else:
            stop = next((i for i in range(start + 1, end) if _ends_block(lines[i], 2)), end)
            current = lines[start:stop]
            if not enabled and not any(line.strip() == MARK for line in current):
                return text  # set by hand, not by this application: left alone
            new = [*lines[:start], *_block(enabled), *lines[stop:]]
    result = "\n".join(new) + "\n"
    return text if result == text else result


def paper_supported(mc_version: str) -> bool:
    try:
        return tuple(int(p) for p in mc_version.split(".")) >= PAPER_MIN_VERSION
    except ValueError:
        return False


def configure_paper(server_dir: Path, mc_version: str, enabled: bool) -> bool:
    """Turn Paper's anti-X-ray on or off before a start; True if the file was changed.

    A missing file (first start) is created with the block alone: Paper adds everything else,
    and takes a file without ``_version`` as written for its own version.
    """
    if not paper_supported(mc_version):
        return False
    path = server_dir / "config" / "paper-world-defaults.yml"
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        text = ""
    new = edit_defaults(text, enabled)
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new, "utf-8")
    os.replace(tmp, path)
    return True
