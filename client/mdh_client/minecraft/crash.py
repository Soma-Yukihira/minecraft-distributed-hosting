"""Why Minecraft stopped (docs/ARCHITECTURE.md 4.16).

Read after a crash: the newest crash report and the end of the console. The mod at fault is
named only on evidence that points to it — Fabric's own verdict on missing or incompatible
mods, a Mixin error naming its configuration, a class compiled for a newer Java, or the first
frame of the stack trace that belongs to a mod's JAR. Nothing is sent but the verdict.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from mdh_client.minecraft.mods import file_name, folder
from mdh_shared.models.api import CrashReport, Mod
from mdh_shared.models.enums import Loader

MAX_REPORT_BYTES = 2 * 1024**2
CONSOLE_LINES = 400
MAX_NESTED_BYTES = 32 * 1024**2
#: Packages of Java, Minecraft and the loaders: a frame there accuses nobody.
_NEUTRAL = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "net.minecraft.", "com.mojang.", "net.fabricmc.loader.",
            "net.fabricmc.api.", "org.spongepowered.", "com.llamalad7.", "org.apache.", "com.google.", "io.netty.",
            "it.unimi.", "org.slf4j.", "org.objectweb.", "kotlin.", "kotlinx.", "org.joml.", "io.papermc.",
            "org.bukkit.", "org.spigotmc.", "net.md_5.", "com.destroystokyo.", "ca.spottedleaf.", "net.kyori.",
            "org.jetbrains.", "org.lwjgl.", "oshi.", "com.electronwill.")

_FRAME = re.compile(r"^\s*at (?:[\w.$-]+/)?([\w$]+(?:\.[\w$]+)+)\.([\w$<>]+)\(")
_MIXIN_HANDLER = re.compile(r"\$[a-z]{3}\d{3}\$([a-z][a-z0-9_]*)\$")
_MISSING = re.compile(r"Mod '([^']+)' \(([\w.-]+)\)[^\n]*? requires [^\n]*?of '?([^',!\n]+?)'?(?: \(([\w.-]+)\))?, "
                      r"which is missing")
_WRONG_MC = re.compile(r"Mod '([^']+)' \(([\w.-]+)\)[^\n]*? requires [^\n]*?of (?:'Minecraft' \()?minecraft\)?, "
                       r"but only the wrong version is present")
_INCOMPATIBLE = re.compile(r"Mod '([^']+)' \(([\w.-]+)\)[^\n]*? is incompatible with [^\n]*?'?([^',!\n]+?)'?"
                           r"(?: \(([\w.-]+)\))?, but (?:it )?is present")
_MIXIN = (re.compile(r"Mixin apply for mod ([\w.-]+) failed"),
          re.compile(r"Mixin \[([\w.-]+\.json):[^\]]*\] from mod ([\w.-]+)"),
          re.compile(r"from mod ([\w.-]+)\] [^\n]*(?:failed|FAILED|InvalidMixin|InjectionError)"),
          re.compile(r"[Mm]ixin config ([\w.-]+\.json)[^\n]*(?:failed|could not|couldn't)"))
_JAVA = re.compile(r"UnsupportedClassVersionError: ([\w/$.]+) has been compiled by a more recent version")
_PLUGIN = re.compile(r"Could not load '(?:plugins[/\\])?([^']+\.jar)'")
_PAPER_ENABLE = re.compile(r"Error occurred while enabling (\S+)")
_WORLD = re.compile(r"Failed to load level|Exception reading [^\n]*level\.dat|Failed to read level\.dat|"
                    r"Couldn't load chunk|Chunk file at [^\n]* is in the wrong location")
_PLUGIN_NAME = re.compile(r"^name:[ \t]*['\"]?([\w .-]{1,64}?)['\"]?[ \t]*$", re.M)
_WATCHDOG = re.compile(r"The server has stopped responding|A single server tick took [\d.]+ seconds")


@dataclass
class _Jar:
    mod: Mod
    ids: set[str] = field(default_factory=set)
    mixins: set[str] = field(default_factory=set)
    packages: set[str] = field(default_factory=set)


def _read_meta(z: zipfile.ZipFile, jar: _Jar, depth: int = 0) -> None:
    names = z.namelist()
    jar.packages |= {str(PurePosixPath(n).parent).replace("/", ".") for n in names
                     if n.endswith(".class") and "/" in n and not n.startswith("META-INF/")}
    if "fabric.mod.json" in names:
        try:
            meta = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
        except ValueError:
            meta = {}
        if isinstance(meta.get("id"), str):
            jar.ids.add(meta["id"])
        for m in meta.get("mixins", []) if isinstance(meta.get("mixins"), list) else []:
            config = m.get("config") if isinstance(m, dict) else m
            if isinstance(config, str):
                jar.mixins.add(config)
        if depth == 0:  # jar-in-jar (Fabric API's modules...): they belong to this mod
            for nested in meta.get("jars", []) if isinstance(meta.get("jars"), list) else []:
                path = nested.get("file") if isinstance(nested, dict) else None
                if isinstance(path, str) and path in names and z.getinfo(path).file_size <= MAX_NESTED_BYTES:
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(path))) as inner:
                            _read_meta(inner, jar, depth + 1)
                    except zipfile.BadZipFile:
                        pass
    for plugin in ("plugin.yml", "paper-plugin.yml"):
        if plugin in names:
            # Only the top-level "name:" is needed: no YAML parser for that.
            if m := _PLUGIN_NAME.search(z.read(plugin).decode("utf-8", "replace")):
                jar.ids.add(m.group(1).strip().lower())


def index_jars(server_dir: Path, loader: Loader, mods: list[Mod]) -> list[_Jar]:
    jars = []
    for mod in mods:
        path = folder(server_dir, loader) / file_name(mod)
        jar = _Jar(mod, ids={mod.slug.lower()})
        try:
            with zipfile.ZipFile(path) as z:
                _read_meta(z, jar)
        except (OSError, zipfile.BadZipFile):
            pass
        jars.append(jar)
    return jars


def _newest_report(server_dir: Path, since: float) -> str:
    reports = server_dir / "crash-reports"
    try:
        candidates = [p for p in reports.glob("crash-*.txt") if p.stat().st_mtime >= since - 5]
    except OSError:
        return ""
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        with open(newest, "rb") as f:
            return f.read(MAX_REPORT_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _by_id(jars: list[_Jar], mod_id: str) -> Mod | None:
    mod_id = mod_id.lower()
    return next((j.mod for j in jars if mod_id in {i.lower() for i in j.ids}), None)


def _by_class(jars: list[_Jar], cls: str) -> Mod | None:
    if cls.startswith(_NEUTRAL):
        return None
    package = cls.rsplit(".", 1)[0]
    return next((j.mod for j in jars if package in j.packages), None)


def _from_frames(jars: list[_Jar], text: str) -> Mod | None:
    for line in text.splitlines():
        m = _FRAME.match(line)
        if not m:
            continue
        cls, method = m.group(1), m.group(2)
        if (handler := _MIXIN_HANDLER.search(method)) and (mod := _by_id(jars, handler.group(1))):
            return mod
        if mod := _by_class(jars, cls):
            return mod
    return None


def _named(mod: Mod) -> str:
    return f"{mod.name} {mod.version_number}"


def _report(cause: str, message: str, mod: Mod | None = None) -> CrashReport:
    return CrashReport(cause=cause, message=message[:300], mod_project_id=mod.project_id if mod else None,
                       mod_name=mod.name[:100] if mod else None)


def analyze(server_dir: Path, lines: list[str], mods: list[Mod], loader: Loader, since: float,
            exit_code: int | None = None) -> CrashReport:
    report = _newest_report(server_dir, since)
    console = "\n".join(lines[-CONSOLE_LINES:])
    text = report + "\n" + console
    jars = index_jars(server_dir, loader, mods) if mods else []

    if m := _MISSING.search(text):
        mod = _by_id(jars, m.group(2))
        needed = m.group(3).strip()
        who = _named(mod) if mod else m.group(1)
        return _report("missing_dependency", f"{who} a besoin de {needed}, qui manque : ajoute-le depuis « Mods », "
                                             f"ou retire {mod.name if mod else m.group(1)}.", mod)
    if m := _WRONG_MC.search(text):
        mod = _by_id(jars, m.group(2))
        return _report("mod", f"{_named(mod) if mod else m.group(1)} n'est pas prévu pour cette version de "
                              "Minecraft : mets-le à jour ou retire-le.", mod)
    if m := _INCOMPATIBLE.search(text):
        mod = _by_id(jars, m.group(2))
        return _report("mod", f"{_named(mod) if mod else m.group(1)} est incompatible avec {m.group(3).strip()} : "
                              "retire l'un des deux.", mod)
    if m := _JAVA.search(text):
        mod = _by_class(jars, m.group(1).replace("/", "."))
        return _report("java", (f"{_named(mod)} demande une version de Java plus récente que celle de ce "
                                "Minecraft : choisis une autre version de ce mod, ou retire-le.") if mod else
                       "Un fichier demande une version de Java plus récente que celle de ce Minecraft.", mod)
    if "java.lang.OutOfMemoryError" in text:
        return _report("memory", "Minecraft a manqué de mémoire : donne-lui plus de RAM dans « Configuration », "
                                 "ou retire des mods gourmands.")
    for pattern in _MIXIN:
        if m := pattern.search(text):
            mod = None
            for g in m.groups():
                mod = mod or _by_id(jars, g) or next((j.mod for j in jars if g in j.mixins), None)
            if mod is not None:
                return _report("mod", f"{_named(mod)} ne s'accorde pas avec ce Minecraft ou avec un autre mod "
                                      "(erreur Mixin) : mets-le à jour ou retire-le.", mod)
    if m := _PLUGIN.search(text):
        mod = next((j.mod for j in jars if file_name(j.mod) == m.group(1)), None)
        if mod is not None:
            return _report("mod", f"Le plugin {_named(mod)} n'a pas pu être chargé : mets-le à jour ou retire-le.",
                           mod)
    if _WORLD.search(text):
        return _report("world", "Le monde n'a pas pu être lu : restaure une sauvegarde précédente (page "
                                "« Stockage »).")
    mod = _from_frames(jars, report) or _from_frames(jars, console)
    if _WATCHDOG.search(text):
        return _report("watchdog", (f"Le serveur s'est figé, apparemment dans {_named(mod)} : mets-le à jour ou "
                                    "retire-le.") if mod else
                       "Le serveur s'est figé plus d'une minute (une ferme énorme, ou un mod) : regarde le "
                       "journal du serveur.", mod)
    if mod is not None:
        return _report("mod", f"Le plantage vient apparemment de {_named(mod)} : mets-le à jour ou retire-le.", mod)
    if m := _PAPER_ENABLE.search(console):
        mod = _by_id(jars, m.group(1))
        if mod is not None:
            return _report("mod", f"Le plugin {_named(mod)} plante au démarrage : mets-le à jour ou retire-le.", mod)
    code = f" (code {exit_code})" if exit_code is not None else ""
    return _report("unknown", f"Minecraft s'est arrêté sans cause évidente{code} : regarde le journal du serveur.")


def since_start(started_monotonic: float | None) -> float:
    """Wall-clock time of a start measured with time.monotonic()."""
    if started_monotonic is None:
        return 0.0
    return time.time() - (time.monotonic() - started_monotonic)
