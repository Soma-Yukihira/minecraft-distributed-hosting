"""Datapacks from Modrinth in the world's ``datapacks`` folder (docs/ARCHITECTURE.md 3.12).

Like mods: chosen by an admin, downloaded from Modrinth's CDN by each host, SHA-512 checked,
allowed PC by PC. The application manages only the files it put there (``mdh-*.zip``, listed in
``.mdh/datapacks.json``): a datapack added by hand stays where it is.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from mdh_client.minecraft.mods import ModInstaller, Status
from mdh_shared.models.api import Datapack

MANIFEST = Path(".mdh") / "datapacks.json"
_SAFE = re.compile(r"[^A-Za-z0-9_.+-]")


def file_name(pack: Datapack) -> str:
    return f"mdh-{_SAFE.sub('_', pack.slug)[:40]}-{pack.version_id}.zip"


def level_name(properties: dict) -> str:
    name = str(properties.get("level-name") or "world")
    return name if re.fullmatch(r"[A-Za-z0-9_ .-]{1,64}", name) and name.strip(". ") == name else "world"


async def install(server_dir: Path, level: str, packs: list[Datapack], installer: ModInstaller,
                  status: Status | None = None) -> None:
    """Make the world's datapacks folder hold the group's datapacks, verified."""
    manifest = server_dir / MANIFEST
    try:
        previous = set(json.loads(manifest.read_text("utf-8")))
    except (OSError, ValueError, TypeError):
        previous = set()
    if not packs and not previous:
        return
    target = server_dir / level / "datapacks"
    target.mkdir(parents=True, exist_ok=True)
    wanted = {file_name(p): p for p in packs}
    for name, pack in wanted.items():
        cached = await installer.fetch_file(pack.url, pack.sha512, pack.size, ".zip", pack.name, status)
        dest = target / name
        if not dest.is_file() or dest.stat().st_size != pack.size:
            shutil.copyfile(cached, dest.with_suffix(".part"))
            os.replace(dest.with_suffix(".part"), dest)
    for name in previous - set(wanted):
        if re.fullmatch(r"mdh-[A-Za-z0-9_.+-]+\.zip", name):
            (target / name).unlink(missing_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(sorted(wanted)), "utf-8")
