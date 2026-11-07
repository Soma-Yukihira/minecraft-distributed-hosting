"""Persist lease transitions atomically in SQLite.

Each operation reads the row, applies a pure rule from :mod:`lease`, writes the result and
commits inside one ``BEGIN IMMEDIATE`` transaction: concurrent candidates are serialised by
SQLite's write lock, so at most one of them can be granted a given epoch.
"""

from __future__ import annotations

import sqlite3

from mdh_server.coordination.lease import LeasePolicy, LeaseState, acquire, release, renew
from mdh_server.database.db import immediate


def load(conn: sqlite3.Connection, server_id: str) -> LeaseState:
    row = conn.execute(
        "SELECT epoch, holder_host_id, expires_at FROM leases WHERE server_id = ?", (server_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no lease row for server {server_id}")
    return LeaseState(server_id, row["epoch"], row["holder_host_id"], row["expires_at"])


def save(conn: sqlite3.Connection, state: LeaseState, now: float, *, granted: bool) -> None:
    conn.execute(
        "UPDATE leases SET epoch = ?, holder_host_id = ?, expires_at = ?,"
        " granted_at = CASE WHEN ? THEN ? ELSE granted_at END WHERE server_id = ?",
        (state.epoch, state.holder, state.expires_at, granted, now, state.server_id),
    )


def acquire_lease(
    conn: sqlite3.Connection,
    server_id: str,
    host_id: str,
    now: float,
    policy: LeasePolicy,
    *,
    control_plane_started_at: float,
) -> LeaseState:
    with immediate(conn):
        new = acquire(
            load(conn, server_id), host_id, now, policy,
            control_plane_started_at=control_plane_started_at,
        )
        save(conn, new, now, granted=True)
    return new


def renew_lease(
    conn: sqlite3.Connection, server_id: str, host_id: str, epoch: int, now: float, policy: LeasePolicy
) -> LeaseState:
    with immediate(conn):
        new = renew(load(conn, server_id), host_id, epoch, now, policy)
        save(conn, new, now, granted=False)
    return new


def release_lease(
    conn: sqlite3.Connection, server_id: str, host_id: str, epoch: int, now: float
) -> LeaseState:
    with immediate(conn):
        new = release(load(conn, server_id), host_id, epoch)
        save(conn, new, now, granted=False)
    return new
