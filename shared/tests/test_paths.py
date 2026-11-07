from pathlib import Path

import pytest

from mdh_shared.paths import (
    UnsafePathError,
    find_case_collisions,
    is_safe_relative_path,
    safe_join,
    validate_relative_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "world/region/r.0.0.mca",
        "server.properties",
        "world_nether/DIM-1/region/r.-1.2.mca",
        "plugins/Essentials/config.yml",
        "config/été.json",
    ],
)
def test_accepts_canonical_paths(path: str) -> None:
    assert validate_relative_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../outside",
        "world/../../outside",
        "world/./level.dat",
        "world//level.dat",
        "world/",
        "world\\level.dat",
        "..\\..\\Windows\\system32",
        "C:/Windows/win.ini",
        "C:level.dat",
        "world/level.dat:hidden",           # NTFS alternate data stream
        "//server/share/file",              # UNC
        "CON",
        "world/nul.txt",
        "world/Com1.mca",
        "LPT9.log",
        "world/trailing.",
        "world/trailing ",
        " leading",
        "world/a\x00b",
        "world/a\nb",
        "world/a*b",
        "world/a?b",
        'world/a"b',
        "a" * 300,
        "e\u0301t\u00e9.txt",               # not NFC
    ],
)
def test_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(UnsafePathError):
        validate_relative_path(path)
    assert not is_safe_relative_path(path)


def test_safe_join_stays_inside_root(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "world/level.dat") == tmp_path.resolve() / "world" / "level.dat"
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, "../escape")


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks requires Developer Mode or admin rights on Windows")
    with pytest.raises(UnsafePathError):
        safe_join(root, "link/file.txt")


def test_case_collisions() -> None:
    assert find_case_collisions(["world/a.mca", "World/A.mca", "world/b.mca"]) == [
        ("world/a.mca", "World/A.mca")
    ]
    assert find_case_collisions(["a", "b"]) == []
