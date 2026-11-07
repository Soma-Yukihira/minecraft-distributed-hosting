"""World map in a browser with BlueMap (docs/ARCHITECTURE.md 4.11).

BlueMap is an ordinary mod/plugin of the group's list. Before each start, the host rewrites the
two settings that are this PC's business, whatever the synchronised files say:

* its web server listens on ``127.0.0.1`` only (the VPS relay reaches it through the host's
  outgoing tunnel; nothing is opened on the box or in the Windows firewall);
* ``accept-download``: BlueMap downloads the game's textures from Mojang, under the Minecraft
  EULA. It is set only if this PC's user accepted that EULA in the application, never otherwise.

Anonymous statistics sent by BlueMap are turned off, like everything this application sends.
The rendered map (``bluemap/``) is not synchronised: each host renders it again from the world.
When the world is new (no ``world/level.dat`` yet, ARCHITECTURE 3.17), the map drawn from the
previous world is dropped first: BlueMap would otherwise keep showing it where nobody went yet.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mdh_shared.models.enums import Loader

HEADER = "# Écrit par Minecraft Distributed Hosting à chaque démarrage : les modifications seront remplacées.\n"


def config_dir(server_dir: Path, loader: Loader) -> Path:
    return server_dir / ("plugins/BlueMap" if loader == Loader.PAPER else "config/bluemap")


def configure(server_dir: Path, loader: Loader, port: int, *, eula_accepted: bool) -> None:
    if not 1024 <= port <= 65535:
        raise ValueError("map port out of range")
    if not (server_dir / "world" / "level.dat").exists():
        forget_rendered_map(server_dir)
    folder = config_dir(server_dir, loader)
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / "core.conf", HEADER + (
        f"accept-download: {'true' if eula_accepted else 'false'}\n"
        'data: "bluemap"\n'
        # One thread: the game's ticks come first on a PC that is also used for something else.
        "render-thread-count: 1\n"
        "scan-for-mod-resources: true\n"
        "metrics: false\n"
        'log: {\n  file: "bluemap/logs/debug.log"\n  append: false\n}\n'))
    _write(folder / "webserver.conf", HEADER + (
        "enabled: true\n"
        'webroot: "bluemap/web"\n'
        'ip: "127.0.0.1"\n'
        f"port: {port}\n"
        'log: {\n  file: "bluemap/logs/webserver.log"\n  append: false\n}\n'))


def forget_rendered_map(server_dir: Path) -> None:
    """Drop the tiles and render progress, keep the downloaded textures (no new download)."""
    data = server_dir / "bluemap"
    shutil.rmtree(data / "web" / "maps", ignore_errors=True)
    for name in ("pluginState.json", "tasks.dat"):
        (data / name).unlink(missing_ok=True)


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
