"""0.7.0 on the PC: datapacks from Modrinth, settings files, spark's answers, server templates."""

import asyncio
import hashlib
import json

import pytest

from fake_modrinth import FakeModrinth
from mdh_client.application import templates
from mdh_client.minecraft import config_files, datapacks
from mdh_client.minecraft.downloads import make_client
from mdh_client.minecraft.log_parser import LogParser
from mdh_client.minecraft.modrinth import Modrinth, ModrinthError
from mdh_client.minecraft.mods import ModInstaller
from mdh_shared.models.enums import Loader

TERRA, DUNGEONS = "8oi3bsk5", "tpehi7ww"


def fake() -> FakeModrinth:
    f = FakeModrinth()
    f.add(TERRA, "terralith", loaders=("datapack",), project_type="datapack", version_id="TerrAAAA",
          version_number="2.6.5+26.3")
    f.add(TERRA, "terralith", loaders=("datapack",), project_type="datapack", version_id="TerrBBBB",
          game_versions=("26.4",), version_number="2.6.6+26.4")
    f.add(DUNGEONS, "dungeons-and-taverns", loaders=("datapack",), project_type="datapack", version_id="DungCCCC")
    f.add("gvQqBUqZ", "lithium", version_id="LithBBBB")
    return f


def run(f, tmp_path, coro_of):
    async def go():
        async with make_client(f.transport()) as client:
            (tmp_path / "dl").mkdir(exist_ok=True)
            return await coro_of(Modrinth(client), ModInstaller(tmp_path / "cache", tmp_path / "dl", client))
    return asyncio.run(go())


def test_datapacks_from_modrinth(tmp_path) -> None:
    f = fake()
    hits = run(f, tmp_path, lambda m, _: m.search_datapacks("terra", "26.3"))
    assert [h.slug for h in hits] == ["terralith"]
    assert run(f, tmp_path, lambda m, _: m.search_datapacks("lithium", "26.3")) == []     # a mod is not one
    pack = run(f, tmp_path, lambda m, _: m.datapack("terralith", "26.3"))
    assert (pack.version_id, pack.mc_version, pack.version_number) == ("TerrAAAA", "26.3", "2.6.5+26.3")
    assert pack.sha512 == hashlib.sha512(f.files[pack.url]).hexdigest() and pack.url.endswith(".zip")
    moved, missing = run(f, tmp_path, lambda m, _: m.datapacks_for([pack], "26.4"))
    assert [p.version_id for p in moved] == ["TerrBBBB"] and missing == []
    assert run(f, tmp_path, lambda m, _: m.datapacks_for([pack], "27.1")) == ([], ["Terralith"])
    with pytest.raises(ModrinthError, match="pas de datapack"):
        run(f, tmp_path, lambda m, _: m.datapack("lithium", "26.3"))


def test_datapacks_in_the_world(tmp_path) -> None:
    f = fake()
    terra = run(f, tmp_path, lambda m, _: m.datapack("terralith", "26.3"))
    dungeons = run(f, tmp_path, lambda m, _: m.datapack("dungeons-and-taverns", "26.3"))
    server = tmp_path / "server"
    mine = server / "survie" / "datapacks" / "my-own.zip"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"PK")
    run(f, tmp_path, lambda _, i: datapacks.install(server, "survie", [terra, dungeons], i))
    folder = server / "survie" / "datapacks"
    assert sorted(p.name for p in folder.iterdir()) == [datapacks.file_name(dungeons), datapacks.file_name(terra),
                                                       "my-own.zip"]
    assert (folder / datapacks.file_name(terra)).read_bytes() == f.files[terra.url]
    run(f, tmp_path, lambda _, i: datapacks.install(server, "survie", [dungeons], i))
    assert sorted(p.name for p in folder.iterdir()) == [datapacks.file_name(dungeons), "my-own.zip"]
    assert json.loads((server / datapacks.MANIFEST).read_text()) == [datapacks.file_name(dungeons)]
    assert datapacks.level_name({"level-name": "../evil"}) == "world"
    assert datapacks.level_name({"level-name": "survie"}) == "survie"


def test_settings_files(tmp_path) -> None:
    server = tmp_path / "server"
    files = {"config/lithium.properties": "mixin.ai=false\n", "plugins/LuckPerms/config.yml": "server: survie\n",
             "config/../escape.toml": "x = 1", "kubejs/server_scripts/a.js": "evil()"}
    assert config_files.apply(server, files) == 2
    assert (server / "config" / "lithium.properties").read_text("utf-8") == "mixin.ai=false\n"
    assert not (tmp_path / "escape.toml").exists() and not (server / "kubejs").exists()
    assert config_files.is_config("config/sodium.json", 100) and not config_files.is_config("config/x.json", 10**7)
    assert "JSON invalide, ligne 2" in config_files.syntax_problem("config/a.json", '{"a": 1,\n}')
    assert "TOML invalide" in config_files.syntax_problem("config/a.toml", "a = = 1")
    assert "tabulations" in config_files.syntax_problem("plugins/X/config.yml", "a:\n\tb: 1")
    assert config_files.syntax_problem("config/a.toml", "a = 1") is None


def test_spark_answers_in_the_console() -> None:
    p = LogParser()
    assert p.parse("[10:00:00] [Server thread/INFO]: [⚡] https://spark.lucko.me/AbCdEf1234").value == \
        "https://spark.lucko.me/AbCdEf1234"
    assert p.parse("[10:00:00 INFO]: [?] https://spark.lucko.me/Zz12345").kind == "spark_url"   # garbled prefix
    assert p.parse("[10:00:00 INFO]: [⚡] Profiler operation failed unexpectedly. Error: x").kind == "spark_failed"
    assert p.parse("[10:00:00 INFO]: <Eve> https://spark.lucko.me/AbCdEf1234").kind == "chat"
    assert p.parse("[10:00:00 INFO]: [⚡] https://spark.lucko.me/AbCdEf1234/../../x") is None


def test_templates() -> None:
    keys = [t.key for t in templates.TEMPLATES]
    assert keys == ["vanilla", "optimized", "plugins", "modpack"]
    opt = templates.by_key("optimized")
    d = opt.defaults()
    assert d["loader"] == "fabric" and d["properties"]["simulation-distance"] == 8 and d["start_on_join"]
    assert {"lithium", "chunky", "spark"} <= set(opt.mods)
    assert templates.by_key("plugins").loader == Loader.PAPER and "spark" not in templates.by_key("plugins").mods
    assert templates.by_key("modpack").modpack
