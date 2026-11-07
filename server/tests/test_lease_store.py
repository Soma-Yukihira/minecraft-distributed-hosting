"""Lease transitions persisted in SQLite, including concurrent candidates."""

import sqlite3
import threading

import pytest

from mdh_server.coordination.lease import LeaseHeld, LeasePolicy, StaleEpoch
from mdh_server.coordination.lease_store import acquire_lease, load, release_lease, renew_lease
from mdh_server.database.db import connect

POLICY = LeasePolicy()
BOOT = -1e9


def test_acquire_renew_release_persisted(db) -> None:
    conn, seed = db
    a = seed.host_ids[0]
    s = acquire_lease(conn, seed.server_id, a, 100, POLICY, control_plane_started_at=BOOT)
    assert load(conn, seed.server_id) == s
    renew_lease(conn, seed.server_id, a, s.epoch, 110, POLICY)
    assert load(conn, seed.server_id).expires_at == 140

    # Survives a reconnect (i.e. a control-plane restart).
    reopened = connect(seed.path)
    assert load(reopened, seed.server_id).holder == a
    reopened.close()

    release_lease(conn, seed.server_id, a, s.epoch, 120)
    assert load(conn, seed.server_id).holder is None


def test_failed_transition_leaves_row_unchanged(db) -> None:
    conn, seed = db
    a, b = seed.host_ids[:2]
    s = acquire_lease(conn, seed.server_id, a, 100, POLICY, control_plane_started_at=BOOT)
    with pytest.raises(LeaseHeld):
        acquire_lease(conn, seed.server_id, b, 101, POLICY, control_plane_started_at=BOOT)
    with pytest.raises(StaleEpoch):
        renew_lease(conn, seed.server_id, b, s.epoch, 101, POLICY)
    assert load(conn, seed.server_id) == s
    assert not conn.in_transaction


def test_two_simultaneous_candidates_only_one_wins(db) -> None:
    conn, seed = db
    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(len(seed.host_ids))

    def candidate(host_id: str) -> None:
        own = connect(seed.path)  # one connection per thread, like separate requests
        barrier.wait()
        try:
            acquire_lease(own, seed.server_id, host_id, 100, POLICY, control_plane_started_at=BOOT)
            winners.append(host_id)
        except LeaseHeld:
            losers.append(host_id)
        finally:
            own.close()

    for _ in range(20):
        conn.execute("UPDATE leases SET holder_host_id = NULL, expires_at = NULL")
        winners.clear()
        losers.clear()
        threads = [threading.Thread(target=candidate, args=(h,)) for h in seed.host_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1, winners
        assert len(losers) == len(seed.host_ids) - 1
        assert load(conn, seed.server_id).holder == winners[0]


def test_epoch_cannot_go_backwards_even_by_mistake(db) -> None:
    conn, seed = db
    acquire_lease(conn, seed.server_id, seed.host_ids[0], 100, POLICY, control_plane_started_at=BOOT)
    with pytest.raises(sqlite3.IntegrityError, match="cannot decrease"):
        conn.execute("UPDATE leases SET epoch = 0")
