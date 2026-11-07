"""Application ↔ control plane versions: clear refusals instead of strange errors."""

import asyncio
import json

import pytest
import websockets

import mdh_server.api.ws as ws_routes
import mdh_server.app as server_app
from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from mdh_client.gui.texts import error_text
from mdh_client.networking.api_client import ApiError
from mdh_shared import __version__, versions
from test_agent_live import PW, LiveBackend, Recorder, plenty_of_resources, until


@pytest.fixture
def backend(tmp_path):
    b = LiveBackend(tmp_path)
    b.start()
    yield b
    b.stop()


def agent(tmp_path, backend, rec=None) -> HostAgent:
    layout = Layout(tmp_path / "client")
    layout.ensure()
    return HostAgent(layout, ClientSettings(api_url=backend.url), emit=rec or Recorder(),
                     store_credentials=False, detect_resources=plenty_of_resources)


def test_version_comparison():
    assert versions.older_than("0.2.9", "0.3.0") and versions.older_than("0.9.0", "0.10.0")
    assert not versions.older_than("0.3.0", "0.3.0") and not versions.older_than("1.0.0", "0.3.0")
    assert versions.older_than(None, "0.1.0") and versions.older_than("test", "0.1.0")


def test_health_tells_the_minimum_application_and_old_ones_are_refused_on_the_websocket(backend):
    async def scenario():
        a = agent(backend.db.parent.parent, backend)
        try:
            info = await a.api.health()
            assert info["version"] == __version__ and info["min_client_version"]
            await a.api.register("oscar", PW, None)
            await a.api.login("oscar", PW)
            url = a.api.ws_url
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "hello", "access_token": a.api.access_token,
                                          "client_version": "0.1.0"}))
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await ws.recv()
            assert closed.value.rcvd.code == ws_routes.CLIENT_TOO_OLD
        finally:
            await a.close()

    asyncio.run(scenario())


def test_a_control_plane_too_old_for_the_application_is_explained(tmp_path, backend, monkeypatch):
    monkeypatch.setattr(server_app, "__version__", "0.2.0")

    async def scenario():
        a = agent(tmp_path, backend)
        try:
            with pytest.raises(ApiError) as refused:
                await a.register("oscar", PW, None)
            text = error_text(refused.value)
            assert "0.2.0" in text and "trop ancien" in text and "install.sh app" in text
            assert a.user is None
        finally:
            await a.close()

    asyncio.run(scenario())


def test_an_application_too_old_logs_in_to_update_but_hosts_nothing(tmp_path, backend, monkeypatch):
    monkeypatch.setattr(server_app, "MIN_CLIENT_VERSION", "9.0.0")
    monkeypatch.setattr(ws_routes, "MIN_CLIENT_VERSION", "9.0.0")
    rec = Recorder()

    async def scenario():
        a = agent(tmp_path, backend, rec)
        try:
            await a.register("oscar", PW, None)             # still possible: to fetch the update
            assert a.user is not None and a.too_old_for == "9.0.0"
            assert ("incompatible", {"current": __version__, "minimum": "9.0.0"}) in rec.events
            assert a._availability()["hosting_allowed"] is False
            await asyncio.sleep(1.5)                       # the control plane closes the WebSocket
            assert not a.channel.connected
            assert await a.api.client_release() is None    # update endpoints still answer

            # The VPS lowers its minimum again (e.g. rollback): a new login clears the state.
            monkeypatch.setattr(server_app, "MIN_CLIENT_VERSION", "0.1.0")
            monkeypatch.setattr(ws_routes, "MIN_CLIENT_VERSION", "0.1.0")
            await a.check_compatibility()
            assert a.too_old_for is None and a._availability()["hosting_allowed"]
            await until(lambda: a.channel.connected, timeout=30, what="websocket accepted again")
        finally:
            await a.close()

    asyncio.run(scenario())


def test_too_many_attempts_says_the_real_wait():
    def text(seconds):
        return error_text(ApiError(429, "rate_limited", "too many", {"retry_after_s": seconds}))

    assert text(30) == "Trop de tentatives d'affilée. Réessaie dans 30 secondes."
    assert text(700) == "Trop de tentatives d'affilée. Réessaie dans 12 minutes."
    assert "plus tard" in error_text(ApiError(429, "rate_limited", "too many")) or "60 secondes" in error_text(
        ApiError(429, "rate_limited", "too many"))


def test_alert_texts_for_admins():
    from mdh_client.gui.texts import alert_text

    gb = 1024**3
    disk = alert_text({"code": "vps_disk_low", "severity": "warning",
                       "details": {"free_bytes": 3 * gb, "total_bytes": 80 * gb}})
    assert "3.0 Go libres sur 80 Go" in disk
    quota = alert_text({"code": "group_quota_high", "severity": "critical",
                        "details": {"group_name": "Les copains", "used_bytes": 19 * gb, "quota_bytes": 20 * gb}})
    assert "« Les copains » utilise 95 %" in quota
    assert "install.sh status" in alert_text({"code": "vps_restarted", "severity": "warning", "details": {}})
