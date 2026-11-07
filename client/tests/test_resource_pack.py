"""The server's resource pack: from Modrinth or a direct link, written to server.properties."""

import asyncio
import hashlib
import io
import zipfile

import httpx
import pytest

from fake_modrinth import FakeModrinth
from mdh_client.minecraft import resource_pack
from mdh_client.minecraft.downloads import make_client
from mdh_client.minecraft.modrinth import Modrinth, ModrinthError
from mdh_client.minecraft.resource_pack import PackError
from mdh_shared.models.api import ResourcePack


def zip_with(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "{}")
    return buf.getvalue()


def test_pack_from_modrinth() -> None:
    fake = FakeModrinth()
    fake.add("w0TnApzs", "faithful-32x", loaders=("minecraft",), project_type="resourcepack",
             version_id="oHo9IUvX", version_number="26.3-nov", content=zip_with("pack.mcmeta"))
    fake.add("gvQqBUqZ", "lithium", version_id="LithBBBB")

    async def go():
        async with make_client(fake.transport()) as client:
            m = Modrinth(client)
            hits = await m.search_packs("faith", "26.3")
            assert [h.slug for h in hits] == ["faithful-32x"]
            assert await m.search_packs("lith", "26.3") == []             # mods are not packs
            pack = await m.pack("w0TnApzs", "26.3", required=True)
            content = fake.files[pack.url]
            assert pack.sha1 == hashlib.sha1(content).hexdigest() and pack.size == len(content)
            assert (pack.project_id, pack.version_id, pack.version_number, pack.required) == (
                "w0TnApzs", "oHo9IUvX", "26.3-nov", True)
            assert pack.url.startswith("https://cdn.modrinth.com/data/w0TnApzs/versions/oHo9IUvX/")
            with pytest.raises(ModrinthError, match="pas de fichier pour Minecraft 26.4"):
                await m.pack("w0TnApzs", "26.4", required=False)

    asyncio.run(go())


def link_client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    return resource_pack.make_client(httpx.MockTransport(lambda r: routes.get(str(r.url), httpx.Response(404))))


def test_pack_from_a_direct_link(monkeypatch) -> None:
    good = zip_with("pack.mcmeta", "assets/minecraft/textures/block/stone.png")
    routes = {
        "https://example.org/packs/Mon%20Pack.zip": httpx.Response(200, content=good),
        "https://example.org/page.html": httpx.Response(200, content=b"<html>Dropbox</html>"),
        "https://example.org/notapack.zip": httpx.Response(200, content=zip_with("readme.txt")),
        "https://example.org/empty.zip": httpx.Response(200, content=b""),
        "https://example.org/moved": httpx.Response(302, headers={"location": "http://example.org/x.zip"}),
    }

    async def go():
        async with link_client(routes) as client:
            pack = await resource_pack.from_url(client, "https://example.org/packs/Mon%20Pack.zip", required=False)
            assert pack == ResourcePack(name="Mon Pack", url="https://example.org/packs/Mon%20Pack.zip",
                                        sha1=hashlib.sha1(good).hexdigest(), size=len(good))
            for url, needle in (("https://example.org/page.html", "lien de téléchargement direct"),
                                ("https://example.org/notapack.zip", "pack.mcmeta"),
                                ("https://example.org/empty.zip", "vide"),
                                ("https://example.org/missing.zip", "HTTP 404"),
                                ("https://example.org/moved", "https://"),
                                ("http://example.org/pack.zip", "https://"),
                                ("https://example.org/a b.zip", "espace")):
                with pytest.raises(PackError, match=needle):
                    await resource_pack.from_url(client, url, required=False)
            monkeypatch.setattr(resource_pack, "MAX_PACK_BYTES", 100)
            with pytest.raises(PackError, match="250 Mo"):
                await resource_pack.from_url(client, "https://example.org/packs/Mon%20Pack.zip", required=False)

    asyncio.run(go())


def test_pack_in_server_properties() -> None:
    assert resource_pack.properties(None) == {"resource-pack": "", "resource-pack-sha1": "",
                                              "require-resource-pack": "false", "resource-pack-prompt": ""}
    props = resource_pack.properties({"url": "https://example.org/p.zip", "sha1": "a" * 40, "required": True})
    assert props["resource-pack"] == "https://example.org/p.zip" and props["resource-pack-sha1"] == "a" * 40
    assert props["require-resource-pack"] == "true" and "nécessaire" in props["resource-pack-prompt"]
