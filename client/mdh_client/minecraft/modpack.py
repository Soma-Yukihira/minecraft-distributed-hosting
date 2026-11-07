"""Modrinth modpacks (docs/ARCHITECTURE.md 3.11).

* **Import**: an admin picks a Fabric modpack on Modrinth. Its mods become the server's list of
  mods (each one an ordinary entry, downloaded and checked by every host); files hosted
  anywhere else are left out and named. Every host then downloads the ``.mrpack`` itself,
  checks its SHA-512 and copies its configuration files (``overrides/``, then
  ``server-overrides/``) into the server folder: never a program, never the world nor what the
  application manages itself.
* **Export for players**: the mods players need in their own Minecraft, as a ``.mrpack``
  (Prism Launcher, Modrinth App, ATLauncher) or as a ``.zip`` of files to drop in the mods
  folder (TLauncher, or by hand).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import ValidationError

from mdh_client.minecraft.modrinth import Modrinth, ModrinthError
from mdh_client.minecraft.mods import ModInstaller, Status
from mdh_shared.models.api import (ANTICHEAT_MOD_SLUG, FABRIC_ANTIXRAY_SLUG, MAP_MOD_SLUG, MAX_MODS,
                                   PREGEN_MOD_SLUG, Mod, Modpack)
from mdh_shared.models.enums import Loader

INDEX = "modrinth.index.json"
MAX_INDEX_BYTES = 8 * 1024**2
#: What a modpack may copy into the server folder.
MAX_OVERRIDE_FILES = 20_000
MAX_OVERRIDE_BYTES = 1024**3
#: Files of the server folder that came from the modpack (removed when the modpack changes).
MANIFEST = Path(".mdh") / "modpack-files.json"
_CDN_URL = re.compile(r"^https://cdn\.modrinth\.com/data/([A-Za-z0-9]{8})/versions/([A-Za-z0-9]{8})/[^/?#]+$")
#: Never copied out of a modpack: programs.
_PROGRAMS = frozenset({".jar", ".exe", ".dll", ".so", ".dylib", ".jnilib", ".bat", ".cmd", ".ps1", ".sh",
                       ".com", ".msi", ".vbs", ".scr"})
#: Folders and files the application or Minecraft manage, or that only matter in the game.
_MANAGED_DIRS = frozenset({"mods", "plugins", "libraries", "versions", ".mdh", ".fabric", "logs",
                           "crash-reports", "bluemap", "saves", "resourcepacks", "shaderpacks", "screenshots"})
_MANAGED_FILES = frozenset({"server.properties", "eula.txt", "ops.json", "whitelist.json", "banned-players.json",
                            "banned-ips.json", "usercache.json", "options.txt"})
#: Server tools: nothing for players to see in their game.
#: Grim and the Fabric anti-X-ray work on the server only (Grim's page does not say so).
_SERVER_TOOLS = frozenset({MAP_MOD_SLUG, PREGEN_MOD_SLUG, ANTICHEAT_MOD_SLUG, FABRIC_ANTIXRAY_SLUG})


@dataclass
class ImportedPack:
    """What importing a modpack means, before anything is saved."""

    modpack: Modpack
    mc_version: str
    mods: list[Mod]
    #: Files only players need (shaders, client mods): not on the server.
    client_only: int = 0
    #: Mods hosted outside Modrinth: left out (their files are not checked by Modrinth).
    outside: list[str] = field(default_factory=list)
    #: Programs among the configuration files: never copied.
    programs: int = 0


def _bad(message: str) -> ModrinthError:
    return ModrinthError(f"Ce modpack ne peut pas être utilisé : {message}")


def read_index(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(INDEX)
            if info.file_size > MAX_INDEX_BYTES:
                raise _bad("index trop gros.")
            index = json.loads(z.read(info))
            programs = sum(1 for i in z.infolist() if _override_path(i.filename) is not None
                           and PurePosixPath(i.filename).suffix.lower() in _PROGRAMS)
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad("ce n'est pas un fichier .mrpack valide.") from exc
    if not isinstance(index, dict) or index.get("game") != "minecraft" or not isinstance(index.get("files"), list):
        raise _bad("ce n'est pas un modpack Minecraft.")
    index["_programs"] = programs
    return index


async def import_pack(modrinth: Modrinth, installer: ModInstaller, ref: str,
                      status: Status | None = None) -> ImportedPack:
    """Download the newest Fabric version of a modpack and turn it into a list of mods."""
    modpack, _ = await modrinth.modpack(ref)
    path = await installer.fetch_file(modpack.url, modpack.sha512, modpack.size, ".mrpack", modpack.name, status)
    index = await asyncio.to_thread(read_index, path)
    deps = index.get("dependencies") if isinstance(index.get("dependencies"), dict) else {}
    if "fabric-loader" not in deps:
        other = next((k for k in ("neoforge", "forge", "quilt-loader") if k in deps), None)
        raise _bad(f"il utilise {other or 'un autre chargeur que Fabric'}, et le serveur ne lance que Fabric.")
    mc_version = str(deps.get("minecraft") or "")
    result = ImportedPack(modpack=modpack, mc_version=mc_version, mods=[], programs=index["_programs"])
    found: list[tuple[str, str, str, str, int, str]] = []
    for f in index["files"]:
        if not isinstance(f, dict):
            continue
        rel = str(f.get("path", ""))
        env = f.get("env") if isinstance(f.get("env"), dict) else {}
        if env.get("server") == "unsupported" or not (rel.startswith("mods/") and rel.endswith(".jar")):
            result.client_only += 1
            continue
        name = PurePosixPath(rel).name
        url = next((u for u in f.get("downloads", []) if isinstance(u, str) and _CDN_URL.match(u)), None)
        sha512 = (f.get("hashes") or {}).get("sha512")
        if url is None or not isinstance(sha512, str) or not isinstance(f.get("fileSize"), int):
            result.outside.append(name)
            continue
        m = _CDN_URL.match(url)
        assert m is not None
        found.append((m.group(1), m.group(2), url, sha512, f["fileSize"], name))
    if len(found) > MAX_MODS:
        raise _bad(f"il contient {len(found)} mods pour le serveur, au-delà des {MAX_MODS} gérés.")
    projects = await modrinth.projects(sorted({p for p, *_ in found}))
    versions = await modrinth.versions(sorted({v for _, v, *_ in found}))
    present = {p for p, *_ in found}
    for project_id, version_id, url, sha512, size, name in found:
        project, version = projects.get(project_id), versions.get(version_id)
        if project is None or version is None:
            result.outside.append(name)
            continue
        requires = sorted({d["project_id"] for d in version.get("dependencies", [])
                           if d.get("dependency_type") == "required" and d.get("project_id") in present
                           and d["project_id"] != project_id})
        try:
            mod = Mod(project_id=project_id, version_id=version_id, slug=project["slug"],
                      name=str(project.get("title") or project["slug"])[:100].strip() or project["slug"],
                      version_number=str(version.get("version_number") or version_id)[:64].strip(),
                      url=url, sha512=sha512, size=size, loader=Loader.FABRIC, mc_version=mc_version,
                      requires=requires)
        except (KeyError, TypeError, ValidationError):
            result.outside.append(name)
            continue
        if all(m.project_id != mod.project_id for m in result.mods):
            result.mods.append(mod)
    if not result.mods:
        raise _bad("aucun de ses mods n'est publié sur Modrinth pour un serveur.")
    return result


def _override_path(name: str) -> str | None:
    """The path inside the server folder of a configuration file of the modpack, if it is one."""
    for prefix in ("overrides/", "server-overrides/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def _skipped(rel: str, worlds: set[str]) -> bool:
    parts = PurePosixPath(rel).parts
    if (not parts or rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts)
            or ":" in parts[0]):
        return True  # outside the server folder
    if parts[0] in _MANAGED_DIRS or parts[0] in worlds or (len(parts) == 1 and parts[0] in _MANAGED_FILES):
        return True
    return PurePosixPath(rel).suffix.lower() in _PROGRAMS


def apply_overrides(pack: Path, server_dir: Path) -> int:
    """Copy the modpack's configuration files into the server folder. Returns how many."""
    manifest = server_dir / MANIFEST
    try:
        previous = set(json.loads(manifest.read_text("utf-8")))
    except (OSError, ValueError, TypeError):
        previous = set()
    with zipfile.ZipFile(pack) as z:
        infos = z.infolist()
        # A folder holding level.dat is a world: the group's world is never replaced this way.
        worlds = set()
        for i in infos:
            rel = _override_path(i.filename)
            if rel is not None and PurePosixPath(rel).name == "level.dat":
                worlds.add(PurePosixPath(rel).parts[0])
        chosen: dict[str, zipfile.ZipInfo] = {}
        for i in infos:  # server-overrides/ comes after overrides/ in the loop below: it wins
            rel = _override_path(i.filename)
            if rel is not None and not i.is_dir() and i.filename.startswith("overrides/") and not _skipped(rel, worlds):
                chosen[rel] = i
        for i in infos:
            rel = _override_path(i.filename)
            if rel is not None and not i.is_dir() and i.filename.startswith("server-overrides/") \
                    and not _skipped(rel, worlds):
                chosen[rel] = i
        if len(chosen) > MAX_OVERRIDE_FILES or sum(i.file_size for i in chosen.values()) > MAX_OVERRIDE_BYTES:
            raise _bad("ses fichiers de configuration sont trop nombreux ou trop gros.")
        root = server_dir.resolve()
        written = []
        for rel, info in chosen.items():
            dest = (server_dir / rel).resolve()
            if not dest.is_relative_to(root):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".mdh-part")
            try:
                with z.open(info) as src, open(tmp, "wb") as out:
                    remaining = info.file_size + 1
                    while chunk := src.read(min(1024 * 1024, remaining)):
                        remaining -= len(chunk)
                        if remaining <= 0:
                            raise _bad("un fichier est plus gros qu'annoncé.")
                        out.write(chunk)
                os.replace(tmp, dest)
                written.append(rel)
            except OSError:
                tmp.unlink(missing_ok=True)  # a name Windows refuses: skipped
    for rel in previous - set(written):  # left by another modpack (or an older version of it)
        if not _skipped(rel, set()):
            path = (server_dir / rel).resolve()
            if path.is_relative_to(root) and path.is_file():
                path.unlink()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(sorted(written)), "utf-8")
    return len(written)


# --- For players ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlayerMod:
    mod: Mod
    #: False: players may do without it (performance, comfort).
    required: bool
    sha1: str
    file_name: str


async def for_players(modrinth: Modrinth, mods: list[Mod]) -> list[PlayerMod]:
    """The server's mods that players install too, as Modrinth describes each project."""
    projects = await modrinth.projects(sorted({m.project_id for m in mods}))
    versions = await modrinth.versions(sorted({m.version_id for m in mods}))
    by_id = {m.project_id: m for m in mods}
    level: dict[str, bool] = {}  # project id -> required
    for m in mods:
        side = (projects.get(m.project_id) or {}).get("client_side")
        if m.slug in _SERVER_TOOLS or side == "unsupported":
            continue
        level[m.project_id] = side == "required"
    changed = True
    while changed:  # what a required mod needs is required too
        changed = False
        for pid in [p for p, req in level.items() if req]:
            for dep in by_id[pid].requires:
                if dep in level and not level[dep]:
                    level[dep] = changed = True
    result = []
    for m in mods:
        if m.project_id not in level:
            continue
        files = (versions.get(m.version_id) or {}).get("files", [])
        file = next((f for f in files if f.get("url") == m.url), None)
        sha1 = (file or {}).get("hashes", {}).get("sha1")
        if not isinstance(sha1, str) or not re.fullmatch(r"[0-9a-f]{40}", sha1):
            raise ModrinthError(f"Modrinth ne donne pas l'empreinte de {m.name} : réessaie plus tard.")
        result.append(PlayerMod(m, level[m.project_id], sha1, _file_name(m)))
    return result


def _file_name(mod: Mod) -> str:
    name = unquote(mod.url.rsplit("/", 1)[1])
    return name if re.fullmatch(r"[^\\/:*?\"<>|\x00-\x1f]{1,200}\.jar", name) else f"{mod.slug}-{mod.version_id}.jar"


def write_mrpack(dest: Path, server: dict[str, Any], loader_version: str, mods: list[PlayerMod]) -> None:
    index = {
        "formatVersion": 1, "game": "minecraft",
        "versionId": f"{server['mc_version']}-{server.get('config_revision', 1)}",
        "name": server["name"],
        "summary": f"Les mods pour jouer sur {server['name']} (Minecraft {server['mc_version']}, Fabric).",
        "files": [{"path": f"mods/{p.file_name}", "hashes": {"sha1": p.sha1, "sha512": p.mod.sha512},
                   "env": {"client": "required" if p.required else "optional", "server": "required"},
                   "downloads": [p.mod.url], "fileSize": p.mod.size} for p in mods],
        "dependencies": {"minecraft": server["mc_version"], "fabric-loader": loader_version},
    }
    _write_zip(dest, {INDEX: json.dumps(index, indent=2, ensure_ascii=False).encode()}, {})


def readme(server: dict[str, Any], loader_version: str, optional: bool) -> str:
    mc = server["mc_version"]
    return (f"Mods pour jouer sur « {server['name']} » : Minecraft {mc} avec Fabric {loader_version}.\r\n\r\n"
            "Avec TLauncher :\r\n"
            f"1. Choisis la version « Fabric {mc} » dans TLauncher et lance-la une fois, puis quitte le jeu.\r\n"
            "2. Ouvre le dossier des mods (bouton dossier de TLauncher, puis « mods ») et retires-en les "
            "anciens mods.\r\n"
            "3. Copies-y tous les fichiers du dossier « mods » de ce zip.\r\n"
            + ("4. Facultatif : ajoute aussi ceux du dossier « facultatifs » (performance, confort).\r\n"
               if optional else "")
            + "\r\nAvec Prism Launcher, le Modrinth App ou ATLauncher, demande plutôt le fichier .mrpack : "
              "il s'installe en un clic.\r\n")


async def write_mods_zip(dest: Path, server: dict[str, Any], loader_version: str, mods: list[PlayerMod],
                         installer: ModInstaller, status: Status | None = None) -> None:
    files: dict[str, Path] = {}
    for p in mods:
        path = await installer.fetch(p.mod, status)
        files[f"{'mods' if p.required else 'facultatifs'}/{p.file_name}"] = path
    text = readme(server, loader_version, any(not p.required for p in mods))
    await asyncio.to_thread(_write_zip, dest, {"LISEZMOI.txt": text.encode("utf-8")}, files)


def _write_zip(dest: Path, data: dict[str, bytes], files: dict[str, Path]) -> None:
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in data.items():
            z.writestr(name, content)
        for name, path in files.items():
            z.write(path, name, zipfile.ZIP_STORED)  # JARs are already compressed
    os.replace(tmp, dest)


async def export_for_players(modrinth: Modrinth, installer: ModInstaller, server: dict[str, Any],
                             loader_version: str, dest: Path, kind: Literal["mrpack", "zip"],
                             status: Status | None = None) -> list[PlayerMod]:
    if Loader(server["loader"]) != Loader.FABRIC:
        raise ModrinthError("Seuls les mods Fabric s'installent aussi chez les joueurs : avec Paper, les "
                            "plugins tournent sur le serveur seul.")
    mods = await for_players(modrinth, [Mod.model_validate(m) for m in server.get("mods", [])])
    if not mods:
        raise ModrinthError("Aucun mod de ce serveur n'est à installer chez les joueurs : ils jouent avec "
                            "Fabric seul, ou même sans mod.")
    if kind == "mrpack":
        await asyncio.to_thread(write_mrpack, dest, server, loader_version, mods)
    else:
        await write_mods_zip(dest, server, loader_version, mods, installer, status)
    return mods
