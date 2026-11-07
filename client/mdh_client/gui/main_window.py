"""Main window: header (group/server/connection), sidebar, pages, tray icon."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mdh_client.application.agent import HostAgent
from mdh_client.gui import dialogs, group_dialogs
from mdh_client.gui.bridge import Bridge
from mdh_client.gui.pages import (AppState, GroupPage, HomePage, ServerPage, SettingsPage, StatsPage, StoragePage,
                                  mods_key)
from mdh_client.system import autostart
from mdh_client.system.updates import Updater, UpdateError
from mdh_shared import __version__
from mdh_client.minecraft.modrinth import ModrinthError, without
from mdh_shared.models.api import (MAP_MOD_SLUG, MOD_LOADERS, PREGEN_MOD_SLUG, SPARK_MOD_SLUG, Datapack, Mod,
                                   anticheat_enabled, anticheat_slugs)
from mdh_shared.models.enums import Loader
from mdh_client.gui.texts import MIGRATION_DETAILS, PHASE_LABELS, alert_text, error_text
from mdh_client.gui.theme import COLORS
from mdh_client.gui.widgets import Pill, human_bytes

log = logging.getLogger("mdh.gui")

#: The control plane silent for this long: tell the user (docs/ARCHITECTURE.md 5).
OFFLINE_WARNING_S = 180
#: How often an admin's PC asks Modrinth for newer files of the server's mods.
MOD_UPDATE_CHECK_S = 6 * 3600

SECTIONS = ("Accueil", "Serveur", "Groupe", "Stockage", "Statistiques", "Paramètres")


def app_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#3fb27f"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setBrush(QColor("#7a5230"))
    p.drawRect(4, 36, 56, 24)
    p.setBrush(QColor("#5b9cf6"))
    p.drawEllipse(40, 10, 14, 14)
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge, agent: HostAgent) -> None:
        super().__init__()
        self.bridge, self.agent = bridge, agent
        self.state = AppState(user=agent.user)
        # Subscribe first, read the agent's state second: an event emitted in between is queued
        # (cross-thread) and delivered once the event loop runs, so nothing can be missed.
        bridge.agent_event.connect(self._on_agent_event)
        bridge.offer_question.connect(self._on_offer_question)
        self._quitting = False
        self._tray_hint_shown = False
        self.setWindowTitle("Minecraft Distributed Hosting")
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)
        self._setup_tray()  # before anything can call render(), which updates the tray tooltip

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 16, 10)
        title = QLabel("⛏  Minecraft Distributed Hosting")
        title.setStyleSheet("font-weight: 600; font-size: 11pt; background: transparent;")
        hl.addWidget(title)
        hl.addStretch()
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(180)
        self.group_combo.currentIndexChanged.connect(self._group_selected)
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(180)
        self.server_combo.currentIndexChanged.connect(self._server_selected)
        # The agent may have connected during login, before this window existed: start from its
        # current state rather than waiting for the next "connection" event.
        online = bool(agent.channel and agent.channel.connected)
        self.state.online = online
        self.connection = Pill("Connecté" if online else "Hors ligne", "ok" if online else "warn")
        hl.addWidget(QLabel("Groupe"))
        hl.addWidget(self.group_combo)
        hl.addWidget(QLabel("Serveur"))
        hl.addWidget(self.server_combo)
        self.new_server_btn = QPushButton("+")
        self.new_server_btn.setToolTip("Nouveau serveur dans ce groupe (un autre monde, une autre version…). "
                                       "On passe de l'un à l'autre avec la liste « Serveur ».")
        self.new_server_btn.setFixedWidth(32)
        self.new_server_btn.setVisible(False)
        self.new_server_btn.clicked.connect(self.create_server)
        hl.addWidget(self.new_server_btn)
        hl.addWidget(self.connection)
        self.update_btn = QPushButton()
        self.update_btn.setObjectName("Primary")
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(lambda: self.install_update())
        hl.addWidget(self.update_btn)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(190)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for name, glyph in zip(SECTIONS, ("⌂", "▶", "👥", "💾", "📊", "⚙"), strict=True):
            self.sidebar.addItem(f"{glyph}   {name}")
        self.stack = QStackedWidget()
        self.pages = [HomePage(self), ServerPage(self), GroupPage(self), StoragePage(self), StatsPage(self),
                      SettingsPage(self)]
        for page in self.pages:
            self.stack.addWidget(page)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.currentRowChanged.connect(lambda _: self.render())
        self.sidebar.setCurrentRow(0)
        body.addWidget(self.sidebar)
        body.addWidget(self.stack, 1)
        body_w = QWidget()
        body_w.setLayout(body)
        root.addWidget(body_w, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Prêt")

        self._mod_checks: dict[str, tuple[tuple, float]] = {}
        self.status_timer = QTimer(self, interval=3000, timeout=self.refresh_server)
        self.details_timer = QTimer(self, interval=10000, timeout=self.refresh_details)
        # Installs a ready update by itself once the window is closed and nothing is hosted.
        self.update_timer = QTimer(self, interval=60_000, timeout=self._maybe_install_update)
        self.status_timer.start()
        self.details_timer.start()
        self.update_timer.start()
        self._offline_since: float | None = None if online else time.monotonic()
        self._offline_warned = False
        self._alerts_seen: set[tuple[str, str | None]] = set()
        self.connection_timer = QTimer(self, interval=30_000, timeout=self._watch_connection)
        self.connection_timer.start()
        self.reload_groups()
        if agent.too_old_for is not None:  # found at login, before this window existed
            self._on_agent_event("incompatible", {"current": __version__, "minimum": agent.too_old_for})
        if (ready := agent.updater.ready) is not None:  # found while the login dialog was open
            self._on_agent_event("update", {"version": ready.version, "notes": ready.notes})

    # ------------------------------------------------------------------ tray / close

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Minecraft Distributed Hosting")
        menu = QMenu()
        show = QAction("Ouvrir", self)
        show.triggered.connect(self.show_normal)
        quit_ = QAction("Quitter", self)
        quit_.triggered.connect(self.quit_app)
        menu.addAction(show)
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def show_normal(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
            return
        # Closing the window keeps the agent running (it may be hosting): hide to the tray.
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage("Toujours actif", "L'application continue en arrière-plan. "
                                  "Clic droit sur l'icône pour quitter.")

    def quit_app(self) -> None:
        if self.agent.hosted is not None:
            answer = QMessageBox.question(
                self, "Quitter ?", "Ce PC héberge le serveur. Il va prévenir les joueurs, s'arrêter "
                "proprement et laisser la place à un autre PC. Continuer ?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.statusBar().showMessage("Arrêt propre en cours…")
        self._quitting = True

        def done(_: Any = None) -> None:
            self.tray.hide()
            QApplication.quit()

        self.bridge.run(self.agent.close(), on_ok=done, on_error=lambda e: done())

    # ------------------------------------------------------------------ control plane down?

    def _watch_connection(self, online: bool | None = None) -> None:
        """Tell the user when the control plane stays unreachable (the VPS or its service down,
        or this PC offline): the control plane cannot say it itself."""
        now = time.monotonic()
        if online is True:
            if self._offline_warned:
                self.tray.showMessage("Serveur central", "Le serveur central répond de nouveau.")
            self._offline_since, self._offline_warned = None, False
        elif online is False and self._offline_since is None:
            self._offline_since = now
        elif (online is None and self._offline_since is not None and not self._offline_warned
              and now - self._offline_since >= OFFLINE_WARNING_S and not self._quitting):
            self._offline_warned = True
            self.tray.showMessage(
                "Serveur central injoignable", "Il ne répond plus depuis 3 minutes. Si Internet fonctionne "
                "sur ce PC, le VPS ou son service est peut-être arrêté (sur le VPS : sudo bash install.sh "
                "status). Le monde ne risque rien : un PC qui héberge arrête Minecraft proprement.",
                QSystemTrayIcon.MessageIcon.Warning)

    # ------------------------------------------------------------------ updates

    def _maybe_install_update(self) -> None:
        if (self.state.update and self.agent.settings.auto_update and not self._quitting
                and self.agent.user is not None and not self.agent.busy
                # Too old for the control plane: nothing works until updated, no need to wait.
                and (not self.isVisible() or self.isMinimized() or self.state.incompatible)
                and QApplication.activeModalWidget() is None):
            log.info("installing version %s automatically", self.state.update["version"])
            self.install_update(automatic=True)

    def check_updates(self) -> None:
        """The "Vérifier les mises à jour" button (Paramètres > Application)."""
        page = self.pages[5]  # SettingsPage
        page.check_btn.setEnabled(False)
        page.check_btn.setText("Recherche…")
        page.check_result = ""

        def done(ready: Any) -> None:
            page.check_btn.setText("Vérifier les mises à jour")
            page.check_btn.setEnabled(True)
            if ready is not None:
                # Maybe already known (then no new "update" event): shown all the same.
                self.state.update = {"version": ready.version, "notes": ready.notes}
                self.update_btn.setText(f"Mettre à jour ({ready.version})")
                self.update_btn.setVisible(True)
                self.statusBar().showMessage(f"Version {ready.version} prête à installer.", 8000)
            else:
                self.state.update = None
                self.update_btn.setVisible(False)
                page.check_result = f"C'est la dernière version (vérifié à {datetime.now():%H:%M})."
                self.statusBar().showMessage("Aucune mise à jour : c'est la dernière version.", 8000)
            self.render()

        def failed(exc: BaseException) -> None:
            page.check_btn.setText("Vérifier les mises à jour")
            page.check_btn.setEnabled(True)
            page.check_result = f"Vérification impossible : {error_text(exc)}"
            self.render()

        self.bridge.run(self.agent.check_for_updates(), on_ok=done, on_error=failed)

    def install_update(self, automatic: bool = False) -> None:
        """Quit cleanly, then let the verified installer replace the program and restart it."""
        if self._quitting or not self.state.update:
            return
        if not automatic and self.agent.busy:
            answer = QMessageBox.question(
                self, "Mettre à jour maintenant ?", "Ce PC héberge le serveur. Il va prévenir les "
                "joueurs, s'arrêter proprement et laisser la place à un autre PC, puis l'application se "
                "mettra à jour et se rouvrira. Continuer ?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        relaunch = "show" if self.isVisible() and not self.isMinimized() else "tray"
        self._quitting = True
        self.statusBar().showMessage("Mise à jour : arrêt propre en cours…")

        def prepared(ready: Any) -> None:
            def closed(_: Any = None) -> None:
                try:
                    Updater.launch(ready, relaunch=relaunch, log_file=self.agent.layout.logs / "update-install.log")
                except (OSError, UpdateError) as exc:
                    log.error("installer did not start: %s", exc)
                    QMessageBox.warning(self, "Mise à jour", "L'installateur n'a pas pu démarrer "
                                        f"({exc}). Rouvre l'application pour continuer.")
                self.tray.hide()
                QApplication.quit()

            self.bridge.run(self.agent.close(), on_ok=closed, on_error=lambda e: closed())

        def failed(exc: BaseException) -> None:
            self._quitting = False
            self.state.update = None
            self.update_btn.setVisible(False)
            self.statusBar().showMessage(f"Mise à jour annulée : {exc}", 15000)
            self.render()

        self.bridge.run(self.agent.prepare_update(), on_ok=prepared, on_error=failed)

    # ------------------------------------------------------------------ data loading

    def run(self, coro, on_ok=None, success: str | None = None) -> None:
        def ok(result: Any) -> None:
            if success:
                self.statusBar().showMessage(success, 6000)
            if on_ok:
                on_ok(result)

        self.bridge.run(coro, on_ok=ok, on_error=self.show_error)

    def show_error(self, exc: BaseException) -> None:
        text = error_text(exc)
        self.statusBar().showMessage(text, 10000)
        QMessageBox.warning(self, "Oups", text)

    def reload_groups(self, select: str | None = None) -> None:
        def loaded(me: dict) -> None:
            self.state.user = me
            current = select or (self.state.group or {}).get("group_id")
            self.group_combo.blockSignals(True)
            self.group_combo.clear()
            for g in me["groups"]:
                self.group_combo.addItem(g["name"], g)
            index = next((i for i, g in enumerate(me["groups"]) if g["group_id"] == current), 0)
            self.group_combo.blockSignals(False)
            if me["groups"]:
                self.group_combo.setCurrentIndex(index)
                self._group_selected(index)
            else:
                self.state.group = None
                self.state.server = None
                self.server_combo.clear()
                self.render()

        self.run(self.agent.api.me(), loaded)

    def _group_selected(self, index: int) -> None:
        if index < 0:
            return
        self.state.group = self.group_combo.itemData(index)
        self.reload_servers()

    def reload_servers(self, select: str | None = None) -> None:
        group = self.state.group
        if not group:
            return

        def loaded(servers: list[dict]) -> None:
            current = select or (self.state.server or {}).get("server_id")
            self.server_combo.blockSignals(True)
            self.server_combo.clear()
            for s in servers:
                self.server_combo.addItem(s["name"], s["server_id"])
            self.server_combo.blockSignals(False)
            if servers:
                index = next((i for i, s in enumerate(servers) if s["server_id"] == current), 0)
                self.server_combo.setCurrentIndex(index)
                self.state.server = servers[index]
            else:
                self.state.server = None
            self.refresh_details()
            self.render()

        self.run(self.agent.api.servers(group["group_id"]), loaded)

    def _server_selected(self, index: int) -> None:
        if index >= 0:
            self.refresh_server()
            self.refresh_details()

    def current_server_id(self) -> str | None:
        return self.server_combo.currentData()

    def refresh_server(self) -> None:
        sid = self.current_server_id()
        if not sid:
            return

        def loaded(status: dict) -> None:
            self._notice_requests(status)
            self.state.server = status
            self.render()

        self.bridge.run(self.agent.api.server(sid), on_ok=loaded, on_error=lambda e: None)
        # Players and chat, seen from any PC: only while the Server page is open.
        if self.stack.currentIndex() == 1 and self.state.server and self.state.server["state"] == "running":
            def got_activity(activity: dict) -> None:
                self.state.activity = activity
                self.pages[1].refresh(self.state)

            self.bridge.run(self.agent.api.activity(sid), on_ok=got_activity, on_error=lambda e: None)
            if self.pages[1].wants_log():
                self._fetch_console(sid)

    def _fetch_console(self, sid: str) -> None:
        """Newer console lines of the server hosted on another PC (read-only)."""
        s = self.state
        after = s.console_next if s.console_key and s.console_key[0] == sid else 0

        def got_console(log: dict) -> None:
            key = (sid, log["epoch"])
            if key != s.console_key or log["next"] < after:  # another host, or the VPS restarted
                s.console_log = []
            s.console_key, s.console_next = key, log["next"]
            s.console_log = (s.console_log + log["lines"])[-2000:]
            self.pages[1].refresh(s)

        self.bridge.run(self.agent.api.console(sid, after), on_ok=got_console, on_error=lambda e: None)

    def refresh_details(self) -> None:
        group = self.state.group
        if not group:
            return
        sid = self.current_server_id()

        async def fetch() -> tuple:
            members = await self.agent.api.members(group["group_id"])
            hosts = await self.agent.api.server_hosts(sid) if sid else []
            events = await self.agent.api.events(sid, limit=40) if sid else []
            versions = await self.agent.api.versions(sid, limit=100) if sid else []
            storage = await self.agent.api.storage(group["group_id"])
            return members, hosts, events, versions, storage

        def loaded(result: tuple) -> None:
            (self.state.members, self.state.hosts, self.state.events, self.state.versions,
             self.state.storage) = result
            self.render()
            self._check_mod_updates()

        self.bridge.run(fetch(), on_ok=loaded, on_error=lambda e: None)

    def _check_mod_updates(self) -> None:
        """Admins see when Modrinth has newer files for the server's mods (asked every 6 hours)."""
        srv = self.state.server
        if not srv or not self.state.is_admin or not srv.get("mods"):
            return
        key = mods_key(srv)
        checked = self._mod_checks.get(srv["server_id"])
        if checked and checked[0] == key and time.monotonic() - checked[1] < MOD_UPDATE_CHECK_S:
            return
        self._mod_checks[srv["server_id"]] = (key, time.monotonic())

        def found(result: tuple) -> None:
            mods, changes = result
            self.state.mod_updates[srv["server_id"]] = {"key": key, "mods": mods, "changes": changes}
            self.render()

        self.bridge.run(self.agent.mod_updates(srv), on_ok=found, on_error=lambda e: None)

    def _sync_connection(self) -> None:
        online = bool(self.agent.channel and self.agent.channel.connected)
        if online != self.state.online or self.connection.text() != ("Connecté" if online else "Hors ligne"):
            self.state.online = online
            self.connection.set("Connecté" if online else "Hors ligne", "ok" if online else "warn")

    def render(self) -> None:
        self._sync_connection()
        self.new_server_btn.setVisible(bool(self.state.group and self.state.is_admin))
        page = self.pages[self.stack.currentIndex()]
        page.refresh(self.state)
        if page is not self.pages[0]:
            self.pages[0].refresh(self.state)  # keeps the tray tooltip source in sync
        phase = self.state.hosting_phase if self.state.hosting_server else None
        self.tray.setToolTip("Minecraft Distributed Hosting\n" + PHASE_LABELS.get(phase, "En veille"))

    # ------------------------------------------------------------------ agent events

    def _on_agent_event(self, name: str, payload: dict) -> None:
        if name == "connection":
            self._watch_connection(payload["online"])
            self.state.online = payload["online"]
            self.connection.set("Connecté" if payload["online"] else "Hors ligne",
                                "ok" if payload["online"] else "warn")
        elif name == "log":
            self.pages[1].append_log(payload["line"])
            return
        elif name == "progress":
            self.pages[1].show_progress(payload["message"], payload.get("fraction"))
            return
        elif name == "hosting":
            self.state.hosting_server = payload["server_id"] if payload["phase"] != "released" else None
            self.state.hosting_phase = payload["phase"] if payload["phase"] != "released" else None
            self.statusBar().showMessage(PHASE_LABELS.get(payload["phase"], payload["phase"]), 8000)
            if payload["phase"] == "running":
                self.tray.showMessage("Serveur en ligne", f"Ce PC héberge « {payload['server_name']} ».")
        elif name == "stats":
            self.state.stats = payload
            self.state.players = payload.get("players", [])
        elif name == "players":
            self.state.players = payload["players"]
        elif name == "group_update":
            # A role, a member or a server changed: read the groups again (rights apply at once).
            self.reload_groups()
            return
        elif name == "server_update" and payload.get("state") == "deleted":
            if self.state.server and self.state.server["server_id"] == payload["server_id"]:
                self.state.server = None
            self.reload_servers()
            return
        elif name in ("server_update", "sync"):
            self.refresh_server()
            self.refresh_details()
            return
        elif name == "migration":
            self._on_migration(payload)
            return
        elif name == "fenced":
            self.tray.showMessage("Arrêt de sécurité", "Connexion au serveur central perdue : Minecraft a été "
                                  "arrêté pour protéger le monde.", QSystemTrayIcon.MessageIcon.Warning)
        elif name == "alerts":
            new = [a for a in payload["alerts"] if (a["code"], a.get("group_id")) not in self._alerts_seen]
            self._alerts_seen = {(a["code"], a.get("group_id")) for a in payload["alerts"]}
            self.state.alerts = payload["alerts"]
            for alert in new:
                self.tray.showMessage("Serveur central", alert_text(alert), QSystemTrayIcon.MessageIcon.Warning)
        elif name == "incompatible":
            self.state.incompatible = payload
            self.tray.showMessage("Mise à jour obligatoire", "Cette application est trop ancienne pour le "
                                  "serveur central : elle se met à jour dès que possible.",
                                  QSystemTrayIcon.MessageIcon.Warning)
        elif name == "standby":
            self.state.standby[payload["server_id"]] = payload["at"]
        elif name == "update":
            self.state.update = payload
            self.update_btn.setText(f"Mettre à jour ({payload['version']})")
            self.update_btn.setVisible(True)
            self.tray.showMessage(
                "Mise à jour disponible", f"La version {payload['version']} est prête. " + (
                    "Elle s'installera toute seule quand la fenêtre sera fermée et que ce PC "
                    "n'hébergera pas le serveur." if self.agent.settings.auto_update else
                    "Installe-la depuis Paramètres > Application."))
        elif name == "mods_approval":
            self.tray.showMessage("Mods à autoriser", f"« {payload['server_name']} » utilise des mods pas encore "
                                  "autorisés sur ce PC : il ne l'hébergera pas avant (page Serveur).",
                                  QSystemTrayIcon.MessageIcon.Warning)
        elif name == "eula_required":
            QTimer.singleShot(0, self._eula_needed)
        elif name == "error":
            self.statusBar().showMessage(payload["message"], 15000)
            self.tray.showMessage("Minecraft Distributed Hosting", payload["message"],
                                  QSystemTrayIcon.MessageIcon.Warning)
        elif name == "session" and payload.get("user") is None:
            return
        self.render()

    def _on_migration(self, p: dict) -> None:
        phase = p["phase"]
        self.state.migration_phase = phase if phase in ("prefetching", "countdown") else None
        name = f"« {p['server_name']} »" if p.get("server_name") else "le serveur"
        message = {
            ("target", "prefetching"): f"Ce PC se prépare à héberger {name} : téléchargement du monde à l'avance.",
            ("target", "ready"): "Monde téléchargé : ce PC prendra le relais dès que l'hôte actuel aura terminé.",
            ("target", "refused"): f"Ce PC ne peut pas prendre le relais : {p.get('reason') or 'raison inconnue'}.",
            ("source", "countdown"): (f"{name.capitalize()} passe sur un autre PC dans {p.get('countdown_s', 0)} s "
                                      "(les joueurs sont prévenus)."),
        }.get((p["role"], phase))
        if phase == "cancelled":
            why = MIGRATION_DETAILS.get(p.get("reason") or "", "le serveur reste où il est")
            message = f"Changement de PC annulé : {why}."
        if message:
            self.statusBar().showMessage(message, 15000)
            self.tray.showMessage("Changement de PC", message)
        self.refresh_server()
        self.refresh_details()
        self.render()

    def _on_offer_question(self, info: dict, future: concurrent.futures.Future) -> None:
        self.show_normal()
        if not future.done():
            future.set_result(dialogs.ask_hosting_offer(self, info))

    def _eula_needed(self) -> None:
        self.show_normal()
        QMessageBox.information(self, "EULA Minecraft", "Ce PC a été choisi pour héberger, mais l'EULA "
                                "Minecraft n'est pas acceptée ici. Le serveur a été confié à un autre PC.\n\n"
                                "Tu peux l'accepter dans Paramètres pour pouvoir héberger.")
        self.sidebar.setCurrentRow(5)

    # ------------------------------------------------------------------ user actions

    def start_server(self) -> None:
        sid = self.current_server_id()
        if sid:
            def started(status: dict) -> None:
                self._set_server(status)
                self.refresh_details()  # fetch the election outcome (e.g. why no PC qualifies)

            self.run(self.agent.api.start(sid), started, "Démarrage demandé")

    def stop_server(self) -> None:
        sid = self.current_server_id()
        if not sid:
            return
        answer = QMessageBox.question(self, "Arrêter le serveur ?",
                                      "Les joueurs sont prévenus 30 s avant l'arrêt. Continuer ?")
        if answer == QMessageBox.StandardButton.Yes:
            self.run(self.agent.api.stop(sid, 30), lambda s: self._set_server(s), "Arrêt demandé")

    def transfer_server(self) -> None:
        sid = self.current_server_id()
        if not sid:
            return
        choices = self.state.transfer_choices(self.agent.settings.host_id)
        if not choices:
            QMessageBox.information(self, "Changer de PC", "Aucun autre PC du groupe n'est connecté et disponible "
                                    "pour héberger en ce moment.")
            return
        dialog = dialogs.TransferDialog(choices, self)
        if dialog.exec():
            target, countdown = dialog.values()
            self.run(self.agent.api.transfer(sid, target, countdown), self._set_server, "Changement de PC demandé")

    def show_player_addresses(self) -> None:
        sid = self.current_server_id()
        if sid:
            dialogs.PlayerAddressesDialog(self.bridge, self.agent, sid, self).exec()

    def cancel_transfer(self) -> None:
        sid = self.current_server_id()
        if sid:
            self.run(self.agent.api.cancel_transfer(sid), self._set_server, "Changement de PC annulé")

    def restart_server(self) -> None:
        self.run(self.agent.restart_hosted(), success="Redémarrage en cours")

    def _set_server(self, status: dict) -> None:
        self.state.server = status
        self.render()

    def _saved_needs_restart(self, status: dict) -> None:
        """A change that applies when Minecraft starts: offer to restart now if it runs."""
        self._set_server(status)
        if status["state"] != "running":
            return
        answer = QMessageBox.question(
            self, "Appliquer maintenant ?",
            "Le serveur tourne : ce changement s'appliquera à son prochain démarrage.\n\n"
            "Redémarrer maintenant ? Les joueurs sont prévenus 5 secondes avant, et peuvent se "
            "reconnecter dès que le serveur est relancé (moins d'une minute en général).")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.state.hosting_server == status["server_id"]:
            self.run(self.agent.restart_hosted(), success="Redémarrage en cours")
        else:
            self.run(self.agent.api.send_action(status["server_id"], {"kind": "restart"}),
                     success="Redémarrage demandé au PC hôte")

    def confirm_failover(self) -> None:
        answer = QMessageBox.question(
            self, "Relancer ailleurs ?",
            "Le serveur va redémarrer sur un autre PC à partir de la dernière sauvegarde. Ce qui a été joué "
            "depuis sur le PC disparu sera mis de côté (jamais supprimé). Continuer ?")
        sid = self.current_server_id()
        if answer == QMessageBox.StandardButton.Yes and sid:
            self.run(self.agent.api.confirm_failover(sid), self._set_server, "Relance confirmée")

    def backup_now(self) -> None:
        self.statusBar().showMessage("Sauvegarde en cours…")
        self.run(self.agent.backup_now(), lambda _: self.refresh_details(), "Sauvegarde envoyée au serveur central")

    def restore_version(self, version: dict) -> None:
        sid = self.current_server_id()
        when = datetime.fromisoformat(version["created_at"]).astimezone().strftime("%d/%m/%Y à %H:%M")
        answer = QMessageBox.question(
            self, "Restaurer cette sauvegarde ?",
            f"Le monde du {when} deviendra le monde actuel au prochain démarrage.\n\n"
            "Rien n'est effacé : la version actuelle reste dans l'historique et peut être restaurée à "
            "son tour. Continuer ?")
        if answer == QMessageBox.StandardButton.Yes and sid:
            self.run(self.agent.api.restore(sid, version["version_id"]), lambda _: self.refresh_details(),
                     "Sauvegarde restaurée : elle sera utilisée au prochain démarrage")

    def delete_server(self) -> None:
        srv = self.state.server
        if not srv:
            return
        if srv["state"] != "offline" or srv["desired_running"]:
            QMessageBox.information(self, "Supprimer le serveur", "Arrête d'abord le serveur.")
            return
        name, ok = QInputDialog.getText(
            self, "Supprimer le serveur",
            f"« {srv['name']} » sera supprimé définitivement, avec toutes ses sauvegardes : impossible "
            "de revenir en arrière.\nPour garder le monde, télécharge-le d'abord (page Stockage, "
            "« Télécharger (.zip)… »).\n\nPour confirmer, tape le nom du serveur :")
        if not ok:
            return
        if name.strip() != srv["name"]:
            QMessageBox.warning(self, "Supprimer le serveur", "Le nom ne correspond pas : rien n'a été supprimé.")
            return

        def done(_: dict) -> None:
            self.state.server = None
            self.reload_servers()

        self.run(self.agent.api.delete_server(srv["server_id"], srv["name"]), done,
                 f"Serveur « {srv['name']} » supprimé")

    def new_world(self) -> None:
        srv = self.state.server
        if not srv:
            return
        if srv["state"] != "offline" or srv["desired_running"]:
            QMessageBox.information(self, "Nouveau monde", "Arrête d'abord le serveur : le nouveau monde est "
                                    "créé à son prochain démarrage.")
            return
        dlg = dialogs.NewWorldDialog(srv, self)
        if not dlg.exec():
            return
        values = dlg.values()

        def done(status: dict) -> None:
            self._set_server(status)
            self.refresh_details()
            seed = f"la graine « {values['seed']} »" if values["seed"] else "une graine au hasard"
            QMessageBox.information(self, "Nouveau monde prêt",
                                    f"Le nouveau monde sera créé avec {seed} au prochain démarrage du serveur.\n\n"
                                    "L'ancien est dans la page Stockage, marqué important, si vous voulez y revenir.")

        self.run(self.agent.api.new_world(srv["server_id"], **values), done)

    def import_world(self) -> None:
        srv = self.state.server
        if not srv:
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Importer un monde",
                          "Que veux-tu importer ?\n\n• un dossier : un monde solo (dans .minecraft\\saves) ou "
                          "le dossier d'un ancien serveur ;\n• un fichier .zip de l'un ou de l'autre.", parent=self)
        folder = box.addButton("Un dossier…", QMessageBox.ButtonRole.AcceptRole)
        archive = box.addButton("Un fichier .zip…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is folder:
            path = QFileDialog.getExistingDirectory(self, "Dossier du monde ou du serveur")
        elif box.clickedButton() is archive:
            path = QFileDialog.getOpenFileName(self, "Monde en .zip", "", "Archives (*.zip)")[0]
        else:
            return
        if not path:
            return
        if QMessageBox.question(
                self, "Remplacer le monde ?", f"Le monde de « {srv['name']} » va être remplacé par celui-ci :\n"
                f"{path}\n\nL'ancien monde reste dans l'historique : tu pourras le restaurer.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.statusBar().showMessage("Import du monde en cours…")

        def done(result: tuple) -> None:
            _, summary = result
            self.statusBar().showMessage("Monde importé", 8000)
            QMessageBox.information(self, "Monde importé", f"« {summary.world_name} » est maintenant le monde "
                                    f"du serveur ({summary.files} fichiers, {human_bytes(summary.size)}). "
                                    "Il sera utilisé au prochain démarrage.")
            self.refresh_details()

        self.run(self.agent.import_world(srv["server_id"], Path(path)), done)

    def export_version(self, version: dict) -> None:
        srv = self.state.server
        if not srv:
            return
        when = datetime.fromisoformat(version["created_at"]).astimezone().strftime("%Y-%m-%d_%Hh%M")
        default = str(Path.home() / "Downloads" / f"{srv['name']}-{when}.zip")
        path = QFileDialog.getSaveFileName(self, "Enregistrer le monde", default, "Archives (*.zip)")[0]
        if not path:
            return
        self.statusBar().showMessage("Préparation du fichier .zip…")
        self.run(self.agent.export_world(srv["server_id"], version["version_id"], Path(path)),
                 lambda n: self.statusBar().showMessage(f"Monde enregistré : {path} ({n} fichiers)", 15000))

    def pin_version(self, version: dict, pinned: bool) -> None:
        sid = self.current_server_id()
        label = None
        if pinned:
            label, ok = QInputDialog.getText(self, "Sauvegarde importante",
                                             "Nom (facultatif), par ex. « avant le dragon » :")
            if not ok:
                return
        if sid:
            self.run(self.agent.api.pin_version(sid, version["version_id"], pinned, (label or "").strip()[:100] or None),
                     lambda _: self.refresh_details())

    def admin_action(self, kind: str, player: str | None = None, message: str | None = None) -> None:
        """An action of the closed list, carried out by whichever PC hosts (docs/API.md 4 bis)."""
        srv = self.state.server
        if not srv:
            return
        labels = {"kick": "expulsé", "ban": "banni", "pardon": "débanni", "op": "nommé opérateur",
                  "deop": "n'est plus opérateur"}
        if kind in ("kick", "ban") and QMessageBox.question(
                self, "Confirmer", f"{'Expulser' if kind == 'kick' else 'Bannir'} {player} ?"
                + ("\nIl ne pourra plus revenir tant qu'il n'est pas débanni." if kind == "ban" else "")) \
                != QMessageBox.StandardButton.Yes:
            return
        action: dict[str, Any] = {"kind": kind}
        if player:
            action["player"] = player
        if message:
            action["message"] = message
        done = f"{player} : {labels[kind]}" if player else "Annonce envoyée"
        self.run(self.agent.api.send_action(srv["server_id"], action), lambda _: self.refresh_server(), done)

    def send_console(self, command: str) -> None:
        self.pages[1].append_log(f"> {command}")
        self.run(self.agent.send_console(command))

    def create_group(self) -> None:
        name, ok = QInputDialog.getText(self, "Créer un groupe", "Nom du groupe :")
        if ok and name.strip():
            self.run(self.agent.api.create_group(name.strip()[:64]),
                     lambda g: self.reload_groups(g["group_id"]), "Groupe créé")

    def join_group(self) -> None:
        code, ok = QInputDialog.getText(self, "Rejoindre un groupe", "Code d'invitation :")
        if ok and code.strip():
            self.run(self.agent.api.join_group(code.strip()),
                     lambda g: self.reload_groups(g["group_id"]), "Bienvenue dans le groupe !")

    def create_invite(self) -> None:
        group = self.state.group
        if group:
            self.run(self.agent.api.create_invite(group["group_id"]),
                     lambda inv: dialogs.show_invite(self, inv["code"]))

    def create_server(self) -> None:
        group = self.state.group
        if not group:
            return
        picker = dialogs.TemplateDialog(self)
        if not picker.exec():
            return
        template = picker.chosen
        dlg = dialogs.ServerConfigDialog(self.bridge, self.agent, None, self,
                                         defaults=template.defaults() if template else None)
        if dlg.exec():
            values = dlg.values()

            async def create() -> tuple[dict, list[str]]:
                server = await self.agent.api.create_server(group["group_id"], **values)
                if self.agent.settings.host_id:
                    # The creator's PC starts as the preferred host.
                    await self.agent.api.set_priority(server["server_id"], self.agent.settings.host_id, 100)
                if template and template.mods:
                    return await self.agent.apply_template(server, template.key)
                return server, []

            def created(result: tuple[dict, list[str]]) -> None:
                server, missing = result
                self.reload_servers(server["server_id"])
                if missing:
                    QMessageBox.information(
                        self, "Serveur créé", "Pas encore de fichier pour Minecraft "
                        f"{server['mc_version']} : {', '.join(missing)}. Le serveur est créé sans eux ; tu pourras "
                        "les ajouter plus tard depuis « Mods ».")
                if template and template.modpack:
                    pick = group_dialogs.ModpackDialog(self.bridge, self.agent, server, self, popular=True)
                    if pick.exec() and pick.imported is not None:
                        self.run(self.agent.save_modpack(server, pick.imported), self._set_server,
                                 f"Modpack {pick.imported.modpack.name} importé")

            self.run(create(), created, "Serveur créé")

    def configure_server(self) -> None:
        srv = self.state.server
        if not srv:
            return
        dlg = dialogs.ServerConfigDialog(self.bridge, self.agent, srv, self)
        if dlg.exec():
            values = dlg.values()
            mods = [Mod.model_validate(m) for m in srv.get("mods", [])]
            changed = (values["loader"], values["mc_version"]) != (srv["loader"], srv["mc_version"])
            # The anti-cheat files differ by server type: chosen again for the new one, like the others.
            protected = anticheat_enabled(mods)
            for slug in anticheat_slugs(Loader(srv["loader"])) if protected and changed else ():
                for m in [m for m in mods if m.slug == slug]:
                    mods = without(mods, m.project_id)

            datapacks = [Datapack.model_validate(d) for d in srv.get("datapacks", [])]

            async def save() -> dict:
                if datapacks and values["mc_version"] != srv["mc_version"]:
                    new_packs, missing = await self.agent.modrinth.datapacks_for(datapacks, values["mc_version"])
                    if missing:
                        raise ValueError("Pas de datapack pour " + ", ".join(missing) + f" en Minecraft "
                                         f"{values['mc_version']} : retire-les d'abord (bouton Datapacks), ou "
                                         "garde la version actuelle.")
                    values["datapacks"] = [p.model_dump(mode="json") for p in new_packs]
                    self.agent.approve_mods(new_packs)
                if (mods or protected) and changed:
                    # Mods are chosen for one Minecraft version: pick the files for the new one.
                    new, missing = (await self.agent.modrinth.for_server(mods, Loader(values["loader"]),
                                                                         values["mc_version"])
                                    if mods else ([], []))
                    if protected and Loader(values["loader"]) in MOD_LOADERS and not missing:
                        try:
                            new += await self.agent.anticheat_mods(Loader(values["loader"]), values["mc_version"],
                                                                   new)
                        except ModrinthError as exc:
                            raise ValueError(f"Pas d'anti-triche pour {values['loader']} {values['mc_version']} : "
                                             "décoche d'abord la protection (bouton Mods), ou garde la version "
                                             "actuelle.") from exc
                    if missing:
                        raise ValueError("Pas de fichier pour " + ", ".join(missing) + f" en {values['loader']} "
                                         f"{values['mc_version']} : retire-les d'abord (bouton Mods), ou "
                                         "garde la version actuelle.")
                    values["mods"] = [m.model_dump(mode="json") for m in new]
                    if srv.get("map_enabled") and not any(m.slug == MAP_MOD_SLUG for m in new):
                        values["map_enabled"] = False
                    self.agent.approve_mods(new)
                return await self.agent.api.update_server(srv["server_id"], **values)

            # The allowed players and how they connect apply at once; anything else waits for a restart.
            restart = any(values.get(k) != srv.get(k) for k in values
                          if k not in ("allowed_players", "personal_addresses"))
            self.run(save(), self._saved_needs_restart if restart else self._set_server,
                     "Configuration enregistrée")

    def set_priority(self, host_id: str, priority: int) -> None:
        sid = self.current_server_id()
        if sid:
            self.run(self.agent.api.set_priority(sid, host_id, priority), lambda _: self.refresh_details(),
                     "Priorité enregistrée")

    def set_role(self, user_id: str, role: str) -> None:
        group = self.state.group
        if group:
            self.run(self.agent.api.update_member(group["group_id"], user_id, role=role),
                     lambda _: self.refresh_details(), "Rôle enregistré")

    def _notice_requests(self, status: dict) -> None:
        """Tell the admins when a player asks to join (once per new request), and why it crashed."""
        previous = self.state.server
        crash = status.get("last_crash")
        if (self.state.is_admin and crash and previous and previous["server_id"] == status["server_id"]
                and crash != previous.get("last_crash") and self.state.hosting_server != status["server_id"]):
            self.tray.showMessage(f"Plantage de « {status['name']} »", crash["message"],
                                  QSystemTrayIcon.MessageIcon.Warning)
        pending = int(status.get("pending_access_requests") or 0)
        if (self.state.is_admin and previous and previous["server_id"] == status["server_id"]
                and pending > int(previous.get("pending_access_requests") or 0)):
            self.tray.showMessage("Demande d'accès", f"Une demande attend pour « {status['name']} » : un joueur "
                                  "veut entrer, ou se connecte depuis un nouvel endroit (page Serveur, bouton "
                                  "Demandes d'accès).")

    def show_access_requests(self) -> None:
        srv = self.state.server
        if srv:
            dlg = group_dialogs.AccessRequestsDialog(self.bridge, self.agent, srv, self)
            dlg.exec()
            self.refresh_server()

    def show_mods(self) -> None:
        srv = self.state.server
        if not srv:
            return
        dlg = group_dialogs.ModsDialog(self.bridge, self.agent, srv, self.state.is_admin, self,
                                       updates=self.state.mod_updates_for(srv))
        accepted = dlg.exec()
        if dlg.open_settings:
            self.show_config_files()
            return
        if accepted and self.state.is_admin:
            if dlg.imported is not None:
                self.run(self.agent.save_modpack(srv, dlg.imported), self._saved_needs_restart,
                         f"Modpack {dlg.imported.modpack.name} importé")
                return
            mods, map_enabled = dlg.values()
            self.run(self.agent.save_mods(srv["server_id"], mods, map_enabled), self._saved_needs_restart,
                     "Liste enregistrée")

    def show_config_files(self) -> None:
        srv = self.state.server
        if not srv or not self.state.is_admin:
            return
        dlg = group_dialogs.ConfigFilesDialog(self.bridge, self.agent, srv, self)
        if dlg.exec() and dlg.saved_status is not None:
            self.statusBar().showMessage("Réglages enregistrés", 8000)
            self._saved_needs_restart(dlg.saved_status)

    def show_datapacks(self) -> None:
        srv = self.state.server
        if not srv:
            return
        dlg = group_dialogs.DatapacksDialog(self.bridge, self.agent, srv, self.state.is_admin, self)
        if dlg.exec() and self.state.is_admin:
            packs = dlg.values()
            if [p.model_dump(mode="json") for p in packs] != srv.get("datapacks", []):
                self.run(self.agent.save_datapacks(srv["server_id"], packs), self._saved_needs_restart,
                         "Datapacks enregistrés")

    def start_profile(self) -> None:
        """One minute of spark on the hosting PC; the report is published by spark."""
        srv = self.state.server
        if not srv or not self.state.is_admin:
            return
        if srv["loader"] == "fabric" and not any(m.get("slug") == SPARK_MOD_SLUG for m in srv.get("mods", [])):
            if QMessageBox.question(self, "Analyser le lag", "Il faut le mod spark dans la liste des mods. "
                                    "L'ajouter ? (chaque PC l'autorise, puis le serveur redémarre ; relance "
                                    "ensuite l'analyse)") != QMessageBox.StandardButton.Yes:
                return
            mods = [Mod.model_validate(m) for m in srv.get("mods", [])]

            async def add_spark() -> dict:
                added = await self.agent.resolve_mod(srv, SPARK_MOD_SLUG, mods)
                return await self.agent.save_mods(srv["server_id"], mods + added)

            self.run(add_spark(), self._saved_needs_restart, "spark ajouté")
            return
        if QMessageBox.question(
                self, "Analyser le lag",
                "spark mesure le serveur pendant une minute, puis publie son rapport sur spark.lucko.me (le site "
                "de spark) : toute personne qui a le lien peut le lire. Il contient les mods, les réglages du "
                "serveur et les régions du monde les plus chargées, pas les adresses des joueurs.\n\n"
                "Lancer l'analyse ?") != QMessageBox.StandardButton.Yes:
            return
        self.run(self.agent.api.send_action(srv["server_id"], {"kind": "profile_start", "seconds": 60}),
                 lambda _: self.refresh_server(), "Analyse lancée : le rapport arrive dans une minute")

    def show_pregen(self) -> None:
        srv = self.state.server
        if not srv or not self.state.is_admin:
            return
        dlg = group_dialogs.PregenDialog(srv, self)
        if not dlg.exec():
            return
        if dlg.missing:
            mods = [Mod.model_validate(m) for m in srv.get("mods", [])]

            async def add_chunky() -> dict:
                added = await self.agent.resolve_mod(srv, PREGEN_MOD_SLUG, mods)
                return await self.agent.save_mods(srv["server_id"], mods + added)

            self.run(add_chunky(), self._saved_needs_restart, "Chunky ajouté")
            return
        dimension, radius = dlg.values()
        self.run(self.agent.api.send_action(srv["server_id"], {"kind": "pregen_start", "dimension": dimension,
                                                               "radius": radius}),
                 lambda _: self.refresh_server(), "Pré-génération lancée")

    def cancel_pregen(self) -> None:
        srv = self.state.server
        if srv and QMessageBox.question(self, "Pré-génération", "Arrêter la pré-génération ? Ce qui est déjà "
                                        "généré est gardé.") == QMessageBox.StandardButton.Yes:
            self.run(self.agent.api.send_action(srv["server_id"], {"kind": "pregen_cancel"}),
                     lambda _: self.refresh_server(), "Pré-génération arrêtée")

    def remove_crashed_mod(self) -> None:
        """Take out the mod the last crash points to (and the dependencies only it needed)."""
        srv = self.state.server
        crash = (srv or {}).get("last_crash") or {}
        mods = [Mod.model_validate(m) for m in (srv or {}).get("mods", [])]
        mod = next((m for m in mods if m.project_id == crash.get("mod_project_id")), None)
        if srv is None or mod is None or not self.state.is_admin:
            return
        if QMessageBox.question(self, "Retirer le mod", f"Retirer {mod.name} {mod.version_number} ?\n\n"
                                "Ses blocs et objets disparaissent du monde au prochain démarrage ; la sauvegarde "
                                "actuelle reste dans l'historique (page Stockage).") != QMessageBox.StandardButton.Yes:
            return
        kept = without(mods, mod.project_id)
        self.run(self.agent.save_mods(srv["server_id"], kept,
                                      bool(srv.get("map_enabled")) and any(m.slug == MAP_MOD_SLUG for m in kept)),
                 self._after_mod_removed, f"{mod.name} retiré")

    def _after_mod_removed(self, status: dict) -> None:
        self._set_server(status)
        if status["state"] == "offline" and not status["desired_running"]:
            if QMessageBox.question(self, "Démarrer", "Démarrer le serveur maintenant ?") == \
                    QMessageBox.StandardButton.Yes:
                self.start_server()
        elif status["state"] == "running":
            self._saved_needs_restart(status)

    def show_resource_pack(self) -> None:
        srv = self.state.server
        if not srv or not self.state.is_admin:
            return
        dlg = group_dialogs.ResourcePackDialog(self.bridge, self.agent, srv, self)
        if dlg.exec():
            pack = dlg.value()
            if (pack.model_dump(mode="json") if pack else None) == srv.get("resource_pack"):
                return
            self.run(self.agent.api.update_server(srv["server_id"],
                                                  resource_pack=pack.model_dump(mode="json") if pack else None),
                     self._saved_needs_restart, "Pack de ressources enregistré")

    def approve_mods(self) -> None:
        srv = self.state.server
        if not srv:
            return
        blocked = self.agent.unapproved_mods(srv)
        if blocked and group_dialogs.ModApprovalDialog(srv["name"], blocked, self).exec():
            self.agent.approve_mods(blocked)
            self.statusBar().showMessage("Mods autorisés sur ce PC", 8000)
            self.render()

    def set_can_host(self, user_id: str, allowed: bool) -> None:
        group = self.state.group
        if group:
            self.run(self.agent.api.update_member(group["group_id"], user_id, can_host=allowed),
                     lambda _: self.refresh_details())

    def save_settings(self, **changes: Any) -> None:
        if autostart.available() and "start_with_windows" in changes:
            try:
                autostart.set_enabled(changes["start_with_windows"])
            except OSError as exc:
                QMessageBox.warning(self, "Démarrage avec l'ordinateur", f"Réglage impossible : {exc}")
                changes.pop("start_with_windows")
        self.run(self.agent.update_settings(**changes), lambda _: self.render(), "Paramètres enregistrés")

    def change_password(self) -> None:
        if dialogs.PasswordDialog(self.bridge, self.agent, self).exec():
            self.statusBar().showMessage("Mot de passe changé", 8000)
            QMessageBox.information(self, "Mot de passe changé", "C'est fait. Tes autres PC te demanderont "
                                    "le nouveau mot de passe.")

    def accept_eula(self) -> None:
        if dialogs.EulaDialog(self).exec():
            self.agent.accept_eula()
            self.render()

    def logout(self) -> None:
        if QMessageBox.question(self, "Se déconnecter ?", "Ce PC ne pourra plus héberger tant que tu ne "
                                "te reconnectes pas.") == QMessageBox.StandardButton.Yes:
            self._quitting = True
            self.run(self.agent.logout(), lambda _: (self.tray.hide(), QApplication.quit()))
