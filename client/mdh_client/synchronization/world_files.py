"""Worlds coming from elsewhere, and worlds taken away (docs/ARCHITECTURE.md 3.8).

* :func:`prepare_import` turns what the user picked -- a single-player world, an old server
  folder, or a ``.zip`` of either -- into the layout of a server folder (``world/``...), ready to
  be uploaded as a new version.
* :func:`export_world` writes a version as a ``.zip`` that opens in single-player: unzip it in
  ``.minecraft/saves``.

Nothing executable is ever imported, and a ``.zip`` cannot write outside its staging folder.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mdh_shared.constants import FORBIDDEN_EXTENSIONS
from mdh_shared.paths import UnsafePathError, safe_join, validate_relative_path

#: Server files worth keeping from an old server (who is op, who is banned).
KEPT_SERVER_FILES = ("ops.json", "banned-players.json", "banned-ips.json")
#: Unpacked size limit of an imported .zip (protects the disk from "zip bombs").
MAX_IMPORT_BYTES = 32 * 1024**3
SKIPPED_NAMES = {"session.lock"}


class WorldImportError(ValueError):
    """Shown to the user as is (French)."""


@dataclass(frozen=True)
class ImportSummary:
    source_kind: str      # "solo" (a single world) or "server" (an old server folder)
    world_name: str
    files: int
    size: int


def prepare_import(source: Path, staging: Path) -> ImportSummary:
    """Build ``staging`` (emptied first) from ``source``: a folder or a ``.zip``."""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise WorldImportError("Choisis un dossier de monde ou un fichier .zip.")
        unpacked = staging.parent / (staging.name + "-unzipped")
        shutil.rmtree(unpacked, ignore_errors=True)
        try:
            _unzip(source, unpacked)
            return _from_folder(_single_child(unpacked), staging)
        finally:
            shutil.rmtree(unpacked, ignore_errors=True)
    if source.is_dir():
        return _from_folder(source, staging)
    raise WorldImportError(f"Introuvable : {source}")


def _unzip(archive: Path, target: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if sum(m.file_size for m in members) > MAX_IMPORT_BYTES:
                raise WorldImportError("Archive trop grosse une fois décompressée (plus de 32 Go).")
            for member in members:
                name = member.filename.replace("\\", "/").lstrip("/")
                if _skipped(name):
                    continue
                try:
                    validate_relative_path(name)
                    dest = safe_join(target, name) if target.exists() else None
                except UnsafePathError:
                    raise WorldImportError(f"Archive refusée : chemin dangereux « {member.filename} ».") from None
                if dest is None:
                    target.mkdir(parents=True)
                    dest = safe_join(target, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
    except zipfile.BadZipFile:
        raise WorldImportError("Ce fichier .zip est abîmé ou n'en est pas un.") from None
    target.mkdir(parents=True, exist_ok=True)


def _single_child(folder: Path) -> Path:
    """Archives often wrap everything in one folder: look inside it."""
    entries = [p for p in folder.iterdir() if p.name not in ("__MACOSX",)]
    if len(entries) == 1 and entries[0].is_dir() and not (folder / "level.dat").exists():
        return entries[0]
    return folder


def _skipped(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in SKIPPED_NAMES or Path(name).suffix.lower() in FORBIDDEN_EXTENSIONS


def _copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    files = size = 0
    for path in sorted(src.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if _skipped(rel):
            continue
        target = dst.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files += 1
        size += path.stat().st_size
    return files, size


def _level_name(server_dir: Path) -> str:
    try:
        for line in (server_dir / "server.properties").read_text("latin-1").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "level-name" and value.strip():
                return value.strip()
    except OSError:
        pass
    return "world"


def _from_folder(folder: Path, staging: Path) -> ImportSummary:
    if (folder / "level.dat").is_file():                       # a world (single-player saves/X)
        files, size = _copy_tree(folder, staging / "world")
        return ImportSummary("solo", folder.name, files, size)
    if (folder / "server.properties").is_file():               # an old server folder
        level = _level_name(folder)
        if "/" in level or "\\" in level or not (folder / level / "level.dat").is_file():
            raise WorldImportError(f"Ce dossier de serveur n'a pas de monde « {level} » (level.dat absent).")
        files = size = 0
        # Paper/Spigot keep the Nether and the End next to the world; they are renamed with it.
        for suffix in ("", "_nether", "_the_end"):
            if (folder / f"{level}{suffix}").is_dir():
                f, s = _copy_tree(folder / f"{level}{suffix}", staging / f"world{suffix}")
                files, size = files + f, size + s
        for name in KEPT_SERVER_FILES:
            if (folder / name).is_file():
                shutil.copy2(folder / name, staging / name)
                files += 1
        return ImportSummary("server", level, files, size)
    raise WorldImportError("Aucun monde Minecraft trouvé ici : il faut un dossier qui contient level.dat "
                           "(monde solo) ou server.properties (ancien serveur).")


def export_world(server_dir: Path, dest: Path, name: str) -> int:
    """Zip the world of a server folder for single-player; returns the number of files.

    Single-player keeps the Nether and the End inside the world (``DIM-1``, ``DIM1``), where
    Paper keeps them in ``world_nether`` / ``world_the_end``: they are put back inside.
    """
    world = server_dir / "world"
    if not (world / "level.dat").is_file():
        raise WorldImportError("Cette sauvegarde ne contient pas de monde (level.dat absent).")
    folder = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "Monde"
    sources = [(world, "")]
    for suffix, dim in (("_nether", "DIM-1"), ("_the_end", "DIM1")):
        split = server_dir / f"world{suffix}" / dim
        if split.is_dir() and not (world / dim).exists():
            sources.append((split, dim + "/"))
    tmp = dest.with_name(dest.name + ".part")
    count = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, prefix in sources:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.name not in SKIPPED_NAMES:
                    zf.write(path, f"{folder}/{prefix}{path.relative_to(root).as_posix()}")
                    count += 1
    tmp.replace(dest)
    return count
