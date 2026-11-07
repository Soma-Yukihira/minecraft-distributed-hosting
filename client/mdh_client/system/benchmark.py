"""Measures of this PC for the automatic choice of the host (docs/ARCHITECTURE.md 3.15).

* Single-core speed: a minecraft server spends most of its time on one thread, so a short fixed
  workload on one core says more than the number of cores. 1.0 = the reference desktop.
* Round trip to the VPS: from the heartbeats (nothing extra is sent).
* Upload speed to the VPS: a couple of random megabytes sent from time to time, while this PC
  hosts nothing (players play through the VPS: what the host uploads is what they receive).
"""

from __future__ import annotations

import hashlib
import statistics
import time

#: Seconds the workload below takes on the reference desktop (the development PC, November 2026,
#: best of 3: 35 ms). Only comparisons between the group's PCs matter.
REFERENCE_S = 0.035


def _workload() -> int:
    # Integer arithmetic and small dictionaries, like a game loop; then hashing, in C.
    total = 0
    table: dict[int, int] = {}
    for i in range(300_000):
        total = (total * 31 + i) & 0xFFFFFFFF
        table[i & 1023] = total
    hashlib.sha256(bytes(4 * 1024 * 1024)).digest()
    return total + len(table)


def cpu_score(rounds: int = 3) -> float:
    """Best of a few rounds (the others may have been slowed down by something else)."""
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        _workload()
        best = min(best, time.perf_counter() - start)
    return round(min(REFERENCE_S / best, 100.0), 2) if best > 0 else 1.0


class RoundTrips:
    """Median of the last heartbeat round trips, in milliseconds."""

    def __init__(self, keep: int = 15) -> None:
        self.keep = keep
        self.samples: list[float] = []

    def add(self, seconds: float) -> None:
        if 0 <= seconds < 60:
            self.samples = (self.samples + [seconds * 1000])[-self.keep:]

    @property
    def ms(self) -> float | None:
        return round(statistics.median(self.samples), 1) if len(self.samples) >= 3 else None
