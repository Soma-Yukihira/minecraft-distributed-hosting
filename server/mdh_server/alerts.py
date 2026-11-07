"""What a group admin should know about the VPS (docs/API.md section 2 bis).

Computed on request, never stored: disk and memory of the VPS, the group's storage quota, and a
restart of the control plane after a crash (a marker file left by a run that did not stop
cleanly). "The control plane is down" cannot be said by the control plane itself: the
application notices it (no answer for several minutes) and tells the user.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mdh_shared.models.api import Alert

DISK_WARN_FREE = 0.10
DISK_CRITICAL_FREE = 0.05
DISK_MIN_FREE_BYTES = 2 * 1024**3
QUOTA_WARN = 0.80
QUOTA_CRITICAL = 0.95
MEMORY_WARN_MB = 150
RESTART_SHOWN_S = 24 * 3600


class RunMarker:
    """``control-plane.running`` exists while the control plane runs; still there at start-up
    means the previous run ended without a clean stop (crash, power cut, OOM kill)."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / "control-plane.running"
        self.unclean_start_at: float | None = None

    def started(self, now: float) -> None:
        if self.path.exists():
            self.unclean_start_at = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(now), "ascii")

    def stopped(self) -> None:
        self.path.unlink(missing_ok=True)


def _available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text("ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None  # not Linux (development PC)


def vps_alerts(storage_dir: Path, marker: RunMarker, now: float) -> list[Alert]:
    alerts: list[Alert] = []
    try:
        usage = shutil.disk_usage(storage_dir if storage_dir.exists() else storage_dir.parent)
    except OSError:
        usage = None
    if usage is not None:
        free = usage.free / usage.total if usage.total else 1.0
        if free < DISK_WARN_FREE or usage.free < DISK_MIN_FREE_BYTES:
            alerts.append(Alert(code="vps_disk_low",
                                severity="critical" if free < DISK_CRITICAL_FREE else "warning",
                                details={"free_bytes": usage.free, "total_bytes": usage.total}))
    memory = _available_memory_mb()
    if memory is not None and memory < MEMORY_WARN_MB:
        alerts.append(Alert(code="vps_memory_low", severity="warning", details={"available_mb": memory}))
    if marker.unclean_start_at is not None and now - marker.unclean_start_at < RESTART_SHOWN_S:
        alerts.append(Alert(code="vps_restarted", severity="warning",
                            details={"at": marker.unclean_start_at}))
    return alerts


def quota_alert(group_id: str, group_name: str, used: int, quota: int) -> Alert | None:
    ratio = used / quota if quota else 0.0
    if ratio < QUOTA_WARN:
        return None
    return Alert(code="group_quota_high", severity="critical" if ratio >= QUOTA_CRITICAL else "warning",
                 group_id=group_id,
                 details={"group_name": group_name, "used_bytes": used, "quota_bytes": quota})
