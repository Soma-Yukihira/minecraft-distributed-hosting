"""Where server jars and Java runtimes come from, and how much we can trust them.

| Source | Integrity |
|--------|-----------|
| Mojang (vanilla) | SHA-1 published in the version JSON, itself SHA-1-pinned by the manifest |
| PaperMC Fill v3 | SHA-256 per build (stable channel only) |
| Fabric meta | no checksum published: SHA-256 pinned on first use, re-checked afterwards |
| Eclipse Temurin (Adoptium) | SHA-256 per binary |
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import TypeAdapter, ValidationError

from mdh_client.minecraft.downloads import DownloadError, fetch_json
from mdh_client.system.host_os import JavaTarget, java_target
from mdh_shared.models.api import McVersion
from mdh_shared.models.enums import Loader

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
FABRIC_META = "https://meta.fabricmc.net/v2"
ADOPTIUM_API = "https://api.adoptium.net/v3"

_version_adapter = TypeAdapter(McVersion)
_FABRIC_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,40}$")


class SourceError(DownloadError):
    pass


@dataclass(frozen=True)
class Artifact:
    url: str
    filename: str
    size: int | None
    sha256: str | None = None
    sha1: str | None = None
    trust: Literal["sha256", "sha1", "pin-on-first-use"] = "sha256"


@dataclass(frozen=True)
class ServerDistribution:
    loader: Loader
    mc_version: str
    java_major: int
    jar: Artifact
    build: str


def _valid_version(mc_version: str) -> str:
    try:
        return _version_adapter.validate_python(mc_version)
    except ValidationError as exc:
        raise SourceError(f"invalid Minecraft version {mc_version!r}") from exc


class SourceResolver:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._manifest: dict | None = None

    async def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = await fetch_json(self.client, MOJANG_MANIFEST)
        return self._manifest

    async def release_versions(self) -> list[str]:
        return [v["id"] for v in (await self.manifest())["versions"] if v["type"] == "release"]

    async def latest_release(self) -> str:
        return (await self.manifest())["latest"]["release"]

    async def mojang_version(self, mc_version: str) -> dict:
        mc_version = _valid_version(mc_version)
        entry = next((v for v in (await self.manifest())["versions"] if v["id"] == mc_version), None)
        if entry is None:
            raise SourceError(f"Minecraft {mc_version} does not exist")
        return await fetch_json(self.client, entry["url"], sha1=entry.get("sha1"))

    async def java_major(self, mc_version: str) -> int:
        info = await self.mojang_version(mc_version)
        return int(info.get("javaVersion", {}).get("majorVersion", 8))

    async def resolve(self, loader: Loader, mc_version: str) -> ServerDistribution:
        if loader == Loader.VANILLA:
            return await self.vanilla(mc_version)
        if loader == Loader.PAPER:
            return await self.paper(mc_version)
        if loader == Loader.FABRIC:
            return await self.fabric(mc_version)
        raise SourceError(f"unsupported loader {loader}")

    async def vanilla(self, mc_version: str) -> ServerDistribution:
        info = await self.mojang_version(mc_version)
        server = info.get("downloads", {}).get("server")
        if not server:
            raise SourceError(f"Mojang publishes no server for {mc_version}")
        jar = Artifact(url=server["url"], filename=f"minecraft_server-{mc_version}.jar",
                       size=server.get("size"), sha1=server["sha1"], trust="sha1")
        return ServerDistribution(Loader.VANILLA, mc_version,
                                  int(info.get("javaVersion", {}).get("majorVersion", 8)), jar, mc_version)

    async def paper_versions(self) -> list[str]:
        project = await fetch_json(self.client, PAPER_API)
        return [v for group in project["versions"].values() for v in group]

    async def paper(self, mc_version: str) -> ServerDistribution:
        mc_version = _valid_version(mc_version)
        builds = await fetch_json(self.client, f"{PAPER_API}/versions/{mc_version}/builds")
        if not isinstance(builds, list):
            raise SourceError(f"Paper has no builds for {mc_version}")
        stable = [b for b in builds if b.get("channel") == "STABLE"]
        if not stable:
            raise SourceError(f"Paper has no stable build for {mc_version} yet")
        build = max(stable, key=lambda b: int(b["id"]))
        download = build["downloads"]["server:default"]
        version_info = await fetch_json(self.client, f"{PAPER_API}/versions/{mc_version}")
        paper_java = int(version_info["version"]["java"]["version"]["minimum"])
        mojang_java = await self.java_major(mc_version)
        jar = Artifact(url=download["url"], filename=download["name"], size=download.get("size"),
                       sha256=download["checksums"]["sha256"], trust="sha256")
        return ServerDistribution(Loader.PAPER, mc_version, max(paper_java, mojang_java), jar,
                                  str(build["id"]))

    async def fabric(self, mc_version: str) -> ServerDistribution:
        mc_version = _valid_version(mc_version)
        loaders = await fetch_json(self.client, f"{FABRIC_META}/versions/loader/{mc_version}")
        stable_loaders = [entry["loader"]["version"] for entry in loaders if entry["loader"].get("stable")]
        installers = await fetch_json(self.client, f"{FABRIC_META}/versions/installer")
        stable_installers = [i["version"] for i in installers if i.get("stable")]
        if not stable_loaders or not stable_installers:
            raise SourceError(f"Fabric has no stable loader for {mc_version}")
        loader, installer = stable_loaders[0], stable_installers[0]
        for v in (loader, installer):
            if not _FABRIC_VERSION.fullmatch(v):
                raise SourceError(f"unexpected Fabric version string {v!r}")
        url = f"{FABRIC_META}/versions/loader/{mc_version}/{loader}/{installer}/server/jar"
        jar = Artifact(url=url, filename=f"fabric-server-{mc_version}-{loader}-{installer}.jar",
                       size=None, trust="pin-on-first-use")
        return ServerDistribution(Loader.FABRIC, mc_version, await self.java_major(mc_version), jar,
                                  f"{loader}/{installer}")

    async def java_runtime(self, major: int, target: JavaTarget | None = None) -> Artifact:
        if not 8 <= major <= 99:
            raise SourceError(f"unexpected Java version {major}")
        target = target or java_target()
        assets = await fetch_json(
            self.client,
            f"{ADOPTIUM_API}/assets/latest/{major}/hotspot?architecture={target.architecture}&image_type=jre"
            f"&os={target.os}&vendor=eclipse")
        archives = [a for a in assets if a["binary"]["package"]["name"].endswith(target.package_suffix)]
        if not archives:
            raise SourceError(f"no Temurin {major} JRE for {target.os} {target.architecture}")
        package = archives[0]["binary"]["package"]
        return Artifact(url=package["link"], filename=package["name"], size=package.get("size"),
                        sha256=package["checksum"], trust="sha256")
