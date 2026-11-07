"""Settings files of mods and plugins, edited in the application (docs/ARCHITECTURE.md 3.13).

The group's version of each file is kept by the control plane and written by every host before
Minecraft starts, after the modpack's own files: the same settings whatever PC hosts. Only text
files in the settings folders (``config/``, ``plugins/<plugin>/``…), never a program.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from mdh_shared.models.api import MAX_CONFIG_FILE_BYTES, config_path_problem


def is_config(path: str, size: int = 0) -> bool:
    return config_path_problem(path) is None and size <= MAX_CONFIG_FILE_BYTES


def apply(server_dir: Path, files: dict[str, str]) -> int:
    """Write the group's settings files. Returns how many."""
    root = server_dir.resolve()
    count = 0
    for path, content in files.items():
        if config_path_problem(path) is not None:
            continue  # checked by the control plane too
        dest = (server_dir / path).resolve()
        if not dest.is_relative_to(root):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".mdh-part")
        tmp.write_bytes(content.encode("utf-8"))
        os.replace(tmp, dest)
        count += 1
    return count


def syntax_problem(path: str, content: str) -> str | None:
    """A readable reason when the file would not load (None: looks fine, or cannot tell)."""
    suffix = path.rpartition(".")[2].lower()
    try:
        if suffix == "json":
            json.loads(content)
        elif suffix == "toml":
            tomllib.loads(content)
    except json.JSONDecodeError as exc:
        return f"JSON invalide, ligne {exc.lineno} : {exc.msg}"
    except tomllib.TOMLDecodeError as exc:
        return f"TOML invalide : {exc}"
    if suffix in ("yml", "yaml") and "\t" in content:
        return "YAML invalide : les tabulations sont interdites (utilise des espaces)"
    return None
