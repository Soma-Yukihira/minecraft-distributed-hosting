"""Modrinth modpacks: import into the server (mods and configuration files), and export for players."""

import asyncio
import hashlib
import io
import json
import zipfile

import pytest

from fake_modrinth import FakeModrinth
from mdh_client.minecraft import modpack
from mdh_client.minecraft.downloads import make_client
from mdh_client.minecraft.modrinth import Modrinth, ModrinthError
from mdh_client.minecraft.mods import ModInstaller

FAPI, LITH, SODIUM, CHUNKY, PACK = "P7dR8mSH", "gvQqBUqZ", "AANobbMI", "fALzjamp", "1KVo5zza"


def mrpack(files: list[dict], extra: dict[str, bytes] | None = None, loader: str = "fabric-loader",
           mc: str = "1.21.1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "versionId": "1.0", "name": "Pack", "files": files,
            "dependencies": {"minecraft": mc, loader: "0.16.9"}}))
        for name, content in (extra or {}).items():
            z.writestr(name, content)
    return buf.getvalue()


def entry(fake: FakeModrinth, project_id: str, server: str = "required") -> dict:
    v = fake.versions[project_id][0]
    f = v["files"][0]
    return {"path": f"mods/{f['filename']}", "hashes": {"sha1": f["hashes"]["sha1"], "sha512": f["hashes"]["sha512"]},
            "env": {"client": "required", "server": server}, "downloads": [f["url"]], "fileSize": f["size"]}


def world_with_pack(files_of, extra=None, **kw) -> FakeModrinth:
    fake = FakeModrinth()
    fake.add(FAPI, "fabric-api", game_versions=("1.21.1",), version_id="FapiAAAA", client_side="required")
    fake.add(LITH, "lithium", game_versions=("1.21.1",), version_id="LithBBBB", deps=[FAPI])
    fake.add(SODIUM, "sodium", game_versions=("1.21.1",), version_id="SodiCCCC", server_side="unsupported",
             client_side="required")
    fake.add(PACK, "cobblemon-pack", loaders=("fabric",), game_versions=("1.21.1",), project_type="modpack",
             version_id="PackDDDD", title="Cobblemon Pack", content=mrpack(files_of(fake), extra, **kw))
    return fake


def run(fake, tmp_path, coro_of):
    async def go():
        async with make_client(fake.transport()) as client:
            installer = ModInstaller(tmp_path / "cache", tmp_path / "dl", client)
            (tmp_path / "dl").mkdir(exist_ok=True)
            return await coro_of(Modrinth(client), installer)
    return asyncio.run(go())


def test_import_a_modpack(tmp_path) -> None:
    def files(fake):
        return [entry(fake, FAPI), entry(fake, LITH), entry(fake, SODIUM, server="unsupported"),
                {"path": "mods/from-github.jar", "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                 "env": {"client": "required", "server": "required"},
                 "downloads": ["https://github.com/someone/mod/releases/download/1.0/mod.jar"], "fileSize": 10},
                {"path": "shaderpacks/pretty.zip", "hashes": {}, "downloads": [], "fileSize": 1}]

    fake = world_with_pack(files, {"overrides/config/lithium.properties": b"x=1",
                                   "overrides/mods/bundled.jar": b"PK", "overrides/run.bat": b"del *"})
    pack = run(fake, tmp_path, lambda m, i: modpack.import_pack(m, i, "cobblemon-pack"))
    assert (pack.modpack.name, pack.modpack.version_number, pack.mc_version) == ("Cobblemon Pack", "1.0-fabric",
                                                                                 "1.21.1")
    assert [m.slug for m in pack.mods] == ["fabric-api", "lithium"]
    lith = pack.mods[1]
    assert lith.requires == [FAPI] and lith.loader == "fabric" and lith.mc_version == "1.21.1"
    assert lith.sha512 == fake.versions[LITH][0]["files"][0]["hashes"]["sha512"]
    assert pack.client_only == 2 and pack.outside == ["from-github.jar"] and pack.programs == 2
    assert pack.modpack.sha512 == hashlib.sha512(fake.files[pack.modpack.url]).hexdigest()


def test_a_modpack_for_another_loader_is_refused(tmp_path) -> None:
    fake = world_with_pack(lambda f: [entry(f, FAPI)], loader="neoforge")
    with pytest.raises(ModrinthError, match="neoforge"):
        run(fake, tmp_path, lambda m, i: modpack.import_pack(m, i, "cobblemon-pack"))
    with pytest.raises(ModrinthError, match="pas un modpack"):
        run(fake, tmp_path, lambda m, i: modpack.import_pack(m, i, "lithium"))


def test_configuration_files_only(tmp_path) -> None:
    pack = tmp_path / "pack.mrpack"
    pack.write_bytes(mrpack([], {
        "overrides/config/lithium.properties": b"from overrides",
        "overrides/config/shared.toml": b"overrides",
        "server-overrides/config/shared.toml": b"server-overrides",    # wins
        "overrides/kubejs/server_scripts/recipes.js": b"// recipes",
        "overrides/mods/bundled.jar": b"PK", "overrides/tools/run.bat": b"del *", "overrides/lib/x.dll": b"MZ",
        "overrides/server.properties": b"online-mode=false", "overrides/ops.json": b"[]",
        "overrides/world/level.dat": b"their world", "overrides/world/region/r.0.0.mca": b"x",
        "overrides/../escape.txt": b"out", "overrides/resourcepacks/pack.zip": b"PK",
    }))
    server = tmp_path / "server"
    (server / "config").mkdir(parents=True)
    (server / "server.properties").write_text("online-mode=true")
    assert modpack.apply_overrides(pack, server) == 3
    assert (server / "config" / "shared.toml").read_bytes() == b"server-overrides"
    assert (server / "kubejs" / "server_scripts" / "recipes.js").exists()
    assert (server / "server.properties").read_text() == "online-mode=true"
    for never in ("mods/bundled.jar", "tools/run.bat", "lib/x.dll", "ops.json", "world/level.dat",
                  "resourcepacks/pack.zip"):
        assert not (server / never).exists(), never
    assert not (tmp_path / "escape.txt").exists()
    assert sorted(json.loads((server / modpack.MANIFEST).read_text())) == [
        "config/lithium.properties", "config/shared.toml", "kubejs/server_scripts/recipes.js"]

    # Another modpack: what came from the first one and is not in the new one goes away.
    (server / "config" / "mine.toml").write_text("not from a modpack")
    other = tmp_path / "other.mrpack"
    other.write_bytes(mrpack([], {"overrides/config/shared.toml": b"v2"}))
    assert modpack.apply_overrides(other, server) == 1
    assert not (server / "config" / "lithium.properties").exists() and not (server / "kubejs" / "server_scripts"
                                                                            / "recipes.js").exists()
    assert (server / "config" / "shared.toml").read_bytes() == b"v2"
    assert (server / "config" / "mine.toml").exists()


def mods_for_players(fake: FakeModrinth) -> list:
    fake.add(FAPI, "fabric-api", version_id="FapiAAAA", client_side="required")
    fake.add(LITH, "lithium", version_id="LithBBBB", deps=[FAPI], client_side="optional")
    fake.add(CHUNKY, "chunky", version_id="ChunEEEE", client_side="optional")
    fake.add("SrvOnly1", "server-only", version_id="SrvOFFFF", client_side="unsupported")
    fake.add("Optlib01", "optional-lib", version_id="OptlGGGG", client_side="optional")
    fake.add("NeedLib1", "needs-lib", version_id="NeedHHHH", deps=["Optlib01"], client_side="required")

    async def go(m, _):
        mods = []
        for ref in ("lithium", "chunky", "server-only", "needs-lib"):
            mods += await m.resolve(ref, "fabric", "26.3", mods)
        return mods
    return go


def test_mods_for_players(tmp_path) -> None:
    fake = FakeModrinth()
    mods = run(fake, tmp_path, mods_for_players(fake))
    players = run(fake, tmp_path, lambda m, i: modpack.for_players(m, mods))
    level = {p.mod.slug: p.required for p in players}
    # Chunky is a server tool; "server-only" says so; a library needed by a required mod is required.
    assert level == {"lithium": False, "fabric-api": True, "needs-lib": True, "optional-lib": True}
    assert all(p.sha1 == hashlib.sha1(fake.files[p.mod.url]).hexdigest() for p in players)

    server = {"name": "Survie", "mc_version": "26.3", "loader": "fabric", "config_revision": 7,
              "mods": [m.model_dump(mode="json") for m in mods]}
    dest = tmp_path / "Survie.mrpack"
    run(fake, tmp_path, lambda m, i: modpack.export_for_players(m, i, server, "0.17.2", dest, "mrpack"))
    with zipfile.ZipFile(dest) as z:
        assert z.namelist() == ["modrinth.index.json"]
        index = json.loads(z.read("modrinth.index.json"))
    assert index["dependencies"] == {"minecraft": "26.3", "fabric-loader": "0.17.2"}
    assert index["name"] == "Survie" and index["versionId"] == "26.3-7"
    lith = next(f for f in index["files"] if "lithium" in f["path"])
    assert lith["env"] == {"client": "optional", "server": "required"}
    assert lith["path"] == "mods/lithium-1.0+26.3.jar" and lith["downloads"][0].startswith("https://cdn.modrinth.com/")
    assert set(lith["hashes"]) == {"sha1", "sha512"}

    zipped = tmp_path / "Survie - mods.zip"
    run(fake, tmp_path, lambda m, i: modpack.export_for_players(m, i, server, "0.17.2", zipped, "zip"))
    with zipfile.ZipFile(zipped) as z:
        names = set(z.namelist())
        readme = z.read("LISEZMOI.txt").decode("utf-8")
        assert z.read("mods/fabric-api-1.0+26.3.jar") == fake.files[next(m.url for m in mods if m.slug == "fabric-api")]
    assert names == {"LISEZMOI.txt", "mods/fabric-api-1.0+26.3.jar", "mods/needs-lib-1.0+26.3.jar",
                     "mods/optional-lib-1.0+26.3.jar", "facultatifs/lithium-1.0+26.3.jar"}
    assert "Fabric 26.3" in readme and "TLauncher" in readme and "facultatifs" in readme

    with pytest.raises(ModrinthError, match="Paper"):
        run(fake, tmp_path, lambda m, i: modpack.export_for_players(m, i, {**server, "loader": "paper"}, "x",
                                                                    dest, "mrpack"))
