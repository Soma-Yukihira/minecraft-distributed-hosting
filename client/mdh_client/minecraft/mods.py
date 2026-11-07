"""Put the group's mods (Fabric) or plugins (Paper) in a server folder, verified.

* Files come from Modrinth's CDN only (checked by the ``Mod`` model and by the download
  allowlist), and are kept only if their size and SHA-512 are those of the configuration.
* The folder ends up with exactly the declared JARs: any other JAR found there (left by an
  older configuration, or put there by hand) is moved aside to ``.mdh/removed-jars``, never
  deleted and never loaded.
* A PC runs a mod only once its owner allowed it (docs/ARCHITECTURE.md P7): see
  :func:`unapproved`.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx

from mdh_client.minecraft.downloads import download, sha512_file
from mdh_shared.models.api import Mod
from mdh_shared.models.enums import Loader

Status = Callable[[str, float | None], None]

_SAFE = re.compile(r"[^A-Za-z0-9_.+-]")


def folder(server_dir: Path, loader: Loader) -> Path:
    return server_dir / ("plugins" if loader == Loader.PAPER else "mods")


def file_name(mod: Mod) -> str:
    return f"{_SAFE.sub('_', mod.slug)[:40]}-{mod.version_id}.jar"


def unapproved(mods: Iterable[Mod], approved: Iterable[str]) -> list[Mod]:
    allowed = set(approved)
    return [m for m in mods if m.sha512 not in allowed]


class ModInstaller:
    def __init__(self, cache: Path, downloads: Path, client: httpx.AsyncClient) -> None:
        self.cache = cache
        self.downloads = downloads
        self.client = client

    def cached(self, mod: Mod) -> Path | None:
        path = self.cache / f"{mod.sha512}.jar"
        if path.is_file() and path.stat().st_size == mod.size and sha512_file(path) == mod.sha512:
            return path
        return None

    async def fetch(self, mod: Mod, status: Status | None = None) -> Path:
        """The verified file of ``mod`` in the cache, downloaded if needed."""
        return await self.fetch_file(mod.url, mod.sha512, mod.size, ".jar", mod.name, status)

    async def fetch_file(self, url: str, sha512: str, size: int, suffix: str, name: str,
                         status: Status | None = None) -> Path:
        """Any file from Modrinth's CDN (a mod, a modpack), verified, in the cache."""
        path = self.cache / f"{sha512}{suffix}"
        if path.is_file() and path.stat().st_size == size and sha512_file(path) == sha512:
            return path
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = self.downloads / f"{sha512[:32]}{suffix}.download"
        if status:
            status(f"Téléchargement de {name}", None)
        await download(self.client, url, tmp, sha512=sha512, expected_size=size, max_bytes=size)
        os.replace(tmp, path)
        return path

    async def fetch_all(self, mods: list[Mod], status: Status | None = None) -> None:
        for mod in mods:
            await self.fetch(mod, status)

    async def install(self, server_dir: Path, loader: Loader, mods: list[Mod],
                      status: Status | None = None) -> list[str]:
        """Make the mods (or plugins) folder hold exactly ``mods``. Returns the JARs moved aside."""
        await self.fetch_all(mods, status)
        if not mods and loader not in (Loader.PAPER, Loader.FABRIC):
            return []
        target = folder(server_dir, loader)
        target.mkdir(parents=True, exist_ok=True)
        wanted = {file_name(m): m for m in mods}
        moved: list[str] = []
        for path in target.iterdir():  # top level only: Paper keeps its own files in sub-folders
            if path.is_file() and path.suffix.lower() == ".jar":
                mod = wanted.get(path.name)
                if mod is not None and path.stat().st_size == mod.size and sha512_file(path) == mod.sha512:
                    continue
                aside = server_dir / ".mdh" / "removed-jars" / time.strftime("%Y%m%d-%H%M%S")
                aside.mkdir(parents=True, exist_ok=True)
                os.replace(path, aside / path.name)
                moved.append(path.name)
        for name, mod in wanted.items():
            dest = target / name
            if not dest.exists():
                cached = self.cached(mod)
                assert cached is not None
                shutil.copyfile(cached, dest.with_suffix(".part"))
                os.replace(dest.with_suffix(".part"), dest)
        return moved
