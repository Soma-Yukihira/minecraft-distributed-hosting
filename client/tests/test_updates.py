"""Automatic updates against a real control plane: only a newer, correctly signed installer that
matches its signature byte for byte is ever kept to be run."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from mdh_client.networking.api_client import ApiClient
from mdh_client.system import updates
from mdh_client.system.updates import UpdateError, Updater, is_newer
from mdh_shared.models.api import release_message
from test_agent_live import PW, LiveBackend, plenty_of_resources

DEV_KEY = Ed25519PrivateKey.generate()
OTHER_KEY = Ed25519PrivateKey.generate()


def public(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


TRUSTED = (public(DEV_KEY),)


def publish(folder: Path, version: str, data: bytes, *, key: Ed25519PrivateKey = DEV_KEY,
            signed_version: str | None = None, notes: str | None = None) -> None:
    """What sign_release.py + install.sh publish-client leave on the VPS."""
    folder.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    signature = key.sign(release_message(signed_version or version, sha, len(data)))
    (folder / f"MinecraftDistributedHosting-Setup-{version}.exe").write_bytes(data)
    manifest = {"version": version, "size": len(data), "sha256": sha,
                "signature": base64.b64encode(signature).decode()}
    if notes:
        manifest["notes"] = notes
    (folder / "release.json").write_text(json.dumps(manifest, indent=2), "utf-8")


@pytest.fixture
def backend(tmp_path):
    b = LiveBackend(tmp_path)
    b.start()
    yield b
    b.stop()


async def logged_in(backend: LiveBackend) -> ApiClient:
    api = ApiClient(backend.url)
    await api.register("oscar", PW, None)
    await api.login("oscar", PW)
    return api


def test_version_order():
    assert is_newer("0.2.0", "0.1.0") and is_newer("0.10.0", "0.9.9") and is_newer("1.0.0", "0.99.99")
    assert not is_newer("0.1.0", "0.1.0") and not is_newer("0.1.9", "0.2.0")


def test_the_embedded_key_is_a_real_ed25519_key():
    assert updates.TRUSTED_KEYS
    for key in updates.TRUSTED_KEYS:
        assert len(base64.b64decode(key, validate=True)) == 32


def test_downloads_and_verifies_a_newer_signed_release(backend, tmp_path):
    installer = b"MZ" + bytes(range(256)) * 9000          # > 2 MiB: several chunks
    publish(backend.updates, "0.2.0", installer, notes="Corrige la liste blanche.")
    ready_events = []

    async def scenario():
        api = await logged_in(backend)
        try:
            # The endpoints are for logged-in members only.
            async with httpx.AsyncClient(base_url=backend.url) as anonymous:
                assert (await anonymous.get("/api/v1/client/release")).status_code == 401
                assert (await anonymous.get("/api/v1/client/release/installer")).status_code == 401

            up = Updater(tmp_path / "updates", lambda: api, current="0.1.0", keys=TRUSTED,
                         on_ready=ready_events.append)
            ready = await up.check()
            assert ready is not None and ready.version == "0.2.0"
            assert ready.notes == "Corrige la liste blanche."
            assert ready.installer.read_bytes() == installer
            assert [p.name for p in (tmp_path / "updates").iterdir()] == [ready.installer.name]
            assert up.verify_ready() == ready

            # Checking again neither downloads again nor announces it twice.
            mtime = ready.installer.stat().st_mtime_ns
            assert await up.check() == ready
            assert ready.installer.stat().st_mtime_ns == mtime and len(ready_events) == 1

            # An up-to-date PC keeps nothing.
            current = Updater(tmp_path / "other", lambda: api, current="0.2.0", keys=TRUSTED)
            assert await current.check() is None and not (tmp_path / "other").exists()

            # Withdrawn on the VPS (rollback.sh client): the downloaded installer goes too.
            (backend.updates / "release.json").unlink()
            assert await up.check() is None and up.ready is None
            assert list((tmp_path / "updates").iterdir()) == []
        finally:
            await api.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["other_key", "relabelled_old_version", "tampered_file_locally"])
def test_never_keeps_an_installer_that_does_not_match_the_developer_signature(backend, tmp_path, case):
    if case == "other_key":                     # e.g. signed by whoever took over the VPS
        publish(backend.updates, "0.2.0", b"MZ evil", key=OTHER_KEY)
    elif case == "relabelled_old_version":      # a genuine 0.1.5 passed off as 0.2.0
        publish(backend.updates, "0.2.0", b"MZ old", signed_version="0.1.5")
    else:
        publish(backend.updates, "0.2.0", b"MZ good")

    async def scenario():
        api = await logged_in(backend)
        try:
            up = Updater(tmp_path / "updates", lambda: api, current="0.1.0", keys=TRUSTED)
            if case == "tampered_file_locally":
                ready = await up.check()
                ready.installer.write_bytes(b"MZ evil")   # swapped between download and install
                with pytest.raises(UpdateError):
                    up.verify_ready()
                assert up.ready is None
                return
            with pytest.raises(UpdateError, match="signature"):
                await up.check()
            assert up.ready is None
            assert not (tmp_path / "updates").exists()  # rejected before downloading anything
        finally:
            await api.close()

    asyncio.run(scenario())


def test_the_control_plane_refuses_to_serve_a_file_that_does_not_match(backend, tmp_path):
    publish(backend.updates, "0.2.0", b"MZ good")
    (backend.updates / "MinecraftDistributedHosting-Setup-0.2.0.exe").write_bytes(b"MZ g00d")

    async def scenario():
        api = await logged_in(backend)
        try:
            assert await api.client_release() is None   # 404: nothing is distributed
            up = Updater(tmp_path / "updates", lambda: api, current="0.1.0", keys=TRUSTED)
            assert await up.check() is None
        finally:
            await api.close()

    asyncio.run(scenario())


def test_a_pc_about_to_update_hosts_nothing_new(tmp_path):
    layout = Layout(tmp_path / "client")
    layout.ensure()

    async def scenario():
        agent = HostAgent(layout, ClientSettings(), store_credentials=False,
                          detect_resources=plenty_of_resources)
        try:
            assert agent._availability()["hosting_allowed"] and not agent.busy
            with pytest.raises(UpdateError):            # nothing downloaded yet
                await agent.prepare_update()
            assert not agent.updating
            agent.updating = True
            assert not agent._availability()["hosting_allowed"]
        finally:
            await agent.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 0.12.0


def test_a_published_release_reaches_the_applications_at_once(tmp_path, monkeypatch):
    """The control plane announces it; the application fetches it without waiting for its
    hourly check (made to never come here)."""
    from mdh_client.application import agent as agent_module
    from mdh_server import app as server_app
    from mdh_server import releases
    from test_agent_live import Recorder, until

    monkeypatch.setattr(server_app, "RELEASE_POLL_EVERY_S", 0.2)
    monkeypatch.setattr(updates, "FIRST_CHECK_AFTER_S", 3600.0)
    monkeypatch.setattr(agent_module, "ANNOUNCE_SPREAD_S", 0.0)
    b = LiveBackend(tmp_path)
    publish(b.updates, "0.1.5", b"MZ old")                 # already there at startup: not announced
    b.start()
    layout = Layout(tmp_path / "client")
    layout.ensure()

    async def scenario():
        agent = HostAgent(layout, ClientSettings(api_url=b.url), emit=Recorder(), store_credentials=False,
                          detect_resources=plenty_of_resources, auto_updates=True)
        agent.updater.current, agent.updater.keys = "0.1.0", TRUSTED
        try:
            await agent.api.register("oscar", PW, None)
            await agent.login("oscar", PW)
            await until(lambda: agent.channel.connected, what="online")
            await asyncio.sleep(1)
            assert agent.updater.ready is None                 # nothing announced, no hourly check yet

            installer = b"MZ" + bytes(range(256)) * 100
            publish(b.updates, "0.2.0", installer, notes="Ajout de l'anti-triche.")
            await until(lambda: agent.updater.ready is not None, timeout=10, what="release announced")
            assert agent.updater.ready.version == "0.2.0" and agent.updater.ready.installer.read_bytes() == installer
            assert ("update", {"version": "0.2.0", "notes": "Ajout de l'anti-triche."}) in agent._listener.events

            # The button: same answer, nothing downloaded twice; an older announcement is ignored.
            assert (await agent.check_for_updates()).version == "0.2.0"
            from mdh_shared.protocol.messages import ClientReleaseNotice
            agent._on_release_notice(ClientReleaseNotice(platform="windows", version="0.0.9"))
            agent._on_release_notice(ClientReleaseNotice(platform="macos-arm64", version="9.0.0"))
        finally:
            await agent.close()

    try:
        asyncio.run(scenario())
    finally:
        b.stop()
    watcher = releases.ReleaseWatcher(releases.shelves(b.updates))
    assert watcher.poll() == []                                # nothing new since it looked


def test_checks_never_overlap_and_the_button_needs_the_installed_app(backend, tmp_path):
    publish(backend.updates, "0.2.0", b"MZ" + b"x" * 300_000)

    async def scenario():
        api = await logged_in(backend)
        downloads = []
        real = api.download_client_installer

        async def counted(*a, **k):
            downloads.append(1)
            return await real(*a, **k)

        api.download_client_installer = counted
        try:
            up = Updater(tmp_path / "updates", lambda: api, current="0.1.0", keys=TRUSTED)
            first, second, third = await asyncio.gather(up.check(), up.check(), up.check())
            assert first == second == third and first is not None and len(downloads) == 1
        finally:
            await api.close()

        layout = Layout(tmp_path / "dev")
        layout.ensure()
        dev = HostAgent(layout, ClientSettings(), store_credentials=False, detect_resources=plenty_of_resources,
                        auto_updates=False)
        try:
            assert not dev.can_update
            with pytest.raises(RuntimeError, match="installée"):
                await dev.check_for_updates()
        finally:
            await dev.close()

    asyncio.run(scenario())
