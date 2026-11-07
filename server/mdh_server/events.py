"""Audit journal: every important decision is recorded with who/what/when."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

log = logging.getLogger("mdh.events")


def record(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    type: str,
    now: float,
    server_id: str | None = None,
    actor_user_id: str | None = None,
    host_id: str | None = None,
    **payload: Any,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (group_id, server_id, actor_user_id, host_id, type, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (group_id, server_id, actor_user_id, host_id, type, json.dumps(payload, default=str), now),
    )
    log.info("event %s group=%s server=%s host=%s %s", type, group_id, server_id, host_id, payload)
    return int(cur.lastrowid or 0)


def list_events(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    server_id: str | None,
    before: int | None,
    limit: int,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE group_id = ?"
    args: list[Any] = [group_id]
    if server_id is not None:
        sql += " AND server_id = ?"
        args.append(server_id)
    if before is not None:
        sql += " AND id < ?"
        args.append(before)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()
