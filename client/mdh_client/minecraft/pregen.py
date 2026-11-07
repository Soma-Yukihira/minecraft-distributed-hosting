"""Pre-generating the world with Chunky (docs/ARCHITECTURE.md 4.15).

Generating terrain is the heaviest work of a Minecraft server; done ahead, exploring only reads
chunks from the disk. The hosting PC maps the admin's request to fixed Chunky commands: a
square around the world spawn, one dimension at a time.
"""

from __future__ import annotations

from mdh_shared.models.enums import Loader

#: Rough size of a generated chunk on disk (region files, vanilla terrain).
BYTES_PER_CHUNK = 8 * 1024
#: Free space kept on the hosting PC on top of the estimate.
DISK_MARGIN_BYTES = 2 * 1024**3
#: Seconds between two progress lines in the console.
UPDATE_INTERVAL_S = 30
DIMENSIONS = {"overworld": "Surface", "the_nether": "Nether", "the_end": "End"}
RADII = (1000, 2500, 5000, 10000)


def chunks(radius: int) -> int:
    side = 2 * radius // 16 + 1
    return side * side


def estimate_bytes(radius: int) -> int:
    return chunks(radius) * BYTES_PER_CHUNK


def world_name(loader: Loader, dimension: str, level_name: str = "world") -> str:
    """How Chunky names a dimension: an identifier on Fabric, a Bukkit world name on Paper."""
    if loader == Loader.PAPER:
        return level_name + {"overworld": "", "the_nether": "_nether", "the_end": "_the_end"}[dimension]
    return f"minecraft:{dimension}"


def dimension_of(world: str) -> str:
    for dimension in ("the_nether", "the_end"):
        if world.endswith(dimension) or world.endswith(dimension.removeprefix("the")):
            return dimension
    return "overworld"


def start_commands(world: str, radius: int) -> list[str]:
    # "confirm" answers Chunky when a task already exists for that world (start a new one); the
    # free space was checked before. Otherwise it only says "Nothing to confirm!".
    return [f"chunky world {world}", "chunky spawn", "chunky shape square", f"chunky radius {radius}",
            f"chunky quiet {UPDATE_INTERVAL_S}", "chunky start", "chunky confirm"]


CANCEL_COMMANDS = ["chunky cancel", "chunky confirm"]
