"""Verified downloads, official sources and archive extraction (network mocked)."""

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from mdh_client.minecraft.downloads import (
    ChecksumMismatch,
    DownloadError,
    UntrustedHost,
    download,
    make_client,
)
from mdh_client.minecraft.installer import safe_extract_zip
from mdh_client.minecraft.sources import SourceError, SourceResolver
from mdh_shared.models.enums import Loader

JAR = b"PK\x03\x04 fake jar content" * 100
SHA256 = hashlib.sha256(JAR).hexdigest()
SHA1 = hashlib.sha1(JAR).hexdigest()


def run(coro):
    return asyncio.run(coro)


def client_for(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        resp = routes.get(str(request.url))
        return resp if resp is not None else httpx.Response(404)

    return make_client(httpx.MockTransport(handler))


def test_download_verifies_checksums(tmp_path: Path) -> None:
    url = "https://fill-data.papermc.io/v1/objects/x/paper.jar"
    dest = tmp_path / "paper.jar"

    async def go():
        async with client_for({url: httpx.Response(200, content=JAR)}) as c:
            assert await download(c, url, dest, sha256=SHA256, expected_size=len(JAR)) == SHA256
            assert dest.read_bytes() == JAR
            with pytest.raises(ChecksumMismatch):
                await download(c, url, tmp_path / "bad.jar", sha256="0" * 64)
            with pytest.raises(ChecksumMismatch):
                await download(c, url, tmp_path / "bad1.jar", sha1="0" * 40)
            with pytest.raises(DownloadError):
                await download(c, url, tmp_path / "small.jar", expected_size=10)

    run(go())
    # Nothing that failed verification is left on disk, not even partially.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["paper.jar"]


@pytest.mark.parametrize("url", ["http://fill-data.papermc.io/x.jar", "https://evil.example.com/x.jar",
                                 "https://papermc.io.evil.com/x.jar"])
def test_untrusted_urls_rejected(tmp_path: Path, url: str) -> None:
    async def go():
        async with client_for({}) as c:
            await download(c, url, tmp_path / "x.jar")

    with pytest.raises(UntrustedHost):
        run(go())


def test_redirect_to_untrusted_host_is_blocked(tmp_path: Path) -> None:
    url = "https://github.com/adoptium/x.zip"
    routes = {url: httpx.Response(302, headers={"location": "https://evil.example.com/x.zip"}),
              "https://evil.example.com/x.zip": httpx.Response(200, content=b"malware")}

    async def go():
        async with client_for(routes) as c:
            await download(c, url, tmp_path / "x.zip")

    with pytest.raises(UntrustedHost):
        run(go())
    assert not list(tmp_path.iterdir())


MANIFEST = {"latest": {"release": "26.3"}, "versions": [
    {"id": "26.3", "type": "release",
     "url": "https://piston-meta.mojang.com/v1/packages/abc/26.3.json"},
    {"id": "26.4-snapshot-6", "type": "snapshot", "url": "https://piston-meta.mojang.com/s.json"}]}
VERSION_JSON = {"javaVersion": {"majorVersion": 25}, "downloads": {"server": {
    "sha1": SHA1, "size": len(JAR), "url": "https://piston-data.mojang.com/v1/objects/x/server.jar"}}}


def resolver_routes() -> dict[str, httpx.Response]:
    version_bytes = json.dumps(VERSION_JSON).encode()
    MANIFEST["versions"][0]["sha1"] = hashlib.sha1(version_bytes).hexdigest()
    paper = "https://fill.papermc.io/v3/projects/paper"
    return {
        "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json": httpx.Response(200, json=MANIFEST),
        "https://piston-meta.mojang.com/v1/packages/abc/26.3.json": httpx.Response(200, content=version_bytes),
        f"{paper}/versions/26.3/builds": httpx.Response(200, json=[
            {"id": 231, "channel": "STABLE", "downloads": {"server:default": {
                "name": "paper-26.3-231.jar", "checksums": {"sha256": SHA256}, "size": len(JAR),
                "url": "https://fill-data.papermc.io/v1/objects/a/paper-26.3-231.jar"}}},
            {"id": 232, "channel": "ALPHA", "downloads": {"server:default": {
                "name": "paper-26.3-232.jar", "checksums": {"sha256": "1" * 64}, "size": 1,
                "url": "https://fill-data.papermc.io/v1/objects/b/paper-26.3-232.jar"}}},
            {"id": 200, "channel": "STABLE", "downloads": {"server:default": {
                "name": "old.jar", "checksums": {"sha256": "2" * 64}, "size": 1,
                "url": "https://fill-data.papermc.io/v1/objects/c/old.jar"}}}]),
        f"{paper}/versions/26.3": httpx.Response(200, json={"version": {"java": {"version": {"minimum": 25}}}}),
        "https://meta.fabricmc.net/v2/versions/loader/26.3": httpx.Response(200, json=[
            {"loader": {"version": "0.20.1", "stable": True}}, {"loader": {"version": "0.20.0", "stable": False}}]),
        "https://meta.fabricmc.net/v2/versions/installer": httpx.Response(200, json=[
            {"version": "1.1.2", "stable": True}]),
        "https://api.adoptium.net/v3/assets/latest/25/hotspot?architecture=x64&image_type=jre&os=windows&vendor=eclipse":
            httpx.Response(200, json=[{"binary": {"package": {
                "name": "OpenJDK25U-jre_x64_windows_hotspot_25.0.5_8.zip", "checksum": "3" * 64,
                "size": 58589419, "link": "https://github.com/adoptium/temurin25-binaries/x.zip"}}}]),
    }


def test_sources_resolution() -> None:
    async def go():
        async with client_for(resolver_routes()) as c:
            r = SourceResolver(c)
            assert await r.latest_release() == "26.3"
            assert await r.release_versions() == ["26.3"]
            paper = await r.resolve(Loader.PAPER, "26.3")
            assert (paper.build, paper.jar.sha256, paper.java_major) == ("231", SHA256, 25)
            vanilla = await r.resolve(Loader.VANILLA, "26.3")
            assert (vanilla.jar.sha1, vanilla.jar.trust, vanilla.java_major) == (SHA1, "sha1", 25)
            fabric = await r.resolve(Loader.FABRIC, "26.3")
            assert fabric.jar.url.endswith("/loader/26.3/0.20.1/1.1.2/server/jar")
            assert fabric.jar.trust == "pin-on-first-use"
            java = await r.java_runtime(25)
            assert java.sha256 == "3" * 64 and java.filename.endswith(".zip")
            with pytest.raises(SourceError):
                await r.resolve(Loader.PAPER, "../../etc")
            with pytest.raises(SourceError):
                await r.resolve(Loader.VANILLA, "9.99")

    run(go())


def test_tampered_version_json_is_rejected() -> None:
    routes = resolver_routes()
    routes["https://piston-meta.mojang.com/v1/packages/abc/26.3.json"] = httpx.Response(
        200, json={**VERSION_JSON, "downloads": {"server": {"sha1": "0" * 40, "url": "https://piston-data.mojang.com/evil"}}})

    async def go():
        async with client_for(routes) as c:
            await SourceResolver(c).vanilla("26.3")

    with pytest.raises(ChecksumMismatch):
        run(go())


def make_zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_safe_extract_ok(tmp_path: Path) -> None:
    archive = tmp_path / "jre.zip"
    archive.write_bytes(make_zip({"jdk-25+8-jre/bin/java.exe": b"MZ", "jdk-25+8-jre/release": b"x"}))
    safe_extract_zip(archive, tmp_path / "out")
    assert (tmp_path / "out" / "jdk-25+8-jre" / "bin" / "java.exe").read_bytes() == b"MZ"


@pytest.mark.parametrize("entries, symlink", [
    ({"../evil.txt": b"x"}, None),
    ({"jre/../../evil.txt": b"x"}, None),
    ({"/abs/evil.txt": b"x"}, None),
    ({"C:/Windows/evil.txt": b"x"}, None),
    ({"jre/ok.txt": b"x"}, "jre/link"),
])
def test_safe_extract_rejects_malicious_archives(tmp_path: Path, entries, symlink) -> None:
    archive = tmp_path / "evil.zip"
    archive.write_bytes(make_zip(entries, symlink))
    with pytest.raises(DownloadError):
        safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()
