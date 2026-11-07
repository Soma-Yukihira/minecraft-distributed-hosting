"""Statistics of a server, for every member of its group (docs/API.md section 4 quater)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from mdh_server import perf, stats
from mdh_server.auth.deps import Ctx, CurrentUser, server_membership
from mdh_shared.models.api import HostStat, HourStat, PlayerStat, ServerPerformance, ServerStats

router = APIRouter(tags=["stats"])


@router.get("/servers/{server_id}/stats")
async def server_stats(server_id: str, user: CurrentUser, ctx: Ctx,
                       days: int = Query(default=30, ge=1, le=365),
                       utc_offset_min: int = Query(default=0, ge=-14 * 60, le=14 * 60)) -> ServerStats:
    server_membership(ctx.conn, server_id, user.user_id)
    now = ctx.now()
    ctx.coordinator.stats.flush(ctx.conn, now)  # include what is still in memory
    since = stats.first_day(now, days)
    players = ctx.conn.execute(
        "SELECT player_name, SUM(seconds) AS seconds, MAX(last_at) AS last_at FROM play_stats"
        " WHERE server_id = ? AND day >= ? GROUP BY player_name ORDER BY seconds DESC LIMIT 200",
        (server_id, since)).fetchall()
    hosts = ctx.conn.execute(
        "SELECT h.id, h.display_name, u.username, SUM(s.seconds) AS seconds FROM host_stats s"
        " JOIN hosts h ON h.id = s.host_id JOIN users u ON u.id = h.user_id"
        " WHERE s.server_id = ? AND s.day >= ? GROUP BY h.id ORDER BY seconds DESC",
        (server_id, since)).fetchall()
    online: dict[tuple[int, int], float] = {}
    played: dict[tuple[int, int], float] = {}
    total = 0.0
    shift = timedelta(minutes=utc_offset_min)
    for r in ctx.conn.execute("SELECT day, hour, online_seconds, player_seconds FROM hour_stats"
                              " WHERE server_id = ? AND day >= ?", (server_id, since)):
        local = datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(hours=r["hour"]) + shift
        key = (local.weekday(), local.hour)
        online[key] = online.get(key, 0.0) + r["online_seconds"]
        played[key] = played.get(key, 0.0) + r["player_seconds"]
        total += r["online_seconds"]
    hours = [HourStat(weekday=d, hour=h, online_seconds=round(online.get((d, h), 0.0)),
                      average_players=round(played[(d, h)] / online[(d, h)], 2) if online.get((d, h)) else None)
             for d in range(7) for h in range(24)]
    return ServerStats(
        days=days, online_seconds=round(total),
        players=[PlayerStat(player=r["player_name"], seconds=round(r["seconds"]),
                            last_seen_at=datetime.fromtimestamp(r["last_at"], UTC)) for r in players],
        hosts=[HostStat(host_id=r["id"], display_name=r["display_name"], owner=r["username"],
                        seconds=round(r["seconds"])) for r in hosts],
        hours=hours)


@router.get("/servers/{server_id}/performance")
async def server_performance(server_id: str, user: CurrentUser, ctx: Ctx,
                             hours: int = Query(default=24, ge=1, le=168)) -> ServerPerformance:
    """TPS, MSPT, memory, processor and players over time (docs/API.md section 4 quater)."""
    server_membership(ctx.conn, server_id, user.user_id)
    now = ctx.now()
    ctx.coordinator.perf.flush(ctx.conn, now)  # include what is still in memory
    step, points = perf.query(ctx.conn, server_id, now, hours)
    return ServerPerformance(hours=hours, step_s=step, points=points)
