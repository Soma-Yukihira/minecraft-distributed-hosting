"""Minecraft must not outlive the application (macOS).

On Windows, a job object kills Java with the application (minecraft/job.py). macOS has no such
thing: a small process started next to Minecraft waits for either to end. If the application
ends first (crash, forced quit), it stops Minecraft's process group: first politely (Minecraft
saves on SIGTERM), then for good after a grace period. Otherwise a server could keep writing a
world whose lease went to another PC.

    <python> -m mdh_client.system.watchdog <application pid> <minecraft pid>
    <packaged app> --mdh-watchdog <application pid> <minecraft pid>
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable

FLAG = "--mdh-watchdog"
#: Between the polite request and the end.
GRACE_S = 30.0
POLL_S = 1.0


def command(app_pid: int, child_pid: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, FLAG, str(app_pid), str(child_pid)]
    return [sys.executable, "-m", "mdh_client.system.watchdog", str(app_pid), str(child_pid)]


def spawn(child_pid: int) -> subprocess.Popen:
    """Started by the application right after Minecraft (POSIX only)."""
    return subprocess.Popen(command(os.getpid(), child_pid), stdin=subprocess.DEVNULL,  # noqa: S603
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            close_fds=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_first(app_pid: int, child_pid: int, is_alive: Callable[[int], bool], sleep: Callable[[float], None]) -> str:
    """'app' or 'child': whichever ends first. kqueue on macOS, polling elsewhere."""
    import select

    if hasattr(select, "kqueue"):
        kq = select.kqueue()
        events = []
        for pid in (app_pid, child_pid):
            events.append(select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                                        fflags=select.KQ_NOTE_EXIT))
        try:
            kq.control(events, 0)
        except ProcessLookupError:  # one of them is already gone
            return "child" if not is_alive(child_pid) else "app"
        for ev in kq.control(None, 1):
            return "app" if ev.ident == app_pid else "child"
    while True:
        if not is_alive(child_pid):
            return "child"
        if not is_alive(app_pid):
            return "app"
        sleep(POLL_S)


def watch(app_pid: int, child_pid: int, *, is_alive: Callable[[int], bool] = alive,
          killpg: Callable[[int, int], None] | None = None, sleep: Callable[[float], None] = time.sleep,
          grace_s: float = GRACE_S) -> str:
    """Returns what happened: 'child' (Minecraft ended, nothing to do), 'stopped' or 'killed'."""
    killpg = killpg or os.killpg  # POSIX only: looked up when used
    if _wait_first(app_pid, child_pid, is_alive, sleep) == "child":
        return "child"
    with contextlib.suppress(ProcessLookupError, PermissionError):
        killpg(child_pid, signal.SIGTERM)
    waited = 0.0
    while waited < grace_s:
        if not is_alive(child_pid):
            return "stopped"
        sleep(POLL_S)
        waited += POLL_S
    with contextlib.suppress(ProcessLookupError, PermissionError):
        killpg(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    return "killed"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not all(a.isdigit() for a in argv):
        return 2
    watch(int(argv[0]), int(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
