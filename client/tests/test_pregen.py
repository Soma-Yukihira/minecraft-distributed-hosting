"""Pre-generating the world with Chunky: its console messages, its commands."""

from mdh_client.minecraft import pregen
from mdh_client.minecraft.log_parser import LogParser
from mdh_shared.models.enums import Loader


def parse(line: str):
    return LogParser().parse(line)


def test_chunky_progress_in_the_console() -> None:
    # Messages of Chunky 1.5.3 (lang/en.json), as Fabric and Paper log them.
    ev = parse("[10:00:00] [Chunky-minecraft:overworld/INFO]: [Chunky] Task running for minecraft:overworld. "
               "Processed: 12345 chunks (12.34%), ETA: 1:02:03, Rate: 210.5 cps, Current: 120, -45")
    assert ev.kind == "pregen" and ev.value == ("minecraft:overworld", 12.34, 3723)
    ev = parse("[10:00:00 INFO]: [Chunky] Task running for world_nether. Processed: 1,234 chunks (56,7%), "
               "ETA: 0:00:09, Rate: 99.0 cps, Current: 1, 2")
    assert ev.value == ("world_nether", 56.7, 9)
    ev = parse("[10:00:00 INFO]: [Chunky] Task finished for minecraft:the_end. Processed: 500 chunks (100.00%), "
               "Total time: 0:01:00")
    assert (ev.kind, ev.value) == ("pregen_done", "minecraft:the_end")
    ev = parse("[10:00:00] [Server thread/INFO]: [Chunky] Task cancelled for minecraft:overworld.")
    assert (ev.kind, ev.value) == ("pregen_stopped", ("minecraft:overworld", "cancelled"))
    # A player cannot fake it from the chat.
    assert parse("[10:00:00] [Server thread/INFO]: <Eve> Task finished for minecraft:overworld. Processed").kind == "chat"


def test_worlds_and_commands() -> None:
    assert pregen.world_name(Loader.FABRIC, "the_nether") == "minecraft:the_nether"
    assert pregen.world_name(Loader.PAPER, "overworld", "survie") == "survie"
    assert pregen.world_name(Loader.PAPER, "the_end") == "world_the_end"
    assert [pregen.dimension_of(w) for w in ("minecraft:the_nether", "world_nether", "world_the_end", "world")] == [
        "the_nether", "the_nether", "the_end", "overworld"]
    commands = pregen.start_commands("minecraft:overworld", 2500)
    assert commands[0] == "chunky world minecraft:overworld" and "chunky radius 2500" in commands
    assert commands[-2:] == ["chunky start", "chunky confirm"]
    assert pregen.chunks(2500) == 313 ** 2
    assert 700 * 1024**2 < pregen.estimate_bytes(2500) < 800 * 1024**2
