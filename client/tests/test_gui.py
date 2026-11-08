"""The real window (offscreen), the real agent thread, a real control plane, a fake Minecraft."""

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from mdh_client.application.agent import HostAgent  # noqa: E402
from mdh_client.application.runtime import AgentRuntime  # noqa: E402
from mdh_client.config import ClientSettings, Layout  # noqa: E402
from mdh_client.gui.bridge import Bridge  # noqa: E402
from mdh_client.gui.main_window import MainWindow  # noqa: E402
from mdh_client.gui.theme import STYLESHEET  # noqa: E402
from test_agent_live import PW, LiveBackend, fake_prepare, plenty_of_resources  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    return app


def wait(app, predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for {what}")


def test_offline_accounts_option_in_server_dialog(qapp, monkeypatch):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import ServerConfigDialog

    bridge = SimpleNamespace(run=lambda coro, **kw: coro.close())
    agent = SimpleNamespace(_resources=plenty_of_resources(None), available_versions=lambda loader: _noop())
    current = {"name": "Survie", "loader": "fabric", "mc_version": "26.3", "memory_mb": 3072,
               "properties": {"white-list": False}, "allow_offline_accounts": False, "allowed_players": []}
    dlg = ServerConfigDialog(bridge, agent, current)

    # The settings scroll, never taller than the screen; the wheel does not change a list it passes over.
    assert dlg.height() <= dlg.screen().availableGeometry().height()
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    before = dlg.difficulty.currentIndex()
    wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(dlg.difficulty, wheel)
    assert dlg.difficulty.currentIndex() == before and wheel.type() == QEvent.Type.Wheel

    # Refusing the warning leaves the option off.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg.offline.click()
    assert not dlg.offline.isChecked() and dlg.whitelist.isEnabled() and not dlg.personal.isEnabled()

    # Accepting it forces the whitelist.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.offline.click()
    assert dlg.offline.isChecked() and dlg.whitelist.isChecked() and not dlg.whitelist.isEnabled()
    assert dlg.personal.isEnabled() and dlg.personal.currentData() is True

    # The server's address for everybody: only after the warning.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg.personal.setCurrentIndex(1)
    dlg.personal.activated.emit(1)
    assert dlg.personal.currentData() is True
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.personal.setCurrentIndex(1)
    dlg.personal.activated.emit(1)
    assert dlg.personal.currentData() is False

    dlg.accept()                                       # empty list: refused, explained
    assert dlg.result() == 0 and "ton propre pseudo" in dlg.error.text()
    dlg.players.setPlainText("oscar\nbad name;op")
    dlg.accept()
    assert dlg.result() == 0 and "bad name;op" in dlg.error.text()
    dlg.players.setPlainText("oscar, Lea_2\n")
    dlg.accept()
    assert dlg.result() == 1
    values = dlg.values()
    assert values["allow_offline_accounts"] is True and values["personal_addresses"] is False
    assert values["allowed_players"] == ["oscar", "Lea_2"]
    assert values["properties"]["white-list"] is True


async def _noop():
    return []


def test_explains_why_no_pc_hosts_and_connection_pill_self_heals(qapp, tmp_path):
    """Regression (real test on the user's PC): a server needing more RAM than the PC has free
    stayed "Arrêté" with no explanation, and the pill showed "Hors ligne" while connected."""
    backend = LiveBackend(tmp_path)
    backend.start()
    runtime = AgentRuntime()
    bridge = Bridge(runtime)
    layout = Layout(tmp_path / "client")
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name="PC d'Oscar", max_ram_mb=4096,
                              eula_accepted_at="2026-10-31T10:00:00+00:00")

    async def build():
        return HostAgent(layout, settings, emit=bridge.emit_from_agent, prepare=fake_prepare,
                         store_credentials=False, detect_resources=plenty_of_resources)

    agent = runtime.call(build())
    try:
        runtime.call(agent.api.register("oscar", PW, None))
        runtime.call(agent.login("oscar", PW), timeout=30)
        group = runtime.call(agent.api.create_group("dev"))
        server = runtime.call(agent.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                                      loader="fabric", memory_mb=7680))
        window = MainWindow(bridge, agent)
        window.show()
        home = window.pages[0]
        wait(qapp, lambda: window.state.server is not None, what="server")
        wait(qapp, lambda: agent.channel.connected, what="websocket")

        window.connection.set("Hors ligne", "warn")    # as if the "connected" event was missed
        window.render()
        assert window.connection.text() == "Connecté"

        window.start_server()
        wait(qapp, lambda: home.waiting.isVisible() and "Ce PC" in home.waiting.text.text(),
             what="explanation banner")
        text = home.waiting.text.text()
        assert "attend un PC hôte" in text and "Ce PC" in text and "pas assez de RAM libre" in text
        assert "limité à 4.0 Go" in text or "limité à 4,0 Go" in text
        assert home.ram_btn.isVisible()                # admin: offered to lower the RAM
        assert not home.pill.text() == "En ligne"
        window.close()
    finally:
        runtime.call(agent.close(), timeout=60)
        runtime.stop()
        backend.stop()


def test_dashboard_end_to_end(qapp, tmp_path, monkeypatch):
    # Any unexpected error dialog fails the test instead of blocking it.
    def no_dialog(*args, **kwargs):
        raise AssertionError(f"unexpected dialog: {args[1:3]}")

    monkeypatch.setattr(QMessageBox, "warning", no_dialog)
    backend = LiveBackend(tmp_path)
    backend.start()
    runtime = AgentRuntime()
    bridge = Bridge(runtime)
    layout = Layout(tmp_path / "client")
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name="PC d'Oscar", server_port=25598,
                              max_ram_mb=2048, eula_accepted_at="2026-10-31T10:00:00+00:00")

    async def build():
        return HostAgent(layout, settings, emit=bridge.emit_from_agent, ask_offer=bridge.ask_offer,
                         prepare=fake_prepare, store_credentials=False, ready_timeout_s=20,
                         detect_resources=plenty_of_resources,
                         command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)])

    agent = runtime.call(build())
    try:
        runtime.call(agent.api.register("oscar", PW, None))
        runtime.call(agent.login("oscar", PW), timeout=30)
        group = runtime.call(agent.api.create_group("Les copains"))
        window = MainWindow(bridge, agent)
        window.show()
        home = window.pages[0]

        wait(qapp, lambda: window.group_combo.count() == 1, what="group list")
        # Admin of a group without server: invited to create it.
        wait(qapp, lambda: home.empty.isVisible() and home.create_server_btn.isVisible(),
             what="create-server invitation")
        assert "pas encore de serveur" in home.empty.text.text()

        server = runtime.call(agent.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                                      loader="paper", memory_mb=1024))
        runtime.call(agent.api.set_priority(server["server_id"], agent.settings.host_id, 100))
        window.reload_servers()
        wait(qapp, lambda: window.server_combo.count() == 1 and window.state.server is not None,
             what="server list")
        assert home.pill.text() == "Arrêté"
        assert home.start_btn.isEnabled()
        wait(qapp, lambda: window.connection.text() == "Connecté", what="connection pill")

        home.start_btn.click()
        wait(qapp, lambda: home.pill.text() == "En ligne", timeout=30, what="server online")
        assert "PC d'Oscar" in home.cards["host"].value.text()
        assert window.state.hosting_phase == "running"

        window.sidebar.setCurrentRow(1)
        server_page = window.pages[1]
        wait(qapp, lambda: "Done (" in server_page.console.toPlainText(), what="console output")
        assert server_page.command.isEnabled() and server_page.restart_btn.isEnabled()
        server_page.command.setText("list")
        server_page.send_btn.click()
        wait(qapp, lambda: "There are 1 of a max" in server_page.console.toPlainText(), what="command result")

        window.sidebar.setCurrentRow(2)
        window.refresh_details()
        group_page = window.pages[2]
        wait(qapp, lambda: group_page.hosts.rowCount() == 1 and group_page.history.count() > 3,
             what="group details")
        assert group_page.hosts.item(0, 0).text() == "PC d'Oscar"
        assert group_page.hosts.cellWidget(0, 4).value() == 100
        history = [group_page.history.item(i).text() for i in range(group_page.history.count())]
        assert any("Serveur en ligne" in h for h in history)

        window.sidebar.setCurrentRow(5)
        qapp.processEvents()
        assert "acceptée" in window.pages[5].eula.text()

        # Storage page: manual backup while this PC hosts.
        window.sidebar.setCurrentRow(3)
        storage = window.pages[3]
        wait(qapp, lambda: storage.backup_btn.isEnabled(), what="backup button")
        storage.backup_btn.click()
        wait(qapp, lambda: any(v["kind"] == "manual" for v in window.state.versions), timeout=30,
             what="manual backup listed")
        assert not storage.restore_btn.isEnabled()          # server running: restore not allowed

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        window.stop_server()
        wait(qapp, lambda: home.pill.text() == "Arrêté" and window.state.hosting_phase is None,
             timeout=60, what="server stopped")
        window.refresh_details()
        wait(qapp, lambda: storage.table.rowCount() == 2, what="final backup listed")
        assert storage.table.item(0, 1).text() == "Fin de session · actuelle"
        assert home.cards["sync"].value.text() == "À jour"
        storage.table.selectRow(1)                           # the manual backup
        qapp.processEvents()
        assert storage.restore_btn.isEnabled()
        storage.restore_btn.click()
        wait(qapp, lambda: any(v["kind"] == "restore" for v in window.state.versions), what="restored")
        window.close()
    finally:
        runtime.call(agent.close(), timeout=60)
        runtime.stop()
        backend.stop()


def test_change_of_pc_from_the_window(qapp, tmp_path, monkeypatch):
    from mdh_client.gui.dialogs import TransferDialog

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"unexpected dialog: {a[1:3]}"))
    backend = LiveBackend(tmp_path)
    backend.start()
    runtime = AgentRuntime()
    bridge = Bridge(runtime)

    def make(name: str, display: str, emit):
        layout = Layout(tmp_path / name)
        layout.ensure()
        settings = ClientSettings(api_url=backend.url, display_name=display, max_ram_mb=2048,
                                  eula_accepted_at="2026-10-31T10:00:00+00:00")

        async def build():
            return HostAgent(layout, settings, emit=emit, prepare=fake_prepare, store_credentials=False,
                             ready_timeout_s=20, stop_timeout_s=10, detect_resources=plenty_of_resources,
                             command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)])
        return runtime.call(build())

    oscar = make("oscar", "PC d'Oscar", bridge.emit_from_agent)
    lea = make("lea", "PC de Léa", lambda *a, **k: None)
    try:
        runtime.call(oscar.api.register("oscar", PW, None))
        runtime.call(oscar.login("oscar", PW), timeout=30)
        group = runtime.call(oscar.api.create_group("Les copains"))
        server = runtime.call(oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                                      loader="paper", memory_mb=1024))
        code = runtime.call(oscar.api.create_invite(group["group_id"]))["code"]
        runtime.call(lea.register("lea", PW, code), timeout=30)
        runtime.call(oscar.api.set_priority(server["server_id"], oscar.settings.host_id, 100))
        window = MainWindow(bridge, oscar)
        window.show()
        home = window.pages[0]
        wait(qapp, lambda: window.state.server is not None and window.connection.text() == "Connecté",
             what="window ready")
        assert not home.transfer_btn.isEnabled()                 # nothing to move while stopped
        home.start_btn.click()
        wait(qapp, lambda: home.pill.text() == "En ligne", timeout=30, what="server online")
        assert home.cards["address"].value.text() == f"mc.test:{backend.relay_port}"   # via the VPS relay
        assert home.copy_address_btn.isVisible()
        window.refresh_details()
        wait(qapp, lambda: home.transfer_btn.isEnabled() and len(window.state.hosts) == 2
             and all(h["online"] for h in window.state.hosts), what="transfer possible")

        def choose_lea(dialog):
            labels = [dialog.target.itemText(i) for i in range(dialog.target.count())]
            assert labels == ["Automatique : le meilleur PC disponible", "PC de Léa (lea)"]
            dialog.target.setCurrentIndex(1)
            dialog.countdown.setValue(5)
            return 1
        monkeypatch.setattr(TransferDialog, "exec", choose_lea)
        home.transfer_btn.click()
        wait(qapp, lambda: home.migration.isVisible(), what="migration banner")
        assert "ce PC → PC de Léa (lea)" in home.migration.text.text()
        assert home.pill.text() == "Changement de PC…" and not home.transfer_btn.isEnabled()

        wait(qapp, lambda: home.pill.text() == "En ligne" and "PC de Léa" in home.cards["host"].value.text(),
             timeout=60, what="server running on Léa's PC")
        wait(qapp, lambda: not home.migration.isVisible() and window.state.hosting_phase is None,
             what="banner gone, this PC released")

        # Oscar (admin) reads the console of Léa's PC, read-only.
        window.sidebar.setCurrentRow(1)
        server_page = window.pages[1]
        wait(qapp, lambda: not server_page.console_mode.isHidden(), what="console choice for the admin")
        assert "Discussion en jeu" in server_page.console_label.text()
        server_page.console_mode.setCurrentIndex(1)
        wait(qapp, lambda: "Done (" in server_page.console.toPlainText(), timeout=20, what="console of Léa's PC")
        assert "Journal du serveur" in server_page.console_label.text()
        assert server_page.command.placeholderText() == "Message à tous les joueurs (annonce)"
        window.refresh_details()
        window.sidebar.setCurrentRow(2)
        history = window.pages[2].history
        wait(qapp, lambda: any("Changement de PC terminé" in history.item(i).text()
                               for i in range(history.count())), what="journal")
        window.close()
    finally:
        runtime.call(oscar.close(), timeout=60)
        runtime.call(lea.close(), timeout=60)
        runtime.stop()
        backend.stop()

def test_player_addresses_dialog(qapp, monkeypatch):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import PlayerAddressesDialog

    calls = []
    rows = [{"player": "Alex", "address": "abcdefghijkl.mc.example.org"},
            {"player": "Krystal", "address": "mnopqrstuvwx.mc.example.org"}]

    def run(coro, on_ok=None, on_error=None):
        calls.append(coro.cr_code.co_name)
        coro.close()
        on_ok(rows if calls[-1] == "_noop" else {})

    api = SimpleNamespace(player_addresses=lambda sid: _noop(),
                          regenerate_player_address=lambda sid, player: _regen())
    dlg = PlayerAddressesDialog(SimpleNamespace(run=run), SimpleNamespace(api=api), "0" * 32)
    assert dlg.table.rowCount() == 2 and dlg.table.item(1, 1).text() == "mnopqrstuvwx.mc.example.org"
    # The buttons fit whole, without widening the window.
    cell = dlg.table.cellWidget(0, 2)
    assert dlg.table.columnWidth(2) >= cell.sizeHint().width()
    assert dlg.table.rowHeight(0) >= cell.sizeHint().height()
    copy =next(b for b in dlg.table.cellWidget(0, 2).findChildren(QPushButton) if b.text() == "Copier")
    copy.click()
    assert QApplication.clipboard().text() == "abcdefghijkl.mc.example.org"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._regenerate("Krystal")
    assert calls == ["_noop", "_regen", "_noop"]


async def _regen():
    return {}

def test_automatic_failover_setting(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import ServerConfigDialog

    bridge = SimpleNamespace(run=lambda coro, **kw: coro.close())
    agent = SimpleNamespace(_resources=plenty_of_resources(None), available_versions=lambda loader: _noop())
    base = {"name": "Survie", "loader": "paper", "mc_version": "26.3", "memory_mb": 3072, "properties": {},
            "allow_offline_accounts": False, "allowed_players": []}

    dlg = ServerConfigDialog(bridge, agent, {**base, "auto_failover_after_s": None})
    assert dlg.failover.currentIndex() == 0 and dlg.values()["auto_failover_after_s"] is None
    dlg.failover.setCurrentIndex(dlg.failover.findData(600))
    assert dlg.values()["auto_failover_after_s"] == 600
    assert "10 minutes" in dlg.failover.currentText()

    # A delay chosen elsewhere (API) is shown and kept as is.
    dlg = ServerConfigDialog(bridge, agent, {**base, "auto_failover_after_s": 420})
    assert dlg.values()["auto_failover_after_s"] == 420 and "7 minutes" in dlg.failover.currentText()


def test_orphaned_banner_tells_when_the_server_restarts_elsewhere(qapp):
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from mdh_client.gui.pages import AppState, HomePage

    page = HomePage(MagicMock())
    at = datetime(2026, 10, 31, 21, 45, tzinfo=UTC)
    srv = {"server_id": "0" * 32, "name": "Survie", "state": "orphaned", "desired_running": True,
           "mc_version": "26.3", "loader": "paper", "memory_mb": 3072, "properties": {},
           "allow_offline_accounts": False, "allowed_players": [], "active_host_id": None,
           "players_online": None, "head_version_id": "1" * 32, "migration": None, "public_address": None,
           "relay_connections": 0, "tps": None, "mspt": None, "host_state": None, "lease_epoch": 3,
           "last_backup_at": "2026-10-31T21:30:00+00:00", "auto_failover_at": at.isoformat()}
    member = AppState(user={"username": "lea"}, group={"name": "Amis", "role": "member"}, server=srv)
    page.refresh(member)
    text = page.banner.text.text()
    assert f"vers {at.astimezone().strftime('%H:%M')}" in text and "toi-même" not in text
    assert page.failover_btn.isHidden()

    admin = AppState(user={"username": "krystal"}, group={"name": "Amis", "role": "owner"}, server={**srv, "auto_failover_at": None})
    page.refresh(admin)
    assert "tu peux relancer" in page.banner.text.text() and not page.failover_btn.isHidden()

def test_a_ready_update_installs_itself_once_the_window_is_closed(qapp, tmp_path, monkeypatch):
    from mdh_client.system import updates
    from test_updates import TRUSTED, publish

    backend = LiveBackend(tmp_path)
    backend.start()
    publish(backend.updates, "9.0.0", b"MZ new version", notes="Nouveautés.")
    monkeypatch.setattr(updates, "TRUSTED_KEYS", TRUSTED)
    launched = []
    monkeypatch.setattr(updates.Updater, "launch",
                        staticmethod(lambda ready, relaunch, log_file: launched.append((ready.version, relaunch))))
    monkeypatch.setattr(QApplication, "quit", lambda: None)
    runtime = AgentRuntime()
    bridge = Bridge(runtime)
    layout = Layout(tmp_path / "client")
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name="PC d'Oscar")

    async def build():
        agent = HostAgent(layout, settings, emit=bridge.emit_from_agent, prepare=fake_prepare,
                          store_credentials=False, detect_resources=plenty_of_resources)
        agent.updater.keys = TRUSTED
        return agent

    agent = runtime.call(build())
    try:
        runtime.call(agent.api.register("oscar", PW, None))
        runtime.call(agent.login("oscar", PW), timeout=30)
        window = MainWindow(bridge, agent)
        window.show()
        runtime.call(agent.updater.check(), timeout=30)
        wait(qapp, lambda: window.update_btn.isVisible(), what="update button")
        assert "9.0.0" in window.update_btn.text()
        settings_page = window.pages[5]
        settings_page.refresh(window.state)
        assert "9.0.0" in settings_page.version.text() and "Nouveautés." in settings_page.version.text()

        window._maybe_install_update()                   # window open: never interrupts the user
        assert not launched and not agent.updating
        window.hide()
        window._maybe_install_update()
        wait(qapp, lambda: launched, what="installer start")
        assert launched == [("9.0.0", "tray")]           # restarts in the notification area
        assert agent.updating and not agent._tasks       # agent stopped before the installer ran
    finally:
        runtime.call(agent.close(), timeout=60)
        runtime.stop()
        backend.stop()


# --------------------------------------------------------------------------- 0.4.0 dialogs


class SyncBridge:
    """Runs each coroutine at once, like the real bridge but without the agent thread."""

    def run(self, coro, on_ok=None, on_error=None):
        import asyncio

        try:
            result = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return
        if on_ok:
            on_ok(result)


def _mods_agent(fake):
    from types import SimpleNamespace

    from mdh_client.minecraft.downloads import make_client
    from mdh_client.minecraft.modrinth import Modrinth
    from mdh_shared.models.enums import Loader

    async def search(server, query):
        async with make_client(fake.transport()) as c:
            return await Modrinth(c).search(query, Loader(server["loader"]), server["mc_version"])

    async def resolve(server, ref, current):
        async with make_client(fake.transport()) as c:
            return await Modrinth(c).resolve(ref, Loader(server["loader"]), server["mc_version"], current)

    async def updates(server):
        from mdh_shared.models.api import Mod

        async with make_client(fake.transport()) as c:
            return await Modrinth(c).updates([Mod.model_validate(m) for m in server["mods"]],
                                             Loader(server["loader"]), server["mc_version"])

    return SimpleNamespace(search_mods=search, resolve_mod=resolve, mod_updates=updates)


def test_mod_updates_in_one_click(qapp, monkeypatch):
    from fake_modrinth import FakeModrinth
    from mdh_client.gui.group_dialogs import ModsDialog
    from mdh_client.gui.pages import AppState, mods_key

    fake = FakeModrinth()
    fake.add("P7dR8mSH", "fabric-api", version_id="FapiAAAA")
    fake.add("gvQqBUqZ", "lithium", deps=["P7dR8mSH"], version_id="LithBBBB")
    agent = _mods_agent(fake)
    server = {"server_id": "a" * 32, "loader": "fabric", "mc_version": "26.3", "mods": [], "map_enabled": False}
    dlg = ModsDialog(SyncBridge(), agent, server, editable=True)
    dlg._add_project("gvQqBUqZ")
    server["mods"] = [m.model_dump(mode="json") for m in dlg.mods]

    questions = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: questions.append(a[2]) or QMessageBox.StandardButton.Yes)
    dlg = ModsDialog(SyncBridge(), agent, server, editable=True)
    assert dlg.update_btn.text() == "Vérifier les mises à jour"
    dlg._update()
    assert "Tout est à jour" in dlg.info.text() and not questions

    fake.add("gvQqBUqZ", "lithium", deps=["P7dR8mSH"], version_id="Lith015x", version_number="0.15")
    dlg._update()
    assert "Lithium : 1.0-fabric → 0.15" in questions[-1]
    assert {m.version_id for m in dlg.mods} == {"Lith015x", "FapiAAAA"} and "enregistre" in dlg.info.text()
    assert dlg.values()[0] == dlg.mods

    # Found in the background by the admin's PC: shown on the button, applied from the dialog.
    state = AppState(server=server)
    state.mod_updates[server["server_id"]] = {"key": mods_key(server), "mods": dlg.mods,
                                              "changes": ["one"]}
    assert state.mod_updates_for(server) is not None
    assert state.mod_updates_for({**server, "mc_version": "26.4"}) is None
    shown = ModsDialog(SyncBridge(), agent, server, editable=True, updates=state.mod_updates_for(server))
    assert shown.update_btn.text() == "Mettre à jour (1)"


def test_mods_dialog(qapp, monkeypatch):
    from fake_modrinth import FakeModrinth
    from mdh_client.gui.group_dialogs import ModsDialog

    fake = FakeModrinth()
    fake.add("P7dR8mSH", "fabric-api", version_id="FapiAAAA")
    fake.add("gvQqBUqZ", "lithium", deps=["P7dR8mSH"], version_id="LithBBBB")
    fake.add("swbUV1cr", "bluemap", deps=["P7dR8mSH"], version_id="BlueCCCC")
    server = {"server_id": "a" * 32, "loader": "fabric", "mc_version": "26.3", "mods": [], "map_enabled": False}
    dlg = ModsDialog(SyncBridge(), _mods_agent(fake), server, editable=True)
    dlg.query.setText("lith")
    dlg._search()
    assert dlg.results.count() == 1 and "Lithium" in dlg.results.item(0).text()
    dlg.results.setCurrentRow(0)
    dlg._add()
    assert [m.slug for m in dlg.mods] == ["lithium", "fabric-api"]
    assert "nécessaire à Lithium" in dlg.table.item(1, 3).text() and "Fabric-api" in dlg.info.text()
    # A dependency cannot be removed by itself; lithium can, and takes Fabric API with it.
    dlg.table.selectRow(1)
    assert not dlg.remove_btn.isEnabled()
    dlg.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._remove()
    assert dlg.mods == []
    # The map option adds BlueMap (and what it needs).
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dlg.map.click()
    mods, map_enabled = dlg.values()
    assert map_enabled and [m.slug for m in mods] == ["bluemap", "fabric-api"]
    dlg.map.click()
    assert dlg.values() == ([], False)

    viewer = ModsDialog(SyncBridge(), _mods_agent(fake), server, editable=False)
    assert viewer.query.isHidden() and not viewer.map.isEnabled() and "administrateur" in viewer.info.text()
    vanilla = ModsDialog(SyncBridge(), _mods_agent(fake), {**server, "loader": "vanilla"}, editable=True)
    assert vanilla.query.isHidden() and not vanilla.map.isEnabled()


def test_mod_approval_and_access_requests_dialogs(qapp, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QLabel, QTableWidget

    from fake_modrinth import FakeModrinth
    from mdh_client.gui.group_dialogs import AccessRequestsDialog, ModApprovalDialog
    from mdh_shared.models.api import Mod

    v = FakeModrinth().add("gvQqBUqZ", "lithium", version_id="LithBBBB")
    f = v["files"][0]
    mod = Mod(project_id="gvQqBUqZ", version_id="LithBBBB", slug="lithium", name="Lithium", version_number="1",
              url=f["url"], sha512=f["hashes"]["sha512"], size=f["size"], loader="fabric", mc_version="26.3")
    dlg = ModApprovalDialog("Survie", [mod])
    table = dlg.findChild(QTableWidget)
    assert table.rowCount() == 1 and table.item(0, 3).text() == mod.sha512[:16] + "…"

    rows = [{"player": "Steve", "first_at": "2026-11-01T10:00:00+00:00", "last_at": "2026-11-01T10:05:00+00:00",
             "attempts": 2, "status": "pending"},
            {"player": "Krystal", "first_at": "2026-11-01T10:00:00+00:00", "last_at": "2026-11-01T10:06:00+00:00",
             "attempts": 1, "status": "pending", "kind": "new_place", "place": "5.6.7.0/24"}]
    answered = []

    async def listing(sid):
        return [r for r in rows if r["player"] not in {a[0] for a in answered}]

    async def answer(sid, player, accept, place=None):
        answered.append((player, place))
        return {}

    async def player_addresses(sid):
        return [{"player": "Steve", "address": "abcdefghijkl.mc.test"}]

    shown = []
    monkeypatch.setattr("mdh_client.gui.group_dialogs.show_player_address", lambda *a: shown.append(a[1:]))
    agent = SimpleNamespace(api=SimpleNamespace(access_requests=listing, answer_access_request=answer,
                                                player_addresses=player_addresses))
    requests = AccessRequestsDialog(SyncBridge(), agent, {"server_id": "a" * 32, "allow_offline_accounts": True})
    assert requests.table.rowCount() == 2 and "adresse personnelle" in requests.findChild(QLabel).text()
    assert requests.table.item(1, 1).text() == "Nouvel endroit (5.6.7.0/24)"
    requests.table.selectRow(1)
    requests._answer(True)
    requests.table.selectRow(0)
    requests._answer(True)
    assert answered == [("Krystal", "5.6.7.0/24"), ("Steve", "")]
    assert shown == [("Steve", "abcdefghijkl.mc.test")]     # a new place has no address to show
    assert requests.table.rowCount() == 0 and requests.changed
    shared = AccessRequestsDialog(SyncBridge(), agent, {"server_id": "a" * 32, "allow_offline_accounts": True,
                                                        "personal_addresses": False})
    assert "Nouvel endroit" in shared.findChild(QLabel).text()


def test_resource_pack_dialog(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.group_dialogs import ResourcePackDialog
    from mdh_client.minecraft.modrinth import SearchHit
    from mdh_client.minecraft.resource_pack import PackError
    from mdh_shared.models.api import ResourcePack

    async def search(server, query):
        return [SearchHit("w0TnApzs", "faithful-32x", "Faithful 32x", "textures", 5000)]

    async def from_modrinth(server, ref, required):
        return ResourcePack(name="Faithful 32x", url="https://cdn.modrinth.com/data/w0TnApzs/versions/oHo9IUvX/f.zip",
                            sha1="b" * 40, size=10_000_000, required=required, project_id=ref,
                            version_id="oHo9IUvX", version_number="26.3")

    async def from_url(url, required):
        raise PackError("Ce fichier n'est pas un pack de ressources (pas de pack.mcmeta à la racine du zip).")

    agent = SimpleNamespace(search_packs=search, modrinth_pack=from_modrinth, pack_from_url=from_url)
    dlg = ResourcePackDialog(SyncBridge(), agent, {"server_id": "a" * 32, "mc_version": "26.3"})
    assert "Aucun pack" in dlg.current.text() and not dlg.remove_btn.isEnabled() and dlg.value() is None
    dlg.query.setText("faith")
    dlg._search()
    dlg.results.setCurrentRow(0)
    dlg._choose()
    assert dlg.value().project_id == "w0TnApzs" and "facultatif" in dlg.current.text()
    dlg.required.setChecked(True)
    assert dlg.value().required and "obligatoire" in dlg.current.text()
    dlg.link.setText("https://example.org/readme.zip")
    dlg._check_link()
    assert "pack.mcmeta" in dlg.error.text() and dlg.value().project_id == "w0TnApzs"   # unchanged
    dlg._remove()
    assert dlg.value() is None and not dlg.required.isChecked()


def test_stats_page_fill(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.pages import StatsPage

    win = SimpleNamespace(state=SimpleNamespace(server=None), bridge=SyncBridge(), agent=None,
                          start_profile=lambda: None)
    page = StatsPage(win)
    hours = [{"weekday": d, "hour": h, "online_seconds": 3600 if (d, h) == (5, 21) else 0,
              "average_players": 3.5 if (d, h) == (5, 21) else None} for d in range(7) for h in range(24)]
    page._fill({"days": 30, "online_seconds": 5400,
                "players": [{"player": "Oscar", "seconds": 4000, "last_seen_at": "2026-11-01T20:00:00+00:00"}],
                "hosts": [{"host_id": "a" * 32, "display_name": "PC Oscar", "owner": "oscar", "seconds": 5400}],
                "hours": hours})
    assert page.online.value.text() == "1 h 30" and page.top.value.text() == "Oscar"
    assert page.peak.value.text() == "Sam vers 21h" and page.hours.item(5, 21).text() == "3,5"
    assert page.hosts.item(0, 3).text() == "100 %"


def test_modpack_import_and_the_file_for_players(qapp, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QFileDialog

    from fake_modrinth import FakeModrinth
    from mdh_client.gui import group_dialogs
    from mdh_client.gui.group_dialogs import ModpackDialog, ModsDialog
    from mdh_client.minecraft import modpack
    from mdh_client.minecraft.downloads import make_client
    from mdh_client.minecraft.modrinth import Modrinth
    from mdh_client.minecraft.mods import ModInstaller
    from test_modpack import PACK, entry, mrpack

    fake = FakeModrinth()
    fake.add("P7dR8mSH", "fabric-api", version_id="FapiAAAA", game_versions=("1.21.1",))
    fake.add("gvQqBUqZ", "lithium", version_id="LithBBBB")
    fake.add(PACK, "cobblemon-pack", project_type="modpack", version_id="PackDDDD", title="Cobblemon Pack",
             game_versions=("1.21.1",), content=mrpack([entry(fake, "P7dR8mSH")]))
    (tmp_path / "dl").mkdir()

    async def search_modpacks(query):
        async with make_client(fake.transport()) as c:
            return await Modrinth(c).search_modpacks(query)

    async def import_modpack(ref):
        async with make_client(fake.transport()) as c:
            return await modpack.import_pack(Modrinth(c), ModInstaller(tmp_path / "c", tmp_path / "dl", c), ref)

    exported = []

    async def export_for_players(server, dest, kind):
        exported.append((dest, kind))
        return [SimpleNamespace(required=True), SimpleNamespace(required=False)]

    agent = SimpleNamespace(search_modpacks=search_modpacks, import_modpack=import_modpack,
                            export_for_players=export_for_players, **vars(_mods_agent(fake)))
    server = {"server_id": "a" * 32, "name": "Survie", "loader": "fabric", "mc_version": "26.3", "mods": [],
              "map_enabled": False}
    questions = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: questions.append(a[2]) or QMessageBox.StandardButton.Yes)
    dlg = ModpackDialog(SyncBridge(), agent, server)
    dlg.query.setText("cobblemon")
    dlg._search()
    dlg.results.setCurrentRow(0)
    assert not dlg.import_btn.isEnabled()
    dlg._read()
    assert "Cobblemon Pack" in dlg.summary.text() and "Minecraft 1.21.1" in dlg.summary.text()
    dlg._confirm()
    assert "plus ancien que 26.3" in questions[-1] and dlg.imported.mods[0].slug == "fabric-api"

    # From the mods dialog: the chosen modpack closes it, the window applies it.
    chosen = SimpleNamespace(exec=lambda: 1, imported=dlg.imported)
    monkeypatch.setattr(group_dialogs, "ModpackDialog", lambda *a, **k: chosen)
    mods_dlg = ModsDialog(SyncBridge(), agent, server, editable=True)
    mods_dlg._import()
    assert mods_dlg.imported is dlg.imported and mods_dlg.result() == 1

    # The file for players: only for the saved list.
    mods_dlg = ModsDialog(SyncBridge(), agent, server, editable=True)
    mods_dlg._add_project("gvQqBUqZ")
    assert mods_dlg.mods
    assert not mods_dlg.players_btn.isEnabled()
    saved = {**server, "mods": [m.model_dump(mode="json") for m in mods_dlg.mods],
             "modpack": dlg.imported.modpack.model_dump(mode="json")}
    mods_dlg = ModsDialog(SyncBridge(), agent, saved, editable=False)
    assert mods_dlg.players_btn.isEnabled() and mods_dlg.modpack_btn.isHidden()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: next(b for b in self.buttons() if "TLauncher" in b.text()))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "Survie - mods.zip"), ""))
    mods_dlg._export()
    assert exported == [(tmp_path / "Survie - mods.zip", "zip")]
    assert "2 mod(s), dont 1 facultatif(s)" in mods_dlg.info.text()
    assert not ModsDialog(SyncBridge(), agent, {**saved, "loader": "paper"}, editable=True).players_btn.isVisibleTo(
        mods_dlg)


def test_pregeneration_and_crash_on_the_server_page(qapp):
    from mdh_client.gui.group_dialogs import PregenDialog
    from mdh_client.gui.pages import AppState, ServerPage

    chunky = {"slug": "chunky", "project_id": "fALzjamp"}
    lith = {"slug": "lithium", "project_id": "gvQqBUqZ", "name": "Lithium"}
    dlg = PregenDialog({"mods": [lith]})
    assert dlg.missing and dlg.ok_btn.text() == "Ajouter Chunky"
    dlg = PregenDialog({"mods": [lith, chunky]})
    assert not dlg.missing and dlg.ok_btn.text() == "Pré-générer" and dlg.values() == ("overworld", 2500)
    assert "Mo" in dlg.estimate.text() or "Go" in dlg.estimate.text()

    class Win:
        def __init__(self):
            self.agent = type("A", (), {"unapproved_mods": lambda self, s: [],
                                        "settings": type("S", (), {"host_id": "h" * 32, "hosting_allowed": True})()})()

        def __getattr__(self, name):
            return lambda *a, **k: None

    win = Win()
    page = ServerPage(win)
    server = {"server_id": "a" * 32, "name": "Survie", "loader": "fabric", "state": "running", "desired_running": True,
              "mods": [lith, chunky], "pregen": {"dimension": "the_nether", "radius": 5000, "state": "running",
                                                 "percent": 42.5, "eta_s": 4000},
              "last_crash": {"at": "2026-11-02T10:00:00+00:00", "cause": "mod", "message": "Le plantage vient de "
                             "Lithium 1.0 : mets-le à jour ou retire-le.", "mod_project_id": "gvQqBUqZ",
                             "mod_name": "Lithium"}}
    state = AppState(group={"role": "admin"}, server=server)
    win.state = state
    page.refresh(state)
    assert page.pregen_label.text() == "Pré-génération (Nether, 5 000 blocs) : 42.5 % — reste environ 1 h 07"
    assert not page.pregen_stop.isHidden() and not page.pregen_btn.isEnabled()
    assert not page.crash.isHidden() and "Lithium 1.0" in page.crash.text.text()
    assert page.crash_btn.text() == "Retirer Lithium…" and not page.crash_btn.isHidden()
    server.update(pregen={**server["pregen"], "state": "done", "percent": 100.0}, last_crash=None)
    page.refresh(state)
    assert "terminée" in page.pregen_label.text() and page.pregen_stop.isHidden() and page.pregen_btn.isEnabled()
    assert page.crash.isHidden()
    state.group = {"role": "member"}
    page.refresh(state)
    assert page.pregen_btn.isHidden()


def test_templates_fill_the_creation_dialog(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import ServerConfigDialog, TemplateDialog

    picker = TemplateDialog()
    assert list(picker.buttons) == ["vanilla", "optimized", "plugins", "modpack"]
    picker.buttons["optimized"].click()
    assert picker.result() == 1 and picker.chosen.key == "optimized"
    bridge = SimpleNamespace(run=lambda coro, **kw: coro.close())
    agent = SimpleNamespace(_resources=plenty_of_resources(None), available_versions=lambda loader: _noop())
    dlg = ServerConfigDialog(bridge, agent, None, defaults=picker.chosen.defaults())
    values = dlg.values()
    assert dlg.windowTitle() == "Nouveau serveur"
    assert values["loader"] == "fabric" and values["properties"]["simulation-distance"] == 8
    assert values["start_on_join"] and values["stop_when_empty_after_s"] == 1800 and values["name"] == "Survie optimisée"


def test_performance_graphs_and_spark(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.pages import AppState, StatsPage

    state = AppState(group={"role": "admin"},
                     server={"server_id": "a" * 32, "loader": "fabric", "state": "running", "mods": [],
                             "profile": {"state": "done", "url": "https://spark.lucko.me/AbCdEf1234"}})
    win = SimpleNamespace(state=state, bridge=SyncBridge(), agent=None, start_profile=lambda: None)
    page = StatsPage(win)
    page._refresh_spark(state)
    assert "https://spark.lucko.me/AbCdEf1234" in page.spark_label.text() and page.spark_btn.isEnabled()
    state.server["profile"] = {"state": "running", "url": None}
    page._refresh_spark(state)
    assert "en cours" in page.spark_label.text() and not page.spark_btn.isEnabled()
    state.group = {"role": "member"}
    page._refresh_spark(state)
    assert page.spark_btn.isHidden()
    page._fill_perf({"hours": 1, "step_s": 60, "points": [
        {"at": "2026-11-02T10:00:00+00:00", "tps": 19.5, "tps_min": 12.0, "mspt": 30.0, "mspt_max": 80.0,
         "ram_used_mb": 4000, "cpu_percent": 35.0, "players": 2},
        {"at": "2026-11-02T10:01:00+00:00", "tps": None, "tps_min": None, "mspt": None, "mspt_max": None,
         "ram_used_mb": None, "cpu_percent": None, "players": None}]})
    assert page.tps_chart.points[0][1:] == (19.5, 12.0) and page.ram_chart.points[0][2] is None
    page.tps_chart.resize(400, 160)
    page.tps_chart.grab()       # draws without error, gaps included


def test_datapacks_and_settings_dialogs(qapp, monkeypatch):
    from types import SimpleNamespace

    from mdh_client.gui.group_dialogs import ConfigFilesDialog, DatapacksDialog
    from mdh_client.minecraft.modrinth import SearchHit
    from mdh_shared.models.api import Datapack

    terra = Datapack(project_id="8oi3bsk5", version_id="TerrAAAA", slug="terralith", name="Terralith",
                     version_number="2.6.5", url="https://cdn.modrinth.com/data/8oi3bsk5/versions/TerrAAAA/T.zip",
                     sha512="d" * 128, size=2_000_000, mc_version="26.3")

    async def search(server, query):
        return [SearchHit("8oi3bsk5", "terralith", "Terralith", "terrain", 900)]

    async def one(server, ref):
        return terra

    saved = {}

    async def config_file_list(server):
        return [("config/lithium.properties", 14, False), ("config/sodium.json", 20, True)]

    async def config_files(sid):
        return {"config/sodium.json": '{"a": 1}'}

    async def read_config_file(server, path):
        return "mixin.ai=true\n"

    async def save_config_files(sid, files):
        saved.update(files)
        return {"server_id": sid, "config_file_paths": sorted(files)}

    agent = SimpleNamespace(search_datapacks=search, datapack=one, config_file_list=config_file_list,
                            read_config_file=read_config_file, save_config_files=save_config_files,
                            api=SimpleNamespace(config_files=config_files))
    server = {"server_id": "a" * 32, "mc_version": "26.3", "datapacks": []}
    dlg = DatapacksDialog(SyncBridge(), agent, server, editable=True)
    dlg.query.setText("terra")
    dlg._search()
    dlg.results.setCurrentRow(0)
    dlg._add()
    assert [p.slug for p in dlg.values()] == ["terralith"] and "Enregistre" in dlg.info.text()
    viewer = DatapacksDialog(SyncBridge(), agent, {**server, "datapacks": [terra.model_dump(mode="json")]},
                             editable=False)
    assert viewer.query.isHidden() and viewer.table.rowCount() == 1

    cfg = ConfigFilesDialog(SyncBridge(), agent, {"server_id": "a" * 32})
    labels = [cfg.files.item(i).text() for i in range(cfg.files.count())]
    assert labels == ["config/lithium.properties", "✎ config/sodium.json"]
    cfg.files.setCurrentRow(0)
    assert cfg.editor.toPlainText() == "mixin.ai=true\n" and "dernière sauvegarde" in cfg.status.text()
    cfg.editor.setPlainText("mixin.ai=false\n")
    assert cfg.managed["config/lithium.properties"] == "mixin.ai=false\n"
    cfg.files.setCurrentRow(1)
    cfg.editor.setPlainText('{"a": 1,}')
    assert "JSON invalide" in cfg.error.text()
    questions = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: questions.append(a[2]) or QMessageBox.StandardButton.No)
    cfg._save()
    assert "JSON invalide" in questions[-1] and not saved          # not saved while the user hesitates
    cfg._unmanage()
    cfg._save()
    assert saved == {"config/lithium.properties": "mixin.ai=false\n"}
    assert cfg.saved_status["config_file_paths"] == ["config/lithium.properties"]


def test_automatic_choice_of_the_host_in_the_window(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import ServerConfigDialog
    from mdh_client.gui.pages import measures_text

    bridge = SimpleNamespace(run=lambda coro, **kw: coro.close())
    agent = SimpleNamespace(_resources=plenty_of_resources(None), available_versions=lambda loader: _noop())
    dlg = ServerConfigDialog(bridge, agent, {"name": "Survie", "loader": "paper", "mc_version": "26.3",
                                             "host_choice": "best"})
    assert dlg.values()["host_choice"] == "best"
    dlg.host_choice.setCurrentIndex(0)
    assert dlg.values()["host_choice"] == "priority"
    assert measures_text({"online": True, "rtt_ms": 12.4, "upload_kbps": 45_200, "cpu_score": 1.24,
                          "score": 78.4}) == "12 ms · 45 Mbit/s · ×1,2 · 78/100"
    assert measures_text({"online": True, "upload_kbps": 2500}) == "2,5 Mbit/s"
    assert measures_text({"online": False}) == "hors ligne"


def test_seed_at_creation_and_new_world_dialog(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.dialogs import NewWorldDialog, ServerConfigDialog, preset_of

    bridge = SimpleNamespace(run=lambda coro, **kw: coro.close())
    agent = SimpleNamespace(_resources=plenty_of_resources(None), available_versions=lambda loader: _noop())
    dlg = ServerConfigDialog(bridge, agent, None)
    dlg.version.setEditText("26.3")
    dlg.world.seed.setText("  -4172144997902289642 ")
    dlg.world.level_type.setCurrentIndex(dlg.world.level_type.findData("amplified"))
    props = dlg.values()["properties"]
    assert props["level-seed"] == "-4172144997902289642" and props["level-type"] == "minecraft:amplified"
    assert props["generate-structures"] is True

    # Editing: the seed is shown, not editable, and kept as it is (with settings the dialog hides).
    current = {"name": "Survie", "loader": "paper", "mc_version": "1.18.2",
               "properties": {"level-seed": "abc", "level-type": "largeBiomes", "spawn-protection": 0}}
    edit = ServerConfigDialog(bridge, agent, current)
    assert edit.world is None
    kept = edit.values()["properties"]
    assert kept["level-seed"] == "abc" and kept["spawn-protection"] == 0 and kept["level-type"] == "largeBiomes"

    assert [preset_of(v) for v in ("minecraft:large_biomes", "largeBiomes", "default", None)] == \
        ["large_biomes", "large_biomes", "normal", "normal"]
    nw = NewWorldDialog({"name": "Survie", "properties": {"level-seed": "abc", "level-type": "minecraft:flat",
                                                          "generate-structures": False}})
    assert nw.values() == {"seed": "", "level_type": "flat", "generate_structures": False}
    nw.world.seed.setText("Les copains")
    assert nw.values()["seed"] == "Les copains"


def test_deleting_a_server_needs_its_exact_name(qapp, monkeypatch):
    from types import SimpleNamespace

    from mdh_client.gui import main_window

    calls = []
    srv = {"server_id": "a" * 32, "name": "Survie", "state": "offline", "desired_running": False}
    fake = SimpleNamespace(state=SimpleNamespace(server=dict(srv)), run=lambda coro, ok, msg: calls.append(coro),
                           reload_servers=lambda: None,
                           agent=SimpleNamespace(api=SimpleNamespace(delete_server=lambda sid, name: (sid, name))))
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(main_window.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(main_window.QInputDialog, "getText", lambda *a, **k: ("Survi", True))
    main_window.MainWindow.delete_server(fake)
    assert calls == []
    monkeypatch.setattr(main_window.QInputDialog, "getText", lambda *a, **k: (" Survie ", True))
    main_window.MainWindow.delete_server(fake)
    assert calls == [("a" * 32, "Survie")]
    fake.state.server["desired_running"] = True  # running: not even asked
    main_window.MainWindow.delete_server(fake)
    assert len(calls) == 1


def test_the_check_for_updates_button(qapp):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QPushButton

    from mdh_client.gui import main_window
    from mdh_client.system.updates import ReadyUpdate

    answers = []
    page = SimpleNamespace(check_btn=QPushButton("Vérifier les mises à jour"), check_result="")
    header = QPushButton()
    messages = []

    async def check():
        result = answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    fake = SimpleNamespace(pages=[None] * 5 + [page], state=SimpleNamespace(update=None), update_btn=header,
                           bridge=SyncBridge(), agent=SimpleNamespace(check_for_updates=check),
                           statusBar=lambda: SimpleNamespace(showMessage=lambda m, t=0: messages.append(m)),
                           render=lambda: None)
    answers.append(None)
    main_window.MainWindow.check_updates(fake)
    assert "dernière version" in page.check_result and page.check_btn.isEnabled() and fake.state.update is None

    answers.append(ReadyUpdate("0.12.0", "Bouton de mise à jour.", None, "0" * 64, 1))
    main_window.MainWindow.check_updates(fake)
    assert fake.state.update == {"version": "0.12.0", "notes": "Bouton de mise à jour."}
    assert header.text() == "Mettre à jour (0.12.0)" and "0.12.0" in messages[-1]

    answers.append(RuntimeError("serveur central injoignable"))
    main_window.MainWindow.check_updates(fake)
    assert page.check_result.startswith("Vérification impossible") and page.check_btn.text() == "Vérifier les mises à jour"


def test_members_can_leave_be_removed_or_be_handed_the_group(qapp):
    from types import SimpleNamespace

    from mdh_client.gui.pages import AppState, GroupPage

    calls = []
    win = SimpleNamespace(create_invite=lambda: None, join_group=lambda: None, create_group=lambda: None,
                          set_role=lambda *a: None, set_can_host=lambda *a: None, set_priority=lambda *a: None,
                          remove_member=lambda uid, name: calls.append(("remove", name)),
                          transfer_ownership=lambda uid, name: calls.append(("owner", name)))
    page = GroupPage(win)
    people = [{"user_id": "a" * 32, "username": "oscar", "role": "owner", "can_host": True, "hosts": []},
              {"user_id": "b" * 32, "username": "lea", "role": "admin", "can_host": True, "hosts": []},
              {"user_id": "c" * 32, "username": "tom", "role": "member", "can_host": True, "hosts": []},
              {"user_id": "d" * 32, "username": "eve", "role": "member", "can_host": False, "hosts": [],
               "leaving": True}]

    def actions(me: str, role: str) -> dict[str, list[str]]:
        state = AppState(user={"user_id": me}, group={"group_id": "g" * 32, "name": "Amis", "role": role},
                         members=people)
        page.refresh(state)
        found = {}
        for r, m in enumerate(people):
            cell = page.members.cellWidget(r, 3)
            found[m["username"]] = ([b.text() for b in cell.findChildren(QPushButton)] if cell else
                                    [page.members.item(r, 3).text()] if page.members.item(r, 3).text() else [])
        return found

    # The owner hands the group over or removes anyone; the one leaving shows as such.
    assert actions("a" * 32, "owner") == {"oscar": [], "lea": ["Rendre propriétaire", "Retirer"],
                                          "tom": ["Rendre propriétaire", "Retirer"], "eve": ["Départ en cours…"]}
    # An admin removes members (not another admin), and leaves.
    assert actions("b" * 32, "admin") == {"oscar": [], "lea": ["Quitter le groupe"], "tom": ["Retirer"],
                                          "eve": ["Départ en cours…"]}
    # A member can only leave.
    assert actions("c" * 32, "member") == {"oscar": [], "lea": [], "tom": ["Quitter le groupe"],
                                           "eve": ["Départ en cours…"]}
    page.members.cellWidget(2, 3).findChildren(QPushButton)[0].click()
    assert calls == [("remove", "tom")]
