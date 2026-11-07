"""The sections of the dashboard."""

from __future__ import annotations

import html
import os
import sys
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import psutil

from mdh_client.gui.texts import (
    INELIGIBILITY_LABELS,
    PHASE_LABELS,
    ROLE_LABELS,
    alert_text,
    event_label,
    health_label,
    state_label,
)
from mdh_client.system import autostart
from mdh_client.gui.theme import COLORS
from mdh_client.gui.widgets import Banner, Card, LineChart, Pill, human_bytes, page_title
from mdh_client.minecraft import pregen
from mdh_client.monitoring.resources import max_allowed_memory_mb, suggest_memory_mb
from mdh_shared import __version__
from mdh_shared.constants import HOST_RAM_HEADROOM_MB

if TYPE_CHECKING:
    from mdh_client.gui.main_window import MainWindow

LOADER_NAMES = {"paper": "Paper", "vanilla": "Vanilla", "fabric": "Fabric"}


def mods_key(srv: dict[str, Any]) -> tuple:
    """What a check for mod updates depends on."""
    return (srv["server_id"], srv["loader"], srv["mc_version"], tuple(m["version_id"] for m in srv.get("mods", [])))


#: The system named in the texts (the memory it keeps, how the app starts with it).
OS_NAME = "macOS" if sys.platform == "darwin" else "Windows"


@dataclass
class AppState:
    user: dict[str, Any] | None = None
    online: bool = False
    group: dict[str, Any] | None = None
    server: dict[str, Any] | None = None
    hosts: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    storage: dict[str, Any] | None = None
    hosting_phase: str | None = None
    hosting_server: str | None = None
    #: This PC's part in a migration in progress (prefetching as target, countdown as source).
    migration_phase: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    players: list[str] = field(default_factory=list)
    #: Alerts of the VPS for the admins (disk, memory, quota, restart after a crash).
    alerts: list[dict[str, Any]] = field(default_factory=list)
    #: Players and last chat lines of the running server, as reported by its host.
    activity: dict[str, Any] | None = None
    #: Console of the running server as read from the control plane (admins, moderators).
    console_log: list[dict[str, Any]] = field(default_factory=list)
    console_next: int = 0
    console_key: tuple | None = None
    #: Set when the control plane no longer accepts this application ({"current", "minimum"}).
    incompatible: dict[str, Any] | None = None
    #: When this PC last brought its standby copy of a server up to date, by server id.
    standby: dict[str, float] = field(default_factory=dict)
    #: A newer version, downloaded and verified ({"version", "notes"}), not installed yet.
    update: dict[str, Any] | None = None
    #: Newer files on Modrinth for a server's mods, by server id ({"key", "mods", "changes"}).
    mod_updates: dict[str, dict[str, Any]] = field(default_factory=dict)

    def mod_updates_for(self, srv: dict[str, Any] | None) -> dict[str, Any] | None:
        """Updates found for exactly the mods the server has now (none once they are applied)."""
        found = self.mod_updates.get(srv["server_id"]) if srv else None
        return found if found and found["changes"] and found["key"] == mods_key(srv) else None

    def waiting_for_host(self) -> dict[str, list[str]] | None:
        """Reasons per PC when the group wants the server up but no PC was chosen, else None."""
        srv = self.server
        if not srv or not srv.get("desired_running") or srv["state"] != "offline":
            return None
        for e in self.events:  # newest first
            if e["type"] in ("lease_granted", "lease_released", "stop_requested"):
                return {}
            if e["type"] == "no_eligible_host":
                return e["payload"].get("rejected", {})
        return {}

    @property
    def is_admin(self) -> bool:
        return bool(self.group and self.group.get("role") in ("owner", "admin"))

    @property
    def can_moderate(self) -> bool:
        """Admins and moderators: kick, ban, pardon, announce (never op nor configuration)."""
        return bool(self.group and self.group.get("role") in ("owner", "admin", "moderator"))

    def can_transfer(self, my_host_id: str | None) -> bool:
        """The "change host" action makes sense (the control plane still checks the rights)."""
        srv = self.server
        return bool(srv and srv["state"] == "running" and not srv.get("migration")
                    and (self.is_admin or srv.get("active_host_id") == my_host_id
                         or any(h["host_id"] == my_host_id and h["can_host"] for h in self.hosts)))

    def transfer_choices(self, my_host_id: str | None) -> list[tuple[str, str | None]]:
        """Where the server may go: admins and the current host pick any PC, others only their own."""
        srv = self.server
        if not srv:
            return []
        anyone = self.is_admin or srv.get("active_host_id") == my_host_id
        choices: list[tuple[str, str | None]] = [("Automatique : le meilleur PC disponible", None)] if anyone else []
        for h in self.hosts:
            if h["host_id"] == srv.get("active_host_id") or not (h["online"] and h["hosting_allowed"]
                                                                  and h["can_host"] and h["enabled"]):
                continue
            if anyone or h["host_id"] == my_host_id:
                mine = " — ce PC" if h["host_id"] == my_host_id else ""
                choices.append((f"{h['display_name']} ({h['owner']}){mine}", h["host_id"]))
        return choices if len(choices) > (1 if anyone else 0) else []

    def host_name(self, host_id: str | None) -> str:
        for h in self.hosts:
            if h["host_id"] == host_id:
                return f"{h['display_name']} ({h['owner']})"
        return "—" if host_id is None else "PC inconnu"


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


def _go(mb: float) -> str:
    """Megabytes as French gigabytes: 7680 -> "7,5 Go"."""
    return f"{mb / 1024:.1f}".replace(".", ",") + " Go"


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "Aucune"
    return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")


def _fmt_clock(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def _fmt_seconds(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")


def measures_text(h: dict[str, Any]) -> str:
    """"12 ms · 45 Mbit/s · ×1,2 · 78/100" from what a PC last measured."""
    parts = []
    if h.get("rtt_ms") is not None:
        parts.append(f"{h['rtt_ms']:.0f} ms")
    if h.get("upload_kbps") is not None:
        parts.append(f"{h['upload_kbps'] / 1000:.0f} Mbit/s" if h["upload_kbps"] >= 10_000 else
                     f"{h['upload_kbps'] / 1000:.1f} Mbit/s".replace(".", ","))
    if h.get("cpu_score") is not None:
        parts.append(f"×{h['cpu_score']:.1f}".replace(".", ","))
    if h.get("score") is not None:
        parts.append(f"{h['score']:.0f}/100")
    return " · ".join(parts) if parts else ("—" if h.get("online") else "hors ligne")


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return "moins d'une minute"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes:02d}" if hours else f"{minutes} min"


class HomePage(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Accueil", "L'état du serveur partagé, d'un coup d'œil."))
        self.too_old = Banner()
        self.too_old_btn = QPushButton("Mettre à jour maintenant")
        self.too_old_btn.setObjectName("Primary")
        self.too_old_btn.clicked.connect(lambda: win.install_update())
        self.too_old.add_action(self.too_old_btn)
        layout.addWidget(self.too_old)
        self.alerts = Banner()
        layout.addWidget(self.alerts)
        self.banner = Banner()
        self.failover_btn = QPushButton("Relancer depuis la dernière sauvegarde")
        self.failover_btn.setObjectName("Primary")
        self.failover_btn.clicked.connect(win.confirm_failover)
        self.banner.add_action(self.failover_btn)
        layout.addWidget(self.banner)
        self.waiting = Banner()
        self.ram_btn = QPushButton("Réduire la RAM du serveur…")
        self.ram_btn.clicked.connect(win.configure_server)
        self.waiting.add_action(self.ram_btn)
        layout.addWidget(self.waiting)
        self.migration = Banner("Info")
        self.cancel_migration_btn = QPushButton("Annuler le changement de PC")
        self.cancel_migration_btn.clicked.connect(win.cancel_transfer)
        self.migration.add_action(self.cancel_migration_btn)
        layout.addWidget(self.migration)
        self.empty = Banner("Info")
        self.create_group_btn = QPushButton("Créer un groupe")
        self.create_group_btn.clicked.connect(win.create_group)
        self.join_btn = QPushButton("Rejoindre avec un code")
        self.join_btn.clicked.connect(win.join_group)
        self.create_server_btn = QPushButton("Créer le serveur")
        self.create_server_btn.setObjectName("Primary")
        self.create_server_btn.clicked.connect(win.create_server)
        for b in (self.create_group_btn, self.join_btn, self.create_server_btn):
            self.empty.add_action(b)
        layout.addWidget(self.empty)

        status_row = QHBoxLayout()
        self.server_name = QLabel()
        self.server_name.setObjectName("PageTitle")
        self.server_name.setTextFormat(Qt.TextFormat.PlainText)
        self.pill = Pill()
        status_row.addWidget(self.server_name)
        status_row.addWidget(self.pill)
        status_row.addStretch()
        self.start_btn = QPushButton("Démarrer")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(win.start_server)
        self.stop_btn = QPushButton("Arrêter")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(win.stop_server)
        self.transfer_btn = QPushButton("Changer de PC…")
        self.transfer_btn.setToolTip("Faire héberger le serveur par un autre PC du groupe, sans rien perdre.")
        self.transfer_btn.clicked.connect(win.transfer_server)
        status_row.addWidget(self.start_btn)
        status_row.addWidget(self.transfer_btn)
        status_row.addWidget(self.stop_btn)
        layout.addLayout(status_row)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards = {
            "group": Card("Groupe"),
            "host": Card("PC hôte actuel"),
            "players": Card("Joueurs connectés"),
            "version": Card("Version"),
            "sync": Card("Synchronisation"),
            "backup": Card("Dernière sauvegarde"),
            "this_pc": Card("Ce PC"),
            "address": Card("Adresse pour se connecter"),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 2, i % 2)
        self.cards["address"].value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_address_btn = QPushButton("Copier l'adresse")
        self.copy_address_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.cards["address"].value.text()))
        self.cards["address"].layout().addWidget(self.copy_address_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.status_page_btn = QPushButton("Copier le lien de la page de statut")
        self.status_page_btn.setToolTip("Une page web à envoyer aux amis : en ligne ou non, qui joue, adresse.")
        self.status_page_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText((self.win.state.server or {}).get("status_page_url") or ""))
        self.cards["address"].layout().addWidget(self.status_page_btn, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(grid)
        layout.addStretch()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))

    def refresh(self, s: AppState) -> None:
        srv = s.server
        self.alerts.setVisible(bool(s.alerts))
        if s.alerts:
            self.alerts.text.setText("<b>Serveur central</b><br>" + "<br>".join(
                ("⚠ " if a["severity"] == "critical" else "• ") + html.escape(alert_text(a)) for a in s.alerts))
        self.too_old.setVisible(bool(s.incompatible))
        if s.incompatible:
            self.too_old.text.setText(
                f"<b>Mise à jour obligatoire.</b> Cette application (version {s.incompatible['current']}) est "
                f"trop ancienne pour le serveur central, qui demande au moins la {s.incompatible['minimum']}. "
                "Ce PC ne peut plus héberger d'ici là. " + (
                    "La nouvelle version est prête." if s.update else
                    "Elle s'installera dès qu'elle sera téléchargée ; si rien ne vient, réinstalle "
                    "l'application."))
            self.too_old_btn.setVisible(bool(s.update))
        self.empty.setVisible(s.group is None or srv is None)
        if s.group is None:
            self.empty.text.setText("<b>Tu ne fais partie d'aucun groupe.</b><br>Crée ton groupe ou rejoins "
                                    "celui de tes amis avec le code qu'ils t'ont envoyé.")
        elif srv is None:
            self.empty.text.setText("<b>Ce groupe n'a pas encore de serveur.</b><br>" + (
                "Crée-le : choisis la version et le type, le reste est automatique."
                if s.is_admin else "Un administrateur du groupe doit le créer."))
        self.create_group_btn.setVisible(s.group is None)
        self.join_btn.setVisible(s.group is None)
        self.create_server_btn.setVisible(s.group is not None and srv is None and s.is_admin)
        orphaned = bool(srv and srv["state"] == "orphaned")
        self.banner.setVisible(orphaned)
        if orphaned:
            auto_at = srv.get("auto_failover_at")
            what_next = (
                f"Sinon, il sera relancé automatiquement sur un autre PC vers {_fmt_clock(auto_at)}, "
                "à partir de la dernière sauvegarde" + (" (ou tout de suite si tu le relances toi-même)"
                                                         if s.is_admin else "")
                if auto_at else
                "Sinon, tu peux relancer chez quelqu'un d'autre à partir de la dernière sauvegarde"
                if s.is_admin else
                "Sinon, un administrateur du groupe peut relancer chez quelqu'un d'autre à partir de "
                "la dernière sauvegarde")
            self.banner.text.setText(
                f"<b>Le PC hôte a disparu sans sauvegarde finale.</b><br>Dernière sauvegarde : "
                f"{_fmt_time(srv.get('last_backup_at'))}. S'il revient, il reprendra sans perte. "
                f"{what_next} : ce qui a été joué depuis sera mis de côté, jamais supprimé.")
            self.failover_btn.setVisible(s.is_admin)
        self._show_waiting(s)
        self._show_migration(s)

        for w in (self.server_name, self.pill, self.start_btn, self.stop_btn, self.transfer_btn):
            w.setVisible(srv is not None)
        self.transfer_btn.setEnabled(s.can_transfer(self.win.agent.settings.host_id))
        self.cards["group"].set(s.group["name"] if s.group else "—",
                                {"owner": "Propriétaire", "admin": "Administrateur", "member": "Membre"}
                                .get(s.group["role"], "") if s.group else "")
        if srv is None:
            for key in ("host", "players", "version", "sync", "backup", "address"):
                self.cards[key].set("—", "")
            self.copy_address_btn.setVisible(False)
            self.status_page_btn.setVisible(False)
        else:
            self.server_name.setText(srv["name"])
            label, kind = state_label(srv["state"])
            self.pill.set(label, kind)
            running_like = srv["state"] not in ("offline", "orphaned")
            self.start_btn.setEnabled(not srv["desired_running"] and srv["state"] == "offline")
            self.stop_btn.setEnabled(running_like or bool(srv["desired_running"]))
            self.cards["host"].set(s.host_name(srv["active_host_id"]),
                                   health_label(srv.get("tps"), srv.get("mspt"))
                                   or "Choisi automatiquement selon la priorité et la disponibilité.")
            players = srv.get("players_online")
            relayed = srv.get("relay_connections") or 0
            if srv.get("stops_when_empty_at"):
                players_detail = f"Personne ne joue : arrêt automatique vers {_fmt_clock(srv['stops_when_empty_at'])}."
            elif srv["state"] == "offline" and srv.get("start_on_join"):
                players_detail = "En veille : démarre tout seul quand un joueur se connecte."
            else:
                players_detail = f"dont {relayed} via l'adresse du groupe" if relayed else ""
            self.cards["players"].set("—" if players is None else str(players), players_detail)
            self.cards["version"].set(f"{LOADER_NAMES.get(srv['loader'], srv['loader'])} {srv['mc_version']}",
                                      f"{_go(srv['memory_mb'])} de RAM · " + (
                                          "comptes non officiels acceptés (liste blanche)"
                                          if srv.get("allow_offline_accounts") else "comptes officiels uniquement"))
            phase_here = s.hosting_phase if s.hosting_server == srv["server_id"] else None
            if phase_here == "saving":
                self.cards["sync"].set("Envoi en cours…", "Sauvegarde finale vers le serveur central.")
            elif srv.get("head_version_id"):
                self.cards["sync"].set("À jour", "Chaque PC hôte récupère la dernière version vérifiée.")
            else:
                self.cards["sync"].set("Aucune sauvegarde encore",
                                       "La première est envoyée à la fin de la première session.")
            self.cards["backup"].set(_fmt_time(srv.get("last_backup_at")))
            address = srv.get("public_address")
            personal = bool(srv.get("allow_offline_accounts")) and srv.get("personal_addresses", True)
            if personal and address:
                self.cards["address"].set(
                    address,
                    "Pour la première connexion : un nouveau joueur s'y connecte et sa demande arrive dans "
                    "« Demandes d'accès ». Une fois accepté, il y voit son adresse personnelle et joue "
                    "ensuite avec elle (aussi dans « Adresses des joueurs »). Personne ne peut ainsi "
                    "prendre le pseudo d'un autre.")
            elif address:
                self.cards["address"].set(address, "La même pour tout le monde, quel que soit le PC qui héberge.")
            else:
                self.cards["address"].set("—", "Le relais du serveur central est désactivé.")
            self.copy_address_btn.setVisible(bool(address) and not personal)
            self.status_page_btn.setVisible(bool(srv.get("status_page_url")))
        phase = s.hosting_phase if s.hosting_server else None
        stats = s.stats
        detail = ""
        if phase == "running" and stats:
            detail = f"CPU {stats.get('cpu_percent') or 0:.0f} % · RAM {stats.get('ram_mb') or 0} Mo"
        elif phase is None and srv and (copied_at := s.standby.get(srv["server_id"])):
            when = datetime.fromtimestamp(copied_at).strftime("%H:%M")
            detail = f"Copie de secours du monde à jour ({when}) : prêt à prendre le relais."
        self.cards["this_pc"].set(PHASE_LABELS.get(phase, "Disponible" if self.win.agent.settings.hosting_allowed
                                                    else "Hébergement désactivé"), detail)


    def _show_migration(self, s: AppState) -> None:
        mig = s.server.get("migration") if s.server else None
        self.migration.setVisible(bool(mig))
        if not mig:
            return
        source = html.escape(s.host_name(mig["source_host_id"]))
        target = html.escape(s.host_name(mig["target_host_id"]))
        mine = self.win.agent.settings.host_id
        if mig["source_host_id"] == mine:
            source = "ce PC"
        if mig["target_host_id"] == mine:
            target = "ce PC"
        why = "Le PC prioritaire est de retour. " if mig["reason"] == "preemption" else ""
        countdown = mig["countdown_s"]
        steps = {
            "prefetching": f"Étape 1/3 : {target} télécharge le monde à l'avance, la partie continue.",
            "stopping": ("Étape 2/3 : " + (f"les joueurs sont prévenus ({countdown} s), puis " if countdown else "")
                         + f"{source} arrête le serveur et envoie la sauvegarde finale."),
            "handover": f"Étape 3/3 : {target} récupère la sauvegarde finale et démarre le serveur.",
        }
        self.migration.text.setText(
            f"<b>Changement de PC : {source} → {target}</b><br>{why}{steps.get(mig['status'], '')}<br>"
            "Interruption d'environ une minute. En cas de problème, le serveur reste sur le PC actuel.")
        self.cancel_migration_btn.setVisible(mig["status"] != "handover")

    def _show_waiting(self, s: AppState) -> None:
        reasons = s.waiting_for_host()
        self.waiting.setVisible(reasons is not None)
        self.ram_btn.setVisible(False)
        if reasons is None:
            return
        srv = s.server
        assert srv is not None
        lines = ["<b>Le serveur attend un PC hôte.</b> Il démarrera tout seul dès qu'un PC convient."]
        if not reasons:
            lines.append("Recherche en cours…")
        my_host = self.win.agent.settings.host_id
        for host_id, why in reasons.items():
            name = "Ce PC" if host_id == my_host else html.escape(s.host_name(host_id))
            lines.append(f"• {name} : " + ", ".join(INELIGIBILITY_LABELS.get(r, r) for r in why))
            if host_id == my_host and "not_enough_ram" in why:
                available = psutil.virtual_memory().available / 1024**3
                cap = self.win.agent.memory_cap_mb()
                needed = (srv["memory_mb"] + HOST_RAM_HEADROOM_MB) / 1024
                if srv["memory_mb"] > cap:
                    lines.append(f"&nbsp;&nbsp;Le serveur demande {_go(srv['memory_mb'])} mais ce PC est "
                                 f"limité à {_go(cap)} dans Paramètres.")
                else:
                    lines.append(
                        f"&nbsp;&nbsp;Il faut {_go(needed * 1024)} libres ({_go(srv['memory_mb'])} pour le "
                        f"serveur + {HOST_RAM_HEADROOM_MB // 1024} Go de marge pour {OS_NAME}) ; {OS_NAME} en "
                        f"annonce {_go(available * 1024)}. Ferme des applications (le jeu Minecraft lui-même en "
                        "utilise beaucoup) ou réduis la RAM du serveur.")
                self.ram_btn.setVisible(s.is_admin)
        self.waiting.text.setText("<br>".join(lines))


class ServerPage(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Serveur", "Démarrer, arrêter, suivre ce qui se passe."))
        self.approval = Banner()
        approve = QPushButton("Voir et autoriser…")
        approve.setObjectName("Primary")
        approve.clicked.connect(win.approve_mods)
        self.approval.add_action(approve)
        self.approval.setVisible(False)
        layout.addWidget(self.approval)
        # Why the server crashed, as the hosting PC understood it (admins).
        self.crash = Banner()
        self.crash_btn = QPushButton("Retirer ce mod…")
        self.crash_btn.setObjectName("Danger")
        self.crash_btn.clicked.connect(win.remove_crashed_mod)
        self.crash.add_action(self.crash_btn)
        self.crash.setVisible(False)
        layout.addWidget(self.crash)
        row = QHBoxLayout()
        self.start_btn = QPushButton("Démarrer")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(win.start_server)
        self.stop_btn = QPushButton("Arrêter")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(win.stop_server)
        self.restart_btn = QPushButton("Redémarrer")
        self.restart_btn.clicked.connect(win.restart_server)
        self.transfer_btn = QPushButton("Changer de PC…")
        self.transfer_btn.setToolTip("Faire héberger le serveur par un autre PC du groupe, sans rien perdre.")
        self.transfer_btn.clicked.connect(win.transfer_server)
        self.config_btn = QPushButton("Configuration…")
        self.config_btn.clicked.connect(win.configure_server)
        self.new_world_btn = QPushButton("Nouveau monde…")
        self.new_world_btn.setToolTip("Recommencer avec un monde tout neuf, graine au hasard ou choisie. "
                                      "Le monde actuel reste dans l'historique.")
        self.new_world_btn.clicked.connect(win.new_world)
        self.addresses_btn = QPushButton("Adresses des joueurs…")
        self.addresses_btn.setToolTip("Adresse personnelle de chaque joueur autorisé, à leur envoyer.")
        self.addresses_btn.clicked.connect(win.show_player_addresses)
        self.access_btn = QPushButton()
        self.access_btn.setObjectName("Primary")
        self.access_btn.setToolTip("Joueurs refusés par la liste blanche qui demandent à entrer.")
        self.access_btn.clicked.connect(win.show_access_requests)
        self.mods_btn = QPushButton("Mods…")
        self.mods_btn.setToolTip("Mods (Fabric) ou plugins (Paper) depuis Modrinth, et carte du monde.")
        self.mods_btn.clicked.connect(win.show_mods)
        self.pack_btn = QPushButton("Pack de ressources…")
        self.pack_btn.setToolTip("Textures proposées à chaque joueur qui se connecte.")
        self.pack_btn.clicked.connect(win.show_resource_pack)
        self.datapacks_btn = QPushButton("Datapacks…")
        self.datapacks_btn.setToolTip("Nouveaux terrains, donjons, recettes depuis Modrinth, sans mod.")
        self.datapacks_btn.clicked.connect(win.show_datapacks)
        for b in (self.start_btn, self.stop_btn, self.restart_btn, self.transfer_btn):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.access_btn)
        row.addWidget(self.addresses_btn)
        row.addWidget(self.mods_btn)
        row.addWidget(self.pack_btn)
        row.addWidget(self.datapacks_btn)
        row.addWidget(self.new_world_btn)
        row.addWidget(self.config_btn)
        layout.addLayout(row)

        self.progress_label = QLabel()
        self.progress_label.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress)
        pregen_row = QHBoxLayout()
        self.pregen_label = QLabel()
        self.pregen_label.setObjectName("Muted")
        self.pregen_btn = QPushButton("Pré-générer le monde…")
        self.pregen_btn.setToolTip("Calculer d'avance le terrain autour du spawn (Chunky) : moins de lag en "
                                   "explorant.")
        self.pregen_btn.clicked.connect(win.show_pregen)
        self.pregen_stop = QPushButton("Arrêter la pré-génération")
        self.pregen_stop.clicked.connect(win.cancel_pregen)
        pregen_row.addWidget(self.pregen_label, 1)
        pregen_row.addWidget(self.pregen_stop)
        pregen_row.addWidget(self.pregen_btn)
        layout.addLayout(pregen_row)

        stats_row = QHBoxLayout()
        self.cpu, self.ram, self.tps = Card("CPU (Minecraft)"), Card("RAM (Minecraft)"), Card("TPS / MSPT")
        for c in (self.cpu, self.ram, self.tps):
            stats_row.addWidget(c)
        layout.addLayout(stats_row)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Joueurs"))
        self.players = QListWidget()
        self.players.setMaximumWidth(220)
        self.players.itemSelectionChanged.connect(lambda: self.refresh(self.win.state))
        left.addWidget(self.players)
        # Closed list of actions, carried out by the hosting PC: no free command from afar.
        self.admin_buttons: dict[str, QPushButton] = {}
        for kind, label in (("kick", "Expulser"), ("ban", "Bannir"), ("op", "Nommer opérateur"),
                            ("deop", "Retirer opérateur")):
            btn = QPushButton(label)
            btn.setMaximumWidth(220)
            btn.clicked.connect(lambda _=False, k=kind: self._player_action(k))
            self.admin_buttons[kind] = btn
            left.addWidget(btn)
        self.pardon_btn = QPushButton("Débannir un joueur…")
        self.pardon_btn.setMaximumWidth(220)
        self.pardon_btn.clicked.connect(self._pardon)
        left.addWidget(self.pardon_btn)
        body.addLayout(left)
        right = QVBoxLayout()
        label_row = QHBoxLayout()
        self.console_label = QLabel("Console (journal de ce PC)")
        self.console_mode = QComboBox()
        self.console_mode.addItem("Discussion en jeu", "chat")
        self.console_mode.addItem("Journal du serveur", "log")
        self.console_mode.setToolTip("Le journal montre tout ce qu'affiche le serveur (démarrage, erreurs de "
                                     "mods, connexions), en lecture seule.")
        self.console_mode.currentIndexChanged.connect(self._console_mode_changed)
        label_row.addWidget(self.console_label, 1)
        label_row.addWidget(self.console_mode)
        right.addLayout(label_row)
        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        right.addWidget(self.console, 1)
        cmd_row = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("Commande Minecraft (ex. : whitelist add Steve)")
        self.command.returnPressed.connect(self._send)
        self.send_btn = QPushButton("Envoyer")
        self.send_btn.clicked.connect(self._send)
        cmd_row.addWidget(self.command, 1)
        cmd_row.addWidget(self.send_btn)
        right.addLayout(cmd_row)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)

    def _send(self) -> None:
        text = self.command.text().strip()
        if not text:
            return
        s = self.win.state
        if s.hosting_server and s.server and s.hosting_server == s.server["server_id"]:
            self.win.send_console(text)
        else:
            self.win.admin_action("announce", message=text[:200])
        self.command.clear()

    def _refresh_pregen(self, s: AppState, running: bool) -> None:
        srv = s.server
        p = (srv or {}).get("pregen")
        can = bool(srv and s.is_admin and srv["loader"] in ("fabric", "paper"))
        active = bool(running and p and p["state"] == "running")
        self.pregen_btn.setVisible(can)
        self.pregen_btn.setEnabled(running and not active)
        self.pregen_btn.setToolTip("Calculer d'avance le terrain autour du spawn (Chunky) : moins de lag en "
                                   "explorant." if running else "Démarre d'abord le serveur.")
        self.pregen_stop.setVisible(can and active)
        if not running or not p:
            self.pregen_label.setText("")
            return
        where = pregen.DIMENSIONS.get(p["dimension"], p["dimension"])
        size = ", " + f"{p['radius']:,}".replace(",", " ") + " blocs" if p.get("radius") else ""
        if p["state"] == "running":
            left = f" — reste environ {_fmt_duration(p['eta_s'])}" if p.get("eta_s") else ""
            self.pregen_label.setText(f"Pré-génération ({where}{size}) : {p['percent']:.1f} %{left}")
        else:
            self.pregen_label.setText({"done": f"Pré-génération terminée ({where}{size}).",
                                       "cancelled": f"Pré-génération arrêtée ({where}) : le terrain déjà généré "
                                                    "est gardé.",
                                       "no_space": "Pré-génération refusée : pas assez de place sur le disque du "
                                                   "PC hôte."}.get(p["state"], ""))

    def wants_log(self) -> bool:
        """The console of the server hosted elsewhere is shown (polled from the control plane)."""
        return not self.console_mode.isHidden() and self.console_mode.currentData() == "log"

    def _console_mode_changed(self) -> None:
        self.refresh(self.win.state)
        self.win.refresh_server()

    def _selected_player(self) -> str | None:
        items = self.players.selectedItems()
        return items[0].text() if items else None

    def _player_action(self, kind: str) -> None:
        player = self._selected_player()
        if player:
            self.win.admin_action(kind, player)

    def _pardon(self) -> None:
        name, ok = QInputDialog.getText(self, "Débannir", "Pseudo Minecraft du joueur à débannir :")
        name = name.strip()
        if ok and name:
            if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
                QMessageBox.warning(self, "Débannir", "Ce pseudo n'est pas un pseudo Minecraft valide.")
                return
            self.win.admin_action("pardon", name)

    def append_log(self, line: str) -> None:
        if not getattr(self, "_remote_view", False):  # the console shows the chat of another PC
            self.console.appendPlainText(line)

    def show_progress(self, message: str, fraction: float | None) -> None:
        self.progress_label.setText(message)
        self.progress.setVisible(True)
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(fraction * 1000))

    def refresh(self, s: AppState) -> None:
        srv = s.server
        has = srv is not None
        here = has and s.hosting_server == srv["server_id"]
        running_here = here and s.hosting_phase == "running"
        self.start_btn.setEnabled(has and srv["state"] == "offline" and not srv["desired_running"])
        self.stop_btn.setEnabled(has and (srv["state"] not in ("offline", "orphaned") or srv["desired_running"]))
        self.restart_btn.setEnabled(running_here)
        self.transfer_btn.setEnabled(s.can_transfer(self.win.agent.settings.host_id))
        self.config_btn.setEnabled(has and s.is_admin)
        self.new_world_btn.setVisible(has and s.is_admin)
        self.addresses_btn.setVisible(has and s.is_admin and bool(srv.get("allow_offline_accounts"))
                                      and bool(srv.get("public_address")))
        mods = srv.get("mods", []) if has else []
        word = "Plugins" if has and srv["loader"] == "paper" else "Mods"
        updates = s.mod_updates_for(srv) if has and s.is_admin else None
        self.mods_btn.setText((f"{word} ({len(mods)})" + (f" · {len(updates['changes'])} mise(s) à jour"
                                                           if updates else "") + "…") if mods else f"{word}…")
        self.mods_btn.setVisible(has and (srv["loader"] != "vanilla" or bool(mods)))
        self.pack_btn.setVisible(bool(has and s.is_admin))
        packs = srv.get("datapacks", []) if has else []
        self.datapacks_btn.setVisible(bool(has and (s.is_admin or packs)))
        self.datapacks_btn.setText(f"Datapacks ({len(packs)})…" if packs else "Datapacks…")
        self.pack_btn.setText("Pack de ressources ✓…" if has and srv.get("resource_pack") else "Pack de ressources…")
        pending = int(srv.get("pending_access_requests") or 0) if has else 0
        self.access_btn.setText(f"Demandes d'accès ({pending})")
        self.access_btn.setVisible(bool(has and s.is_admin and pending))
        blocked = self.win.agent.unapproved_mods(srv) if has else []
        if blocked and self.win.agent.settings.hosting_allowed:
            self.approval.text.setText(
                f"<b>{len(blocked)} {'plugin' if word == 'Plugins' else 'mod'}(s) pas encore autorisé(s) sur ce "
                "PC.</b> Il n'hébergera pas ce serveur tant que tu ne les as pas vus et autorisés.")
        self.approval.setVisible(bool(blocked) and self.win.agent.settings.hosting_allowed)
        running = has and srv["state"] == "running"
        self._refresh_pregen(s, running)
        crash = srv.get("last_crash") if has and s.is_admin else None
        if crash:
            at = datetime.fromisoformat(crash["at"]).astimezone().strftime("%d/%m %H:%M")
            self.crash.text.setText(f"<b>Dernier plantage ({at}).</b> {html.escape(crash['message'])}")
            in_list = any(m.get("project_id") == crash.get("mod_project_id") for m in mods)
            self.crash_btn.setText(f"Retirer {crash['mod_name']}…" if crash.get("mod_name") else "Retirer ce mod…")
            self.crash_btn.setVisible(bool(crash.get("mod_project_id")) and in_list)
        self.crash.setVisible(bool(crash))
        remote = running and not running_here
        admin_remote = remote and s.can_moderate
        self.command.setEnabled(running_here or admin_remote)
        self.send_btn.setEnabled(running_here or admin_remote)
        self.command.setPlaceholderText("Commande Minecraft (ex. : whitelist add Steve)" if running_here else
                                        "Message à tous les joueurs (annonce)")
        self.console_mode.setVisible(bool(remote and s.can_moderate))
        journal = remote and s.can_moderate and self.console_mode.currentData() == "log"
        if running_here:
            self.console_label.setText("Console (ce PC héberge le serveur)")
        elif journal:
            self.console_label.setText("Journal du serveur (lecture seule, quelques secondes de décalage)")
        elif remote:
            self.console_label.setText("Discussion en jeu (derniers messages, quelques secondes de décalage)")
        else:
            self.console_label.setText("Console : disponible quand ce PC héberge le serveur")
        if journal:
            text = "\n".join(f"[{_fmt_seconds(l['at'])}] {l['text']}" for l in s.console_log)
        elif remote:
            lines = (s.activity or {}).get("lines", [])
            text = "\n".join(f"[{_fmt_clock(l['at'])}] " + (f"<{l['player']}> " if l.get("player") else "")
                             + l["text"] for l in lines)
        if remote:
            if self.console.toPlainText() != text:
                self.console.setPlainText(text)
                bar = self.console.verticalScrollBar()
                bar.setValue(bar.maximum())
        self._remote_view = remote
        chosen = self._selected_player()
        for kind, btn in self.admin_buttons.items():
            allowed = s.can_moderate if kind in ("kick", "ban") else s.is_admin
            btn.setVisible(bool(has and allowed))
            btn.setEnabled(bool(running and allowed and chosen))
        self.pardon_btn.setVisible(bool(has and s.can_moderate))
        self.pardon_btn.setEnabled(bool(running and s.can_moderate))
        if (not here or s.hosting_phase != "preparing") and s.migration_phase != "prefetching":
            self.progress.setVisible(False)
            self.progress_label.setText(PHASE_LABELS.get(s.hosting_phase, "") if here else "")
        st = s.stats if running_here else {}
        self.cpu.set(f"{st['cpu_percent']:.0f} %" if st.get("cpu_percent") is not None else "—")
        self.ram.set(f"{st['ram_mb']} Mo" if st.get("ram_mb") is not None else "—")
        tps = st.get("tps")
        mspt = st.get("mspt")
        hint = f"{mspt:.1f} ms par tick" if mspt is not None else ""
        if tps is not None and tps < 17:
            hint += (" — le serveur ralentit : moins de joueurs ou de fermes actives, une distance de "
                     "simulation plus courte, ou plus de RAM (Configuration) peuvent aider.")
        self.tps.set(f"{tps:.1f}" if tps is not None else "—", hint)
        names = s.players if running_here else (s.activity or {}).get("players", []) if remote else []
        if [self.players.item(i).text() for i in range(self.players.count())] != names:
            self.players.clear()
            self.players.addItems(names)


class GroupPage(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Groupe", "Qui joue, quels PC peuvent héberger, dans quel ordre."))
        row = QHBoxLayout()
        self.invite_btn = QPushButton("Inviter quelqu'un")
        self.invite_btn.setObjectName("Primary")
        self.invite_btn.clicked.connect(win.create_invite)
        join = QPushButton("Rejoindre un groupe")
        join.clicked.connect(win.join_group)
        create = QPushButton("Créer un groupe")
        create.clicked.connect(win.create_group)
        row.addWidget(self.invite_btn)
        row.addWidget(join)
        row.addWidget(create)
        row.addStretch()
        layout.addLayout(row)

        self.hosts_box = hosts_box = QGroupBox("PC pouvant héberger (le plus prioritaire disponible est choisi)")
        hb = QVBoxLayout(hosts_box)
        self.hosts = QTableWidget(0, 6)
        self.hosts.setHorizontalHeaderLabels(["PC", "Propriétaire", "Connecté", "Hébergement", "Priorité",
                                              "Mesures"])
        self.hosts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.hosts.verticalHeader().setVisible(False)
        self.hosts.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hb.addWidget(self.hosts)
        layout.addWidget(hosts_box)

        members_box = QGroupBox("Membres")
        mb = QVBoxLayout(members_box)
        self.members = QTableWidget(0, 3)
        self.members.setHorizontalHeaderLabels(["Nom", "Rôle", "Peut héberger"])
        self.members.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.members.verticalHeader().setVisible(False)
        self.members.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        mb.addWidget(self.members)
        layout.addWidget(members_box)

        history_box = QGroupBox("Historique (hébergement, démarrages, incidents)")
        hl = QVBoxLayout(history_box)
        self.history = QListWidget()
        hl.addWidget(self.history)
        layout.addWidget(history_box)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))

    def refresh(self, s: AppState) -> None:
        self.invite_btn.setEnabled(s.is_admin)
        best = bool(s.server and s.server.get("host_choice") == "best")
        self.hosts_box.setTitle("PC pouvant héberger (le plus performant disponible est choisi)" if best else
                                "PC pouvant héberger (le plus prioritaire disponible est choisi)")
        # Rebuild tables only when data changed, so a periodic refresh never resets a field the
        # user is editing.
        snapshot = (repr(s.hosts), repr(s.members), s.is_admin)
        if snapshot != getattr(self, "_snapshot", None):
            self._snapshot = snapshot
            self._fill_tables(s)
        self.history.clear()
        for e in s.events:
            when = datetime.fromisoformat(e["created_at"]).astimezone().strftime("%d/%m %H:%M:%S")
            who = f" — {s.host_name(e['host_id'])}" if e.get("host_id") else ""
            why = (e.get("payload") or {}).get("explanation")
            self.history.addItem(f"{when}  {event_label(e['type'])}{who}" + (f" : {why}" if why else ""))

    def _fill_tables(self, s: AppState) -> None:
        self.hosts.setRowCount(len(s.hosts))
        for r, h in enumerate(s.hosts):
            self.hosts.setItem(r, 0, QTableWidgetItem(h["display_name"]))
            self.hosts.setItem(r, 1, QTableWidgetItem(h["owner"]))
            self.hosts.setItem(r, 2, QTableWidgetItem("● en ligne" if h["online"] else "hors ligne"))
            allowed = h["hosting_allowed"] and h["can_host"] and h["enabled"]
            self.hosts.setItem(r, 3, QTableWidgetItem("autorisé" if allowed else "désactivé"))
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setValue(h["priority"])
            spin.setEnabled(s.is_admin)
            spin.setToolTip("100 = toujours prioritaire quand il est disponible")
            spin.editingFinished.connect(lambda hid=h["host_id"], sp=spin: self.win.set_priority(hid, sp.value()))
            self.hosts.setCellWidget(r, 4, spin)
            item = QTableWidgetItem(measures_text(h))
            item.setToolTip("Ping vers le VPS, débit montant vers le VPS (ce que reçoivent les joueurs), vitesse "
                            "d'un cœur du processeur (×1 = un PC de bureau récent), et note sur 100 pour ce "
                            "serveur. Mesurés par chaque PC quand il n'héberge pas.")
            self.hosts.setItem(r, 5, item)
        self.members.setRowCount(len(s.members))
        owner = bool(s.group and s.group.get("role") == "owner")
        for r, m in enumerate(s.members):
            self.members.setItem(r, 0, QTableWidgetItem(m["username"]))
            # Owners name admins; admins name moderators (the control plane checks it again).
            choices = ["member", "moderator"] + (["admin"] if owner else [])
            editable = (s.is_admin and m["role"] != "owner" and m["role"] in choices
                        and not (m["role"] == "admin" and not owner))
            if editable:
                combo = QComboBox()
                for value in choices:
                    combo.addItem(ROLE_LABELS[value], value)
                combo.setCurrentIndex(combo.findData(m["role"]))
                combo.setToolTip("Modérateur : expulse, bannit, débannit et fait des annonces, sans pouvoir "
                                 "changer la configuration ni les sauvegardes.")
                combo.activated.connect(lambda _=0, uid=m["user_id"], c=combo: self.win.set_role(uid, c.currentData()))
                self.members.setCellWidget(r, 1, combo)
            else:
                self.members.removeCellWidget(r, 1)
                self.members.setItem(r, 1, QTableWidgetItem(ROLE_LABELS.get(m["role"], m["role"])))
            box = QCheckBox()
            box.setChecked(m["can_host"])
            box.setEnabled(s.is_admin and m["role"] != "owner")
            box.toggled.connect(lambda checked, uid=m["user_id"]: self.win.set_can_host(uid, checked))
            self.members.setCellWidget(r, 2, box)


KIND_LABELS = {"final": "Fin de session", "hot": "Automatique", "manual": "Manuelle", "restore": "Restauration"}


class StoragePage(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title(
            "Stockage", "Le monde est sauvegardé sur le serveur central à chaque fin de session et "
                        "automatiquement pendant le jeu. Chaque PC hôte récupère la dernière version vérifiée."))
        row = QHBoxLayout()
        self.world = Card("Monde (dernière sauvegarde)")
        self.vps = Card("Espace utilisé sur le VPS")
        self.disk = Card("Espace libre sur ce PC")
        for c in (self.world, self.vps, self.disk):
            row.addWidget(c)
        layout.addLayout(row)

        actions = QHBoxLayout()
        self.backup_btn = QPushButton("Sauvegarder maintenant")
        self.backup_btn.setObjectName("Primary")
        self.backup_btn.setToolTip("Disponible quand ce PC héberge le serveur.")
        self.backup_btn.clicked.connect(win.backup_now)
        self.restore_btn = QPushButton("Restaurer cette sauvegarde…")
        self.restore_btn.clicked.connect(self._restore)
        self.pin_btn = QPushButton("Marquer comme importante")
        self.pin_btn.clicked.connect(self._pin)
        self.open_btn = QPushButton("Ouvrir le dossier")
        self.open_btn.clicked.connect(self._open)
        self.export_btn = QPushButton("Télécharger (.zip)…")
        self.export_btn.setToolTip("Une copie de cette sauvegarde, à garder ou à ouvrir en solo "
                                   "(à décompresser dans .minecraft\\saves).")
        self.export_btn.clicked.connect(self._export)
        self.import_btn = QPushButton("Importer un monde…")
        self.import_btn.setToolTip("Remplacer le monde par un monde solo ou celui d'un ancien serveur. "
                                   "Le monde actuel reste dans l'historique.")
        self.import_btn.clicked.connect(win.import_world)
        self.new_world_btn = QPushButton("Nouveau monde…")
        self.new_world_btn.clicked.connect(win.new_world)
        for b in (self.backup_btn, self.restore_btn, self.pin_btn, self.export_btn, self.import_btn,
                  self.new_world_btn):
            actions.addWidget(b)
        actions.addStretch()
        actions.addWidget(self.open_btn)
        layout.addLayout(actions)

        box = QGroupBox("Historique des sauvegardes")
        bl = QVBoxLayout(box)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Type", "Taille", "PC", "Importante"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(lambda: self.refresh(self.win.state))
        bl.addWidget(self.table)
        self.note = QLabel("Conservées : les 5 dernières, une par heure sur 24 h, une par jour sur 7 jours, "
                           "et toutes celles marquées importantes. Une restauration ajoute une nouvelle "
                           "version : rien n'est effacé.")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        bl.addWidget(self.note)
        layout.addWidget(box, 1)
        self._shown: list[str] = []

    def selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        vid = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((v for v in self.win.state.versions if v["version_id"] == vid), None)

    def _restore(self) -> None:
        version = self.selected()
        if version:
            self.win.restore_version(version)

    def _export(self) -> None:
        version = self.selected()
        if version:
            self.win.export_version(version)

    def _pin(self) -> None:
        version = self.selected()
        if version:
            self.win.pin_version(version, not version["pinned"])

    def _open(self) -> None:
        srv = self.win.state.server
        if srv:
            path = self.win.agent.layout.server_dir(srv["server_id"])
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)  # noqa: S606 - opens Explorer on our own data folder

    def refresh(self, s: AppState) -> None:
        srv = s.server
        self.open_btn.setEnabled(srv is not None)
        here = bool(srv and s.hosting_server == srv["server_id"] and s.hosting_phase == "running")
        self.backup_btn.setEnabled(here)
        head = next((v for v in s.versions if srv and v["version_id"] == srv.get("head_version_id")), None)
        self.world.set(human_bytes(head["total_size"]) if head else "Aucune sauvegarde",
                       _fmt_time(head["created_at"]) if head else "")
        if s.storage:
            used, quota = s.storage["used_bytes"], s.storage["quota_bytes"]
            self.vps.set(human_bytes(used), f"sur {human_bytes(quota)} autorisés ({used * 100 // max(quota, 1)} %)")
        self.disk.set(f"{self.win.agent._resources.disk_free_mb / 1024:.0f} Go")

        ids = [v["version_id"] for v in s.versions]
        if ids != self._shown or getattr(self, "_pins", None) != [v["pinned"] for v in s.versions]:
            self._shown, self._pins = ids, [v["pinned"] for v in s.versions]
            selected = self.selected()
            self.table.setRowCount(len(s.versions))
            for r, v in enumerate(s.versions):
                date = QTableWidgetItem(_fmt_time(v["created_at"]))
                date.setData(Qt.ItemDataRole.UserRole, v["version_id"])
                current = srv and v["version_id"] == srv.get("head_version_id")
                kind = KIND_LABELS.get(v["kind"], v["kind"]) + (" · actuelle" if current else "")
                self.table.setItem(r, 0, date)
                self.table.setItem(r, 1, QTableWidgetItem(kind))
                self.table.setItem(r, 2, QTableWidgetItem(human_bytes(v["total_size"])))
                self.table.setItem(r, 3, QTableWidgetItem(s.host_name(v["host_id"]) if v["host_id"] else "—"))
                self.table.setItem(r, 4, QTableWidgetItem("★ " + (v["label"] or "") if v["pinned"] else ""))
                if selected and v["version_id"] == selected["version_id"]:
                    self.table.selectRow(r)
        chosen = self.selected()
        stopped = bool(srv and srv["state"] == "offline" and not srv["desired_running"])
        self.restore_btn.setEnabled(bool(chosen and s.is_admin and stopped
                                         and chosen["version_id"] != srv.get("head_version_id")))
        self.restore_btn.setToolTip("" if stopped else "Arrête d'abord le serveur.")
        self.pin_btn.setEnabled(bool(chosen and s.is_admin))
        self.export_btn.setEnabled(chosen is not None)
        self.import_btn.setEnabled(bool(srv and s.is_admin and stopped))
        self.import_btn.setToolTip("Remplacer le monde par un monde solo ou celui d'un ancien serveur. "
                                   "Le monde actuel reste dans l'historique." if stopped else
                                   "Arrête d'abord le serveur.")
        self.new_world_btn.setEnabled(bool(srv and s.is_admin and stopped))
        self.new_world_btn.setToolTip("Recommencer avec un monde tout neuf, graine au hasard ou choisie. Le "
                                      "monde actuel reste dans l'historique." if stopped else
                                      "Arrête d'abord le serveur.")
        self.pin_btn.setText("Retirer « importante »" if chosen and chosen["pinned"] else "Marquer comme importante")


WEEKDAYS = ("Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim")
STATS_PERIODS = ((7, "7 derniers jours"), (30, "30 derniers jours"), (90, "3 derniers mois"),
                 (365, "12 derniers mois"))
PERF_PERIODS = ((1, "Dernière heure"), (6, "6 dernières heures"), (24, "24 dernières heures"), (168, "7 derniers jours"))


def _hours(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


class StatsPage(QWidget):
    """Play time per player, busy hours, hosting time per PC (docs/ARCHITECTURE.md 4.13)."""

    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        self._key: tuple | None = None
        self._loaded_at = 0.0
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Statistiques", "Qui joue, quand, et quel PC héberge. Comptées par le "
                                    "serveur central pendant que le serveur tourne."))
        row = QHBoxLayout()
        self.period = QComboBox()
        for days, label in STATS_PERIODS:
            self.period.addItem(label, days)
        self.period.setCurrentIndex(1)
        self.period.currentIndexChanged.connect(lambda _: self.load(force=True))
        row.addWidget(QLabel("Période"))
        row.addWidget(self.period)
        row.addStretch()
        layout.addLayout(row)
        cards = QHBoxLayout()
        self.online = Card("Serveur en ligne")
        self.top = Card("Plus gros joueur")
        self.peak = Card("Heure la plus animée")
        for c in (self.online, self.top, self.peak):
            cards.addWidget(c)
        layout.addLayout(cards)

        perf_box = QGroupBox("Performances")
        pb = QVBoxLayout(perf_box)
        perf_row = QHBoxLayout()
        legend = QLabel("Ligne pleine : la moyenne. Ligne pâle : le pire moment (TPS le plus bas, tick le plus "
                        "long). Un serveur fluide tient 20 TPS et moins de 50 ms par tick.")
        legend.setObjectName("Muted")
        legend.setWordWrap(True)
        self.perf_period = QComboBox()
        for hours, label in PERF_PERIODS:
            self.perf_period.addItem(label, hours)
        self.perf_period.setCurrentIndex(2)
        self.perf_period.currentIndexChanged.connect(lambda _: self.load_perf(force=True))
        perf_row.addWidget(legend, 1)
        perf_row.addWidget(self.perf_period)
        pb.addLayout(perf_row)
        charts = QGridLayout()
        self.tps_chart = LineChart("TPS (ticks par seconde)", "", COLORS["ok"], fixed_max=20.0)
        self.mspt_chart = LineChart("Durée d'un tick", "ms", COLORS["warn"])
        self.ram_chart = LineChart("Mémoire de Minecraft", "Mo", COLORS["accent"], decimals=0)
        self.cpu_chart = LineChart("Processeur (Minecraft)", "%", COLORS["busy"], decimals=0)
        for i, chart in enumerate((self.tps_chart, self.mspt_chart, self.ram_chart, self.cpu_chart)):
            charts.addWidget(chart, i // 2, i % 2)
        pb.addLayout(charts)
        spark_row = QHBoxLayout()
        self.spark_label = QLabel()
        self.spark_label.setWordWrap(True)
        self.spark_label.setOpenExternalLinks(True)
        self.spark_label.setTextFormat(Qt.TextFormat.RichText)
        self.spark_btn = QPushButton("Analyser le lag (spark)…")
        self.spark_btn.setToolTip("Mesure pendant une minute ce qui coûte le plus au serveur : mods, fermes, "
                                  "entités, avec leurs coordonnées.")
        self.spark_btn.clicked.connect(win.start_profile)
        spark_row.addWidget(self.spark_label, 1)
        spark_row.addWidget(self.spark_btn)
        pb.addLayout(spark_row)
        layout.addWidget(perf_box)
        self._perf_key: tuple | None = None
        self._perf_at = 0.0

        players_box = QGroupBox("Temps de jeu par joueur")
        pl = QVBoxLayout(players_box)
        self.players = QTableWidget(0, 3)
        self.players.setHorizontalHeaderLabels(["Joueur", "Temps de jeu", "Dernière partie"])
        self.players.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.players.verticalHeader().setVisible(False)
        self.players.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.players.setMinimumHeight(180)
        pl.addWidget(self.players)
        layout.addWidget(players_box)

        hours_box = QGroupBox("Heures de pointe")
        hl = QVBoxLayout(hours_box)
        legend = QLabel("Nombre moyen de joueurs quand le serveur est en ligne, par jour et par heure.")
        legend.setObjectName("Muted")
        legend.setWordWrap(True)
        hl.addWidget(legend)
        self.hours = QTableWidget(7, 24)
        self.hours.setVerticalHeaderLabels(list(WEEKDAYS))
        self.hours.setHorizontalHeaderLabels([str(h) for h in range(24)])
        self.hours.horizontalHeader().setMinimumSectionSize(20)
        self.hours.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.hours.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.hours.verticalHeader().setDefaultSectionSize(26)
        self.hours.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hours.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.hours.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hours.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hours.setFixedHeight(7 * 26 + self.hours.horizontalHeader().sizeHint().height() + 6)
        hl.addWidget(self.hours)
        layout.addWidget(hours_box)

        hosts_box = QGroupBox("Temps d'hébergement par PC")
        hb = QVBoxLayout(hosts_box)
        self.hosts = QTableWidget(0, 4)
        self.hosts.setHorizontalHeaderLabels(["PC", "Propriétaire", "Temps", "Part"])
        self.hosts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.hosts.verticalHeader().setVisible(False)
        self.hosts.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hosts.setMinimumHeight(120)
        hb.addWidget(self.hosts)
        layout.addWidget(hosts_box)
        self.error = QLabel()
        self.error.setObjectName("Muted")
        layout.addWidget(self.error)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))

    def refresh(self, s: AppState) -> None:
        self.load()
        self.load_perf()
        self._refresh_spark(s)

    def _refresh_spark(self, s: AppState) -> None:
        srv = s.server
        running = bool(srv and srv["state"] == "running")
        self.spark_btn.setVisible(bool(srv and s.is_admin and srv["loader"] in ("fabric", "paper")))
        profile = (srv or {}).get("profile") if running else None
        busy = bool(profile and profile["state"] == "running")
        self.spark_btn.setEnabled(running and not busy)
        if not running:
            self.spark_label.setText("L'analyse du lag se lance quand le serveur tourne."
                                     if srv and s.is_admin else "")
        elif profile is None:
            self.spark_label.setText("")
        elif busy:
            self.spark_label.setText("Analyse en cours : spark mesure le serveur pendant une minute…")
        elif profile["state"] == "done":
            url = html.escape(profile["url"])
            self.spark_label.setText(f"Rapport prêt : <a href=\"{url}\">{url}</a> — l'onglet « Platform » / "
                                     "« World » montre les régions avec le plus d'entités (fermes), avec leurs "
                                     "coordonnées.")
        else:
            self.spark_label.setText("L'analyse n'a pas abouti (envoi du rapport impossible) : réessaie plus tard.")

    def load_perf(self, force: bool = False) -> None:
        srv = self.win.state.server
        if srv is None:
            return
        key = (srv["server_id"], self.perf_period.currentData())
        if not force and key == self._perf_key and time.monotonic() - self._perf_at < 60:
            return
        self._perf_key, self._perf_at = key, time.monotonic()
        self.win.bridge.run(self.win.agent.api.performance(srv["server_id"], key[1]), on_ok=self._fill_perf,
                            on_error=lambda e: None)

    def _fill_perf(self, perf: dict[str, Any]) -> None:
        end = time.time()
        span = (end - perf["hours"] * 3600, end)
        pts = [(datetime.fromisoformat(p["at"]).timestamp() + perf["step_s"] / 2, p) for p in perf["points"]]
        for chart, avg, worst in ((self.tps_chart, "tps", "tps_min"), (self.mspt_chart, "mspt", "mspt_max"),
                                  (self.ram_chart, "ram_used_mb", None), (self.cpu_chart, "cpu_percent", None)):
            chart.set_series([(t, p.get(avg), p.get(worst) if worst else None) for t, p in pts], span,
                             perf["step_s"])

    def load(self, force: bool = False) -> None:
        srv = self.win.state.server
        if srv is None:
            return
        key = (srv["server_id"], self.period.currentData())
        if not force and key == self._key and time.monotonic() - self._loaded_at < 60:
            return
        self._key, self._loaded_at = key, time.monotonic()
        offset = datetime.now().astimezone().utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset else 0
        self.win.bridge.run(self.win.agent.api.stats(srv["server_id"], key[1], minutes), on_ok=self._fill,
                            on_error=lambda e: self.error.setText("Statistiques indisponibles pour le moment."))

    def _fill(self, st: dict[str, Any]) -> None:
        self.error.setText("" if st["online_seconds"] else
                           "Rien encore sur cette période : les statistiques se remplissent pendant que le "
                           "serveur tourne.")
        self.online.set(_hours(st["online_seconds"]))
        players = st["players"]
        self.top.set(players[0]["player"] if players else "—",
                     _hours(players[0]["seconds"]) if players else "")
        self.players.setRowCount(len(players))
        for r, p in enumerate(players):
            self.players.setItem(r, 0, QTableWidgetItem(p["player"]))
            self.players.setItem(r, 1, QTableWidgetItem(_hours(p["seconds"])))
            last = p.get("last_seen_at")
            self.players.setItem(r, 2, QTableWidgetItem(
                datetime.fromisoformat(last).astimezone().strftime("%d/%m %H:%M") if last else "—"))
        busiest = max((h for h in st["hours"] if h["average_players"]), key=lambda h: h["average_players"],
                      default=None)
        top = busiest["average_players"] if busiest else 0
        for h in st["hours"]:
            avg = h["average_players"]
            item = QTableWidgetItem("" if avg is None else f"{avg:.1f}".replace(".", ","))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if avg is None:
                item.setToolTip("Serveur jamais en ligne à cette heure-là")
            else:
                alpha = 40 + int(200 * avg / top) if top else 40
                item.setBackground(QBrush(QColor(63, 178, 127, alpha)))
                item.setToolTip(f"{WEEKDAYS[h['weekday']]} {h['hour']}h : {avg:.1f} joueur(s) en moyenne, "
                                f"serveur en ligne {_hours(h['online_seconds'])}")
            self.hours.setItem(h["weekday"], h["hour"], item)
        self.peak.set(f"{WEEKDAYS[busiest['weekday']]} vers {busiest['hour']}h" if busiest else "—",
                      f"{top:.1f} joueur(s) en moyenne".replace(".", ",") if busiest else "")
        hosts = st["hosts"]
        total = sum(h["seconds"] for h in hosts) or 1
        self.hosts.setRowCount(len(hosts))
        for r, h in enumerate(hosts):
            self.hosts.setItem(r, 0, QTableWidgetItem(h["display_name"]))
            self.hosts.setItem(r, 1, QTableWidgetItem(h["owner"]))
            self.hosts.setItem(r, 2, QTableWidgetItem(_hours(h["seconds"])))
            self.hosts.setItem(r, 3, QTableWidgetItem(f"{h['seconds'] * 100 / total:.0f} %"))


class SettingsPage(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        agent = win.agent
        settings = agent.settings
        total = agent._resources.total_ram_mb
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Paramètres"))

        account = QGroupBox("Compte")
        af = QFormLayout(account)
        self.username = QLabel()
        self.api_url = QLabel()
        logout = QPushButton("Se déconnecter")
        logout.clicked.connect(win.logout)
        password = QPushButton("Changer le mot de passe…")
        password.clicked.connect(win.change_password)
        account_buttons = QHBoxLayout()
        account_buttons.addWidget(password)
        account_buttons.addWidget(logout)
        account_buttons.addStretch()
        af.addRow("Connecté en tant que", self.username)
        af.addRow("Serveur central", self.api_url)
        af.addRow("", account_buttons)
        layout.addWidget(account)

        pc = QGroupBox("Ce PC")
        pf = QFormLayout(pc)
        self.display_name = QLineEdit(settings.display_name)
        self.display_name.setMaxLength(64)
        self.hosting = QCheckBox("Ce PC peut héberger le serveur du groupe")
        self.hosting.setChecked(settings.hosting_allowed)
        self.auto_accept = QCheckBox("Accepter automatiquement (sinon, une question s'affiche)")
        self.auto_accept.setChecked(settings.auto_accept_offers)
        self.ram = QSpinBox()
        self.ram.setRange(1024, max(1024, max_allowed_memory_mb(total)))
        self.ram.setSingleStep(512)
        self.ram.setSuffix(" Mo")
        self.ram.setValue(win.agent.memory_cap_mb())
        suggestion = f"{suggest_memory_mb(total) / 1024:.1f}".replace(".0", "").replace(".", ",")
        ram_hint = QLabel(f"Suggestion : {suggestion} Go "
                          f"(ce PC a {total / 1024:.0f} Go ; {OS_NAME} en garde toujours assez).")
        ram_hint.setObjectName("Muted")
        self.priority = QComboBox()
        self.priority.addItem("Normale", "normal")
        self.priority.addItem("Basse (le PC reste fluide pour jouer)", "below_normal")
        self.priority.setCurrentIndex(max(0, self.priority.findData(settings.cpu_priority)))
        self.cpu_limit = QSpinBox()
        self.cpu_limit.setRange(0, 100)
        self.cpu_limit.setSpecialValueText("Aucune limite")
        self.cpu_limit.setSuffix(" % du processeur")
        self.cpu_limit.setValue(settings.cpu_limit_percent or 0)
        if sys.platform == "darwin":
            self.cpu_limit.setEnabled(False)
            self.cpu_limit.setToolTip("Pas disponible sur macOS : seule la priorité s'applique.")
        self.auto_restart = QCheckBox("Redémarrer Minecraft automatiquement après un plantage")
        self.auto_restart.setChecked(settings.auto_restart)
        self.max_restarts = QSpinBox()
        self.max_restarts.setRange(1, 10)
        self.max_restarts.setValue(settings.max_restarts)
        self.max_restarts.setSuffix(" fois en 10 min")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(settings.server_port)
        self.standby = QCheckBox("Garder une copie de secours du monde à jour, pour prendre le relais vite "
                                 "si le PC hôte s'éteint")
        self.standby.setChecked(settings.standby_copy)
        self.standby.setToolTip("Seuls les morceaux du monde qui changent sont téléchargés, après chaque "
                                "sauvegarde automatique.")
        self.autostart = QCheckBox("Lancer l'application à l'ouverture de session (dans la barre des menus)" if sys.platform == "darwin"
                                  else "Lancer l'application au démarrage de Windows (dans la barre des tâches)")
        self.autostart.setChecked(settings.start_with_windows)
        self.autostart.setEnabled(autostart.available())
        if not autostart.available():
            self.autostart.setToolTip("Disponible dans la version installée de l'application.")
        pf.addRow("Nom de ce PC", self.display_name)
        pf.addRow("", self.hosting)
        pf.addRow("", self.auto_accept)
        pf.addRow("", self.standby)
        pf.addRow("RAM max pour Minecraft", self.ram)
        pf.addRow("", ram_hint)
        pf.addRow("Priorité processeur", self.priority)
        pf.addRow("Limite processeur", self.cpu_limit)
        pf.addRow("", self.auto_restart)
        pf.addRow("Redémarrages max", self.max_restarts)
        pf.addRow("Port Minecraft", self.port)
        pf.addRow("", self.autostart)
        save = QPushButton("Enregistrer")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        pf.addRow("", save)
        layout.addWidget(pc)

        app_box = QGroupBox("Application")
        ab = QGridLayout(app_box)
        self.version = QLabel()
        self.version.setWordWrap(True)
        self.update_btn = QPushButton("Installer la mise à jour")
        self.update_btn.setObjectName("Primary")
        self.update_btn.clicked.connect(lambda: win.install_update())
        self.check_btn = QPushButton("Vérifier les mises à jour")
        self.check_btn.clicked.connect(win.check_updates)
        self.check_btn.setEnabled(win.agent.can_update)
        if not win.agent.can_update:
            self.check_btn.setToolTip("Disponible dans la version installée de l'application.")
        #: Result of the last click on the button, shown under the version.
        self.check_result = ""
        self.auto_update = QCheckBox("Installer les mises à jour automatiquement (quand la fenêtre est "
                                     "fermée et que ce PC n'héberge pas le serveur)")
        self.auto_update.setChecked(settings.auto_update)
        self.auto_update.toggled.connect(lambda on: win.save_settings(auto_update=on))
        ab.addWidget(self.version, 0, 0)
        ab.addWidget(self.check_btn, 0, 1)
        ab.addWidget(self.update_btn, 0, 2)
        ab.addWidget(self.auto_update, 1, 0, 1, 3)
        ab.setColumnStretch(0, 1)
        layout.addWidget(app_box)

        eula_box = QGroupBox("Licence Minecraft")
        el = QHBoxLayout(eula_box)
        self.eula = QLabel()
        self.eula.setWordWrap(True)
        self.eula_btn = QPushButton("Lire et accepter l'EULA…")
        self.eula_btn.clicked.connect(win.accept_eula)
        el.addWidget(self.eula, 1)
        el.addWidget(self.eula_btn)
        layout.addWidget(eula_box)

        server_box = QGroupBox("Serveur du groupe")
        sl = QHBoxLayout(server_box)
        self.server_info = QLabel()
        self.server_info.setWordWrap(True)
        self.server_info.setTextFormat(Qt.TextFormat.PlainText)
        self.server_btn = QPushButton("Modifier la configuration…")
        self.server_btn.clicked.connect(win.configure_server)
        self.delete_server_btn = QPushButton("Supprimer ce serveur…")
        self.delete_server_btn.setObjectName("Danger")
        self.delete_server_btn.clicked.connect(win.delete_server)
        sl.addWidget(self.server_info, 1)
        sl.addWidget(self.server_btn)
        sl.addWidget(self.delete_server_btn)
        layout.addWidget(server_box)

        help_box = QGroupBox("Dépannage")
        hl = QHBoxLayout(help_box)
        logs_text = QLabel("Les journaux de l'application aident à comprendre un problème. Ils ne contiennent "
                           "ni mot de passe ni jeton de connexion.")
        logs_text.setWordWrap(True)
        logs_btn = QPushButton("Ouvrir le dossier des journaux")
        logs_btn.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(win.agent.layout.logs))))
        hl.addWidget(logs_text, 1)
        hl.addWidget(logs_btn)
        layout.addWidget(help_box)
        layout.addStretch()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))

    def _save(self) -> None:
        self.win.save_settings(
            display_name=self.display_name.text().strip() or "Mon PC",
            hosting_allowed=self.hosting.isChecked(),
            auto_accept_offers=self.auto_accept.isChecked(),
            standby_copy=self.standby.isChecked(),
            max_ram_mb=self.ram.value(),
            cpu_priority=self.priority.currentData(),
            cpu_limit_percent=self.cpu_limit.value() or None,
            auto_restart=self.auto_restart.isChecked(),
            max_restarts=self.max_restarts.value(),
            server_port=self.port.value(),
            start_with_windows=self.autostart.isChecked(),
        )

    def refresh(self, s: AppState) -> None:
        settings = self.win.agent.settings
        self.username.setText(s.user["username"] if s.user else "—")
        self.api_url.setText(settings.api_url)
        if s.update:
            notes = s.update.get("notes")
            self.version.setText(f"Version {__version__} installée. La version {s.update['version']} est "
                                 "téléchargée et vérifiée." + (f"\n{notes}" if notes else ""))
        else:
            self.version.setText(f"Version {__version__}." + (f" {self.check_result}" if self.check_result else ""))
        self.update_btn.setVisible(bool(s.update))
        if settings.eula_accepted_at:
            when = datetime.fromisoformat(settings.eula_accepted_at).astimezone().strftime("%d/%m/%Y")
            self.eula.setText(f"EULA Minecraft acceptée sur ce PC le {when}.")
            self.eula_btn.setVisible(False)
        else:
            self.eula.setText("EULA Minecraft pas encore acceptée : ce PC ne peut pas héberger.")
            self.eula_btn.setVisible(True)
        srv = s.server
        self.server_btn.setEnabled(bool(srv and s.is_admin))
        owner = bool(s.group and s.group.get("role") == "owner")
        self.delete_server_btn.setVisible(bool(srv and owner))
        stopped = bool(srv and srv["state"] == "offline" and not srv["desired_running"])
        self.delete_server_btn.setEnabled(stopped)
        self.delete_server_btn.setToolTip("Le serveur et toutes ses sauvegardes, définitivement." if stopped else
                                          "Arrête d'abord le serveur.")
        self.server_info.setText(
            "Aucun serveur." if not srv else
            f"{srv['name']} — {LOADER_NAMES.get(srv['loader'], srv['loader'])} {srv['mc_version']}, "
            f"{srv['memory_mb'] // 1024} Go." + ("" if s.is_admin else " (modifiable par un administrateur)"))
