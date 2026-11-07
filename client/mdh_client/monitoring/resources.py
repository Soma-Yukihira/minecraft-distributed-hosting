"""Local resource detection and process metrics.

Only coarse numbers ever leave the PC (free RAM, free disk, CPU %, core count): no machine name,
serial number, local IP or process list.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

import psutil

MB = 1024 * 1024


@dataclass(frozen=True)
class SystemResources:
    total_ram_mb: int
    available_ram_mb: int
    logical_cores: int
    physical_cores: int
    disk_free_mb: int
    os_name: str


def detect(data_dir: Path) -> SystemResources:
    vm = psutil.virtual_memory()
    data_dir.mkdir(parents=True, exist_ok=True)
    return SystemResources(
        total_ram_mb=vm.total // MB,
        available_ram_mb=vm.available // MB,
        logical_cores=psutil.cpu_count(logical=True) or 1,
        physical_cores=psutil.cpu_count(logical=False) or 1,
        disk_free_mb=psutil.disk_usage(str(data_dir)).free // MB,
        os_name=f"{platform.system()} {platform.release()}",
    )


def suggest_memory_mb(total_ram_mb: int) -> int:
    """RAM to give Minecraft by default: a quarter of the PC, 2-8 GB, never starving Windows.

    32 GB -> 8 GB, 16 GB -> 4 GB, 8 GB -> 2 GB.
    """
    quarter = (total_ram_mb // 4) // 512 * 512
    suggestion = min(max(quarter, 2048), 8192)
    return max(1024, min(suggestion, total_ram_mb - 4096))


def max_allowed_memory_mb(total_ram_mb: int) -> int:
    """Hard ceiling the UI allows: keep at least 4 GB (or a third of the RAM) for Windows."""
    reserve = max(4096, total_ram_mb // 3)
    return max(1024, (total_ram_mb - reserve) // 512 * 512)


@dataclass(frozen=True)
class ProcessUsage:
    cpu_percent: float   # % of the whole machine (0-100)
    rss_mb: int


class ProcessMonitor:
    def __init__(self, pid: int) -> None:
        self._proc = psutil.Process(pid)
        self._cores = psutil.cpu_count(logical=True) or 1
        self._proc.cpu_percent(None)  # prime the counter

    def sample(self) -> ProcessUsage | None:
        """CPU and RAM of the server process tree (java.exe, plus any child it starts)."""
        try:
            cpu = self._proc.cpu_percent(None)
            rss = self._proc.memory_info().rss
        except psutil.Error:
            return None
        for child in self._children():
            try:
                cpu += child.cpu_percent(None)
                rss += child.memory_info().rss
            except psutil.Error:
                continue
        return ProcessUsage(round(min(cpu / self._cores, 100.0), 1), rss // MB)

    def _children(self) -> list[psutil.Process]:
        try:
            current = self._proc.children(recursive=True)
        except psutil.Error:
            return []
        known = getattr(self, "_known", {})
        # Keep Process objects across samples: cpu_percent() measures since the previous call.
        self._known = {p.pid: known.get(p.pid, p) for p in current}
        return list(self._known.values())


def machine_cpu_percent() -> float:
    return psutil.cpu_percent(None)
