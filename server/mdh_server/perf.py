"""Performance graphs of a server: TPS, MSPT, memory, processor, players (docs/ARCHITECTURE.md 4.17).

The lease holder's heartbeat (every 10 s) carries its measures. They are summed per minute in
memory, written with the statistics every few minutes, kept 7 days, and read back in at most a
few hundred points: the average of each span, and its worst moment (lowest TPS, highest MSPT).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from mdh_server.database.db import immediate
from mdh_shared.models.api import PerfPoint
from mdh_shared.protocol.messages import HostMetrics

KEEP_S = 7 * 24 * 3600
MAX_POINTS = 360


@dataclass
class _Minute:
    samples: int = 0
    cpu_sum: float = 0.0
    ram_max: int = 0
    tps_sum: float = 0.0
    tps_n: int = 0
    tps_min: float | None = None
    mspt_sum: float = 0.0
    mspt_n: int = 0
    mspt_max: float | None = None
    players_max: int = 0


class PerfRecorder:
    def __init__(self) -> None:
        self._minutes: dict[tuple[str, int], _Minute] = {}

    def observe(self, server_id: str, m: HostMetrics, now: float) -> None:
        acc = self._minutes.setdefault((server_id, int(now // 60) * 60), _Minute())
        acc.samples += 1
        acc.cpu_sum += m.cpu_percent
        acc.ram_max = max(acc.ram_max, m.ram_used_mb)
        acc.players_max = max(acc.players_max, m.players_online or 0)
        if m.tps is not None:
            acc.tps_sum += m.tps
            acc.tps_n += 1
            acc.tps_min = m.tps if acc.tps_min is None else min(acc.tps_min, m.tps)
        if m.mspt is not None:
            acc.mspt_sum += m.mspt
            acc.mspt_n += 1
            acc.mspt_max = m.mspt if acc.mspt_max is None else max(acc.mspt_max, m.mspt)

    def flush(self, conn: sqlite3.Connection, now: float) -> None:
        minutes, self._minutes = self._minutes, {}
        known = {r["id"] for r in conn.execute("SELECT id FROM servers")}
        with immediate(conn):
            for (server_id, minute), a in minutes.items():
                if server_id not in known:
                    continue
                conn.execute(
                    "INSERT INTO perf_minutes (server_id, minute, samples, cpu_sum, ram_max, tps_sum, tps_n,"
                    " tps_min, mspt_sum, mspt_n, mspt_max, players_max) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (server_id, minute) DO UPDATE SET samples = samples + excluded.samples,"
                    " cpu_sum = cpu_sum + excluded.cpu_sum, ram_max = MAX(ram_max, excluded.ram_max),"
                    " tps_sum = tps_sum + excluded.tps_sum, tps_n = tps_n + excluded.tps_n,"
                    " tps_min = MIN(COALESCE(tps_min, excluded.tps_min), COALESCE(excluded.tps_min, tps_min)),"
                    " mspt_sum = mspt_sum + excluded.mspt_sum, mspt_n = mspt_n + excluded.mspt_n,"
                    " mspt_max = MAX(COALESCE(mspt_max, excluded.mspt_max), COALESCE(excluded.mspt_max, mspt_max)),"
                    " players_max = MAX(players_max, excluded.players_max)",
                    (server_id, minute, a.samples, a.cpu_sum, a.ram_max, a.tps_sum, a.tps_n, a.tps_min,
                     a.mspt_sum, a.mspt_n, a.mspt_max, a.players_max))
            conn.execute("DELETE FROM perf_minutes WHERE minute < ?", (now - KEEP_S,))


def query(conn: sqlite3.Connection, server_id: str, now: float, hours: int) -> tuple[int, list[PerfPoint]]:
    """Points from ``hours`` ago until now; minutes when the server did not run are left out."""
    span = hours * 3600
    step = max(60, (span // MAX_POINTS + 59) // 60 * 60)
    since = now - span
    rows = conn.execute(
        "SELECT (minute - ?) / ? AS bucket, SUM(samples) AS samples, SUM(cpu_sum) AS cpu, MAX(ram_max) AS ram,"
        " SUM(tps_sum) AS tps, SUM(tps_n) AS tps_n, MIN(tps_min) AS tps_min, SUM(mspt_sum) AS mspt,"
        " SUM(mspt_n) AS mspt_n, MAX(mspt_max) AS mspt_max, MAX(players_max) AS players"
        " FROM perf_minutes WHERE server_id = ? AND minute >= ? GROUP BY bucket ORDER BY bucket",
        (int(since), step, server_id, since)).fetchall()
    points = []
    for r in rows:
        points.append(PerfPoint(
            at=datetime.fromtimestamp(since + int(r["bucket"]) * step, UTC),
            cpu_percent=round(r["cpu"] / r["samples"], 1) if r["samples"] else None,
            ram_used_mb=r["ram"], tps=round(r["tps"] / r["tps_n"], 2) if r["tps_n"] else None,
            tps_min=r["tps_min"], mspt=round(r["mspt"] / r["mspt_n"], 2) if r["mspt_n"] else None,
            mspt_max=r["mspt_max"], players=r["players"]))
    return step, points
