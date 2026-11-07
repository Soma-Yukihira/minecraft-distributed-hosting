from dataclasses import replace

from mdh_server.election.scoring import (
    Candidate,
    Ineligibility,
    Requirements,
    rank_candidates,
    secondary_score,
    should_preempt,
)

REQ = Requirements(memory_mb=6144, world_size_mb=2000, head_version_id="v9")

BASE = Candidate(
    host_id="h",
    priority=50,
    online=True,
    hosting_allowed=True,
    can_host=True,
    enabled_for_server=True,
    free_ram_mb=16000,
    free_disk_mb=100_000,
    cpu_cores=8,
    latency_ms=20,
    relay_ok=True,
)

OSCAR = replace(BASE, host_id="oscar", priority=100, free_ram_mb=24000, cpu_cores=16)
FAST = replace(BASE, host_id="fast", priority=70, free_ram_mb=30000, cpu_cores=16, latency_ms=5)
MID = replace(BASE, host_id="mid", priority=50)
SLOW = replace(BASE, host_id="slow", priority=20, free_ram_mb=9000, cpu_cores=4)


def test_priority_wins_when_eligible() -> None:
    result = rank_candidates([SLOW, MID, FAST, OSCAR], REQ)
    assert [r.host_id for r in result.ranked] == ["oscar", "fast", "mid", "slow"]
    assert result.winner == "oscar"


def test_priority_alone_is_not_enough() -> None:
    offline_oscar = replace(OSCAR, online=False)
    tiny = replace(FAST, free_ram_mb=7000)  # 6144 + 2048 headroom needed
    result = rank_candidates([offline_oscar, tiny, MID], REQ)
    assert result.winner == "mid"
    assert result.rejected == {
        "oscar": (Ineligibility.OFFLINE,),
        "fast": (Ineligibility.NOT_ENOUGH_RAM,),
    }


def test_all_reasons_are_reported() -> None:
    bad = replace(
        BASE, online=False, hosting_allowed=False, can_host=False, enabled_for_server=False,
        free_ram_mb=0, free_disk_mb=0, relay_ok=False,
    )
    result = rank_candidates([bad], REQ)
    assert result.winner is None
    assert set(result.rejected["h"]) == set(Ineligibility) - {Ineligibility.EXCLUDED}


def test_excluded_host_is_skipped_during_migration() -> None:
    req = replace(REQ, excluded_host_ids=frozenset({"oscar"}))
    result = rank_candidates([OSCAR, FAST], req)
    assert result.winner == "fast"
    assert Ineligibility.EXCLUDED in result.rejected["oscar"]


def test_disk_requirement_counts_staging_copy() -> None:
    just_enough = replace(MID, free_disk_mb=2 * 2000 + 2048)
    too_small = replace(MID, host_id="small", free_disk_mb=2 * 2000 + 2047)
    result = rank_candidates([just_enough, too_small], REQ)
    assert result.winner == "mid"
    assert result.rejected["small"] == (Ineligibility.NOT_ENOUGH_DISK,)


def test_tie_break_by_score_then_host_id() -> None:
    a = replace(MID, host_id="a", local_version_id="v9")   # already has the head: faster start
    b = replace(MID, host_id="b")
    assert rank_candidates([b, a], REQ).winner == "a"
    c = replace(MID, host_id="c")
    assert [r.host_id for r in rank_candidates([c, b], REQ).ranked] == ["b", "c"]


def test_ranking_is_deterministic() -> None:
    hosts = [SLOW, MID, FAST, OSCAR]
    first = rank_candidates(hosts, REQ)
    assert all(rank_candidates(list(reversed(hosts)), REQ) == first for _ in range(5))


def test_score_bounds() -> None:
    assert 0 <= secondary_score(replace(BASE, free_ram_mb=0, cpu_cores=1, latency_ms=10_000), REQ) <= 1
    assert secondary_score(replace(BASE, free_ram_mb=10**6, cpu_cores=64, latency_ms=0,
                                   local_version_id="v9"), REQ) == 1.0


def test_preemption_only_for_strictly_higher_priority() -> None:
    assert should_preempt(active_priority=70, challenger_priority=100)
    assert not should_preempt(active_priority=100, challenger_priority=100)
    assert not should_preempt(active_priority=100, challenger_priority=70)
