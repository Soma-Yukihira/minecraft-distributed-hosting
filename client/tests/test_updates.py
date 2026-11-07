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
