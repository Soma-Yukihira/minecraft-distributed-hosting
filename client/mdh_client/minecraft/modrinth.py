"""Mods and plugins from Modrinth (docs/ARCHITECTURE.md 3.9).

Only the admin's PC talks to Modrinth's API, to search and choose files. What it stores in the
server's configuration is the exact file (project, version, CDN address, SHA-512, size): every
host then downloads that file and nothing else (:mod:`mdh_client.minecraft.mods`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import ValidationError

from mdh_client.minecraft.downloads import DownloadError, fetch_json
from mdh_shared.models.api import MOD_LOADERS, Datapack, Mod, Modpack, ResourcePack
from mdh_shared.models.enums import Loader

MODRINTH_API = "https://api.modrinth.com/v2"
#: Project ids, and the slugs typed by hand ("bluemap").
_PROJECT_REF = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9]{8}$")
MAX_DEPENDENCIES = 30
#: Ids per request of the bulk endpoints (``/projects``, ``/versions``).
_BULK = 100
#: Most stable first.
_CHANNELS = ("release", "beta", "alpha")


class ModrinthError(DownloadError):
    """Shown to the user as it is (French)."""


@dataclass(frozen=True)
class SearchHit:
    project_id: str
    slug: str
    title: str
    description: str
    downloads: int


@dataclass(frozen=True)
class ModUpdate:
    """One line of what updating changes: ``old`` None = added, ``new`` None = removed."""

    name: str
    old: str | None
    new: str | None


def without(mods: list[Mod], project_id: str) -> list[Mod]:
    """The list without that project, nor the dependencies only it needed."""
    kept = [m for m in mods if m.project_id != project_id]
    while True:
        needed = {r for m in kept for r in m.requires}
        pruned = [m for m in kept if not m.dependency or m.project_id in needed]
        if len(pruned) == len(kept):
            return pruned
        kept = pruned


class Modrinth:
    def __init__(self, client: httpx.AsyncClient, api: str = MODRINTH_API) -> None:
        self.client = client
        self.api = api

    async def _get(self, path: str, **params: Any) -> Any:
        query = urlencode({k: json.dumps(v) if isinstance(v, list) else v for k, v in params.items()})
        try:
            return await fetch_json(self.client, f"{self.api}{path}" + (f"?{query}" if query else ""))
        except DownloadError as exc:
            if "HTTP 404" in str(exc):
                raise ModrinthError("Projet introuvable sur Modrinth.") from exc
            raise ModrinthError(f"Modrinth ne répond pas correctement ({exc}).") from exc

    async def search(self, query: str, loader: Loader, mc_version: str, limit: int = 20) -> list[SearchHit]:
        """Projects that have a file for this server type and version, and run on a server."""
        loaders = MOD_LOADERS.get(loader)
        if loaders is None:
            raise ModrinthError("Vanilla ne charge ni mod ni plugin : choisis Paper ou Fabric.")
        facets = [[f"categories:{name}" for name in loaders], [f"versions:{mc_version}"],
                  ["project_type:mod", "project_type:plugin"],
                  ["server_side:required", "server_side:optional"]]
        result = await self._get("/search", query=query.strip()[:100], limit=max(1, min(limit, 50)),
                                 facets=json.dumps(facets))
        hits = []
        for h in result.get("hits", []):
            if isinstance(h, dict) and _ID.fullmatch(str(h.get("project_id", ""))):
                hits.append(SearchHit(h["project_id"], str(h.get("slug") or h["project_id"]),
                                      str(h.get("title") or h["project_id"])[:100],
                                      str(h.get("description") or "")[:300], int(h.get("downloads") or 0)))
        return hits

    async def search_packs(self, query: str, mc_version: str, limit: int = 20) -> list[SearchHit]:
        """Resource packs made for this version of Minecraft."""
        facets = [["project_type:resourcepack"], [f"versions:{mc_version}"]]
        result = await self._get("/search", query=query.strip()[:100], limit=max(1, min(limit, 50)),
                                 facets=json.dumps(facets))
        return [SearchHit(h["project_id"], str(h.get("slug") or h["project_id"]),
                          str(h.get("title") or h["project_id"])[:100], str(h.get("description") or "")[:300],
                          int(h.get("downloads") or 0))
                for h in result.get("hits", [])
                if isinstance(h, dict) and _ID.fullmatch(str(h.get("project_id", "")))]

    async def pack(self, ref: str, mc_version: str, required: bool) -> ResourcePack:
        """The newest file of this resource pack for the server's version of Minecraft."""
        project = await self.project(ref)
        versions = await self._get(f"/project/{project['id']}/version", loaders=["minecraft"],
                                   game_versions=[mc_version])
        for channel in _CHANNELS:
            for v in versions:
                zips = [f for f in v.get("files", []) if str(f.get("filename", "")).endswith(".zip")]
                file = next((f for f in zips if f.get("primary")), zips[0] if zips else None)
                if v.get("version_type") != channel or file is None:
                    continue
                try:
                    return ResourcePack(name=str(project.get("title") or project["slug"])[:100].strip(),
                                        url=file["url"], sha1=file["hashes"]["sha1"], size=file["size"],
                                        required=required, project_id=project["id"], version_id=v["id"],
                                        version_number=str(v.get("version_number") or v["id"])[:64].strip())
                except (KeyError, TypeError, ValidationError) as exc:
                    raise ModrinthError(f"Le fichier de {project.get('title')} ne peut pas être utilisé.") from exc
        raise ModrinthError(f"{project.get('title')} n'a pas de fichier pour Minecraft {mc_version}.")

    async def search_modpacks(self, query: str, limit: int = 20) -> list[SearchHit]:
        """Fabric modpacks (the only kind of modpack the server runs), any Minecraft version."""
        facets = [["project_type:modpack"], ["categories:fabric"]]
        # Nothing typed: the most downloaded ones ("Moddé populaire" template).
        result = await self._get("/search", query=query.strip()[:100], limit=max(1, min(limit, 50)),
                                 facets=json.dumps(facets), index="relevance" if query.strip() else "downloads")
        return [SearchHit(h["project_id"], str(h.get("slug") or h["project_id"]),
                          str(h.get("title") or h["project_id"])[:100], str(h.get("description") or "")[:300],
                          int(h.get("downloads") or 0))
                for h in result.get("hits", [])
                if isinstance(h, dict) and _ID.fullmatch(str(h.get("project_id", "")))]

    async def modpack(self, ref: str) -> tuple[Modpack, str]:
        """The newest Fabric version of this modpack, and the Minecraft version it is made for."""
        project = await self.project(ref)
        if project.get("project_type") != "modpack":
            raise ModrinthError(f"{project.get('title')} n'est pas un modpack.")
        versions = await self._get(f"/project/{project['id']}/version", loaders=["fabric"])
        for channel in _CHANNELS:
            for v in versions:
                packs = [f for f in v.get("files", []) if str(f.get("filename", "")).endswith(".mrpack")]
                file = next((f for f in packs if f.get("primary")), packs[0] if packs else None)
                if v.get("version_type") != channel or file is None or not v.get("game_versions"):
                    continue
                try:
                    return Modpack(project_id=project["id"], version_id=v["id"],
                                   name=str(project.get("title") or project["slug"])[:100].strip(),
                                   version_number=str(v.get("version_number") or v["id"])[:64].strip(),
                                   url=file["url"], sha512=file["hashes"]["sha512"],
                                   size=file["size"]), str(v["game_versions"][0])
                except (KeyError, TypeError, ValidationError) as exc:
                    raise ModrinthError(f"Le fichier de {project.get('title')} ne peut pas être utilisé "
                                        "(trop gros, ou hors du CDN de Modrinth).") from exc
        raise ModrinthError(f"{project.get('title')} n'a pas de version pour Fabric : l'application ne sait "
                            "lancer que les modpacks Fabric.")

    async def search_datapacks(self, query: str, mc_version: str, limit: int = 20) -> list[SearchHit]:
        """Datapacks made for this version of Minecraft (they run on any server type)."""
        facets = [["project_type:datapack"], [f"versions:{mc_version}"]]
        result = await self._get("/search", query=query.strip()[:100], limit=max(1, min(limit, 50)),
                                 facets=json.dumps(facets))
        return [SearchHit(h["project_id"], str(h.get("slug") or h["project_id"]),
                          str(h.get("title") or h["project_id"])[:100], str(h.get("description") or "")[:300],
                          int(h.get("downloads") or 0))
                for h in result.get("hits", [])
                if isinstance(h, dict) and _ID.fullmatch(str(h.get("project_id", "")))]

    async def datapack(self, ref: str, mc_version: str) -> Datapack:
        """The newest datapack file of this project for that version of Minecraft."""
        project = await self.project(ref)
        versions = await self._get(f"/project/{project['id']}/version", loaders=["datapack"],
                                   game_versions=[mc_version])
        for channel in _CHANNELS:
            for v in versions:
                zips = [f for f in v.get("files", []) if str(f.get("filename", "")).endswith(".zip")]
                file = next((f for f in zips if f.get("primary")), zips[0] if zips else None)
                if v.get("version_type") != channel or file is None:
                    continue
                try:
                    return Datapack(project_id=project["id"], version_id=v["id"], slug=project["slug"],
                                    name=str(project.get("title") or project["slug"])[:100].strip(),
                                    version_number=str(v.get("version_number") or v["id"])[:64].strip(),
                                    url=file["url"], sha512=file["hashes"]["sha512"], size=file["size"],
                                    mc_version=mc_version)
                except (KeyError, TypeError, ValidationError) as exc:
                    raise ModrinthError(f"Le fichier de {project.get('title')} ne peut pas être utilisé.") from exc
        raise ModrinthError(f"{project.get('title')} n'a pas de datapack pour Minecraft {mc_version}.")

    async def datapacks_for(self, packs: list[Datapack], mc_version: str) -> tuple[list[Datapack], list[str]]:
        """The same datapacks for another version of Minecraft, and those that have none."""
        found, missing = [], []
        for p in packs:
            try:
                found.append(await self.datapack(p.project_id, mc_version))
            except ModrinthError:
                missing.append(p.name)
        return found, missing

    async def projects(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Several projects at once, by id."""
        found: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), _BULK):
            for p in await self._get("/projects", ids=ids[i:i + _BULK]):
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    found[p["id"]] = p
        return found

    async def versions(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Several versions at once, by id."""
        found: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), _BULK):
            for v in await self._get("/versions", ids=ids[i:i + _BULK]):
                if isinstance(v, dict) and isinstance(v.get("id"), str):
                    found[v["id"]] = v
        return found

    async def project(self, ref: str) -> dict[str, Any]:
        if not _PROJECT_REF.fullmatch(ref):
            raise ModrinthError("Identifiant de projet Modrinth invalide.")
        return await self._get(f"/project/{quote(ref)}")

    async def _best_version(self, project_id: str, loader: Loader, mc_version: str) -> dict[str, Any] | None:
        versions = await self._get(f"/project/{project_id}/version", loaders=list(MOD_LOADERS[loader]),
                                   game_versions=[mc_version])
        for channel in _CHANNELS:  # newest first within each channel
            for v in versions:
                if v.get("version_type") == channel and _jar(v) is not None:
                    return v
        return None

    def _entry(self, project: dict[str, Any], version: dict[str, Any], loader: Loader, mc_version: str,
               dependency: bool) -> Mod:
        file = _jar(version)
        assert file is not None
        requires = [d["project_id"] for d in version.get("dependencies", [])
                    if d.get("dependency_type") == "required" and d.get("project_id")]
        try:
            return Mod(project_id=project["id"], version_id=version["id"], slug=project["slug"],
                       name=str(project.get("title") or project["slug"])[:100].strip() or project["slug"],
                       version_number=str(version.get("version_number") or version["id"])[:64].strip(),
                       url=file["url"], sha512=file["hashes"]["sha512"], size=file["size"], loader=loader,
                       mc_version=mc_version, requires=requires, dependency=dependency)
        except (KeyError, TypeError, ValidationError) as exc:
            raise ModrinthError(f"Le fichier de {project.get('title', project.get('id'))} ne peut pas être "
                                "utilisé (il ne vient pas du CDN de Modrinth ou il est incomplet).") from exc

    async def resolve(self, ref: str, loader: Loader, mc_version: str, current: list[Mod]) -> list[Mod]:
        """What adding this project means: its best file for the server, plus every required
        dependency not already in ``current``. Raises ModrinthError with a readable reason."""
        if loader not in MOD_LOADERS:
            raise ModrinthError("Vanilla ne charge ni mod ni plugin : choisis Paper ou Fabric.")
        have = {m.project_id for m in current}
        added: list[Mod] = []
        todo: list[tuple[str, bool]] = [(ref, False)]
        while todo:
            ref, dependency = todo.pop(0)
            project = await self.project(ref)
            if project.get("id") in have:
                continue
            if project.get("server_side") == "unsupported" and not dependency:
                raise ModrinthError(f"{project.get('title')} ne fonctionne que dans le jeu, pas sur un serveur.")
            version = await self._best_version(project["id"], loader, mc_version)
            if version is None:
                kind = "plugin" if loader == Loader.PAPER else "mod"
                raise ModrinthError(f"{project.get('title')} n'a pas de fichier pour {loader.value.capitalize()} "
                                    f"{mc_version}" + (f" (nécessaire à {added[0].name})" if dependency and added
                                                       else "") + f" : choisis un autre {kind} ou une autre version.")
            conflicts = [d["project_id"] for d in version.get("dependencies", [])
                         if d.get("dependency_type") == "incompatible" and d.get("project_id") in have]
            if conflicts:
                other = next(m.name for m in current if m.project_id == conflicts[0])
                raise ModrinthError(f"{project.get('title')} est incompatible avec {other}.")
            entry = self._entry(project, version, loader, mc_version, dependency)
            added.append(entry)
            have.add(entry.project_id)
            todo.extend((r, True) for r in entry.requires if r not in have)
            if len(added) > MAX_DEPENDENCIES:
                raise ModrinthError("Trop de dépendances : ce mod en demande plus de 30.")
        return added

    async def for_server(self, mods: list[Mod], loader: Loader, mc_version: str) -> tuple[list[Mod], list[str]]:
        """The same projects with their newest file for another server type or version.

        Returns the new list and the names of the projects that have no such file (then the
        list is not usable as it is)."""
        updated: list[Mod] = []
        missing: list[str] = []
        for m in mods:
            if m.dependency:
                continue  # chosen again from the mods that need them
            try:
                for entry in await self.resolve(m.project_id, loader, mc_version, updated):
                    updated.append(entry)
            except ModrinthError:
                missing.append(m.name)
        return updated, missing

    async def updates(self, mods: list[Mod], loader: Loader, mc_version: str) -> tuple[list[Mod], list[ModUpdate]]:
        """The newest files for the same server, and what changes (nothing: all up to date).

        Dependencies follow: a new file may need another project, or no longer need one. A
        project with no usable file any more keeps the one it has, with its dependencies."""
        if not mods or loader not in MOD_LOADERS:
            return mods, []
        fresh, _ = await self.for_server(mods, loader, mc_version)
        have = {m.project_id for m in fresh}
        kept = [m for m in mods if not m.dependency and m.project_id not in have]
        for m in kept + [d for d in mods if d.dependency and any(d.project_id in k.requires for k in kept)]:
            if m.project_id not in have:
                fresh.append(m)
                have.add(m.project_id)
        before = {m.project_id: m for m in mods}
        changes = [ModUpdate(m.name, before[m.project_id].version_number if m.project_id in before else None,
                             m.version_number)
                   for m in fresh if m.project_id not in before or before[m.project_id].version_id != m.version_id]
        changes += [ModUpdate(m.name, m.version_number, None) for m in mods if m.project_id not in have]
        return (fresh if changes else mods), changes


def _jar(version: dict[str, Any]) -> dict[str, Any] | None:
    jars = [f for f in version.get("files", []) if str(f.get("filename", "")).endswith(".jar")]
    return next((f for f in jars if f.get("primary")), jars[0] if jars else None)
