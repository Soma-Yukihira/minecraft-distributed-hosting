"""Dialogs: login, EULA, hosting offer, server configuration, change of host, invitation."""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mdh_client.application import templates
from mdh_client.application.agent import HostAgent
from mdh_client.gui.bridge import Bridge
from mdh_client.gui.texts import error_text
from mdh_client.monitoring.resources import max_allowed_memory_mb, suggest_memory_mb
from mdh_shared.models.api import level_type_value
from mdh_shared.models.enums import Loader

EULA_URL = "https://aka.ms/MinecraftEULA"
PLAYER_NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
#: Automatic failover delays offered (the control plane accepts 5 minutes or more).
FAILOVER_CHOICES_MIN = (1, 2, 5, 10, 15, 30, 60)
BACKUP_CHOICES_MIN = (1, 2, 5, 10, 15, 30, 60)
SLEEP_CHOICES_MIN = (5, 10, 15, 30, 60)
NO_FAILOVER = 0  # combo data for "wait for an admin" (Qt cannot look up a None item)


def _duration(minutes: int) -> str:
    return "1 heure" if minutes == 60 else "1 minute" if minutes == 1 else f"{minutes} minutes"


#: World presets offered (``level-type``): label, preset.
LEVEL_TYPE_CHOICES = (("Normal", "normal"), ("Plat", "flat"), ("Grands biomes", "large_biomes"),
                      ("Amplifié (montagnes géantes)", "amplified"))
_LEGACY_PRESETS = {"default": "normal", "largebiomes": "large_biomes"}


def preset_of(level_type: str | None) -> str:
    """``minecraft:large_biomes``, ``largeBiomes``... -> ``large_biomes``."""
    name = (level_type or "normal").split(":")[-1].lower()
    return _LEGACY_PRESETS.get(name, name)


class WorldFields:
    """Seed, world type and structures: when creating a server, and for a new world."""

    def __init__(self, props: dict[str, Any]) -> None:
        self.seed = QLineEdit(str(props.get("level-seed", "")))
        self.seed.setMaxLength(64)
        self.seed.setPlaceholderText("Vide : au hasard")
        self.seed.setToolTip("Un nombre (comme dans /seed) ou n'importe quel mot. La même graine, la même "
                             "version et le même type donnent le même monde.")
        self.level_type = QComboBox()
        for label, value in LEVEL_TYPE_CHOICES:
            self.level_type.addItem(label, value)
        self.level_type.setCurrentIndex(max(0, self.level_type.findData(preset_of(props.get("level-type")))))
        self.structures = QCheckBox("Villages, temples et autres structures")
        self.structures.setChecked(bool(props.get("generate-structures", True)))

    def add_to(self, form: QFormLayout) -> None:
        form.addRow("Graine du monde", self.seed)
        form.addRow("Type de monde", self.level_type)
        form.addRow("", self.structures)

    def seed_text(self) -> str:
        return "".join(c for c in self.seed.text().strip() if c.isprintable())[:64]

    def properties(self, mc_version: str) -> dict[str, Any]:
        return {"level-seed": self.seed_text(),
                "level-type": level_type_value(self.level_type.currentData(), mc_version),
                "generate-structures": self.structures.isChecked()}


def _every(minutes: int) -> str:
    return {1: "Chaque minute", 60: "Chaque heure"}.get(minutes, f"Toutes les {minutes} minutes")


class LoginDialog(QDialog):
    def __init__(self, bridge: Bridge, agent: HostAgent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent = bridge, agent
        self.setWindowTitle("Minecraft Distributed Hosting — Connexion")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        title = QLabel("Bienvenue !")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel("Connecte-toi pour partager l'hébergement du serveur avec ton groupe.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.url = QLineEdit(agent.settings.api_url)
        self.url.setToolTip("Adresse complète du serveur central (VPS), telle que donnée par l'administrateur,\n"
                            "par exemple https://87.106.117.55:8443#q3mF0k... : la partie après # est\n"
                            "l'empreinte de son certificat, elle garantit que tu parles au bon serveur.")
        self.username = QLineEdit(agent.settings.username or "")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_account = QCheckBox("Créer un nouveau compte")
        self.invite = QLineEdit()
        self.invite.setPlaceholderText("Code reçu d'un membre du groupe")
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Serveur central", self.url)
        form.addRow("Nom d'utilisateur", self.username)
        form.addRow("Mot de passe", self.password)
        form.addRow("", self.new_account)
        self.invite_label = QLabel("Code d'invitation")
        self.confirm_label = QLabel("Confirmer")
        form.addRow(self.confirm_label, self.confirm)
        form.addRow(self.invite_label, self.invite)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setStyleSheet("color: #e35d5d; background: transparent;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.submit = QPushButton("Se connecter")
        self.submit.setObjectName("Primary")
        self.submit.clicked.connect(self._submit)
        layout.addWidget(self.submit)
        self.new_account.toggled.connect(self._toggle)
        self._toggle(False)
        self.password.returnPressed.connect(self._submit)

    def _toggle(self, creating: bool) -> None:
        for w in (self.invite, self.invite_label, self.confirm, self.confirm_label):
            w.setVisible(creating)
        self.submit.setText("Créer le compte" if creating else "Se connecter")

    def _submit(self) -> None:
        self.error.clear()
        username, password = self.username.text().strip(), self.password.text()
        if self.new_account.isChecked() and password != self.confirm.text():
            self.error.setText("Les deux mots de passe sont différents.")
            return
        if self.new_account.isChecked() and len(password) < 10:
            self.error.setText("Le mot de passe doit faire au moins 10 caractères.")
            return
        self.submit.setEnabled(False)

        async def go() -> Any:
            await self.agent.set_api_url(self.url.text().strip())
            if self.new_account.isChecked():
                return await self.agent.register(username, password, self.invite.text().strip() or None)
            return await self.agent.login(username, password)

        def failed(exc: BaseException) -> None:
            self.submit.setEnabled(True)
            self.error.setText(error_text(exc))

        self.bridge.run(go(), on_ok=lambda _: self.accept(), on_error=failed)


class EulaDialog(QDialog):
    """Explicit, informed acceptance. Never pre-checked, never accepted on the user's behalf."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Contrat de licence Minecraft (EULA)")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        text = QLabel(
            "Pour faire tourner un serveur Minecraft sur ce PC, Mojang demande d'accepter son "
            "contrat de licence utilisateur final (EULA).<br><br>"
            f"<a href='{EULA_URL}'>Lire l'EULA Minecraft</a><br><br>"
            "Ton acceptation est enregistrée sur ce PC uniquement, avec sa date.")
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setOpenExternalLinks(True)
        layout.addWidget(text)
        self.check = QCheckBox("J'ai lu et j'accepte l'EULA Minecraft")
        layout.addWidget(self.check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Accepter")
        ok.setEnabled(False)
        self.check.toggled.connect(ok.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def ask_hosting_offer(parent: QWidget, info: dict[str, Any], timeout_s: int = 25) -> bool:
    box = QMessageBox(parent)
    box.setTextFormat(Qt.TextFormat.PlainText)  # the server name comes from another user
    box.setWindowTitle("Héberger le serveur ?")
    reason = {"resume": "reprendre l'hébergement interrompu de",
              "migration": "prendre le relais pour héberger"}.get(info.get("reason", ""), "héberger")
    box.setText(f"Le groupe te propose de {reason} « {info['name']} » sur ce PC.")
    after = ("le serveur reste sur le PC actuel." if info.get("reason") == "migration"
             else "un autre PC sera sollicité.")
    box.setInformativeText(
        f"Minecraft {info['loader']} {info['mc_version']}, {info['memory_mb'] // 1024} Go de RAM.\n"
        f"Sans réponse dans {timeout_s} s, {after}")
    yes = box.addButton("Héberger", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Pas maintenant", QMessageBox.ButtonRole.RejectRole)
    QTimer.singleShot(timeout_s * 1000, box.reject)
    box.exec()
    return box.clickedButton() is yes


class TemplateDialog(QDialog):
    """First step of "Créer un serveur": a ready-made server, or everything by hand."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nouveau serveur")
        self.setMinimumWidth(560)
        self.chosen: templates.Template | None = None
        layout = QVBoxLayout(self)
        intro = QLabel("Choisis un point de départ. Tout reste modifiable ensuite (Configuration, Mods).")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.buttons: dict[str, QPushButton] = {}
        for t in templates.TEMPLATES:
            btn = QPushButton(f"{t.title}\n{t.summary}")
            btn.setObjectName("Template")
            btn.setStyleSheet("text-align: left; padding: 10px;")
            btn.setMinimumHeight(64)
            btn.clicked.connect(lambda _=False, t=t: self._pick(t))
            self.buttons[t.key] = btn
            layout.addWidget(btn)
        custom = QPushButton("Tout choisir moi-même…")
        custom.clicked.connect(lambda: self._pick(None))
        layout.addWidget(custom)
        cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel.rejected.connect(self.reject)
        layout.addWidget(cancel)

    def _pick(self, template: templates.Template | None) -> None:
        self.chosen = template
        self.accept()


class _WheelScrollsThePage(QObject):
    """A list or number under the mouse no longer changes when the page scrolls past it: the wheel
    only changes a field that was clicked first."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and isinstance(watched, QWidget) and not watched.hasFocus():
            event.ignore()  # goes on to the scroll area
            return True
        return False


def _wheel_scrolls_the_page(inner: QWidget) -> None:
    guard = _WheelScrollsThePage(inner)
    for field in [*inner.findChildren(QComboBox), *inner.findChildren(QSpinBox)]:
        field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        field.installEventFilter(guard)


def _fit_to_screen(dialog: QDialog, area: QScrollArea, inner: QWidget) -> None:
    """As tall as its settings, but never taller than the screen: the rest scrolls."""
    area.setMinimumWidth(inner.sizeHint().width() + area.verticalScrollBar().sizeHint().width())
    around = dialog.sizeHint().height() - area.sizeHint().height()
    available = dialog.screen().availableGeometry().height()
    dialog.resize(dialog.sizeHint().width(), min(inner.sizeHint().height() + around, int(available * 0.85)))


class ServerConfigDialog(QDialog):
    def __init__(self, bridge: Bridge, agent: HostAgent, current: dict[str, Any] | None,
                 parent: QWidget | None = None, defaults: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent = bridge, agent
        self.setWindowTitle("Configuration du serveur" if current else "Nouveau serveur")
        self.setMinimumWidth(460)
        self.creating = current is None
        current = current or defaults or {}
        props = current.get("properties", {})
        #: Settings this dialog does not show (seed, set through the API...) are kept as they are.
        self.other_properties = dict(props)
        self.world = WorldFields(props) if self.creating else None
        layout = QVBoxLayout(self)
        # Many settings: they scroll, the buttons stay at the bottom of the window.
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(0, 0, 10, 0)  # room between the fields and the scroll bar
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.name = QLineEdit(current.get("name", "Survie"))
        self.loader = QComboBox()
        for label, value in (("Paper (recommandé : rapide, plugins)", "paper"),
                             ("Vanilla (officiel Mojang)", "vanilla"), ("Fabric (mods)", "fabric")):
            self.loader.addItem(label, value)
        self.loader.setCurrentIndex(max(0, self.loader.findData(current.get("loader", "paper"))))
        self.version = QComboBox()
        self.version.setEditable(True)
        if current.get("mc_version"):
            self.version.addItem(current["mc_version"])
        total = agent._resources.total_ram_mb
        self.memory = QSpinBox()
        self.memory.setRange(1024, max(1024, max_allowed_memory_mb(total)))
        self.memory.setSingleStep(512)
        self.memory.setSuffix(" Mo")
        self.memory.setValue(current.get("memory_mb", suggest_memory_mb(total)))
        self.memory.setToolTip("RAM donnée à Minecraft. Chaque PC hôte la plafonne à ce qu'il peut offrir.")
        self.max_players = QSpinBox()
        self.max_players.setRange(1, 200)
        self.max_players.setValue(int(props.get("max-players", 20)))
        self.view = QSpinBox()
        self.view.setRange(3, 32)
        self.view.setValue(int(props.get("view-distance", 10)))
        self.sim = QSpinBox()
        self.sim.setRange(3, 32)
        self.sim.setValue(int(props.get("simulation-distance", 10)))
        self.difficulty = QComboBox()
        for label, value in (("Paisible", "peaceful"), ("Facile", "easy"), ("Normal", "normal"),
                             ("Difficile", "hard")):
            self.difficulty.addItem(label, value)
        self.difficulty.setCurrentIndex(max(0, self.difficulty.findData(props.get("difficulty", "normal"))))
        self.gamemode = QComboBox()
        for label, value in (("Survie", "survival"), ("Créatif", "creative"), ("Aventure", "adventure")):
            self.gamemode.addItem(label, value)
        self.gamemode.setCurrentIndex(max(0, self.gamemode.findData(props.get("gamemode", "survival"))))
        self.pvp = QCheckBox("Combat entre joueurs (PvP)")
        self.pvp.setChecked(bool(props.get("pvp", True)))
        self.whitelist = QCheckBox("Liste blanche (seuls les joueurs autorisés)")
        self.whitelist.setChecked(bool(props.get("white-list", False)))
        self.motd = QLineEdit(str(props.get("motd", "Un serveur partagé entre amis")))
        self.motd.setMaxLength(100)
        self.offline = QCheckBox("Accepter les comptes non officiels (TLauncher…)")
        self.offline.setChecked(bool(current.get("allow_offline_accounts", False)))
        self.offline.setToolTip("Les comptes ne sont plus vérifiés par Microsoft : seuls les joueurs de la "
                                "liste ci-dessous peuvent entrer.")
        self.personal = QComboBox()
        self.personal.addItem("Une adresse personnelle par joueur (le plus sûr)", True)
        self.personal.addItem("L'adresse du serveur pour tous (pseudo seul vérifié)", False)
        self.personal.setCurrentIndex(0 if current.get("personal_addresses", True) else 1)
        self.personal.setToolTip(
            "Adresse personnelle : chaque joueur a la sienne, personne ne peut prendre le pseudo d'un autre.\n"
            "Adresse du serveur : plus simple à donner, mais quelqu'un qui connaît le pseudo d'un joueur "
            "de la liste peut entrer à sa place.")
        self.host_choice = QComboBox()
        self.host_choice.addItem("Par priorité (réglée dans la page Groupe)", "priority")
        self.host_choice.addItem("Automatique : le PC le plus performant", "best")
        self.host_choice.setCurrentIndex(max(0, self.host_choice.findData(current.get("host_choice", "priority"))))
        self.host_choice.setToolTip(
            "Automatique : le serveur va sur le PC qui le fera le mieux tourner (vitesse du processeur, débit "
            "vers le VPS, ping, mémoire libre), mesuré par chaque PC. Il passe sur un PC nettement meilleur "
            "quand celui-ci revient, selon le réglage ci-dessous. La priorité ne sert plus qu'à départager.")
        self.preemption = QComboBox()
        for label, value in (("Dès que plus personne ne joue (recommandé)", "when_empty"),
                             ("Après avoir prévenu les joueurs", "countdown"),
                             ("Jamais automatiquement", "never")):
            self.preemption.addItem(label, value)
        self.preemption.setCurrentIndex(max(0, self.preemption.findData(current.get("preemption", "when_empty"))))
        self.preemption.setToolTip("Quand un PC plus prioritaire revient alors qu'un autre héberge, le serveur "
                                   "peut revenir sur lui automatiquement.")
        self.preemption_delay = QSpinBox()
        self.preemption_delay.setRange(1, 60)
        self.preemption_delay.setSuffix(" min")
        self.preemption_delay.setValue(max(1, int(current.get("preemption_countdown_s", 300)) // 60))
        self.preemption.currentIndexChanged.connect(
            lambda: self.preemption_delay.setEnabled(self.preemption.currentData() == "countdown"))
        self.preemption_delay.setEnabled(self.preemption.currentData() == "countdown")
        self.failover = QComboBox()
        self.failover.addItem("Attendre qu'un administrateur décide (le plus sûr)", NO_FAILOVER)
        for minutes in FAILOVER_CHOICES_MIN:
            self.failover.addItem(f"Relancer sur un autre PC après {_duration(minutes)}", minutes * 60)
        failover_s = current.get("auto_failover_after_s")
        if failover_s is not None and self.failover.findData(failover_s) < 0:  # set elsewhere (API)
            self.failover.addItem(f"Relancer sur un autre PC après {_duration(failover_s // 60)}", failover_s)
        self.failover.setCurrentIndex(max(0, self.failover.findData(failover_s or NO_FAILOVER)))
        self.failover.setToolTip(
            "Si le PC hôte s'éteint d'un coup (panne, coupure de courant ou d'Internet) :\n"
            "• il reprend sans perte s'il revient avant la relance ;\n"
            "• sinon le serveur repart sur un autre PC depuis la dernière sauvegarde automatique ;\n"
            "  ce qui a été joué depuis est mis de côté sur le PC disparu, jamais supprimé.")
        self.backups = QComboBox()
        for minutes in BACKUP_CHOICES_MIN:
            self.backups.addItem(_every(minutes), minutes * 60)
        backup_s = int(current.get("hot_backup_interval_s", 900))
        if self.backups.findData(backup_s) < 0:  # set elsewhere (API)
            self.backups.addItem(_every(max(1, backup_s // 60)), backup_s)
        self.backups.setCurrentIndex(self.backups.findData(backup_s))
        self.backups.setToolTip(
            "Copie du monde envoyée au serveur central pendant la partie (seuls les morceaux modifiés).\n"
            "Si le PC hôte s'éteint d'un coup, on repart de la dernière : plus c'est fréquent, moins on perd.\n"
            "Sur un très gros monde, une sauvegarde chaque minute peut créer une micro-saccade.")
        self.start_on_join = QCheckBox("Démarrer tout seul quand un joueur essaie de se connecter")
        self.start_on_join.setChecked(bool(current.get("start_on_join")))
        self.start_on_join.setToolTip(
            "Le joueur voit « Le serveur démarre pour toi » et se reconnecte une minute plus tard.\n"
            "Avec la liste blanche, seuls les joueurs autorisés peuvent le réveiller.")
        self.stop_empty = QComboBox()
        self.stop_empty.addItem("Jamais", NO_FAILOVER)
        for minutes in SLEEP_CHOICES_MIN:
            self.stop_empty.addItem(f"Après {_duration(minutes)} sans joueur", minutes * 60)
        empty_s = current.get("stop_when_empty_after_s")
        if empty_s and self.stop_empty.findData(empty_s) < 0:  # set elsewhere (API)
            self.stop_empty.addItem(f"Après {_duration(empty_s // 60)} sans joueur", empty_s)
        self.stop_empty.setCurrentIndex(max(0, self.stop_empty.findData(empty_s or NO_FAILOVER)))
        self.stop_empty.setToolTip("Arrêt propre, avec sauvegarde finale : le PC hôte retrouve sa RAM.")
        self.public_status = QCheckBox("Page de statut dans un navigateur (en ligne ou non, qui joue, adresse)")
        self.public_status.setChecked(bool(current.get("public_status")))
        self.public_status.setToolTip("Pratique pour les amis sans l'application. Toute personne qui connaît "
                                      "l'adresse du serveur peut voir qui joue.")
        self.players = QPlainTextEdit("\n".join(current.get("allowed_players", [])))
        self.players.setPlaceholderText("Un pseudo Minecraft par ligne")
        self.players.setFixedHeight(90)
        self.error = QLabel()
        self.error.setStyleSheet("color: #e35d5d; background: transparent;")
        self.error.setWordWrap(True)
        form.addRow("Nom", self.name)
        form.addRow("Type", self.loader)
        form.addRow("Version", self.version)
        if self.world is not None:
            self.world.add_to(form)
        else:
            seed = str(props.get("level-seed", ""))
            world = QLabel(("Graine : " + seed if seed else "Graine choisie au hasard") +
                           ". Pour recommencer avec un autre monde : bouton « Nouveau monde… » (page Serveur).")
            world.setObjectName("Muted")
            world.setWordWrap(True)
            world.setTextFormat(Qt.TextFormat.PlainText)
            form.addRow("Monde", world)
        form.addRow("RAM", self.memory)
        form.addRow("Joueurs max", self.max_players)
        form.addRow("Distance de vue", self.view)
        form.addRow("Distance de simulation", self.sim)
        form.addRow("Difficulté", self.difficulty)
        form.addRow("Mode de jeu", self.gamemode)
        form.addRow("Message d'accueil", self.motd)
        form.addRow("", self.pvp)
        form.addRow("", self.whitelist)
        form.addRow("", self.offline)
        form.addRow("Connexion", self.personal)
        form.addRow("Joueurs autorisés", self.players)
        form.addRow("Choix du PC hôte", self.host_choice)
        form.addRow("Retour d'un meilleur PC", self.preemption)
        form.addRow("Délai d'annonce", self.preemption_delay)
        form.addRow("Si le PC hôte disparaît", self.failover)
        form.addRow("Sauvegarde automatique", self.backups)
        form.addRow("Réveil", self.start_on_join)
        form.addRow("Mise en veille", self.stop_empty)
        form.addRow("Statut public", self.public_status)
        area.setWidget(inner)
        layout.addWidget(area, 1)
        layout.addWidget(self.error)
        self.offline.clicked.connect(self._offline_clicked)
        self.personal.activated.connect(self._personal_chosen)
        self._sync_whitelist()
        note = QLabel("Les joueurs autorisés sont pris en compte tout de suite ; les autres changements, au "
                      "prochain démarrage du serveur.")
        note.setObjectName("Muted")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _wheel_scrolls_the_page(inner)
        _fit_to_screen(self, area, inner)
        self.loader.currentIndexChanged.connect(self._load_versions)
        self._load_versions()

    def _offline_clicked(self, checked: bool) -> None:
        if checked:
            answer = QMessageBox.warning(
                self, "Comptes non officiels",
                "Le serveur ne vérifiera plus les comptes auprès de Microsoft.\n\n"
                "• Les comptes officiels pourront toujours se connecter.\n"
                "• N'importe qui pourrait se faire passer pour un joueur de la liste s'il connaît son "
                "pseudo et l'adresse du serveur : la liste blanche est donc obligatoire, et une "
                "protection par mot de passe sera nécessaire avant d'ouvrir le serveur à Internet.\n"
                "• L'inventaire et la position des joueurs dépendent de ce réglage : choisis-le avant "
                "de commencer une vraie partie.\n\nActiver quand même ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.offline.setChecked(False)
        self._sync_whitelist()

    def _sync_whitelist(self) -> None:
        offline = self.offline.isChecked()
        if offline:
            self.whitelist.setChecked(True)
        self.whitelist.setEnabled(not offline)
        self.personal.setEnabled(offline)

    def _personal_chosen(self, index: int) -> None:
        if self.personal.itemData(index) is False:
            answer = QMessageBox.warning(
                self, "Adresse du serveur pour tous",
                "Les joueurs entreront avec l'adresse du serveur, comme avec des comptes officiels. Un "
                "joueur pas encore dans la liste envoie une demande d'accès en se connectant.\n\n"
                "Mais rien ne vérifie le pseudo : quelqu'un qui connaît l'adresse et le pseudo d'un "
                "joueur de la liste peut entrer à sa place (et utiliser son inventaire, ses droits "
                "d'opérateur…).\n\nUtiliser l'adresse du serveur pour tous ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.personal.setCurrentIndex(0)

    def _player_names(self) -> list[str] | None:
        names = [n.strip() for n in self.players.toPlainText().replace(",", "\n").splitlines() if n.strip()]
        bad = [n for n in names if not PLAYER_NAME.fullmatch(n)]
        if bad:
            self.error.setText("Pseudo(s) invalide(s) : " + ", ".join(bad[:5])
                               + " (3 à 16 caractères : lettres, chiffres, _).")
            return None
        return names

    def accept(self) -> None:
        names = self._player_names()
        if names is None:
            return
        if self.whitelist.isChecked() and not names:
            self.error.setText("Avec la liste blanche, ajoute au moins ton propre pseudo Minecraft, "
                               "sinon personne ne pourra entrer.")
            return
        super().accept()

    def _load_versions(self) -> None:
        loader = Loader(self.loader.currentData())
        keep = self.version.currentText()

        def fill(versions: list[str]) -> None:
            self.version.clear()
            self.version.addItems(versions[:60])
            index = self.version.findText(keep)
            self.version.setCurrentIndex(index if index >= 0 else 0)

        self.bridge.run(self.agent.available_versions(loader), on_ok=fill,
                        on_error=lambda e: self.version.setEditText(keep))

    def values(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip() or "Serveur",
            "loader": self.loader.currentData(),
            "mc_version": self.version.currentText().strip(),
            "memory_mb": self.memory.value(),
            "properties": {
                **self.other_properties,
                **(self.world.properties(self.version.currentText().strip()) if self.world else {}),
                "max-players": self.max_players.value(),
                "view-distance": self.view.value(),
                "simulation-distance": self.sim.value(),
                "difficulty": self.difficulty.currentData(),
                "gamemode": self.gamemode.currentData(),
                "pvp": self.pvp.isChecked(),
                "white-list": self.whitelist.isChecked(),
                "motd": self.motd.text().strip()[:100],
            },
            "allow_offline_accounts": self.offline.isChecked(),
            "personal_addresses": bool(self.personal.currentData()),
            "allowed_players": self._player_names() or [],
            "preemption": self.preemption.currentData(),
            "host_choice": self.host_choice.currentData(),
            "preemption_countdown_s": self.preemption_delay.value() * 60,
            "auto_failover_after_s": self.failover.currentData() or None,
            "hot_backup_interval_s": self.backups.currentData(),
            "start_on_join": self.start_on_join.isChecked(),
            "stop_when_empty_after_s": self.stop_empty.currentData() or None,
            "public_status": self.public_status.isChecked(),
        }


class NewWorldDialog(QDialog):
    """Start the server over with a new world; the current one stays in the history."""

    def __init__(self, server: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nouveau monde")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"« {server['name']} » repartira d'un monde tout neuf au prochain démarrage : nouvelle carte, "
            "inventaires et progrès remis à zéro.<br><br>"
            "<b>Rien n'est perdu</b> : le monde actuel est gardé dans l'historique (page Stockage, marqué "
            "important) et peut être restauré. Les joueurs autorisés, les opérateurs, les mods et leurs "
            "réglages restent.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        # Start from a random seed: copying the current one would give the same world again.
        self.world = WorldFields({**server.get("properties", {}), "level-seed": ""})
        self.world.add_to(form)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        create = buttons.addButton("Créer le nouveau monde", QDialogButtonBox.ButtonRole.AcceptRole)
        create.setObjectName("Danger")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        return {"seed": self.world.seed_text(), "level_type": self.world.level_type.currentData(),
                "generate_structures": self.world.structures.isChecked()}


class TransferDialog(QDialog):
    """Planned change of host: where to, and how long to warn the players."""

    def __init__(self, choices: list[tuple[str, str | None]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Changer de PC hôte")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Le PC choisi télécharge d'abord le monde pendant que vous jouez. Ensuite les joueurs sont "
            "prévenus, le serveur s'arrête, la sauvegarde finale est envoyée et le nouveau PC démarre : "
            "comptez environ une minute d'interruption.<br>Si quelque chose échoue, le serveur reste "
            "(ou redémarre) sur le PC actuel, sans rien perdre.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.target = QComboBox()
        for label, host_id in choices:
            self.target.addItem(label, host_id)
        self.countdown = QSpinBox()
        self.countdown.setRange(0, 600)
        self.countdown.setSingleStep(15)
        self.countdown.setSuffix(" s")
        self.countdown.setValue(60)
        self.countdown.setToolTip("Les joueurs connectés voient un compte à rebours dans le chat.")
        form.addRow("Vers", self.target)
        form.addRow("Prévenir les joueurs", self.countdown)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Changer de PC")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str | None, int]:
        return self.target.currentData(), self.countdown.value()


class PlayerAddressesDialog(QDialog):
    """Personal addresses of the allowed players (servers accepting unofficial accounts)."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server_id = bridge, agent, server_id
        self.setWindowTitle("Adresses des joueurs")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Ce serveur accepte les comptes non officiels : sans vérification Microsoft, n'importe qui "
            "pourrait prendre le pseudo d'un autre. Chaque joueur se connecte donc avec <b>sa propre "
            "adresse</b>, qui ne laisse entrer que lui. Envoie-la lui en privé.<br>Si une adresse a "
            "circulé, crée-en une nouvelle : l'ancienne cesse aussitôt de fonctionner.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Joueur", "Adresse à saisir dans Minecraft", ""])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.error = QLabel()
        self.error.setStyleSheet("color: #e35d5d; background: transparent;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load()

    def load(self) -> None:
        self.bridge.run(self.agent.api.player_addresses(self.server_id), on_ok=self._fill,
                        on_error=lambda e: self.error.setText(error_text(e)))

    def _fill(self, rows: list[dict[str, str]]) -> None:
        self.table.setRowCount(len(rows))
        buttons = QSize()
        for i, row in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(row["player"]))
            self.table.setItem(i, 1, QTableWidgetItem(row["address"]))
            actions = QWidget()
            box = QHBoxLayout(actions)
            box.setContentsMargins(4, 0, 4, 0)
            copy = QPushButton("Copier")
            copy.clicked.connect(lambda _=False, a=row["address"]: QGuiApplication.clipboard().setText(a))
            renew = QPushButton("Nouvelle adresse")
            renew.clicked.connect(lambda _=False, p=row["player"]: self._regenerate(p))
            box.addWidget(copy)
            box.addWidget(renew)
            self.table.setCellWidget(i, 2, actions)
            buttons = buttons.expandedTo(actions.sizeHint())
        if rows:
            self._fit(rows, buttons)
        else:
            self.error.setText("Aucun joueur autorisé : ajoute les pseudos dans la configuration du serveur.")

    def _fit(self, rows: list[dict[str, str]], buttons: QSize) -> None:
        """Whole buttons, names and addresses, without having to widen the window."""
        self.table.verticalHeader().setDefaultSectionSize(
            max(self.table.verticalHeader().defaultSectionSize(), buttons.height() + 6))
        self.table.setColumnWidth(2, buttons.width())
        self.table.resizeColumnToContents(0)
        metrics = self.table.fontMetrics()
        address = max(metrics.horizontalAdvance(r["address"]) for r in rows) + 24
        scrollbar = self.table.verticalScrollBar().sizeHint().width()
        frame = 2 * self.table.frameWidth()
        self.table.setMinimumWidth(self.table.columnWidth(0) + address + buttons.width() + scrollbar + frame)
        shown = min(len(rows), 8)  # beyond, the table scrolls
        self.table.setMinimumHeight(self.table.horizontalHeader().sizeHint().height() + frame
                                    + shown * self.table.verticalHeader().defaultSectionSize())
        hint = self.sizeHint()
        self.resize(max(self.width(), hint.width()), max(self.height(), hint.height()))

    def _regenerate(self, player: str) -> None:
        answer = QMessageBox.question(self, "Nouvelle adresse",
                                      f"Remplacer l'adresse de {player} ? L'ancienne ne fonctionnera plus, "
                                      "et s'il est connecté avec, il sera déconnecté.")
        if answer == QMessageBox.StandardButton.Yes:
            self.bridge.run(self.agent.api.regenerate_player_address(self.server_id, player),
                            on_ok=lambda _: self.load(), on_error=lambda e: self.error.setText(error_text(e)))


def show_player_address(parent: QWidget, player: str, address: str) -> None:
    """Right after accepting an access request on a server with personal addresses."""
    box = QMessageBox(parent)
    box.setWindowTitle("Adresse personnelle")
    box.setText(f"{player} est accepté. Son adresse personnelle est prête :")
    box.setInformativeText(f"{address}\n\nIl la verra en se reconnectant à l'adresse du serveur, depuis là où il "
                           "a fait sa demande (pendant 7 jours, jusqu'à ce qu'il l'utilise). Tu peux aussi la "
                           "lui envoyer toi-même.")
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    copy = box.addButton("Copier", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(address)


def show_invite(parent: QWidget, code: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("Invitation")
    box.setText("Envoie ce code à la personne à inviter (valable 7 jours, une seule utilisation) :")
    box.setInformativeText(code)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    copy = box.addButton("Copier", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(code)


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


class PasswordDialog(QDialog):
    """Change of password. The other PCs of the account will have to log in again."""

    def __init__(self, bridge: Bridge, agent: HostAgent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent = bridge, agent
        self.setWindowTitle("Changer le mot de passe")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.current, self.new, self.repeat = QLineEdit(), QLineEdit(), QLineEdit()
        for field in (self.current, self.new, self.repeat):
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setMaxLength(256)
        form.addRow("Mot de passe actuel", self.current)
        form.addRow("Nouveau (10 caractères ou plus)", self.new)
        form.addRow("Nouveau, encore une fois", self.repeat)
        layout.addLayout(form)
        note = QLabel("Tes autres PC devront se reconnecter avec le nouveau mot de passe.\n"
                      "Mot de passe oublié ? L'administrateur du VPS peut en donner un nouveau "
                      "(install.sh reset-password).")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error = QLabel()
        self.error.setStyleSheet("color: #e35d5d; background: transparent;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _submit(self) -> None:
        if len(self.new.text()) < 10:
            self.error.setText("Le nouveau mot de passe doit faire au moins 10 caractères.")
            return
        if self.new.text() != self.repeat.text():
            self.error.setText("Les deux nouveaux mots de passe sont différents.")
            return
        if self.new.text() == self.current.text():
            self.error.setText("Le nouveau mot de passe est le même que l'actuel.")
            return
        self.buttons.setEnabled(False)
        self.error.setText("")

        def failed(exc: BaseException) -> None:
            self.buttons.setEnabled(True)
            code = getattr(exc, "code", "")
            self.error.setText("Mot de passe actuel incorrect." if code == "invalid_credentials"
                               else error_text(exc))

        self.bridge.run(self.agent.api.change_password(self.current.text(), self.new.text()),
                        on_ok=lambda _: self.accept(), on_error=failed)
