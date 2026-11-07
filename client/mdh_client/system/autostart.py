"""Start with the computer, without administrator rights.

* Windows: one value in the current user's ``Run`` key. The installer removes it on uninstall.
* macOS: a LaunchAgent (``~/Library/LaunchAgents/fr.mdh.client.plist``), started at login.

Only offered by the installed application (``sys.frozen``): from a development checkout the
command line would depend on a virtualenv and environment variables.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MinecraftDistributedHosting"
MINIMIZED_FLAG = "--minimized"
#: macOS LaunchAgent label (and file name).
AGENT_LABEL = "fr.mdh.client"


def available(platform: str | None = None) -> bool:
    return (platform or sys.platform) in ("win32", "darwin") and bool(getattr(sys, "frozen", False))


def command(executable: str | None = None) -> str:
    exe = Path(executable or sys.executable)
    return f'"{exe}" {MINIMIZED_FLAG}'


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def launch_agent(executable: str | None = None) -> bytes:
    """The LaunchAgent: the application's own program, in the notification area (menu bar)."""
    return plistlib.dumps({
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(executable or sys.executable), MINIMIZED_FLAG],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    })


def is_enabled(key_path: str = RUN_KEY, *, platform: str | None = None, plist: Path | None = None) -> bool:
    if (platform or sys.platform) == "darwin":
        return (plist or launch_agent_path()).is_file()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool, *, executable: str | None = None, key_path: str = RUN_KEY,
                platform: str | None = None, plist: Path | None = None) -> None:
    if (platform or sys.platform) == "darwin":
        path = plist or launch_agent_path()
        if not enabled:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(launch_agent(executable))
        os.replace(tmp, path)  # read at the next login: nothing to load now
        return
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command(executable))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
