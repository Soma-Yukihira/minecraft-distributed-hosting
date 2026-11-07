"""Mods and plugins in a server's configuration: only Modrinth files, always consistent."""

import pytest

from mdh_shared.models.api import Mod

SHA = "a" * 128


def mod(project: str = "AANZvGfr", version: str = "Vv1aaaaa", *, slug: str = "lithium", loader: str = "fabric",
        mc: str = "1.21.4", requires: list[str] | None = None, dependency: bool = False, **kw) -> dict:
    entry = {"project_id": project, "version_id": version, "slug": slug, "name": slug.capitalize(),
             "version_number": "1.0", "url": f"https://cdn.modrinth.com/data/{project}/versions/{version}/{slug}-1.0.jar",
             "sha512": SHA, "size": 1234, "loader": loader, "mc_version": mc, "requires": requires or [],
             "dependency": dependency}
    entry.update(kw)
    return entry


def patch(world, user=None, **body):
    return world.h.client.patch(f"/api/v1/servers/{world.server_id}", headers=(user or world.oscar).headers,
                                json=body)


def test_fabric_server_with_mods(world) -> None:
    api = mod("P7dR8mSH", "Fapi0001", slug="fabric-api", dependency=True)
    lithium = mod(requires=["P7dR8mSH"])
    r = patch(world, loader="fabric", mods=[lithium, api])
    assert r.status_code == 200, r.text
    s = r.json()
    assert [m["slug"] for m in s["mods"]] == ["lithium", "fabric-api"] and s["config_revision"] == 2
    assert s["map_enabled"] is False and s["map_url"] is None
    # Members see the list (they allow it on their own PC), only admins change it.
    assert world.h.status(world.lea, world.server_id)["mods"] == s["mods"]
    assert patch(world, world.lea, mods=[]).status_code == 403


@pytest.mark.parametrize("url", [
    "https://evil.example/data/AANZvGfr/versions/Vv1aaaaa/lithium.jar",
    "http://cdn.modrinth.com/data/AANZvGfr/versions/Vv1aaaaa/lithium.jar",
    "https://cdn.modrinth.com/data/OTHERprj/versions/Vv1aaaaa/lithium.jar",   # another project's file
    "https://cdn.modrinth.com/data/AANZvGfr/versions/Vv1aaaaa/../../x/lithium.jar",
    "https://cdn.modrinth.com/data/AANZvGfr/versions/Vv1aaaaa/lithium.exe",
    "https://cdn.modrinth.com/data/AANZvGfr/versions/Vv1aaaaa/lithium.jar?x=1",
])
def test_only_files_of_modrinth_cdn(world, url) -> None:
    assert patch(world, loader="fabric", mods=[mod(url=url)]).status_code == 422


@pytest.mark.parametrize("bad", [{"sha512": "a" * 127}, {"size": 0}, {"size": 300 * 1024**2},
                                 {"name": "x\nop eve"}, {"project_id": "../etc"}])
def test_mod_entries_are_validated(world, bad) -> None:
    assert patch(world, loader="fabric", mods=[mod(**bad)]).status_code == 422


def test_inconsistent_lists_are_refused(world) -> None:
    def problems(r):
        assert r.status_code == 422, r.text
        return r.json()["error"]["details"]["problems"]

    assert "Vanilla" in problems(patch(world, loader="vanilla", mods=[mod(loader="vanilla")]))[0]
    assert "prévu pour fabric 1.21.4" in problems(patch(world, mods=[mod()]))[0]  # the server is Paper
    assert "absent" in problems(patch(world, loader="fabric", mods=[mod(requires=["P7dR8mSH"])]))[0]
    assert "BlueMap" in problems(patch(world, loader="fabric", map_enabled=True))[0]
    assert patch(world, loader="fabric", mods=[mod(), mod()]).status_code == 422  # same project twice
    # Changing the Minecraft version alone would keep files made for the old one.
    assert patch(world, loader="fabric", mods=[mod()]).status_code == 200
    assert "1.21.4" in problems(patch(world, mc_version="1.21.5"))[0]
    assert patch(world, mc_version="1.21.5", mods=[mod(mc="1.21.5")]).status_code == 200


def test_map_needs_bluemap_and_shows_with_the_status_page(world) -> None:
    bluemap = mod("swbUV1cr", "SX74rxzP", slug="bluemap")
    r = patch(world, loader="fabric", mods=[bluemap], map_enabled=True)
    assert r.status_code == 200 and r.json()["map_enabled"] and r.json()["map_url"] is None  # status page off


def test_paper_plugins(world) -> None:
    r = patch(world, mods=[mod("Vebnzrzj", "b0mk8uS6", slug="luckperms", loader="paper")])
    assert r.status_code == 200 and r.json()["mods"][0]["loader"] == "paper"


def test_mod_model_accepts_real_modrinth_names() -> None:
    m = Mod.model_validate(mod("P7dR8mSH", "abcdEFGH", slug="fabric-api",
                               url="https://cdn.modrinth.com/data/P7dR8mSH/versions/abcdEFGH/fabric-api-0.92.0%2B1.21.4.jar"))
    assert m.slug == "fabric-api"
    Mod.model_validate(mod(url="https://cdn.modrinth.com/data/AANZvGfr/versions/Vv1aaaaa/Lithium 1.0.jar".replace(" ", "%20")))


PACK = {"name": "Faithful 32x", "url": "https://cdn.modrinth.com/data/w0TnApzs/versions/oHo9IUvX/Faithful%2032x.zip",
        "sha1": "b" * 40, "size": 10486470, "required": True, "project_id": "w0TnApzs", "version_id": "oHo9IUvX",
        "version_number": "1.21.4-june-2026"}


def test_resource_pack_in_the_configuration(world) -> None:
    r = patch(world, resource_pack=PACK)
    assert r.status_code == 200, r.text
    assert r.json()["resource_pack"] == PACK
    assert world.h.status(world.lea, world.server_id)["resource_pack"] == PACK
    assert patch(world, world.lea, resource_pack=None).status_code == 403
    link = {"name": "Mon pack", "url": "https://example.org/packs/mon-pack.zip", "sha1": "c" * 40, "size": 99}
    assert patch(world, resource_pack=link).json()["resource_pack"]["project_id"] is None
    assert patch(world, resource_pack=None).json()["resource_pack"] is None


@pytest.mark.parametrize("bad", [
    {"url": "http://example.org/pack.zip"},                 # Minecraft only, and no clear text
    {"url": "https://example.org/a pack.zip"},
    {"url": "https://example.org/pack.zip\nop=true"},       # no way into server.properties
    {"sha1": "B" * 40}, {"sha1": "b" * 39}, {"size": 251 * 1024 * 1024}, {"name": "a\nb"},
])
def test_resource_pack_entries_are_validated(world, bad) -> None:
    assert patch(world, resource_pack={**PACK, **bad}).status_code == 422
