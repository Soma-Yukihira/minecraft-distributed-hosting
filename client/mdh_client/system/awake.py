"""Keep the computer awake while it hosts: asleep, it would stop the server for everybody.

Only idle sleep is prevented (the screen may still turn off). Closing a laptop's lid, or the
user choosing "Sleep", still puts it to sleep: the group then moves on as after any disconnection.

* Windows: ``SetThreadExecutionState`` from the agent's thread (it lasts until reset, or until
  the thread ends).
* macOS: ``caffeinate -i -w <pid>``, which also ends by itself with the application.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("mdh.awake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def caffeinate_command(pid: int) -> list[str]:
    return ["/usr/bin/caffeinate", "-i", "-w", str(pid)]


class KeepAwake:
    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or sys.platform
        self.held = False
        self._caffeinate: subprocess.Popen | None = None

    def hold(self) -> None:
        if self.held:
            return
        try:
            if self.platform == "win32":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
            elif self.platform == "darwin":
                self._caffeinate = subprocess.Popen(  # noqa: S603 - fixed system program
                    caffeinate_command(os.getpid()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            else:
                return
        except OSError as exc:  # never a reason not to host
            log.warning("cannot keep the computer awake: %s", exc)
            return
        self.held = True
        log.info("computer kept awake while hosting")

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            if self.platform == "win32":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
            elif self._caffeinate is not None:
                self._caffeinate.terminate()
                self._caffeinate.wait(5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("cannot let the computer sleep again: %s", exc)
        self._caffeinate = None
        log.info("computer may sleep again")
