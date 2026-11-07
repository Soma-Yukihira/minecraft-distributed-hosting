"""Dialogs added in 0.4.0: mods and plugins, allowing them on this PC, access requests; in 0.5.0,
the server's resource pack; in 0.6.0, modpacks and pre-generating the world; in 0.7.0, datapacks and
the settings files of mods."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mdh_client.application.agent import HostAgent
from mdh_client.gui.bridge import Bridge
from mdh_client.gui.dialogs import show_player_address
from mdh_client.gui.texts import error_text
from mdh_client.gui.widgets import human_bytes
from mdh_client.minecraft import config_files, pregen
from mdh_client.minecraft.modpack import ImportedPack
from mdh_client.minecraft.modrinth import without
from mdh_shared.models.api import (ANTICHEAT_MOD_SLUG, MAP_MOD_SLUG, MOD_LOADERS, PREGEN_MOD_SLUG, Datapack, Mod,
                                   ResourcePack, anticheat_enabled, anticheat_slugs, config_path_problem)
from mdh_shared.models.enums import Loader


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    return table


def _error_label() -> QLabel:
    label = QLabel()
    label.setStyleSheet("color: #e35d5d; background: transparent;")
    label.setWordWrap(True)
    return label


def modrinth_page(mod: Mod) -> str:
    return f"https://modrinth.com/project/{mod.slug}"


class ModsDialog(QDialog):
    """The group's mods (Fabric) or plugins (Paper), chosen on Modrinth by an admin."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any], editable: bool,
                 parent: QWidget | None = None, updates: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server, self.editable = bridge, agent, server, editable
        self.loader = Loader(server["loader"])
        self.mods = [Mod.model_validate(m) for m in server.get("mods", [])]
        #: Updates already found for this exact list (None once the list changes).
        self.updates = updates
        #: A modpack chosen with "Importer un modpack…": the dialog closes to apply it.
        self.imported: ImportedPack | None = None
        #: "Réglages des mods…" was clicked: the dialog closes and the window opens the editor.
        self.open_settings = False
        self.kind = "plugins" if self.loader == Loader.PAPER else "mods"
        self.setWindowTitle("Plugins du serveur" if self.loader == Loader.PAPER else "Mods du serveur")
        self.setMinimumSize(720, 560)
        layout = QVBoxLayout(self)
        supported = self.loader in MOD_LOADERS
        intro = QLabel(
            (f"Les {self.kind} viennent de <b>Modrinth</b>. Chaque PC hôte les télécharge lui-même et vérifie "
             "leur empreinte avant de les lancer ; chaque membre doit les autoriser une fois sur son PC. "
             "Les changements s'appliquent au prochain démarrage du serveur.")
            if supported else
            "Vanilla ne charge ni mod ni plugin. Pour en ajouter, choisis <b>Paper</b> (plugins) ou "
            "<b>Fabric</b> (mods) dans la configuration du serveur.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.map = QCheckBox("Carte du monde dans le navigateur (BlueMap), avec la page de statut")
        self.map.setChecked(bool(server.get("map_enabled")))
        self.map.setEnabled(editable and supported)
        self.map.setToolTip("Ajoute BlueMap. La carte se dessine sur le PC hôte et s'ouvre depuis la page de "
                            "statut, que toute personne connaissant l'adresse peut voir.")
        self.map.clicked.connect(self._map_clicked)
        layout.addWidget(self.map)
        self.anticheat = QCheckBox("Protection anti-triche (Grim et anti-X-ray)")
        self.anticheat.setChecked(anticheat_enabled(self.mods))
        self.anticheat.setEnabled(editable and supported)
        self.anticheat.setToolTip("Ajoute Grim, qui bloque sur le serveur le vol, la vitesse, les coups de trop "
                                  "loin…, et cache les minerais aux mods et aux packs X-ray."
                                  if supported else "Choisis Paper ou Fabric : Vanilla ne charge aucun plugin.")
        self.anticheat.clicked.connect(self._anticheat_clicked)
        layout.addWidget(self.anticheat)

        self.table = _table(["Nom", "Version", "Taille", ""])
        self.table.setMinimumHeight(190)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.page_btn = QPushButton("Voir sur Modrinth")
        self.page_btn.clicked.connect(self._open_page)
        self.remove_btn = QPushButton("Retirer")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.clicked.connect(self._remove)
        self.update_btn = QPushButton()
        self.update_btn.clicked.connect(self._update)
        self.update_btn.setVisible(editable and supported)
        self.players_btn = QPushButton("Pour les joueurs…")
        self.players_btn.setToolTip("Les mods à installer dans le Minecraft des joueurs, en un fichier.")
        self.players_btn.clicked.connect(self._export)
        self.players_btn.setVisible(self.loader == Loader.FABRIC)
        self.modpack_btn = QPushButton("Importer un modpack…")
        self.modpack_btn.setToolTip("Remplacer la liste par un modpack Fabric de Modrinth (Cobblemon, Create…).")
        self.modpack_btn.clicked.connect(self._import)
        self.modpack_btn.setVisible(editable)
        self.settings_btn = QPushButton("Réglages des mods…")
        self.settings_btn.setToolTip("Modifier les fichiers de réglages (config/…), appliqués sur tous les PC.")
        self.settings_btn.clicked.connect(self._settings)
        self.settings_btn.setVisible(editable and self.loader in MOD_LOADERS)
        row.addWidget(self.page_btn)
        row.addWidget(self.settings_btn)
        row.addWidget(self.players_btn)
        row.addStretch()
        row.addWidget(self.modpack_btn)
        row.addWidget(self.update_btn)
        row.addWidget(self.remove_btn)
        layout.addLayout(row)
        pack = server.get("modpack")
        if pack:
            origin = QLabel(f"Fait à partir du modpack <b>{pack['name']} {pack['version_number']}</b> : ses "
                            "fichiers de configuration sont appliqués à chaque démarrage.")
            origin.setObjectName("Muted")
            origin.setWordWrap(True)
            layout.insertWidget(1, origin)

        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(f"Rechercher des {self.kind} pour {self.loader.value.capitalize()} "
                                      f"{server['mc_version']} sur Modrinth")
        self.query.returnPressed.connect(self._search)
        self.search_btn = QPushButton("Rechercher")
        self.search_btn.clicked.connect(self._search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)
        self.results = QListWidget()
        self.results.setFixedHeight(140)
        self.results.itemSelectionChanged.connect(self._selection)
        self.add_btn = QPushButton("Ajouter")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._add)
        for w in (self.query, self.search_btn, self.results, self.add_btn):
            w.setVisible(editable and supported)
        layout.addLayout(search_row)
        layout.addWidget(self.results)
        add_row = QHBoxLayout()
        add_row.addStretch()
        add_row.addWidget(self.add_btn)
        layout.addLayout(add_row)
        self.info = QLabel()
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.error = _error_label()
        layout.addWidget(self.error)
        if editable:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.accept)
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            self.info.setText("Seul un administrateur du groupe peut changer la liste.")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fill()

    def values(self) -> tuple[list[Mod], bool]:
        return self.mods, self.map.isChecked()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.mods))
        for i, m in enumerate(self.mods):
            self.table.setItem(i, 0, QTableWidgetItem(m.name))
            self.table.setItem(i, 1, QTableWidgetItem(m.version_number))
            self.table.setItem(i, 2, QTableWidgetItem(human_bytes(m.size)))
            needed_by = [o.name for o in self.mods if m.project_id in o.requires]
            self.table.setItem(i, 3, QTableWidgetItem(
                "nécessaire à " + ", ".join(needed_by) if m.dependency and needed_by else ""))
        count = len(self.updates["changes"]) if self.updates else 0
        self.update_btn.setText(f"Mettre à jour ({count})" if count else "Vérifier les mises à jour")
        self.update_btn.setObjectName("Primary" if count else "")
        self.update_btn.style().unpolish(self.update_btn)
        self.update_btn.style().polish(self.update_btn)
        self.update_btn.setEnabled(bool(self.mods))
        # What players install is what is saved: export only an unchanged list.
        self.players_btn.setEnabled(bool(self.mods) and self._saved())
        self.players_btn.setToolTip("Les mods à installer dans le Minecraft des joueurs, en un fichier."
                                    if self._saved() else "Enregistre d'abord la liste.")
        self.settings_btn.setEnabled(self._saved())
        self._selection()

    def _saved(self) -> bool:
        return [m.model_dump(mode="json") for m in self.mods] == self.server.get("mods", [])

    def _export(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Mods pour les joueurs")
        box.setText("Chaque joueur doit avoir les mêmes mods que le serveur dans son Minecraft (sauf ceux qui ne "
                    "servent qu'au serveur, laissés de côté).\n\nQuel fichier préparer ?")
        mrpack = box.addButton("Modpack .mrpack (Prism, Modrinth App…)", QMessageBox.ButtonRole.AcceptRole)
        folder = box.addButton("Dossier de mods .zip (TLauncher)", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        kind = "mrpack" if box.clickedButton() is mrpack else "zip" if box.clickedButton() is folder else None
        if kind is None:
            return
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.server["name"]).strip() or "serveur"
        suffix = ".mrpack" if kind == "mrpack" else " - mods.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer", str(Path.home() / "Downloads" / (name + suffix)),
                                              "Modpack (*.mrpack)" if kind == "mrpack" else "Archive zip (*.zip)")
        if not path:
            return
        self._busy(True, "Préparation du fichier (les mods sont téléchargés depuis Modrinth et vérifiés)…")

        def done(mods: list) -> None:
            optional = sum(1 for p in mods if not p.required)
            self._busy(False, f"Enregistré : {path} ({len(mods)} mod(s)"
                              + (f", dont {optional} facultatif(s)" if optional else "") + "). Envoie-le aux joueurs.")

        self.bridge.run(self.agent.export_for_players(self.server, Path(path), kind), on_ok=done,
                        on_error=self._failed)

    def _settings(self) -> None:
        self.open_settings = True
        self.reject()

    def _import(self) -> None:
        dlg = ModpackDialog(self.bridge, self.agent, self.server, self)
        if dlg.exec() and dlg.imported is not None:
            self.imported = dlg.imported
            super().accept()

    def _selected(self) -> Mod | None:
        rows = self.table.selectionModel().selectedRows()
        return self.mods[rows[0].row()] if rows else None

    def _selection(self) -> None:
        mod = self._selected()
        self.page_btn.setEnabled(mod is not None)
        self.remove_btn.setVisible(self.editable)
        self.remove_btn.setEnabled(mod is not None and not mod.dependency)
        self.add_btn.setEnabled(bool(self.results.selectedItems()))

    def _open_page(self) -> None:
        if (mod := self._selected()) is not None:
            QDesktopServices.openUrl(QUrl(modrinth_page(mod)))

    def _update(self) -> None:
        if self.updates:
            self._confirm_update(self.updates)
            return
        self._busy(True, "Recherche des mises à jour sur Modrinth…")

        def found(result: tuple) -> None:
            mods, changes = result
            self._busy(False, "" if changes else f"Tout est à jour pour {self.loader.value.capitalize()} "
                                                 f"{self.server['mc_version']}.")
            self.updates = {"mods": mods, "changes": changes} if changes else None
            self._fill()
            if self.updates:
                self._confirm_update(self.updates)

        server = {**self.server, "mods": [m.model_dump(mode="json") for m in self.mods]}
        self.bridge.run(self.agent.mod_updates(server), on_ok=found, on_error=self._failed)

    def _confirm_update(self, updates: dict[str, Any]) -> None:
        lines = []
        for c in updates["changes"]:
            if c.old is None:
                lines.append(f"• {c.name} {c.new} : ajouté (nécessaire à la nouvelle version)")
            elif c.new is None:
                lines.append(f"• {c.name} : retiré (plus nécessaire)")
            else:
                lines.append(f"• {c.name} : {c.old} → {c.new}")
        risk = ("Une nouvelle version de mod peut transformer des blocs du monde : la sauvegarde actuelle reste "
                "dans l'historique (page Stockage)." if self.loader == Loader.FABRIC else
                "Les réglages des plugins restent dans le dossier du serveur.")
        text = ("\n".join(lines[:20]) + (f"\n… et {len(lines) - 20} autre(s)" if len(lines) > 20 else "")
                + f"\n\n{risk}\n\nChaque fichier est vérifié par son empreinte, et chaque membre l'autorise "
                "sur son PC comme pour un nouveau mod. Mettre à jour ?")
        if QMessageBox.question(self, "Mises à jour", text) != QMessageBox.StandardButton.Yes:
            return
        self.mods = list(updates["mods"])
        self.updates = None
        self._fill()
        self.info.setText(f"{len(lines)} changement(s) prêts : enregistre pour les appliquer.")

    def _busy(self, busy: bool, message: str = "") -> None:
        for w in (self.search_btn, self.add_btn, self.map, self.anticheat, self.update_btn, self.players_btn,
                  self.modpack_btn):
            w.setEnabled(not busy and (w is not self.add_btn or bool(self.results.selectedItems())))
        if not busy:
            self.players_btn.setEnabled(bool(self.mods) and self._saved())
            self.settings_btn.setEnabled(self._saved())
        self.info.setText(message)
        if busy:
            self.error.clear()

    def _failed(self, exc: BaseException) -> None:
        self._busy(False)
        self.error.setText(error_text(exc))

    def _search(self) -> None:
        text = self.query.text().strip()
        if not text:
            return
        self._busy(True, "Recherche sur Modrinth…")

        def show(hits: list) -> None:
            self._busy(False, "" if hits else f"Aucun résultat pour {self.loader.value.capitalize()} "
                                              f"{self.server['mc_version']}.")
            self.results.clear()
            have = {m.project_id for m in self.mods}
            for hit in hits:
                downloads = f"{hit.downloads:,}".replace(",", " ")
                item = QListWidgetItem(f"{hit.title} — {downloads} téléchargements — {hit.description}")
                item.setData(Qt.ItemDataRole.UserRole, hit.project_id)
                item.setToolTip(hit.description)
                if hit.project_id in have:
                    item.setText(f"{hit.title} — déjà dans la liste")
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.results.addItem(item)
            self._selection()

        self.bridge.run(self.agent.search_mods(self.server, text), on_ok=show, on_error=self._failed)

    def _add(self) -> None:
        items = self.results.selectedItems()
        if items:
            self._add_project(items[0].data(Qt.ItemDataRole.UserRole))

    def _add_project(self, ref: str, then: Any = None) -> None:
        self._busy(True, "Choix du fichier compatible et de ses dépendances…")

        def added(entries: list[Mod]) -> None:
            self.mods += entries
            self.updates = None
            self._fill()
            names = ", ".join(m.name for m in entries[1:])
            self._busy(False, (f"Ajouté : {entries[0].name} {entries[0].version_number}"
                               + (f" (et, parce qu'il en a besoin : {names})" if names else "") + ".")
                       if entries else "Déjà dans la liste.")
            if then:
                then()

        def failed(exc: BaseException) -> None:
            self._failed(exc)
            if ref == MAP_MOD_SLUG:
                self.map.setChecked(False)

        self.bridge.run(self.agent.resolve_mod(self.server, ref, self.mods), on_ok=added, on_error=failed)

    def _remove(self) -> None:
        mod = self._selected()
        if mod is None:
            return
        warning = (f"Retirer {mod.name} ?\n\n"
                   + ("Les blocs, objets et entités de ce mod disparaîtront du monde au prochain démarrage. "
                      "La sauvegarde actuelle reste dans l'historique (page Stockage)."
                      if self.loader == Loader.FABRIC else
                      "Ses réglages restent dans le dossier du serveur s'il revient un jour."))
        if QMessageBox.question(self, "Retirer", warning) != QMessageBox.StandardButton.Yes:
            return
        self.mods = without(self.mods, mod.project_id)
        self.updates = None
        if mod.slug == MAP_MOD_SLUG:
            self.map.setChecked(False)
        if mod.slug == ANTICHEAT_MOD_SLUG:
            self._remove_anticheat()
        self._fill()

    def _map_clicked(self, checked: bool) -> None:
        bluemap = next((m for m in self.mods if m.slug == MAP_MOD_SLUG), None)
        if checked and bluemap is None:
            QMessageBox.information(
                self, "Carte du monde",
                "BlueMap dessine la carte sur le PC hôte (un cœur du processeur, sans gêner la partie) et "
                "la page de statut s'active pour la montrer.\n\nPour dessiner les blocs, BlueMap télécharge "
                "les textures du jeu chez Mojang. Il ne le fait que sur les PC dont l'utilisateur a accepté "
                "le contrat de licence de Minecraft (EULA) dans cette application.\n\n"
                "La carte se redessine sur chaque nouveau PC hôte : elle n'est pas sauvegardée sur le VPS.")
            self._add_project(MAP_MOD_SLUG)
        elif not checked and bluemap is not None:
            self.mods = without(self.mods, bluemap.project_id)
            self.updates = None
            self._fill()

    def _anticheat_clicked(self, checked: bool) -> None:
        if not checked:
            self._remove_anticheat()
            self._fill()
            return
        if anticheat_enabled(self.mods):
            return
        QMessageBox.information(self, "Protection anti-triche", ANTICHEAT_TEXT)
        self._busy(True, "Choix des fichiers de Grim" + (" et de l'anti-X-ray" if self.loader == Loader.FABRIC
                                                         else "") + " sur Modrinth…")

        def added(entries: list[Mod]) -> None:
            self.mods += entries
            self.updates = None
            self._fill()
            self._busy(False, "Ajouté : " + ", ".join(f"{m.name} {m.version_number}" for m in entries)
                       + (". L'anti-X-ray de Paper s'allume avec." if self.loader == Loader.PAPER else ".")
                       + " Enregistre, puis redémarre le serveur.")

        def failed(exc: BaseException) -> None:
            self._failed(exc)
            self.anticheat.setChecked(False)

        self.bridge.run(self.agent.anticheat_mods(self.loader, self.server["mc_version"], self.mods),
                        on_ok=added, on_error=failed)

    def _remove_anticheat(self) -> None:
        self.anticheat.setChecked(False)
        for slug in anticheat_slugs(self.loader):
            for m in [m for m in self.mods if m.slug == slug]:
                self.mods = without(self.mods, m.project_id)
        self.updates = None

    def accept(self) -> None:
        if self.map.isChecked() and not any(m.slug == MAP_MOD_SLUG for m in self.mods):
            self.error.setText("Attends que BlueMap soit ajouté, ou décoche la carte.")
            return
        if self.anticheat.isChecked() and not anticheat_enabled(self.mods):
            self.error.setText("Attends que Grim soit ajouté, ou décoche la protection anti-triche.")
            return
        super().accept()


ANTICHEAT_TEXT = (
    "Grim surveille sur le serveur les mouvements et les coups de chaque joueur, et annule ce que le jeu ne "
    "permet pas : voler, aller trop vite, traverser les blocs, frapper de trop loin, ne pas prendre de "
    "dégâts de chute… Ses alertes s'affichent dans la console du serveur. Il ne bannit personne tout "
    "seul : c'est vous qui décidez (Expulser ou Bannir, dans la liste des joueurs).\n\n"
    "L'anti-X-ray cache au jeu des joueurs les minerais qu'ils ne peuvent pas voir : un mod ou un pack X-ray "
    "ne montre plus que de faux minerais.\n\n"
    "Ce qu'aucun serveur Minecraft ne peut faire : savoir quels mods un joueur a installés. Le serveur ne "
    "reçoit que ce que le jeu lui envoie. Les mods sans triche (mini-carte, shaders, Sodium…) restent donc "
    "possibles ; la triche, elle, est bloquée par le serveur.\n\n"
    "Les joueurs n'ont rien à installer. Chaque membre autorise Grim sur son PC, comme tout plugin.")


def _mc_key(version: str) -> tuple[int, ...]:
    """Orders Minecraft versions ("1.21.1" < "26.3"); unknown shapes sort first."""
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return ()


class ModpackDialog(QDialog):
    """Pick a Fabric modpack on Modrinth: its mods replace the server's (applied by the caller)."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any],
                 parent: QWidget | None = None, popular: bool = False) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server = bridge, agent, server
        self.imported: ImportedPack | None = None
        self.setWindowTitle("Choisir un modpack" if popular else "Importer un modpack")
        self.setMinimumSize(660, 520)
        layout = QVBoxLayout(self)
        intro = QLabel("Les mods du modpack remplacent la liste du serveur, qui passe sur <b>Fabric</b> et sur la "
                       "version de Minecraft du modpack. Ses fichiers de configuration sont copiés sur le PC hôte "
                       "à chaque démarrage ; aucun programme hors de Modrinth n'est jamais lancé.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Rechercher un modpack Fabric sur Modrinth (ex. : cobblemon, create)")
        self.query.returnPressed.connect(self._search)
        self.search_btn = QPushButton("Rechercher")
        self.search_btn.clicked.connect(self._search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)
        self.results = QListWidget()
        self.results.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.results, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.info = QLabel()
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.read_btn = QPushButton("Lire ce modpack")
        self.read_btn.clicked.connect(self._read)
        buttons.addButton(self.read_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.import_btn = QPushButton("Importer")
        self.import_btn.setObjectName("Primary")
        self.import_btn.clicked.connect(self._confirm)
        buttons.addButton(self.import_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._read_pack: ImportedPack | None = None
        self._selection()
        if popular:
            self._search(popular=True)

    def _selection(self) -> None:
        items = self.results.selectedItems()
        self.read_btn.setEnabled(bool(items))
        # Only the modpack that was read, and is still the one selected, can be imported.
        self.import_btn.setEnabled(self._read_pack is not None and bool(items)
                                   and items[0].data(Qt.ItemDataRole.UserRole) == self._read_pack.modpack.project_id)

    def _busy(self, busy: bool, message: str = "") -> None:
        for w in (self.search_btn, self.read_btn, self.import_btn):
            w.setEnabled(not busy)
        if not busy:
            self._selection()
        self.info.setText(message)
        if busy:
            self.error.clear()

    def _failed(self, exc: BaseException) -> None:
        self._busy(False)
        self.error.setText(error_text(exc))

    def _search(self, popular: bool = False) -> None:
        text = self.query.text().strip()
        if not text and not popular:
            return
        self._busy(True, "Recherche sur Modrinth…")

        def show(hits: list) -> None:
            self.results.clear()
            for hit in hits:
                downloads = f"{hit.downloads:,}".replace(",", " ")
                item = QListWidgetItem(f"{hit.title} — {downloads} téléchargements — {hit.description}")
                item.setData(Qt.ItemDataRole.UserRole, hit.project_id)
                item.setToolTip(hit.description)
                self.results.addItem(item)
            self._busy(False, ("Les plus téléchargés : choisis-en un, puis « Lire ce modpack »." if popular else "")
                       if hits else "Aucun modpack Fabric trouvé.")

        self.bridge.run(self.agent.search_modpacks(text), on_ok=show, on_error=self._failed)

    def _read(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return
        self._read_pack = None
        self.summary.clear()
        self._busy(True, "Téléchargement du modpack et lecture de ses mods…")
        self.bridge.run(self.agent.import_modpack(items[0].data(Qt.ItemDataRole.UserRole)),
                        on_ok=self._show, on_error=self._failed)

    def _show(self, pack: ImportedPack) -> None:
        self._read_pack = pack
        size = sum(m.size for m in pack.mods)
        lines = [f"<b>{pack.modpack.name} {pack.modpack.version_number}</b> — Fabric, Minecraft {pack.mc_version}, "
                 f"{len(pack.mods)} mod(s) pour le serveur ({human_bytes(size)})."]
        if pack.client_only:
            lines.append(f"{pack.client_only} fichier(s) ne servent qu'aux joueurs (textures, shaders, mods "
                         "d'affichage) : pas sur le serveur.")
        if pack.outside:
            names = ", ".join(pack.outside[:5]) + ("…" if len(pack.outside) > 5 else "")
            lines.append(f"<span style='color:#ff8a80'>{len(pack.outside)} mod(s) hors de Modrinth laissé(s) de "
                         f"côté : {names}. Le modpack peut ne pas démarrer sans eux.</span>")
        if pack.programs:
            lines.append(f"{pack.programs} programme(s) parmi ses fichiers de configuration : jamais copiés.")
        self.summary.setText("<br>".join(lines))
        self._busy(False, "Vérifie ce qui précède, puis « Importer ».")

    def _confirm(self) -> None:
        pack = self._read_pack
        if pack is None:
            return
        current = self.server["mc_version"]
        text = (f"Le serveur passe sur Fabric, Minecraft {pack.mc_version}, avec les {len(pack.mods)} mods du "
                f"modpack. La liste actuelle ({len(self.server.get('mods', []))} mod(s) ou plugin(s)) est remplacée.")
        if _mc_key(pack.mc_version) < _mc_key(current):
            text += (f"\n\nAttention : Minecraft {pack.mc_version} est plus ancien que {current}. Un monde ne revient "
                     "pas en arrière sans risque : crée plutôt un nouveau serveur pour ce modpack, ou restaure "
                     "ensuite une sauvegarde d'avant (page Stockage).")
        elif pack.mc_version != current:
            text += f"\n\nLe monde est mis à jour de Minecraft {current} vers {pack.mc_version} au démarrage."
        text += ("\n\nLes blocs des mods retirés disparaissent du monde ; la sauvegarde actuelle reste dans "
                 "l'historique. Importer ?")
        if QMessageBox.question(self, "Importer un modpack", text) == QMessageBox.StandardButton.Yes:
            self.imported = pack
            self.accept()


class PregenDialog(QDialog):
    """Pre-generate a square around the spawn with Chunky, on the hosting PC."""

    def __init__(self, server: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.server = server
        self.setWindowTitle("Pré-générer le monde")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        intro = QLabel("Le serveur calcule d'avance le terrain autour du point d'apparition. Ensuite, explorer ne "
                       "fait plus que lire le disque : moins de ralentissements, et une carte complète. À lancer "
                       "plutôt quand personne ne joue ; le serveur ne s'arrête pas « faute de joueurs » pendant "
                       "ce temps.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.dimension = QComboBox()
        for key, label in pregen.DIMENSIONS.items():
            self.dimension.addItem(label, key)
        self.radius = QComboBox()
        for r in pregen.RADII:
            self.radius.addItem(f"{r:,} blocs autour du spawn".replace(",", " "), r)
        self.radius.setCurrentIndex(1)
        row.addWidget(QLabel("Dimension"))
        row.addWidget(self.dimension)
        row.addWidget(QLabel("Taille"))
        row.addWidget(self.radius, 1)
        layout.addLayout(row)
        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        self.radius.currentIndexChanged.connect(self._estimate)
        self.dimension.currentIndexChanged.connect(self._estimate)
        self.missing = not any(m.get("slug") == PREGEN_MOD_SLUG for m in server.get("mods", []))
        if self.missing:
            note = QLabel("Il faut <b>Chunky</b> dans la liste des mods (ou des plugins). « Ajouter Chunky » l'ajoute ; "
                          "chaque PC l'autorise, puis le serveur redémarre. Relance ensuite la pré-génération.")
            note.setWordWrap(True)
            layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = QPushButton("Ajouter Chunky" if self.missing else "Pré-générer")
        self.ok_btn.setObjectName("Primary")
        self.ok_btn.clicked.connect(self.accept)
        buttons.addButton(self.ok_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._estimate()

    def values(self) -> tuple[str, int]:
        return self.dimension.currentData(), int(self.radius.currentData())

    def _estimate(self) -> None:
        _, radius = self.values()
        size = pregen.estimate_bytes(radius)
        chunks = f"{pregen.chunks(radius):,}".replace(",", " ")
        self.estimate.setText(
            f"Environ {chunks} tronçons, soit à peu près {human_bytes(size)} de monde en plus "
            "(estimation : le Nether et les mods changent beaucoup la taille). Le premier envoi vers le VPS est "
            "plus long et compte dans le quota. Durée : de quelques minutes à quelques heures selon le PC.")


class DatapacksDialog(QDialog):
    """Datapacks from Modrinth in the world (any server type): same controls as mods."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any], editable: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server, self.editable = bridge, agent, server, editable
        self.packs = [Datapack.model_validate(d) for d in server.get("datapacks", [])]
        self.setWindowTitle("Datapacks du serveur")
        self.setMinimumSize(660, 500)
        layout = QVBoxLayout(self)
        intro = QLabel("Les datapacks changent le jeu sans mod : nouveaux terrains (Terralith…), donjons, "
                       "recettes, mécaniques. Ils fonctionnent avec Vanilla, Paper et Fabric, et les joueurs n'ont "
                       "rien à installer. Chaque PC hôte les télécharge depuis <b>Modrinth</b>, vérifie leur "
                       "empreinte, et chaque membre les autorise sur son PC comme les mods. Appliqués au prochain "
                       "démarrage ; un datapack de terrain ne change que les zones encore jamais visitées.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = _table(["Nom", "Version", "Taille"])
        self.table.setMinimumHeight(150)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addStretch()
        self.remove_btn = QPushButton("Retirer")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.clicked.connect(self._remove)
        self.remove_btn.setVisible(editable)
        row.addWidget(self.remove_btn)
        layout.addLayout(row)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(f"Rechercher des datapacks pour Minecraft {server['mc_version']} sur Modrinth")
        self.query.returnPressed.connect(self._search)
        self.search_btn = QPushButton("Rechercher")
        self.search_btn.clicked.connect(self._search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)
        self.results = QListWidget()
        self.results.setFixedHeight(130)
        self.results.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.results)
        add_row = QHBoxLayout()
        add_row.addStretch()
        self.add_btn = QPushButton("Ajouter")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._add)
        add_row.addWidget(self.add_btn)
        layout.addLayout(add_row)
        for w in (self.query, self.search_btn, self.results, self.add_btn):
            w.setVisible(editable)
        self.info = QLabel()
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.error = _error_label()
        layout.addWidget(self.error)
        if editable:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.accept)
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            self.info.setText("Seul un administrateur du groupe peut changer la liste.")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fill()

    def values(self) -> list[Datapack]:
        return self.packs

    def _fill(self) -> None:
        self.table.setRowCount(len(self.packs))
        for i, p in enumerate(self.packs):
            self.table.setItem(i, 0, QTableWidgetItem(p.name))
            self.table.setItem(i, 1, QTableWidgetItem(p.version_number))
            self.table.setItem(i, 2, QTableWidgetItem(human_bytes(p.size)))
        self._selection()

    def _selection(self) -> None:
        self.remove_btn.setEnabled(bool(self.table.selectionModel().selectedRows()))
        self.add_btn.setEnabled(bool(self.results.selectedItems()))

    def _busy(self, busy: bool, message: str = "") -> None:
        for w in (self.search_btn, self.add_btn):
            w.setEnabled(not busy)
        if not busy:
            self._selection()
        self.info.setText(message)
        if busy:
            self.error.clear()

    def _failed(self, exc: BaseException) -> None:
        self._busy(False)
        self.error.setText(error_text(exc))

    def _search(self) -> None:
        text = self.query.text().strip()
        if not text:
            return
        self._busy(True, "Recherche sur Modrinth…")

        def show(hits: list) -> None:
            self.results.clear()
            have = {p.project_id for p in self.packs}
            for hit in hits:
                downloads = f"{hit.downloads:,}".replace(",", " ")
                item = QListWidgetItem(f"{hit.title} — {downloads} téléchargements — {hit.description}")
                item.setData(Qt.ItemDataRole.UserRole, hit.project_id)
                item.setToolTip(hit.description)
                if hit.project_id in have:
                    item.setText(f"{hit.title} — déjà dans la liste")
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.results.addItem(item)
            self._busy(False, "" if hits else f"Aucun datapack pour Minecraft {self.server['mc_version']}.")

        self.bridge.run(self.agent.search_datapacks(self.server, text), on_ok=show, on_error=self._failed)

    def _add(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return
        self._busy(True, "Choix du fichier pour cette version de Minecraft…")

        def added(pack: Datapack) -> None:
            if all(p.project_id != pack.project_id for p in self.packs):
                self.packs.append(pack)
            self._fill()
            self._busy(False, f"Ajouté : {pack.name} {pack.version_number}. Enregistre pour l'appliquer.")

        self.bridge.run(self.agent.datapack(self.server, items[0].data(Qt.ItemDataRole.UserRole)), on_ok=added,
                        on_error=self._failed)

    def _remove(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            del self.packs[rows[0].row()]
            self._fill()


class ConfigFilesDialog(QDialog):
    """Edit the settings files of mods and plugins; saved for the group, written by every host."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server = bridge, agent, server
        #: The group's files as they will be saved (path -> content).
        self.managed: dict[str, str] = {}
        self.current: str | None = None
        self.saved_status: dict[str, Any] | None = None
        self.setWindowTitle("Réglages des mods")
        self.setMinimumSize(900, 600)
        layout = QVBoxLayout(self)
        intro = QLabel("Les fichiers de réglages (<code>config/</code>, <code>plugins/…/</code>) tels qu'ils sont dans "
                       "la dernière sauvegarde du monde. Un fichier modifié ici est écrit par chaque PC hôte avant "
                       "le démarrage de Minecraft (✎ dans la liste) : les mêmes réglages quel que soit le PC. "
                       "Appliqué au prochain démarrage.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrer (ex. : lithium)")
        self.filter.textChanged.connect(self._fill)
        layout.addWidget(self.filter)
        split = QSplitter()
        self.files = QListWidget()
        self.files.currentItemChanged.connect(self._open)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Console")
        self.editor.setEnabled(False)
        self.editor.textChanged.connect(self._edited)
        split.addWidget(self.files)
        split.addWidget(self.editor)
        split.setSizes([300, 600])
        layout.addWidget(split, 1)
        row = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        self.unmanage_btn = QPushButton("Ne plus imposer ce fichier")
        self.unmanage_btn.setToolTip("Le fichier garde son contenu actuel sur le PC hôte, mais n'est plus réécrit.")
        self.unmanage_btn.clicked.connect(self._unmanage)
        row.addWidget(self.status, 1)
        row.addWidget(self.unmanage_btn)
        layout.addLayout(row)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.entries: list[tuple[str, int, bool]] = []
        self._loading = False
        self.status.setText("Lecture de la liste des fichiers…")
        self.bridge.run(self.agent.config_file_list(server), on_ok=self._listed, on_error=self._failed)

    def _failed(self, exc: BaseException) -> None:
        self.status.clear()
        self.error.setText(error_text(exc))

    def _listed(self, entries: list[tuple[str, int, bool]]) -> None:
        self.entries = entries
        self.status.setText("" if entries else "Aucun fichier de réglages pour l'instant : ils apparaissent après un "
                                               "premier démarrage du serveur avec ses mods.")

        def got(files: dict[str, str]) -> None:
            self.managed = dict(files)
            self._fill()

        self.bridge.run(self.agent.api.config_files(self.server["server_id"]), on_ok=got, on_error=self._failed)

    def _fill(self) -> None:
        text = self.filter.text().strip().lower()
        known = {p for p, _, _ in self.entries} | set(self.managed)
        self.files.blockSignals(True)
        self.files.clear()
        for path in sorted(known):
            if text and text not in path.lower():
                continue
            item = QListWidgetItem(("✎ " if path in self.managed else "") + path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.files.addItem(item)
            if path == self.current:
                self.files.setCurrentItem(item)
        self.files.blockSignals(False)
        self.unmanage_btn.setEnabled(self.current in self.managed)

    def _open(self, item: QListWidgetItem | None, _previous: Any = None) -> None:
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.current = path
        self.error.clear()

        def show(content: str) -> None:
            self._loading = True
            self.editor.setPlainText(content)
            self._loading = False
            self.editor.setEnabled(True)
            self.status.setText(f"{path} — " + ("imposé par le groupe" if path in self.managed else
                                                "tel que dans la dernière sauvegarde"))
            self.unmanage_btn.setEnabled(path in self.managed)

        if path in self.managed:
            show(self.managed[path])
        else:
            self.editor.setEnabled(False)
            self.status.setText(f"Lecture de {path}…")
            self.bridge.run(self.agent.read_config_file(self.server, path), on_ok=show, on_error=self._failed)

    def _edited(self) -> None:
        if self._loading or self.current is None:
            return
        self.managed[self.current] = self.editor.toPlainText()
        problem = config_files.syntax_problem(self.current, self.managed[self.current])
        self.error.setText(problem or "")
        self._fill()

    def _unmanage(self) -> None:
        if self.current in self.managed:
            del self.managed[self.current]
            self._fill()
            self.status.setText(f"{self.current} n'est plus imposé (enregistre pour l'appliquer).")

    def _save(self) -> None:
        bad = [(p, config_files.syntax_problem(p, c)) for p, c in self.managed.items()]
        bad = [(p, why) for p, why in bad if why]
        if bad and QMessageBox.question(
                self, "Réglages", f"{bad[0][0]} : {bad[0][1]}.\n\nUn fichier invalide peut empêcher le mod de "
                "démarrer. Enregistrer quand même ?") != QMessageBox.StandardButton.Yes:
            return
        wrong = next((p for p in self.managed if config_path_problem(p)), None)
        if wrong:
            self.error.setText(f"{wrong} : {config_path_problem(wrong)}")
            return

        def saved(status: dict[str, Any]) -> None:
            self.saved_status = status
            self.accept()

        self.bridge.run(self.agent.save_config_files(self.server["server_id"], self.managed), on_ok=saved,
                        on_error=self._failed)


class ResourcePackDialog(QDialog):
    """The pack Minecraft offers to players when they join: from Modrinth, or a direct link."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server = bridge, agent, server
        current = server.get("resource_pack")
        self.pack: ResourcePack | None = ResourcePack.model_validate(current) if current else None
        self.setWindowTitle("Pack de ressources")
        self.setMinimumSize(640, 520)
        layout = QVBoxLayout(self)
        intro = QLabel("Minecraft propose ce pack à chaque joueur qui se connecte, et le télécharge lui-même "
                       "depuis Modrinth ou depuis ton lien, en vérifiant son empreinte. Le pack ne passe ni par le "
                       "VPS ni par le PC hôte. S'applique au prochain démarrage du serveur.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        current_row = QHBoxLayout()
        self.current = QLabel()
        self.current.setWordWrap(True)
        self.remove_btn = QPushButton("Retirer")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.clicked.connect(self._remove)
        current_row.addWidget(self.current, 1)
        current_row.addWidget(self.remove_btn)
        layout.addLayout(current_row)
        self.required = QCheckBox("Obligatoire : un joueur qui refuse le pack ne peut pas entrer")
        self.required.setChecked(bool(self.pack and self.pack.required))
        self.required.toggled.connect(self._required_changed)
        layout.addWidget(self.required)

        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(f"Rechercher un pack pour Minecraft {server['mc_version']} sur Modrinth")
        self.query.returnPressed.connect(self._search)
        self.search_btn = QPushButton("Rechercher")
        self.search_btn.clicked.connect(self._search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)
        self.results = QListWidget()
        self.results.setMinimumHeight(140)
        self.results.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.results, 1)
        choose_row = QHBoxLayout()
        choose_row.addStretch()
        self.choose_btn = QPushButton("Choisir ce pack")
        self.choose_btn.setObjectName("Primary")
        self.choose_btn.clicked.connect(self._choose)
        choose_row.addWidget(self.choose_btn)
        layout.addLayout(choose_row)

        link_row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("Ou un lien direct vers ton pack : https://…/mon-pack.zip")
        self.link.returnPressed.connect(self._check_link)
        self.link_btn = QPushButton("Vérifier le lien")
        self.link_btn.clicked.connect(self._check_link)
        link_row.addWidget(self.link, 1)
        link_row.addWidget(self.link_btn)
        layout.addLayout(link_row)
        self.info = QLabel()
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_current()

    def value(self) -> ResourcePack | None:
        return self.pack

    def _show_current(self) -> None:
        p = self.pack
        if p is None:
            self.current.setText("<b>Aucun pack.</b> Les joueurs gardent leurs propres textures.")
        else:
            origin = "Modrinth" if p.project_id else "lien direct"
            version = f" {p.version_number}" if p.version_number else ""
            self.current.setText(f"<b>{p.name}{version}</b> — {human_bytes(p.size)}, {origin}, "
                                 + ("obligatoire" if p.required else "facultatif"))
        self.remove_btn.setEnabled(p is not None)
        self.required.setEnabled(p is not None)
        self._selection()

    def _selection(self) -> None:
        self.choose_btn.setEnabled(bool(self.results.selectedItems()))

    def _busy(self, busy: bool, message: str = "") -> None:
        for w in (self.search_btn, self.choose_btn, self.link_btn):
            w.setEnabled(not busy)
        if not busy:
            self._selection()
        self.info.setText(message)
        if busy:
            self.error.clear()

    def _failed(self, exc: BaseException) -> None:
        self._busy(False)
        self.error.setText(error_text(exc))

    def _chosen(self, pack: ResourcePack) -> None:
        self.pack = pack
        self._busy(False, f"Choisi : {pack.name} ({human_bytes(pack.size)}, empreinte {pack.sha1[:12]}…). "
                          "Enregistre pour l'appliquer.")
        self._show_current()

    def _search(self) -> None:
        text = self.query.text().strip()
        if not text:
            return
        self._busy(True, "Recherche sur Modrinth…")

        def show(hits: list) -> None:
            self.results.clear()
            for hit in hits:
                downloads = f"{hit.downloads:,}".replace(",", " ")
                item = QListWidgetItem(f"{hit.title} — {downloads} téléchargements — {hit.description}")
                item.setData(Qt.ItemDataRole.UserRole, hit.project_id)
                item.setToolTip(hit.description)
                self.results.addItem(item)
            self._busy(False, "" if hits else f"Aucun pack pour Minecraft {self.server['mc_version']}.")

        self.bridge.run(self.agent.search_packs(self.server, text), on_ok=show, on_error=self._failed)

    def _choose(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return
        self._busy(True, "Choix du fichier pour cette version de Minecraft…")
        self.bridge.run(self.agent.modrinth_pack(self.server, items[0].data(Qt.ItemDataRole.UserRole),
                                                 self.required.isChecked()),
                        on_ok=self._chosen, on_error=self._failed)

    def _check_link(self) -> None:
        url = self.link.text().strip()
        if not url:
            return
        self._busy(True, "Téléchargement du pack pour vérifier qu'il s'agit bien d'un pack de ressources…")
        self.bridge.run(self.agent.pack_from_url(url, self.required.isChecked()),
                        on_ok=self._chosen, on_error=self._failed)

    def _required_changed(self, checked: bool) -> None:
        if self.pack is not None:
            self.pack = self.pack.model_copy(update={"required": checked})
            self._show_current()

    def _remove(self) -> None:
        self.pack = None
        self.required.setChecked(False)
        self._show_current()


class ModApprovalDialog(QDialog):
    """Allow, on this PC, mods chosen by an admin of the group (docs/ARCHITECTURE.md P7)."""

    def __init__(self, server_name: str, mods: list[Mod], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autoriser des mods sur ce PC")
        self.setMinimumSize(640, 380)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Un administrateur du groupe a ajouté ces mods ou plugins à « {server_name} ». Quand ce PC "
            "héberge le serveur, ils s'exécutent ici avec les mêmes droits que Minecraft.<br><br>"
            "Ils viennent de Modrinth et leur empreinte est vérifiée, mais autorise-les seulement si tu "
            "fais confiance aux administrateurs du groupe. Sans autorisation, ce PC n'héberge pas ce serveur "
            "(les autres PC le peuvent).")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        table = _table(["Nom", "Version", "Taille", "Empreinte (SHA-512)"])
        table.setRowCount(len(mods))
        for i, m in enumerate(mods):
            table.setItem(i, 0, QTableWidgetItem(m.name))
            table.setItem(i, 1, QTableWidgetItem(m.version_number))
            table.setItem(i, 2, QTableWidgetItem(human_bytes(m.size)))
            item = QTableWidgetItem(m.sha512[:16] + "…")
            item.setToolTip(f"{m.sha512}\n{modrinth_page(m)}")
            table.setItem(i, 3, item)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox()
        allow = buttons.addButton("Autoriser sur ce PC", QDialogButtonBox.ButtonRole.AcceptRole)
        allow.setObjectName("Primary")
        buttons.addButton("Pas maintenant", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AccessRequestsDialog(QDialog):
    """Players refused by the whitelist who asked to join, for the admins."""

    def __init__(self, bridge: Bridge, agent: HostAgent, server: dict[str, Any],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge, self.agent, self.server = bridge, agent, server
        self.setWindowTitle("Demandes d'accès")
        self.setMinimumSize(600, 360)
        self.rows: list[dict[str, Any]] = []
        self.changed = False
        layout = QVBoxLayout(self)
        intro = QLabel("Ces joueurs ont essayé de se connecter alors qu'ils ne sont pas dans la liste des joueurs "
                       "autorisés. Accepter un joueur l'ajoute à la liste, tout de suite, sans redémarrer."
                       + (" « Nouvel endroit » : un joueur de la liste se connecte depuis un réseau jamais vu. "
                          "Comme les comptes non officiels ne sont pas vérifiés, accepte seulement si tu es "
                          "sûr que c'est bien lui (demande-lui, par exemple)."
                          if server.get("allow_offline_accounts") and not server.get("personal_addresses", True)
                          else "")
                       + (" Ce serveur accepte les comptes non officiels : une fois accepté, le joueur voit "
                          "son adresse personnelle en se reconnectant à l'adresse du serveur, depuis là "
                          "où il a fait sa demande. Tu peux aussi la lui envoyer toi-même."
                          if server.get("allow_offline_accounts") and server.get("personal_addresses", True)
                          else ""))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = _table(["Joueur", "Demande", "Dernier essai", "Essais", "État"])
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.refuse_btn = QPushButton("Refuser")
        self.refuse_btn.setObjectName("Danger")
        self.refuse_btn.clicked.connect(lambda: self._answer(False))
        self.accept_btn = QPushButton("Accepter")
        self.accept_btn.setObjectName("Primary")
        self.accept_btn.clicked.connect(lambda: self._answer(True))
        row.addStretch()
        row.addWidget(self.refuse_btn)
        row.addWidget(self.accept_btn)
        layout.addLayout(row)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load()

    def load(self) -> None:
        self.bridge.run(self.agent.api.access_requests(self.server["server_id"]), on_ok=self._fill,
                        on_error=lambda e: self.error.setText(error_text(e)))

    def _fill(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            when = datetime.fromisoformat(r["last_at"]).astimezone().strftime("%d/%m %H:%M")
            kind = (f"Nouvel endroit ({r.get('place') or '?'})" if r.get("kind") == "new_place"
                    else "Rejoindre le serveur")
            self.table.setItem(i, 0, QTableWidgetItem(r["player"]))
            self.table.setItem(i, 1, QTableWidgetItem(kind))
            self.table.setItem(i, 2, QTableWidgetItem(when))
            self.table.setItem(i, 3, QTableWidgetItem(str(r["attempts"])))
            self.table.setItem(i, 4, QTableWidgetItem("refusée" if r["status"] == "refused" else "en attente"))
        if not rows:
            self.error.setText("Aucune demande pour le moment.")
        self._selection()

    def _selected(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows()
        return self.rows[rows[0].row()] if rows else None

    def _selection(self) -> None:
        r = self._selected()
        self.accept_btn.setEnabled(r is not None)
        self.refuse_btn.setEnabled(r is not None and r["status"] == "pending")

    def _answer(self, accept: bool) -> None:
        r = self._selected()
        if r is None:
            return

        def done(_: Any) -> None:
            self.changed = True
            self.load()
            if (accept and r.get("kind") != "new_place" and self.server.get("allow_offline_accounts")
                    and self.server.get("personal_addresses", True)):
                self.bridge.run(self.agent.api.player_addresses(self.server["server_id"]), on_ok=show_address,
                                on_error=lambda e: self.error.setText(error_text(e)))

        def show_address(rows: list[dict[str, str]]) -> None:
            found = next((x for x in rows if x["player"].lower() == r["player"].lower()), None)
            if found is not None:
                show_player_address(self, found["player"], found["address"])

        self.bridge.run(self.agent.api.answer_access_request(self.server["server_id"], r["player"], accept,
                                                             r.get("place", "")),
                        on_ok=done, on_error=lambda e: self.error.setText(error_text(e)))
