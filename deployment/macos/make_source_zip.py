"""Pack what a Mac needs to build the application, to send to whoever builds it (docs/DEPLOYMENT.md 5 quater).

    .venv\\Scripts\\python deployment\\macos\\make_source_zip.py

Writes dist\\mdh-mac-source-<version>.zip: the application's code (shared/, client/, without the
tests) and the build scripts. Never the VPS code, the tests, the documentation, secrets or keys.
Text files get Unix line endings (bash refuses a script with Windows ones).
"""

from __future__ import annotations

import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Folders and files copied (relative to the project).
INCLUDED = ("shared/mdh_shared", "shared/pyproject.toml", "client/mdh_client", "client/pyproject.toml",
            "deployment/macos", "deployment/windows/launcher.py", "deployment/windows/make_icon.py")
SKIPPED = ("__pycache__", "*.pyc", "*.egg-info", "build", "dist", ".DS_Store", "make_source_zip.py")
#: Refused outright: a key or a secret must never leave this PC.
FORBIDDEN = ("*.pem", "*.key", ".env", "*.env", "*.sqlite3", "*.token", "settings.json")
TEXT = (".py", ".sh", ".spec", ".toml", ".txt", ".md", ".json", ".cfg")


def files() -> list[Path]:
    found = []
    for entry in INCLUDED:
        path = ROOT / entry
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for p in candidates:
            parts = p.relative_to(ROOT).parts
            if any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in SKIPPED):
                continue
            if any(fnmatch.fnmatch(p.name.lower(), pattern) for pattern in FORBIDDEN):
                raise SystemExit(f"refusé : {p} ressemble à un secret, rien n'a été écrit")
            found.append(p)
    return found


def main() -> int:
    sys.path.insert(0, str(ROOT / "shared"))
    import mdh_shared

    version = mdh_shared.__version__
    top = f"mdh-mac-source-{version}"
    out = ROOT / "dist" / f"{top}.zip"
    out.parent.mkdir(exist_ok=True)
    chosen = files()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in chosen:
            data = p.read_bytes()
            if p.suffix in TEXT:
                data = data.replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(f"{top}/{p.relative_to(ROOT).as_posix()}", (2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if p.suffix == ".sh" else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    print(f"{out} ({len(chosen)} fichiers, {out.stat().st_size / 1024:.0f} Ko)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
