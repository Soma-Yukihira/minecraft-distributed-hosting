"""Statistics of a server: play time per player, busy hours, hosting time per PC
(docs/ARCHITECTURE.md 4.13).

Counted from the heartbeats of the lease holder (one every 10 s while Minecraft runs) and the
players it reports. Kept in memory and written every few minutes: a crash of the control
plane loses at most that much, never anything that matters for the world.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from mdh_server.database.db import immediate

FLUSH_EVERY_S = 300.0
#: A late heartbeat counts at most this much (a frozen host does not earn hours).
MAX_GAP_S = 30.0
KEEP_DAYS = 400


def day_hour(ts: float) -> tuple[str, int]:
    d = datetime.fromtimestamp(ts, UTC)
    return d.strftime("%Y-%m-%d"), d.hour


class StatsRecorder:
    def __init__(self) -> None:
        #: Last sample per server: (time, host).
        self._last: dict[str, tuple[float, str]] = {}
        self._play: defaultdict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self._host: defaultdict[tuple[str, str, str], float] = defaultdict(float)
        self._hour: defaultdict[tuple[str, str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.flushed_at: float | None = None

    def observe(self, server_id: str, host_id: str, players: list[str], count: int | None, now: float) -> None:
        """One heartbeat of the host running ``server_id``, with who plays right now."""
        last = self._last.get(server_id)
        self._last[server_id] = (now, host_id)
        if last is None or last[1] != host_id:
            return  # the clock starts with the second sample of a host
        dt = min(now - last[0], MAX_GAP_S)
        if dt <= 0:
            return
        day, hour = day_hour(now)
        self._host[(server_id, host_id, day)] += dt
        online = len(players) if count is None else count
        bucket = self._hour[(server_id, day, hour)]
        bucket[0] += dt
        bucket[1] += online * dt
        for name in players if online else ():
            entry = self._play[(server_id, name, day)]
            entry[0] += dt
            entry[1] = now

    def forget(self, server_id: str) -> None:
        """The server stopped: the next sample starts the clock again."""
        self._last.pop(server_id, None)

    def flush(self, conn: sqlite3.Connection, now: float) -> None:
        play, host, hour = self._play, self._host, self._hour
        self._play, self._host, self._hour = (defaultdict(lambda: [0.0, 0.0]), defaultdict(float),
                                              defaultdict(lambda: [0.0, 0.0]))
        self.flushed_at = now
        known = {r["id"] for r in conn.execute("SELECT id FROM servers")}
        with immediate(conn):
            for (server_id, name, day), (seconds, last_at) in play.items():
                if server_id in known:
                    conn.execute(
                        "INSERT INTO play_stats (server_id, player_name, day, seconds, last_at) VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT (server_id, player_name, day) DO UPDATE SET"
                        " seconds = seconds + excluded.seconds, last_at = MAX(last_at, excluded.last_at)",
                        (server_id, name, day, seconds, last_at))
            for (server_id, host_id, day), seconds in host.items():
                if server_id in known:
                    conn.execute(
                        "INSERT INTO host_stats (server_id, host_id, day, seconds)"
                        " SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM hosts WHERE id = ?)"
                        " ON CONFLICT (server_id, host_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
                        (server_id, host_id, day, seconds, host_id))
            for (server_id, day, h), (online, players) in hour.items():
                if server_id in known:
                    conn.execute(
                        "INSERT INTO hour_stats (server_id, day, hour, online_seconds, player_seconds)"
                        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (server_id, day, hour) DO UPDATE SET"
                        " online_seconds = online_seconds + excluded.online_seconds,"
                        " player_seconds = player_seconds + excluded.player_seconds",
                        (server_id, day, h, online, players))
            oldest = (datetime.fromtimestamp(now, UTC) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            for table in ("play_stats", "host_stats", "hour_stats"):
                conn.execute(f"DELETE FROM {table} WHERE day < ?", (oldest,))


def first_day(now: float, days: int) -> str:
    return (datetime.fromtimestamp(now, UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
