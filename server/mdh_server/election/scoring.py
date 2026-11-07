"""Host election: who should run a server next.

Two ways, chosen per server (docs/ARCHITECTURE.md 3.15):

* ``priority`` (default): the priority set by a group admin wins among eligible hosts; the
  secondary score only breaks ties between hosts of equal priority.
* ``best``: the PC that will run the server best wins — single-core speed, upload speed and
  round trip to the VPS (through which every player plays), memory to spare, an up-to-date copy
  of the world. Priority only breaks ties.

``host_id`` makes the result deterministic. Eligibility failures are returned with reasons so
the UI can explain "why not my PC?".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mdh_shared.constants import HOST_DISK_HEADROOM_MB, HOST_RAM_HEADROOM_MB


class Ineligibility(StrEnum):
    OFFLINE = "offline"
    HOSTING_DISABLED_BY_USER = "hosting_disabled_by_user"
    NO_HOST_PERMISSION = "no_host_permission"
    DISABLED_FOR_SERVER = "disabled_for_server"
    NOT_ENOUGH_RAM = "not_enough_ram"
    NOT_ENOUGH_DISK = "not_enough_disk"
    NETWORK_UNAVAILABLE = "network_unavailable"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class Candidate:
    host_id: str
    priority: int
    online: bool
    hosting_allowed: bool
    can_host: bool
    enabled_for_server: bool
    free_ram_mb: int
    free_disk_mb: int
    cpu_cores: int
    latency_ms: float | None
    relay_ok: bool
    local_version_id: str | None = None
    #: Measured by the PC: single-core speed (1.0 = a recent desktop), upload to the VPS.
    cpu_score: float | None = None
    upload_kbps: int | None = None


@dataclass(frozen=True, slots=True)
class Requirements:
    memory_mb: int
    world_size_mb: int
    head_version_id: str | None
    #: Hosts that must not be chosen (e.g. the host we are migrating away from).
    excluded_host_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    host_id: str
    priority: int
    score: float


@dataclass(frozen=True, slots=True)
class ElectionResult:
    ranked: tuple[RankedCandidate, ...]
    rejected: dict[str, tuple[Ineligibility, ...]]

    @property
    def winner(self) -> str | None:
        return self.ranked[0].host_id if self.ranked else None


def check_eligibility(c: Candidate, req: Requirements) -> tuple[Ineligibility, ...]:
    reasons: list[Ineligibility] = []
    if c.host_id in req.excluded_host_ids:
        reasons.append(Ineligibility.EXCLUDED)
    if not c.online:
        reasons.append(Ineligibility.OFFLINE)
    if not c.hosting_allowed:
        reasons.append(Ineligibility.HOSTING_DISABLED_BY_USER)
    if not c.can_host:
        reasons.append(Ineligibility.NO_HOST_PERMISSION)
    if not c.enabled_for_server:
        reasons.append(Ineligibility.DISABLED_FOR_SERVER)
    if c.free_ram_mb < req.memory_mb + HOST_RAM_HEADROOM_MB:
        reasons.append(Ineligibility.NOT_ENOUGH_RAM)
    if c.free_disk_mb < 2 * req.world_size_mb + HOST_DISK_HEADROOM_MB:
        reasons.append(Ineligibility.NOT_ENOUGH_DISK)
    if not c.relay_ok:
        reasons.append(Ineligibility.NETWORK_UNAVAILABLE)
    return tuple(reasons)


def secondary_score(c: Candidate, req: Requirements) -> float:
    """Score in [0, 1]: RAM headroom, CPU cores, latency to the VPS, up-to-date local copy."""
    ram_headroom = max(0, c.free_ram_mb - req.memory_mb - HOST_RAM_HEADROOM_MB)
    ram = min(ram_headroom / 8192, 1.0)
    cpu = min(c.cpu_cores / 16, 1.0)
    latency = 0.5 if c.latency_ms is None else 1.0 - min(max(c.latency_ms, 0.0) / 200, 1.0)
    has_head = 1.0 if req.head_version_id and c.local_version_id == req.head_version_id else 0.0
    return round(0.35 * ram + 0.20 * cpu + 0.25 * latency + 0.20 * has_head, 6)


#: Upload speed that counts as plenty (20 Mbit/s: a few dozen players and the backups).
GOOD_UPLOAD_KBPS = 20_000
#: Round trip to the VPS that counts as bad.
BAD_RTT_MS = 150.0
#: How much better a PC must be before the server moves to it on its own (out of 1).
BEST_PREEMPT_MARGIN = 0.15


def performance_score(c: Candidate, req: Requirements, extra_ram_mb: int = 0) -> float:
    """Score in [0, 1] of how well this PC would run the server. Unknown measures count as average.

    ``extra_ram_mb``: memory this PC already gives to the server (the current host)."""
    cpu = 0.5 if c.cpu_score is None else min(c.cpu_score / 1.5, 1.0)
    upload = 0.5 if c.upload_kbps is None else min(c.upload_kbps / GOOD_UPLOAD_KBPS, 1.0)
    rtt = 0.5 if c.latency_ms is None else 1.0 - min(max(c.latency_ms, 0.0) / BAD_RTT_MS, 1.0)
    headroom = max(0, c.free_ram_mb + extra_ram_mb - req.memory_mb - HOST_RAM_HEADROOM_MB)
    ram = min(headroom / 8192, 1.0)
    has_head = 1.0 if req.head_version_id and c.local_version_id == req.head_version_id else 0.0
    return round(0.35 * cpu + 0.25 * upload + 0.15 * rtt + 0.15 * ram + 0.10 * has_head, 6)


def rank_candidates(candidates: list[Candidate], req: Requirements, mode: str = "priority") -> ElectionResult:
    eligible: list[RankedCandidate] = []
    rejected: dict[str, tuple[Ineligibility, ...]] = {}
    for c in candidates:
        reasons = check_eligibility(c, req)
        if reasons:
            rejected[c.host_id] = reasons
        elif mode == "best":
            eligible.append(RankedCandidate(c.host_id, c.priority, performance_score(c, req)))
        else:
            eligible.append(RankedCandidate(c.host_id, c.priority, secondary_score(c, req)))
    if mode == "best":
        eligible.sort(key=lambda r: (-r.score, -r.priority, r.host_id))
    else:
        eligible.sort(key=lambda r: (-r.priority, -r.score, r.host_id))
    return ElectionResult(ranked=tuple(eligible), rejected=rejected)


def should_preempt(active_priority: int, challenger_priority: int) -> bool:
    """A returning host may trigger a (policy-gated) migration only if strictly higher priority."""
    return challenger_priority > active_priority
