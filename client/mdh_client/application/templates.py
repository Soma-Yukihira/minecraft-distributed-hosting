"""Ready-made servers, chosen in three clicks when creating one (docs/ARCHITECTURE.md 3.14).

A template only fills the creation dialog and, once the server exists, picks its mods on
Modrinth for the chosen version of Minecraft: a mod with no file for that version is skipped
and named, never replaced by something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mdh_shared.models.enums import Loader


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    summary: str
    loader: Loader
    #: Modrinth slugs, resolved with their dependencies.
    mods: tuple[str, ...] = ()
    properties: dict[str, Any] = field(default_factory=dict)
    #: Other fields of the creation dialog.
    options: dict[str, Any] = field(default_factory=dict)
    #: A modpack is chosen right after creating the server.
    modpack: bool = False

    def defaults(self) -> dict[str, Any]:
        return {"name": self.title if self.key != "vanilla" else "Survie", "loader": self.loader.value,
                "properties": dict(self.properties), **self.options}


TEMPLATES = (
    Template("vanilla", "Vanilla simple", "Le Minecraft officiel, sans rien ajouter. Le plus simple pour commencer.",
             Loader.VANILLA),
    Template("optimized", "Survie optimisée",
             "Fabric avec Lithium, FerriteCore, ScalableLux et Krypton : le même jeu, bien plus léger pour le PC "
             "hôte. Avec Chunky (pré-génération) et spark (analyse du lag). Les joueurs n'ont rien à installer.",
             Loader.FABRIC, ("lithium", "ferrite-core", "scalablelux", "krypton", "chunky", "spark"),
             properties={"view-distance": 10, "simulation-distance": 8},
             options={"start_on_join": True, "stop_when_empty_after_s": 1800}),
    Template("plugins", "Serveur à plugins",
             "Paper avec LuckPerms (grades et permissions) et Chunky. Paper inclut déjà spark. Les joueurs "
             "n'ont rien à installer.", Loader.PAPER, ("luckperms", "chunky")),
    Template("modpack", "Moddé populaire",
             "Un modpack Fabric de Modrinth (Cobblemon, Create…), choisi juste après parmi les plus "
             "téléchargés. Les joueurs installent le même modpack.", Loader.FABRIC, modpack=True),
)


def by_key(key: str) -> Template:
    return next(t for t in TEMPLATES if t.key == key)
