"""Why Minecraft stopped: the evidence names the mod at fault, or says it does not know."""

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from mdh_client.minecraft import crash
from mdh_client.minecraft.mods import file_name
from mdh_shared.models.api import Mod

LITH, FAPI, SODA = "gvQqBUqZ", "P7dR8mSH", "SodaAAAA"


def mod(project_id: str, slug: str, name: str, loader: str = "fabric") -> Mod:
    version_id = project_id[:4] + "VVVV"
    return Mod(project_id=project_id, version_id=version_id, slug=slug, name=name, version_number="1.2.3",
               url=f"https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{slug}.jar",
               sha512="a" * 128, size=10, loader=loader, mc_version="26.3")


def jar(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


def nested(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def server(tmp_path: Path):
    lith, fapi = mod(LITH, "lithium", "Lithium"), mod(FAPI, "fabric-api", "Fabric API")
    jar(tmp_path / "mods" / file_name(lith), {
        "fabric.mod.json": json.dumps({"id": "lithium", "mixins": ["lithium.mixins.json"]}).encode(),
        "net/caffeinemc/mods/lithium/Lithium.class": b"", "net/caffeinemc/mods/lithium/ai/Pathing.class": b""})
    jar(tmp_path / "mods" / file_name(fapi), {
        "fabric.mod.json": json.dumps({"id": "fabric-api", "jars": [{"file": "META-INF/jars/base.jar"}]}).encode(),
        "META-INF/jars/base.jar": nested({
            "fabric.mod.json": json.dumps({"id": "fabric-api-base", "mixins": [{"config": "fabric-api-base.mixins.json"}]}
                                          ).encode(),
            "net/fabricmc/fabric/impl/base/Event.class": b""})})
    return tmp_path, [lith, fapi]


def analyze(server, lines, report: str | None = None, loader="fabric"):
    directory, mods = server
    if report is not None:
        (directory / "crash-reports").mkdir(exist_ok=True)
        (directory / "crash-reports" / "crash-2026-11-02_10.00.00-server.txt").write_text(report, "utf-8")
    return crash.analyze(directory, lines, mods, loader, since=time.time() - 60, exit_code=1)


def test_missing_dependency(server) -> None:
    r = analyze(server, [
        "[10:00:00] [main/ERROR]: Incompatible mods found!",
        "\t\t - Mod 'Lithium' (lithium) 1.2.3 requires any version of 'Cardinal Components' (cardinal), which is missing!"])
    assert r.cause == "missing_dependency" and r.mod_project_id == LITH and r.mod_name == "Lithium"
    assert "Cardinal Components" in r.message and "Lithium 1.2.3" in r.message


def test_wrong_minecraft_version(server) -> None:
    r = analyze(server, ["\t - Mod 'Lithium' (lithium) 1.2.3 requires version 1.21.1 of minecraft, but only the "
                         "wrong version is present: 26.3!"])
    assert r.cause == "mod" and r.mod_project_id == LITH and "version de Minecraft" in r.message


def test_mixin_error_names_the_mod_of_its_configuration(server) -> None:
    r = analyze(server, ["Caused by: org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: "
                         "Mixin [fabric-api-base.mixins.json:EventMixin] from mod fabric-api-base failed injection"])
    assert r.cause == "mod" and r.mod_project_id == FAPI and "Mixin" in r.message


def test_first_frame_of_a_mod_in_the_crash_report(server) -> None:
    report = ("---- Minecraft Crash Report ----\nDescription: Exception in server tick loop\n\n"
              "java.lang.NullPointerException: Cannot invoke \"Object.hashCode()\"\n"
              "\tat java.base/java.util.HashMap.get(HashMap.java:556)\n"
              "\tat net.minecraft.world.entity.Mob.tick(Mob.java:12)\n"
              "\tat net.caffeinemc.mods.lithium.ai.Pathing.find(Pathing.java:40)\n"
              "\tat net.fabricmc.fabric.impl.base.Event.run(Event.java:1)\n")
    r = analyze(server, ["Exception in server tick loop"], report)
    assert r.cause == "mod" and r.mod_project_id == LITH and "Lithium 1.2.3" in r.message


def test_a_mixin_handler_in_a_minecraft_frame(server) -> None:
    r = analyze(server, ["\tat net.minecraft.server.MinecraftServer.handler$zza000$lithium$onTick(MinecraftServer.java:9)"])
    assert r.mod_project_id == LITH


def test_old_crash_reports_are_ignored(server) -> None:
    directory, _ = server
    report = directory / "crash-reports" / "crash-2026-10-01.txt"
    report.parent.mkdir()
    report.write_text("\tat net.caffeinemc.mods.lithium.Lithium.run(Lithium.java:1)\n", "utf-8")
    old = time.time() - 86400
    os.utime(report, (old, old))
    r = analyze(server, ["Exception in server tick loop"])
    assert r.cause == "unknown" and r.mod_project_id is None and "code 1" in r.message


@pytest.mark.parametrize(("line", "cause", "needle"), [
    ("java.lang.OutOfMemoryError: Java heap space", "memory", "RAM"),
    ("java.lang.UnsupportedClassVersionError: net/caffeinemc/mods/lithium/Lithium has been compiled by a more "
     "recent version of the Java Runtime (class file version 69.0)", "java", "Lithium"),
    ("[Server Watchdog/ERROR]: A single server tick took 60.00 seconds (should be max 0.05)", "watchdog", "figé"),
    ("Failed to load level data", "world", "sauvegarde"),
])
def test_other_causes(server, line, cause, needle) -> None:
    r = analyze(server, [line])
    assert r.cause == cause and needle in r.message


def test_paper_plugin(tmp_path) -> None:
    plugin = mod("LuckPerm", "luckperms", "LuckPerms", loader="paper")
    jar(tmp_path / "plugins" / file_name(plugin), {"plugin.yml": b"name: LuckPerms\nmain: me.lucko.Lp\n",
                                                   "me/lucko/Lp.class": b""})
    r = crash.analyze(tmp_path, ["[10:00:00 ERROR]: Error occurred while enabling LuckPerms v5.4 (Is it up to date?)"],
                      [plugin], "paper", since=time.time(), exit_code=1)
    assert r.mod_project_id == "LuckPerm" and "plugin" in r.message


def test_nothing_known(server) -> None:
    directory, _ = server
    r = crash.analyze(directory, ["Exception in server tick loop"], [], "fabric", since=time.time(), exit_code=None)
    assert r.cause == "unknown" and "code" not in r.message
