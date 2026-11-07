"""Write deployment/macos/requirements.txt: the exact packages of the Mac build, with their hashes.

The Mac application is built on a GitHub Mac, then signed on the Windows PC. Without this list,
the build would take whatever version PyPI serves that day, and the signature would vouch for
code nobody chose. With it, the Mac gets the same versions as the Windows build (those of this
PC's .venv) and pip refuses any file whose SHA-256 differs (``--require-hashes``).

Run it on the Windows PC after updating a dependency, then commit the new requirements.txt:

    .venv\\Scripts\\python deployment\\macos\\lock_requirements.py

pip picks Mac files with ``--platform`` but still reads conditions such as
``sys_platform == "darwin"`` as this Windows PC: the dependencies are therefore walked again
here with the Mac's values, and what only the Mac needs (macholib for PyInstaller) is added.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "deployment" / "macos" / "requirements.txt"
#: What the application needs (client/pyproject.toml, shared/pyproject.toml) and the builder.
TOP_LEVEL = ("PySide6", "httpx", "websockets", "psutil", "zstandard", "cryptography", "pydantic",
             "pyinstaller")
MAC = ("--platform", "macosx_14_0_arm64", "--python-version", "3.12", "--implementation", "cp",
       "--only-binary=:all:")
MAC_ENV = {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
           "platform_machine": "arm64", "python_version": "3.12", "python_full_version": "3.12.10",
           "implementation_name": "cpython", "platform_python_implementation": "CPython"}


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _resolve(names: list[str], constraints: Path) -> dict[str, dict]:
    pins = [f"{n}=={v}" if (v := _version(n)) else n for n in names]
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
                        "--report", str(report), *MAC, "-c", str(constraints), *pins], check=True)
        installs = json.loads(report.read_text("utf-8"))["install"]
    return {canonicalize_name(i["metadata"]["name"]): i for i in installs}


def _needed(found: dict[str, dict]) -> tuple[set[str], set[str]]:
    """(packages the Mac needs, the ones among them pip did not resolve)."""
    seen: set[str] = set()
    missing: set[str] = set()
    todo: list[tuple[str, frozenset[str]]] = [(canonicalize_name(n), frozenset()) for n in TOP_LEVEL]
    visited: set[tuple[str, frozenset[str]]] = set()
    while todo:
        name, extras = todo.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        seen.add(name)
        item = found.get(name)
        if item is None:
            missing.add(name)
            continue
        for spec in item["metadata"].get("requires_dist", []):
            req = Requirement(spec)
            wanted = any(req.marker is None or req.marker.evaluate({**MAC_ENV, "extra": e})
                         for e in (extras or {""}))
            if wanted:
                todo.append((canonicalize_name(req.name), frozenset(req.extras)))
    return seen, missing


def main() -> int:
    for name in TOP_LEVEL:
        if _version(name) is None:
            sys.exit(f"{name} n'est pas installé dans ce .venv")
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "constraints.txt"
        constraints.write_text("".join(
            f"{d.metadata['Name']}=={d.version}\n" for d in metadata.distributions()
            if d.metadata["Name"] and not d.metadata["Name"].startswith("mdh-")), "utf-8")
        names = list(TOP_LEVEL)
        for _ in range(5):
            found = _resolve(names, constraints)
            needed, missing = _needed(found)
            if not missing:
                break
            names += sorted(missing)
        else:
            sys.exit(f"dépendances introuvables pour le Mac : {', '.join(sorted(missing))}")
    lines = ["# Mac build: exact versions and SHA-256 (written by lock_requirements.py, do not edit).\n"]
    for key in sorted(needed):
        item = found[key]
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}\n")
    OUT.write_text("".join(lines), "utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)} : {len(needed)} paquets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
