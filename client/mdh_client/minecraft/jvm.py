"""JVM arguments for the Minecraft server.

The G1 tuning below is the set PaperMC recommends (also known as Aikar's flags). It is **vetted
and hardcoded** here: JVM flags are never taken from the network, because options such as
``-XX:OnOutOfMemoryError=<cmd>`` execute arbitrary commands.
"""

from __future__ import annotations

G1_FLAGS: tuple[str, ...] = (
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC",
    "-XX:+AlwaysPreTouch",
    "-XX:G1HeapWastePercent=5",
    "-XX:G1MixedGCCountTarget=4",
    "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5",
    "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem",
    "-XX:MaxTenuringThreshold=1",
)
#: Generation sizing differs above 12 GB of heap.
_SMALL_HEAP = ("-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
               "-XX:G1ReservePercent=20", "-XX:InitiatingHeapOccupancyPercent=15")
_LARGE_HEAP = ("-XX:G1NewSizePercent=40", "-XX:G1MaxNewSizePercent=50", "-XX:G1HeapRegionSize=16M",
               "-XX:G1ReservePercent=15", "-XX:InitiatingHeapOccupancyPercent=20")


def jvm_args(memory_mb: int) -> list[str]:
    if memory_mb < 1024:
        raise ValueError("Minecraft needs at least 1024 MB")
    # Xms = Xmx: the heap is reserved once, no resizing pauses during play.
    sizing = _LARGE_HEAP if memory_mb > 12 * 1024 else _SMALL_HEAP
    # Console in UTF-8 both ways: on a French Windows, a JVM writing to a pipe would otherwise use
    # cp1252 and accented chat/log lines would arrive garbled.
    return [f"-Xms{memory_mb}M", f"-Xmx{memory_mb}M", *G1_FLAGS, *sizing,
            "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
            "-Dusing.aikars.flags=https://mcflags.emc.gs"]


def server_command(java: str, jar: str, memory_mb: int) -> list[str]:
    # "nogui": the server must never open its own Swing window.
    return [java, *jvm_args(memory_mb), "-jar", jar, "nogui"]
