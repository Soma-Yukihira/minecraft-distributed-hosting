"""Check that a folder of wheels is enough to install a requirement on the VPS, offline.

``pip download --platform`` picks Linux wheels but still evaluates environment markers for
*this* PC: a Linux-only dependency (``uvloop; sys_platform != "win32"``) is silently skipped,
and the offline install on the VPS would fail. This walks the dependency tree with markers
evaluated for Ubuntu 24.04 / CPython 3.12 / x86_64 and prints what is missing, one requirement
per line (exit status 1), so package.ps1 can download it.

    python check_wheels.py WHEEL_DIR "mdh-server==0.1.0"
"""

from __future__ import annotations

import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

LINUX = {
    "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
    "platform_python_implementation": "CPython", "implementation_name": "cpython",
    "python_version": "3.12", "python_full_version": "3.12.3", "implementation_version": "3.12.3",
}


def read_wheels(directory: Path) -> dict[str, tuple[str, list[str]]]:
    found = {}
    for wheel in directory.glob("*.whl"):
        name, version, _, _ = parse_wheel_filename(wheel.name)
        with zipfile.ZipFile(wheel) as z:
            meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
            requires = Parser().parsestr(z.read(meta).decode()).get_all("Requires-Dist") or []
        found[name] = (str(version), requires)
    return found


def missing(directory: Path, root: str) -> list[str]:
    wheels = read_wheels(directory)
    todo, seen, absent = [Requirement(root)], set(), []
    while todo:
        req = todo.pop()
        name = canonicalize_name(req.name)
        key = (name, frozenset(req.extras))
        if key in seen:
            continue
        seen.add(key)
        if name not in wheels or not req.specifier.contains(wheels[name][0], prereleases=True):
            absent.append(str(Requirement(f"{req.name}{req.specifier}")))
            continue
        for line in wheels[name][1]:
            dep = Requirement(line)
            if dep.marker is None or any(dep.marker.evaluate({**LINUX, "extra": extra})
                                         for extra in ("", *req.extras)):
                todo.append(dep)
    return sorted(set(absent))


if __name__ == "__main__":
    result = missing(Path(sys.argv[1]), sys.argv[2])
    print("\n".join(result))
    sys.exit(1 if result else 0)
