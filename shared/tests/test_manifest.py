import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from mdh_shared.constants import BLOCK_SIZE
from mdh_shared.models.manifest import FileEntry, Manifest
from mdh_shared.paths import UnsafePathError, safe_join
from mdh_shared.sync.manifest_builder import (
    IntegrityError,
    build_manifest,
    diff_manifests,
    missing_blocks,
    verify_directory,
)

ZERO = "0" * 64


def make_server_dir(root: Path) -> None:
    (root / "world" / "region").mkdir(parents=True)
    (root / "world" / "level.dat").write_bytes(b"level")
    # 2.5 blocks, with a repeated block to exercise deduplication.
    block = os.urandom(BLOCK_SIZE)
    (root / "world" / "region" / "r.0.0.mca").write_bytes(block + block + b"tail" * 100)
    (root / "server.properties").write_text("motd=hello\n")
    (root / "empty.json").write_bytes(b"")
    # Things that must never be synchronised:
    (root / "logs").mkdir()
    (root / "logs" / "latest.log").write_text("log")
    (root / "world" / "session.lock").write_bytes(b"lock")
    (root / "paper.jar").write_bytes(b"PK")
    (root / "plugins").mkdir()
    (root / "plugins" / "evil.jar").write_bytes(b"PK")
    (root / "cache").mkdir()
    (root / "cache" / "x.bin").write_bytes(b"x")


def test_build_manifest_content_and_exclusions(tmp_path: Path) -> None:
    make_server_dir(tmp_path)
    manifest, report = build_manifest(tmp_path)

    paths = [f.path for f in manifest.files]
    assert paths == [
        "empty.json",
        "server.properties",
        "world/level.dat",
        "world/region/r.0.0.mca",
    ]
    assert sorted(report.skipped_executables) == ["paper.jar", "plugins/evil.jar"]

    region = manifest.files[3]
    assert region.size == 2 * BLOCK_SIZE + 400
    assert len(region.blocks) == 3
    assert region.blocks[0] == region.blocks[1]  # identical blocks share one id
    assert region.sha256 == hashlib.sha256((tmp_path / "world/region/r.0.0.mca").read_bytes()).hexdigest()
    assert manifest.files[0].blocks == ()
    # properties + level.dat + 2 distinct region blocks; the empty file has no block.
    assert len(manifest.block_hashes()) == 4


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_junctions_are_never_followed(tmp_path: Path) -> None:
    import _winapi

    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwords.txt").write_text("hunter2")
    root = tmp_path / "server"
    (root / "world").mkdir(parents=True)
    _winapi.CreateJunction(str(secret), str(root / "world" / "link"))

    manifest, report = build_manifest(root)
    assert manifest.files == ()
    assert report.skipped_links == ["world/link"]
    with pytest.raises(UnsafePathError):
        safe_join(root, "world/link/passwords.txt")


def test_digest_is_deterministic(tmp_path: Path) -> None:
    make_server_dir(tmp_path)
    m1, _ = build_manifest(tmp_path)
    m2, _ = build_manifest(tmp_path)
    assert m1.digest() == m2.digest()
    assert Manifest.model_validate_json(m1.canonical_bytes()).digest() == m1.digest()


def test_diff_detects_added_modified_deleted(tmp_path: Path) -> None:
    make_server_dir(tmp_path)
    before, _ = build_manifest(tmp_path)
    (tmp_path / "world" / "level.dat").write_bytes(b"level2")
    (tmp_path / "server.properties").unlink()
    (tmp_path / "world" / "new.dat").write_bytes(b"new")
    after, _ = build_manifest(tmp_path)

    diff = diff_manifests(before, after)
    assert diff.added == ("world/new.dat",)
    assert diff.modified == ("world/level.dat",)
    assert diff.deleted == ("server.properties",)
    assert diff_manifests(after, after).is_empty
    assert missing_blocks(after, before.block_hashes()) == {
        hashlib.sha256(b"level2").hexdigest(),
        hashlib.sha256(b"new").hexdigest(),
    }


def test_verify_directory_detects_corruption(tmp_path: Path) -> None:
    make_server_dir(tmp_path)
    manifest, _ = build_manifest(tmp_path)
    verify_directory(tmp_path, manifest)

    region = tmp_path / "world" / "region" / "r.0.0.mca"
    data = bytearray(region.read_bytes())
    data[BLOCK_SIZE + 17] ^= 0xFF  # one flipped bit, same size
    region.write_bytes(bytes(data))
    with pytest.raises(IntegrityError, match="content mismatch world/region/r.0.0.mca"):
        verify_directory(tmp_path, manifest)


def test_verify_directory_detects_missing_and_extra(tmp_path: Path) -> None:
    make_server_dir(tmp_path)
    manifest, _ = build_manifest(tmp_path)
    (tmp_path / "world" / "level.dat").unlink()
    (tmp_path / "intruder.txt").write_text("x")
    with pytest.raises(IntegrityError) as exc:
        verify_directory(tmp_path, manifest)
    assert "missing file world/level.dat" in str(exc.value)
    assert "unexpected file intruder.txt" in str(exc.value)


@pytest.mark.parametrize(
    "files, message",
    [
        ([FileEntry.model_construct(path="b", size=0, sha256=ZERO, blocks=()),
          FileEntry.model_construct(path="a", size=0, sha256=ZERO, blocks=())], "sorted"),
        ([FileEntry.model_construct(path="a", size=0, sha256=ZERO, blocks=()),
          FileEntry.model_construct(path="a", size=0, sha256=ZERO, blocks=())], "duplicate"),
        ([FileEntry.model_construct(path="World/a", size=0, sha256=ZERO, blocks=()),
          FileEntry.model_construct(path="world/a", size=0, sha256=ZERO, blocks=())], "case"),
        ([FileEntry.model_construct(path="world", size=0, sha256=ZERO, blocks=()),
          FileEntry.model_construct(path="world/a", size=0, sha256=ZERO, blocks=())], "also a file"),
    ],
)
def test_manifest_rejects_inconsistent_listings(files: list[FileEntry], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Manifest(files=tuple(files))


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"path": "../x", "size": 0, "sha256": ZERO, "blocks": []}, "component"),
        ({"path": "plugins/x.JAR", "size": 0, "sha256": ZERO, "blocks": []}, "executable"),
        ({"path": "a", "size": 10, "sha256": ZERO, "blocks": []}, "expected 1"),
        ({"path": "a", "size": 0, "sha256": ZERO, "blocks": [ZERO]}, "expected 0"),
        ({"path": "a", "size": 0, "sha256": "XYZ", "blocks": []}, "pattern"),
        ({"path": "a", "size": -1, "sha256": ZERO, "blocks": []}, "greater than or equal"),
    ],
)
def test_file_entry_validation(entry: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FileEntry.model_validate(entry)


def test_manifest_from_untrusted_json_is_validated() -> None:
    raw = '{"format_version":1,"block_size":1048576,"files":[{"path":"C:/x","size":0,"sha256":"%s","blocks":[]}]}' % ZERO
    with pytest.raises(ValidationError):
        Manifest.model_validate_json(raw)
