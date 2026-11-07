"""Importing a world from elsewhere and taking one away (docs/ARCHITECTURE.md 3.8)."""

import asyncio
import zipfile
from pathlib import Path

import pytest

from mdh_client.networking.api_client import ApiError
from mdh_client.synchronization.world_files import WorldImportError, export_world, prepare_import
from test_agent_live import PW, Recorder, until
from test_world_handover import backend, pc, state, wait_state, world_file  # noqa: F401


def solo_world(root: Path) -> Path:
    """What .minecraft/saves/<world> looks like."""
    (root / "region").mkdir(parents=True)
    (root / "level.dat").write_bytes(b"solo level")
    (root / "region" / "r.0.0.mca").write_bytes(b"\x01" * 5000)
    (root / "DIM-1" / "region").mkdir(parents=True)
    (root / "DIM-1" / "region" / "r.0.0.mca").write_bytes(b"\x02" * 3000)
    (root / "data").mkdir()
    (root / "data" / "chest").write_text("diamonds", "utf-8")
    (root / "session.lock").write_bytes(b"lock")
    (root / "evil.jar").write_bytes(b"PK jar")
    return root


def test_a_single_player_world_becomes_the_server_world(tmp_path):
    summary = prepare_import(solo_world(tmp_path / "Ma base"), tmp_path / "staging")
    assert (summary.source_kind, summary.world_name) == ("solo", "Ma base")
    out = tmp_path / "staging" / "world"
    assert (out / "level.dat").read_bytes() == b"solo level"
    assert (out / "DIM-1" / "region" / "r.0.0.mca").exists()
    assert not (out / "session.lock").exists() and not (out / "evil.jar").exists()


def test_an_old_server_keeps_its_three_dimensions_and_its_ops(tmp_path):
    old = tmp_path / "vieux serveur"
    solo_world(old / "survie")
    (old / "survie_nether" / "DIM-1").mkdir(parents=True)
    (old / "survie_nether" / "DIM-1" / "x").write_text("nether", "utf-8")
    (old / "server.properties").write_text("motd=salut\nlevel-name=survie\n", "latin-1")
    (old / "ops.json").write_text("[]", "utf-8")
    (old / "plugins").mkdir()
    (old / "plugins" / "Essentials.jar").write_bytes(b"jar")
    (old / "paper.jar").write_bytes(b"jar")
    summary = prepare_import(old, tmp_path / "staging")
    staging = tmp_path / "staging"
    assert (summary.source_kind, summary.world_name) == ("server", "survie")
    assert (staging / "world" / "level.dat").exists() and (staging / "world_nether" / "DIM-1" / "x").exists()
    assert (staging / "ops.json").exists()
    assert not (staging / "server.properties").exists()   # managed by the group's configuration
    assert not any(p.suffix == ".jar" for p in staging.rglob("*"))


def test_zip_archives_wrapped_or_not_and_hostile_ones(tmp_path):
    world = solo_world(tmp_path / "Ma base")
    wrapped = tmp_path / "wrapped.zip"
    with zipfile.ZipFile(wrapped, "w") as zf:
        for p in world.rglob("*"):
            if p.is_file():
                zf.write(p, "Ma base/" + p.relative_to(world).as_posix())
    assert prepare_import(wrapped, tmp_path / "s1").world_name == "Ma base"
    assert (tmp_path / "s1" / "world" / "data" / "chest").read_text("utf-8") == "diamonds"

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as zf:
        zf.writestr("level.dat", b"x")
        zf.writestr("../../outside.txt", b"pwned")
    with pytest.raises(WorldImportError, match="dangereux"):
        prepare_import(hostile, tmp_path / "s2")
    assert not (tmp_path / "outside.txt").exists()

    nothing = tmp_path / "photos"
    nothing.mkdir()
    (nothing / "a.png").write_bytes(b"png")
    with pytest.raises(WorldImportError, match="Aucun monde"):
        prepare_import(nothing, tmp_path / "s3")


def test_export_opens_in_single_player(tmp_path):
    server = tmp_path / "server"
    solo_world(server / "world")
    (server / "world_the_end" / "DIM1").mkdir(parents=True)
    (server / "world_the_end" / "DIM1" / "end").write_text("dragon", "utf-8")
    dest = tmp_path / "Survie.zip"
    export_world(server, dest, "Survie: le retour")
    names = zipfile.ZipFile(dest).namelist()
    assert "Survie_ le retour/level.dat" in names
    assert "Survie_ le retour/DIM1/end" in names                   # the End put back inside
    assert "Survie_ le retour/DIM-1/region/r.0.0.mca" in names


def test_import_then_play_then_export(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar = pc(tmp_path, backend, "pc_oscar", Recorder())
        await oscar.api.register("oscar", PW, None)
        await oscar.login("oscar", PW)
        group = await oscar.api.create_group("g")
        sid = (await oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                             memory_mb=1024))["server_id"]
        await until(lambda: oscar.channel.connected, what="online")

        # The group's world before: one played session.
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.api.stop(sid, countdown_s=0)
        await wait_state(oscar, sid, "offline")
        before = (await state(oscar, sid))["head_version_id"]

        version, summary = await oscar.import_world(sid, solo_world(tmp_path / "Ma base"))
        assert summary.source_kind == "solo"
        s = await state(oscar, sid)
        assert s["head_version_id"] == version["version_id"] and s["state"] == "offline"
        assert version["label"] == "Monde importé : Ma base" and version["parent_version_id"] == before
        assert before in [v["version_id"] for v in await oscar.api.versions(sid)]   # still restorable
        events = [e["type"] for e in await oscar.api.events(sid)]
        assert "import_started" in events and "lease_released" in events

        # The next start plays the imported world.
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert world_file(oscar, sid, "chest") == "diamonds"
        assert (oscar.layout.server_dir(sid) / "world" / "level.dat").read_bytes() == b"solo level"

        # Never while the server runs.
        with pytest.raises(ApiError) as busy:
            await oscar.api.start_import(sid, oscar.settings.host_id)
        assert busy.value.code == "server_busy"
        await oscar.api.stop(sid, countdown_s=0)
        await wait_state(oscar, sid, "offline")

        dest = tmp_path / "export.zip"
        await oscar.export_world(sid, (await state(oscar, sid))["head_version_id"], dest)
        with zipfile.ZipFile(dest) as zf:
            assert zf.read("Survie/data/chest") == b"diamonds"

        # An import whose PC disappears gives the server back after the lease.
        await oscar.api.start_import(sid, oscar.settings.host_id)
        assert (await state(oscar, sid))["state"] == "assigned"
        await wait_state(oscar, sid, "offline", timeout=30)
        assert "import_abandoned" in [e["type"] for e in await oscar.api.events(sid)]
        await oscar.close()

    asyncio.run(scenario())
