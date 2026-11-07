"""Local settings of the client, stored in ``%LOCALAPPDATA%\\MinecraftDistributedHosting`` on
Windows and ``~/Library/Application Support/MinecraftDistributedHosting`` on macOS.

Nothing secret lives in ``settings.json``: the refresh token is stored separately (see
:mod:`mdh_client.credentials`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

APP_DIR_NAME = "MinecraftDistributedHosting"


def app_home(platform: str | None = None) -> Path:
    override = os.environ.get("MDH_CLIENT_HOME")
    if override:
        return Path(override)
    if (platform or sys.platform) == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


@dataclass(frozen=True)
class Layout:
    """Where everything lives on disk. Each server gets its own isolated directory."""

    home: Path

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def credentials_file(self) -> Path:
        return self.home / "credentials.bin"

    @property
    def runtimes(self) -> Path:
        return self.home / "runtimes"

    @property
    def jar_cache(self) -> Path:
        return self.home / "cache" / "jars"

    @property
    def mod_cache(self) -> Path:
        """Verified mods and plugins, by SHA-512 (docs/ARCHITECTURE.md 3.9)."""
        return self.home / "cache" / "mods"

    @property
    def downloads(self) -> Path:
        return self.home / "cache" / "downloads"

    @property
    def servers(self) -> Path:
        return self.home / "servers"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    def server_dir(self, server_id: str) -> Path:
        if not (len(server_id) == 32 and all(c in "0123456789abcdef" for c in server_id)):
            raise ValueError("invalid server id")
        return self.servers / server_id

    def ensure(self) -> None:
        for p in (self.home, self.runtimes, self.jar_cache, self.downloads, self.servers, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class ClientSettings:
    api_url: str = "http://127.0.0.1:8080"
    username: str | None = None
    host_id: str | None = None
    display_name: str = "Mon PC"
    #: Let the control plane elect this PC.
    hosting_allowed: bool = True
    #: Accept hosting offers without asking (recommended on the main PC).
    auto_accept_offers: bool = True
    #: Upper bound of RAM this PC gives to Minecraft (None = suggestion from detected RAM).
    max_ram_mb: int | None = None
    server_port: int = 25565
    cpu_priority: Literal["normal", "below_normal"] = "normal"
    #: Hard CPU cap for the Minecraft process, in % of the whole machine (None = no cap).
    cpu_limit_percent: int | None = None
    auto_restart: bool = True
    max_restarts: int = 3
    restart_window_s: int = 600
    start_with_windows: bool = False
    #: Keep an up-to-date copy of the group's running worlds, to take over in seconds if the
    #: hosting PC disappears (only changed blocks are downloaded after the first time).
    standby_copy: bool = True
    #: Install new versions by itself, when the window is closed and this PC hosts nothing.
    auto_update: bool = True
    #: ISO date at which the user accepted the Minecraft EULA in this app (never set implicitly).
    eula_accepted_at: str | None = None
    #: SHA-256 of jars accepted on first use when the source publishes no checksum (Fabric).
    pinned_jar_hashes: dict[str, str] = field(default_factory=dict)
    #: SHA-512 of the mods and plugins this PC's user allowed to run here (docs/ARCHITECTURE.md P7).
    approved_mods: list[str] = field(default_factory=list)
    #: Local port of BlueMap's web server (127.0.0.1 only), reached through the relay tunnel.
    map_port: int = 8100

    @classmethod
    def load(cls, path: Path) -> ClientSettings:
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
