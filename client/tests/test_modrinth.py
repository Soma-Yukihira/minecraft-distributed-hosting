"""Choosing mods on Modrinth, and installing them verified (docs/ARCHITECTURE.md 3.9)."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from fake_modrinth import FakeModrinth
from mdh_client.minecraft import bluemap
from mdh_client.minecraft.downloads import ChecksumMismatch, UntrustedHost, make_client
from mdh_client.minecraft.modrinth import Modrinth, ModrinthError, ModUpdate, without
from mdh_client.minecraft.mods import ModInstaller, file_name, unapproved
from mdh_shared.models.api import Mod, mod_problems
from mdh_shared.models.enums import Loader

FAPI = "P7dR8mSH"
LITH = "gvQqBUqZ"
BLUE = "swbUV1cr"


@pytest.fixture
def fake() -> FakeModrinth:
    f = FakeModrinth()
    f.add(FAPI, "fabric-api", version_id="FapiAAAA")
    f.add(LITH, "lithium", deps=[FAPI], version_id="LithBBBB")
    f.add(BLUE, "bluemap", deps=[FAPI], version_id="BlueCCCC")
    f.add("PaperPl1", "luckperms", loaders=("bukkit", "paper", "spigot"), version_id="LuckDDDD")
    f.add("OldOnly1", "oldmod", game_versions=("1.20.1",), version_id="OldvEEEE")
    f.add("ClientO1", "minimap", server_side="unsupported", version_id="MiniFFFF")
    f.add("Sodium01", "sodiumlike", incompatible=[LITH], version_id="SodiGGGG")
    return f


def run(coro):
    return asyncio.run(coro)


def test_search_and_resolve_with_dependencies(fake) -> None:
    async def go():
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            hits = await m.search("lith", Loader.FABRIC, "26.3")
            assert [h.slug for h in hits] == ["lithium"]
            added = await m.resolve(LITH, Loader.FABRIC, "26.3", [])
            assert [(x.slug, x.dependency) for x in added] == [("lithium", False), ("fabric-api", True)]
            assert added[0].requires == [FAPI] and added[0].mc_version == "26.3"
            assert added[0].url.endswith("%2B26.3.jar")     # real Modrinth names have "+" in them
            # Already there: nothing twice.
            more = await m.resolve(BLUE, Loader.FABRIC, "26.3", added)
            assert [x.slug for x in more] == ["bluemap"]
            mods = added + more
            assert mod_problems(Loader.FABRIC, "26.3", mods, True) == []
            # Removing lithium keeps Fabric API (BlueMap still needs it); removing BlueMap too drops it.
            assert [x.slug for x in without(mods, LITH)] == ["fabric-api", "bluemap"]
            assert without(without(mods, LITH), BLUE) == []
            # By slug, as the map option does.
            assert (await m.resolve("bluemap", Loader.FABRIC, "26.3", []))[0].project_id == BLUE
            # Paper plugins: bukkit/spigot/paper files are accepted.
            assert (await m.resolve("PaperPl1", Loader.PAPER, "26.3", []))[0].loader == Loader.PAPER

    run(go())


@pytest.mark.parametrize("ref, loader, needle", [
    ("OldOnly1", Loader.FABRIC, "n'a pas de fichier pour Fabric 26.3"),
    ("ClientO1", Loader.FABRIC, "que dans le jeu"),
    ("PaperPl1", Loader.FABRIC, "n'a pas de fichier"),
    (LITH, Loader.VANILLA, "Vanilla"),
    ("nothere1", Loader.FABRIC, "introuvable"),
    ("../../etc", Loader.FABRIC, "invalide"),
])
def test_readable_refusals(fake, ref, loader, needle) -> None:
    async def go():
        async with make_client(fake.transport()) as client:
            with pytest.raises(ModrinthError) as err:
                await Modrinth(client).resolve(ref, loader, "26.3", [])
            assert needle in str(err.value)

    run(go())


def test_incompatible_mods_are_refused(fake) -> None:
    async def go():
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            current = await m.resolve(LITH, Loader.FABRIC, "26.3", [])
            with pytest.raises(ModrinthError, match="incompatible avec Lithium"):
                await m.resolve("Sodium01", Loader.FABRIC, "26.3", current)

    run(go())


def test_new_minecraft_version_picks_new_files(fake) -> None:
    fake.add(FAPI, "fabric-api", game_versions=("26.4",), version_id="Fapi264x")
    fake.add(LITH, "lithium", game_versions=("26.4",), deps=[FAPI], version_id="Lith264x")

    async def go():
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            mods = await m.resolve(LITH, Loader.FABRIC, "26.3", [])
            mods += await m.resolve(BLUE, Loader.FABRIC, "26.3", mods)
            new, missing = await m.for_server(mods, Loader.FABRIC, "26.4")
            assert missing == ["Bluemap"]
            assert {x.version_id for x in new} == {"Lith264x", "Fapi264x"}

    run(go())


def test_updates_follow_dependencies(fake) -> None:
    mods = None

    async def go():
        nonlocal mods
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            mods = await m.resolve(LITH, Loader.FABRIC, "26.3", [])
            mods += await m.resolve(BLUE, Loader.FABRIC, "26.3", mods)
            assert await m.updates(mods, Loader.FABRIC, "26.3") == (mods, [])     # up to date

            # Lithium 0.15 now needs a new library; BlueMap has a newer file (a beta is ignored
            # when a release exists).
            fake.add("Libr0001", "lib", version_id="LibrHHHH", version_number="2.0")
            fake.add(LITH, "lithium", deps=[FAPI, "Libr0001"], version_id="Lith015x", version_number="0.15")
            fake.add(BLUE, "bluemap", deps=[FAPI], version_id="Blue530x", version_number="5.30")
            fake.add(BLUE, "bluemap", deps=[FAPI], version_id="Blue6bta", version_number="6.0-beta",
                     version_type="beta")
            new, changes = await m.updates(mods, Loader.FABRIC, "26.3")
            assert {x.version_id for x in new} == {"Lith015x", "FapiAAAA", "LibrHHHH", "Blue530x"}
            assert set(changes) == {ModUpdate("Lithium", "1.0-fabric", "0.15"), ModUpdate("Lib", None, "2.0"),
                                    ModUpdate("Bluemap", "1.0-fabric", "5.30")}
            assert next(x for x in new if x.project_id == "Libr0001").dependency
            assert {m.project_id for m in new if not m.dependency} == {LITH, BLUE}

            # The new Lithium no longer needs Fabric API, and BlueMap disappeared for 26.3: it
            # keeps its file and what it needs.
            fake.add(LITH, "lithium", version_id="Lith016x", version_number="0.16")
            fake.versions[BLUE] = [v for v in fake.versions[BLUE] if "26.4" in v["game_versions"]]
            new, changes = await m.updates(mods, Loader.FABRIC, "26.3")
            assert {x.version_id for x in new} == {"Lith016x", "BlueCCCC", "FapiAAAA"}
            assert changes == [ModUpdate("Lithium", "1.0-fabric", "0.16")]
            assert await m.updates([], Loader.FABRIC, "26.3") == ([], [])

    run(go())


def mods_for(fake, *refs, loader=Loader.FABRIC):
    async def go():
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            mods = []
            for r in refs:
                mods += await m.resolve(r, loader, "26.3", mods)
            return mods
    return run(go())


def test_install_exactly_the_declared_files(fake, tmp_path: Path) -> None:
    mods = mods_for(fake, LITH)
    server = tmp_path / "server"
    (server / "mods").mkdir(parents=True)
    (server / "mods" / "dropped-by-hand.jar").write_bytes(b"who knows")
    (server / "mods" / file_name(mods[0])).write_bytes(b"tampered copy")  # right name, wrong content
    (server / "mods" / "config.txt").write_text("kept")

    async def go():
        async with make_client(fake.transport()) as client:
            installer = ModInstaller(tmp_path / "cache", tmp_path / "dl", client)
            moved = await installer.install(server, Loader.FABRIC, mods)
            assert sorted(moved) == sorted(["dropped-by-hand.jar", file_name(mods[0])])
            jars = sorted(p.name for p in (server / "mods").glob("*.jar"))
            assert jars == sorted(file_name(m) for m in mods)
            for m in mods:
                data = (server / "mods" / file_name(m)).read_bytes()
                assert hashlib.sha512(data).hexdigest() == m.sha512
            assert (server / "mods" / "config.txt").exists()
            aside = list((server / ".mdh" / "removed-jars").rglob("*.jar"))
            assert sorted(p.name for p in aside) == sorted(moved)       # never deleted
            # Second start: from the cache, nothing downloaded again.
            before = len(fake.requests)
            assert await installer.install(server, Loader.FABRIC, mods) == []
            assert len(fake.requests) == before
            # The group removes everything: the folder empties (aside, not deleted).
            assert len(await installer.install(server, Loader.FABRIC, [])) == 2
            assert not list((server / "mods").glob("*.jar"))

    run(go())


def test_a_changed_file_on_the_cdn_is_never_installed(fake, tmp_path: Path) -> None:
    mods = mods_for(fake, LITH)
    original = fake.files[mods[0].url]
    fake.files[mods[0].url] = b"X" * len(original)  # same size, other content
    server = tmp_path / "server"

    async def go():
        async with make_client(fake.transport()) as client:
            installer = ModInstaller(tmp_path / "cache", tmp_path / "dl", client)
            with pytest.raises(Exception) as err:
                await installer.install(server, Loader.FABRIC, mods)
            assert isinstance(err.value, ChecksumMismatch) or "larger than expected" in str(err.value) \
                or "expected" in str(err.value)
            assert not list(server.rglob("*.jar")) and not list((tmp_path / "cache").rglob("*.jar"))

    run(go())


def test_only_modrinth_cdn_urls(fake, tmp_path: Path) -> None:
    good = mods_for(fake, FAPI)[0]
    with pytest.raises(ValueError):
        Mod.model_validate({**good.model_dump(), "url": "https://github.com/x/y/releases/a.jar"})

    async def go():  # even if a bad entry got through, the download allowlist stops it
        bad = good.model_copy(update={"url": "https://evil.example/data/x.jar"})
        async with make_client(fake.transport()) as client:
            with pytest.raises(UntrustedHost):
                await ModInstaller(tmp_path / "c", tmp_path / "d", client).fetch(bad)

    run(go())


def test_approval_is_per_file(fake) -> None:
    mods = mods_for(fake, LITH)
    assert unapproved(mods, []) == mods
    assert unapproved(mods, [mods[0].sha512]) == [mods[1]]
    # A new version of an approved mod is a new file: asked again.
    newer = mods[0].model_copy(update={"sha512": "b" * 128})
    assert unapproved([newer], [mods[0].sha512]) == [newer]


def test_paper_plugins_go_to_plugins(fake, tmp_path: Path) -> None:
    mods = mods_for(fake, "PaperPl1", loader=Loader.PAPER)
    server = tmp_path / "s"
    (server / "plugins" / ".paper-remapped").mkdir(parents=True)
    (server / "plugins" / ".paper-remapped" / "cached.jar").write_bytes(b"paper's own")

    async def go():
        async with make_client(fake.transport()) as client:
            await ModInstaller(tmp_path / "c", tmp_path / "d", client).install(server, Loader.PAPER, mods)

    run(go())
    assert [p.name for p in (server / "plugins").glob("*.jar")] == [file_name(mods[0])]
    assert (server / "plugins" / ".paper-remapped" / "cached.jar").exists()   # sub-folders untouched


def test_bluemap_settings_are_local(tmp_path: Path) -> None:
    bluemap.configure(tmp_path, Loader.FABRIC, 8123, eula_accepted=False)
    core = (tmp_path / "config/bluemap/core.conf").read_text("utf-8")
    web = (tmp_path / "config/bluemap/webserver.conf").read_text("utf-8")
    assert "accept-download: false" in core and "metrics: false" in core
    assert 'ip: "127.0.0.1"' in web and "port: 8123" in web
    bluemap.configure(tmp_path, Loader.PAPER, 8123, eula_accepted=True)
    assert "accept-download: true" in (tmp_path / "plugins/BlueMap/core.conf").read_text("utf-8")
    with pytest.raises(ValueError):
        bluemap.configure(tmp_path, Loader.PAPER, 80, eula_accepted=True)
