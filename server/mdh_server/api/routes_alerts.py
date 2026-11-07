"""Alerts for the admins of a group (docs/API.md section 2 bis)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from mdh_server import alerts
from mdh_server.api.routes_storage import used_bytes
from mdh_server.auth.deps import Ctx, CurrentUser
from mdh_shared.models.api import Alert

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
async def my_alerts(user: CurrentUser, ctx: Ctx) -> list[Alert]:
    """Only to admins: a plain member can do nothing about a full disk or quota."""
    groups = ctx.conn.execute(
        "SELECT g.id, g.name, g.storage_quota_bytes FROM memberships m JOIN groups g ON g.id = m.group_id"
        " WHERE m.user_id = ? AND m.role IN ('owner', 'admin')", (user.user_id,)).fetchall()
    if not groups:
        return []
    found = alerts.vps_alerts(Path(ctx.settings.storage_dir), ctx.run_marker, ctx.now())
    for g in groups:
        alert = alerts.quota_alert(g["id"], g["name"], used_bytes(ctx.conn, g["id"]), g["storage_quota_bytes"])
        if alert is not None:
            found.append(alert)
    return found
