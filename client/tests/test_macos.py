"""0.9.0: the macOS application (Apple chips), checked from Windows as far as possible.

Everything that depends on the system takes the platform as an argument, so the macOS choices
run here. What only a Mac can tell (the .app, ditto, kqueue, caffeinate) is tried there, with
the procedure in docs/DEPLOYMENT.md.
"""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import plistlib
import shutil
import signal
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import httpx
import pytest

from mdh_client import credentials
from mdh_client.config import ClientSettings, Layout, app_home
from mdh_client.minecraft.downloads import DownloadError
from mdh_client.minecraft.installer import Installer, safe_extract_tar
from mdh_client.minecraft.sources import Artifact, SourceResolver
from mdh_client.system import autostart, awake, host_os, updates, watchdog
from mdh_client.system.updates import UpdateError, Updater
from mdh_shared.models.api import ClientRelease, release_file_name, release_message
from test_updates import DEV_KEY, TRUSTED, backend, logged_in, publish  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
MAC = host_os.java_target("darwin")


def test_platform_choices() -> None:
    assert host_os.release_platform("darwin") == "macos-arm64" and host_os.release_platform("win32") == "windows"
    assert (MAC.os, MAC.architecture, MAC.package_suffix) == ("mac", "aarch64", ".tar.gz")
    assert MAC.java == PurePosixPath("Contents/Home/bin/java")
    assert host_os.java_target("win32").java == PurePosixPath("bin/java.exe")
    assert app_home("darwin") == Path.home() / "Library" / "Application Support" / "MinecraftDistributedHosting"
    assert release_file_name("0.9.0") == "MinecraftDistributedHosting-Setup-0.9.0.exe"
    assert release_file_name("0.9.0", "macos-arm64") == "MinecraftDistributedHosting-0.9.0-macos-arm64.zip"


def test_refresh_token_on_a_mac_is_an_owner_only_file(tmp_path) -> None:
    path = tmp_path / "credentials.bin"
    credentials.store_refresh_token(path, "secret-token", platform="darwin")
    assert path.read_bytes() == b"secret-token"
    assert credentials.load_refresh_token(path, platform="darwin") == "secret-token"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    credentials.store_refresh_token(path, "next", platform="darwin")      # replaced, not appended
    assert credentials.load_refresh_token(path, platform="darwin") == "next"


def test_start_at_login_is_a_launch_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert autostart.available("darwin") and not autostart.available("linux")
    plist = tmp_path / "LaunchAgents" / "fr.mdh.client.plist"
    exe = "/Applications/Minecraft Distributed Hosting.app/Contents/MacOS/MinecraftDistributedHosting"
    assert not autostart.is_enabled(platform="darwin", plist=plist)
    autostart.set_enabled(True, executable=exe, platform="darwin", plist=plist)
    agent = plistlib.loads(plist.read_bytes())
    assert agent["Label"] == "fr.mdh.client" and agent["RunAtLoad"] is True
    assert agent["ProgramArguments"] == [exe, "--minimized"]                 # in the menu bar only
    assert autostart.is_enabled(platform="darwin", plist=plist)
    autostart.set_enabled(False, platform="darwin", plist=plist)
    assert not plist.exists()


def _tar(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tf.addfile(entry)
            else:
                info = tarfile.TarInfo(entry[0])
                info.size, info.mode = len(entry[1]), 0o755
                tf.addfile(info, io.BytesIO(entry[1]))
    return buf.getvalue()


def _link(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info


JRE = [("jdk-21.0.5+11-jre/Contents/Home/bin/java", b"#!java"),
       ("jdk-21.0.5+11-jre/Contents/Home/lib/libjli.dylib", b"lib"),
       _link("jdk-21.0.5+11-jre/Contents/MacOS/libjli.dylib", "../Home/lib/libjli.dylib")]


def test_mac_java_archive_is_unpacked_safely(tmp_path) -> None:
    archive = tmp_path / "jre.tar.gz"
    archive.write_bytes(_tar(JRE))
    safe_extract_tar(archive, tmp_path / "ok")
    assert (tmp_path / "ok" / "jdk-21.0.5+11-jre" / "Contents" / "Home" / "bin" / "java").read_bytes() == b"#!java"
    for bad in ([_link("jre/escape", "../../outside")], [("/etc/evil", b"x")], [("jre/../../evil", b"x")]):
        archive.write_bytes(_tar(bad))
        with pytest.raises(DownloadError):
            safe_extract_tar(archive, tmp_path / "bad")
    assert not (tmp_path / "outside").exists() and not (tmp_path / "evil").exists()


def test_java_for_apple_chips_comes_from_adoptium(tmp_path) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        pkg = {"name": "OpenJDK21U-jre_aarch64_mac_hotspot_21.0.5_11.pkg", "link": "https://x/a.pkg",
               "checksum": "a" * 64, "size": 1}
        tar = {"name": "OpenJDK21U-jre_aarch64_mac_hotspot_21.0.5_11.tar.gz", "link": "https://x/a.tar.gz",
               "checksum": "b" * 64, "size": 2}
        return httpx.Response(200, json=[{"binary": {"package": pkg}}, {"binary": {"package": tar}}])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await SourceResolver(client).java_runtime(21, MAC)

    artifact = asyncio.run(scenario())
    assert artifact.filename.endswith(".tar.gz") and artifact.sha256 == "b" * 64
    assert seen[0].params["os"] == "mac" and seen[0].params["architecture"] == "aarch64"


def test_installer_puts_the_mac_java_in_place(tmp_path) -> None:
    data = _tar(JRE)

    class Resolver:
        async def java_runtime(self, major, target):
            assert target == MAC
            return Artifact(url="https://github.com/adoptium/temurin21-binaries/releases/jre.tar.gz", filename="OpenJDK21U-jre_aarch64_mac.tar.gz",
                            size=len(data), sha256=hashlib.sha256(data).hexdigest())

    async def scenario():
        layout = Layout(tmp_path / "home")
        layout.ensure()
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=data))
        async with httpx.AsyncClient(transport=transport) as client:
            return await Installer(layout, ClientSettings(), client, Resolver(), java=MAC).ensure_java(21)

    java = asyncio.run(scenario())
    assert java.as_posix().endswith("temurin-21-jre/Contents/Home/bin/java") and java.read_bytes() == b"#!java"


class FakeProcesses:
    """Pids that end when told: the watchdog's view of the application and of Minecraft."""

    def __init__(self, app_ends_after: int, child_obeys: bool) -> None:
        self.ticks, self.app_ends_after, self.child_obeys = 0, app_ends_after, child_obeys
        self.child_alive, self.signals = True, []

    def alive(self, pid: int) -> bool:
        return self.ticks < self.app_ends_after if pid == 1 else self.child_alive

    def killpg(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if self.child_obeys or sig != signal.SIGTERM:
            self.child_alive = False

    def sleep(self, _: float) -> None:
        self.ticks += 1


def test_watchdog_stops_minecraft_when_the_application_dies() -> None:
    p = FakeProcesses(app_ends_after=3, child_obeys=True)
    assert watchdog.watch(1, 2, is_alive=p.alive, killpg=p.killpg, sleep=p.sleep) == "stopped"
    assert p.signals == [(2, signal.SIGTERM)]                     # politely first: Minecraft saves

    stubborn = FakeProcesses(app_ends_after=0, child_obeys=False)
    assert watchdog.watch(1, 2, is_alive=stubborn.alive, killpg=stubborn.killpg, sleep=stubborn.sleep,
                          grace_s=5) == "killed"
    assert stubborn.signals[0] == (2, signal.SIGTERM) and len(stubborn.signals) == 2

    ended = FakeProcesses(app_ends_after=99, child_obeys=True)
    ended.child_alive = False                                     # Minecraft ended by itself
    assert watchdog.watch(1, 2, is_alive=ended.alive, killpg=ended.killpg, sleep=ended.sleep) == "child"
    assert ended.signals == []
    assert watchdog.main(["x"]) == 2


def test_keep_awake_commands() -> None:
    assert awake.caffeinate_command(42) == ["/usr/bin/caffeinate", "-i", "-w", "42"]
    other = awake.KeepAwake("linux")
    other.hold()
    assert not other.held                                         # nothing to do there


def test_signatures_are_bound_to_the_system() -> None:
    win = release_message("0.9.0", "a" * 64, 10)
    assert win == release_message("0.9.0", "a" * 64, 10, "windows")   # unchanged for older applications
    assert release_message("0.9.0", "a" * 64, 10, "macos-arm64") != win
    signature = base64.b64encode(DEV_KEY.sign(win)).decode()
    as_mac = ClientRelease(version="0.9.0", platform="macos-arm64", size=10, sha256="a" * 64, signature=signature)
    with pytest.raises(UpdateError):
        updates.verify_signature(as_mac, TRUSTED)                 # a Windows signature passed off as a Mac one


def publish_mac(updates_dir: Path, version: str, data: bytes) -> None:
    folder = updates_dir / "macos-arm64"
    folder.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    signature = DEV_KEY.sign(release_message(version, sha, len(data), "macos-arm64"))
    (folder / release_file_name(version, "macos-arm64")).write_bytes(data)
    (folder / "release.json").write_text(json.dumps({
        "version": version, "platform": "macos-arm64", "size": len(data), "sha256": sha,
        "signature": base64.b64encode(signature).decode()}), "utf-8")


def test_each_system_gets_its_own_release(backend, tmp_path) -> None:  # noqa: F811
    publish(backend.updates, "0.9.1", b"MZ windows")
    publish_mac(backend.updates, "0.9.2", b"PK mac app")

    async def scenario():
        api = await logged_in(backend)
        try:
            # Applications before 0.9.0 refuse unknown fields: the Windows answer has no "platform".
            raw = await api.client_release()
            assert raw["version"] == "0.9.1" and "platform" not in raw
            mac = Updater(tmp_path / "mac", lambda: api, current="0.9.0", keys=TRUSTED, platform="macos-arm64")
            ready = await mac.check()
            assert ready.version == "0.9.2" and ready.installer.read_bytes() == b"PK mac app"
            assert ready.installer.name == "MinecraftDistributedHosting-0.9.2-macos-arm64.zip"
            win = Updater(tmp_path / "win", lambda: api, current="0.9.0", keys=TRUSTED, platform="windows")
            assert (await win.check()).installer.read_bytes() == b"MZ windows"

            # A Mac release put in the Windows folder is not distributed there.
            shutil.copy(backend.updates / "macos-arm64" / "release.json", backend.updates / "release.json")
            shutil.copy(backend.updates / "macos-arm64" / release_file_name("0.9.2", "macos-arm64"),
                        backend.updates / release_file_name("0.9.2", "macos-arm64"))
            assert await api.client_release() is None
        finally:
            await api.close()

    asyncio.run(scenario())


def test_signing_script_knows_both_installers() -> None:
    spec = importlib.util.spec_from_file_location("sign_release", ROOT / "deployment" / "windows" / "sign_release.py")
    sign_release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sign_release)
    assert sign_release.identify("MinecraftDistributedHosting-Setup-0.9.0.exe") == ("0.9.0", None)
    assert sign_release.identify("MinecraftDistributedHosting-0.9.0-macos-arm64.zip") == ("0.9.0", "macos-arm64")
    assert sign_release.identify("MinecraftDistributedHosting-0.9.0-macos-x64.zip") is None


def _posix_shell() -> str | None:
    """Git's bash on Windows (the one in System32 is WSL's), any bash elsewhere."""
    git = shutil.which("git")
    if sys.platform == "win32" and git:
        for folder in Path(git).parents:
            if (folder / "bin" / "bash.exe").is_file() and (folder / "usr" / "bin").is_dir():
                return str(folder / "bin" / "bash.exe")
        return None
    return shutil.which("bash")


@pytest.mark.skipif(_posix_shell() is None, reason="needs a POSIX shell to check the syntax")
def test_mac_scripts_are_valid_shell() -> None:
    script = Path(__file__).parent / "_mac_install_update.sh"
    script.write_text(updates.MAC_INSTALL_SCRIPT, "utf-8", newline="\n")
    try:
        for path in (script, ROOT / "deployment" / "macos" / "build.sh"):
            assert subprocess.run([_posix_shell(), "-n", str(path)], capture_output=True).returncode == 0, path
    finally:
        script.unlink()
    assert updates.mac_bundle("/Applications/X.app/Contents/MacOS/MinecraftDistributedHosting").name == "X.app"
