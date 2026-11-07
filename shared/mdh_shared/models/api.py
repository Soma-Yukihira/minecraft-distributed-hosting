"""Request and response bodies of the REST API (docs/API.md)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from mdh_shared.constants import PRIORITY_MAX, PRIORITY_MIN
from mdh_shared.models.enums import (
    HostRuntimeState,
    Loader,
    MigrationReason,
    MigrationStatus,
    Preemption,
    Role,
    ServerState,
    VersionKind,
)
from mdh_shared.models.manifest import Manifest

Id = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Username = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{3,32}$")]
Password = Annotated[str, StringConstraints(min_length=10, max_length=256)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
#: Minecraft Java player names.
PlayerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{3,16}$")]
#: Version ids as published by Mojang: "1.21.4", "1.21.11-rc3", year-based "26.3", "26.3-rc-3",
#: "26.4-snapshot-6", and legacy weekly snapshots "24w14a".
McVersion = Annotated[str, StringConstraints(
    pattern=r"^(\d+\.\d+(\.\d+)?(-(pre|rc|snapshot)-?\d+)?|\d{2}w\d{2}[a-z])$")]

#: server.properties keys a group admin may set, with their expected type. Everything else
#: (server-port, online-mode, rcon, query, management server...) is decided locally by the host
#: client and can never be pushed through the API.
ALLOWED_SERVER_PROPERTIES: dict[str, type] = {
    "motd": str,
    "difficulty": str,
    "gamemode": str,
    "force-gamemode": bool,
    "hardcore": bool,
    "pvp": bool,
    "max-players": int,
    "view-distance": int,
    "simulation-distance": int,
    "spawn-protection": int,
    "white-list": bool,
    "enforce-whitelist": bool,
    "allow-flight": bool,
    "allow-nether": bool,
    "spawn-monsters": bool,
    "level-seed": str,
    "level-type": str,
    "generate-structures": bool,
    "max-world-size": int,
    "enable-command-block": bool,
}

#: World presets offered in the application (``level-type``), docs/ARCHITECTURE.md 3.17.
LEVEL_TYPES = ("normal", "flat", "large_biomes", "amplified")
LevelType = Literal["normal", "flat", "large_biomes", "amplified"]
#: Before Minecraft 1.19 (world presets), the same choices had other names.
_LEGACY_LEVEL_TYPES = {"normal": "default", "flat": "flat", "large_biomes": "largeBiomes", "amplified": "amplified"}
#: Seed typed by a player: any text (Minecraft hashes words), one line.
WorldSeed = Annotated[str, StringConstraints(max_length=64, pattern=r"^[^\x00-\x1f\x7f]*$")]


def level_type_value(level_type: str, mc_version: str) -> str:
    """``level-type`` as the server of that Minecraft version reads it."""
    m = re.match(r"^(\d+)\.(\d+)", mc_version)
    if m and (int(m[1]), int(m[2])) < (1, 19):
        return _LEGACY_LEVEL_TYPES[level_type]
    return f"minecraft:{level_type}"
_PROPERTY_INT_RANGES: dict[str, tuple[int, int]] = {
    "max-players": (1, 200),
    "view-distance": (3, 32),
    "simulation-distance": (3, 32),
    "spawn-protection": (0, 1000),
    "max-world-size": (1, 29_999_984),
}

Properties = dict[str, str | int | bool]


def _unique_players(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for n in names:
        if n.lower() not in seen:  # Minecraft names are case-insensitive
            seen.add(n.lower())
            result.append(n)
    return result


AllowedPlayers = Annotated[list[PlayerName], Field(max_length=200), AfterValidator(_unique_players)]


def validate_properties(value: dict[str, Any]) -> dict[str, Any]:
    for key, v in value.items():
        expected = ALLOWED_SERVER_PROPERTIES.get(key)
        if expected is None:
            raise ValueError(f"server.properties key {key!r} cannot be set remotely")
        # bool is a subclass of int: check it explicitly.
        if (expected is int and (isinstance(v, bool) or not isinstance(v, int))) or not isinstance(v, expected):
            raise ValueError(f"{key} must be of type {expected.__name__}")
        if isinstance(v, str) and (len(v) > 200 or any(ord(c) < 32 for c in v)):
            # A newline would let a value inject arbitrary extra properties.
            raise ValueError(f"{key}: value too long or contains control characters")
        if key in _PROPERTY_INT_RANGES:
            lo, hi = _PROPERTY_INT_RANGES[key]
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Mods and plugins from Modrinth (docs/ARCHITECTURE.md 3.9) ------------------------------

#: Modrinth project and version ids.
ModrinthId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{8}$")]
#: One line of text shown in the application.
_LINE = r"^[^\x00-\x1f\x7f]+$"
#: Modpacks bring a few hundred mods.
MAX_MODS = 300
MAX_MOD_BYTES = 256 * 1024**2
#: Modrinth loaders whose files each server type can run.
MOD_LOADERS: dict[Loader, tuple[str, ...]] = {
    Loader.PAPER: ("paper", "spigot", "bukkit"),
    Loader.FABRIC: ("fabric",),
}
#: Modrinth slug of BlueMap, the world map served with the status page (ARCHITECTURE 4.11).
MAP_MOD_SLUG = "bluemap"
#: Modrinth slug of Chunky, which pre-generates the world (ARCHITECTURE 4.15).
PREGEN_MOD_SLUG = "chunky"
#: Modrinth slug of spark, the profiler (ARCHITECTURE 4.17); built into Paper.
SPARK_MOD_SLUG = "spark"
#: Modrinth slug of Grim, the server-side anti-cheat (ARCHITECTURE 4.19): its presence in the list
#: is the "protection anti-triche" switch (on Paper it also turns on Paper's own anti-X-ray).
ANTICHEAT_MOD_SLUG = "grimac"
#: Fabric has no anti-X-ray of its own: DrexHD's port of Paper's (MIT, server only).
FABRIC_ANTIXRAY_SLUG = "anti-xray"


def anticheat_slugs(loader: Loader) -> tuple[str, ...]:
    """What the anti-cheat protection adds for that server type (nothing for Vanilla)."""
    return {Loader.PAPER: (ANTICHEAT_MOD_SLUG,),
            Loader.FABRIC: (ANTICHEAT_MOD_SLUG, FABRIC_ANTIXRAY_SLUG)}.get(loader, ())


def anticheat_enabled(mods: list[Mod] | list[dict[str, Any]]) -> bool:
    return any((m.get("slug") if isinstance(m, dict) else m.slug) == ANTICHEAT_MOD_SLUG for m in mods)
_CDN_FILE = re.compile(r"[A-Za-z0-9._+%()\[\]~!-]{1,200}\.jar")
_CDN_PACK = re.compile(r"[A-Za-z0-9._+%()\[\]~!-]{1,200}\.mrpack")
_CDN_ZIP = re.compile(r"[A-Za-z0-9._+%()\[\]~!-]{1,200}\.zip")


class Mod(_Strict):
    """A Fabric mod or a Paper plugin published on Modrinth.

    The JAR is never synchronised between PCs: each host downloads it from Modrinth's CDN and
    checks its SHA-512 before Minecraft can load it. The control plane only stores this entry.
    """

    project_id: ModrinthId
    version_id: ModrinthId
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.+!@$()`,'\"-]+$")
    name: str = Field(min_length=1, max_length=100, pattern=_LINE)
    version_number: str = Field(min_length=1, max_length=64, pattern=_LINE)
    url: str = Field(max_length=400)
    sha512: str = Field(pattern=r"^[0-9a-f]{128}$")
    size: int = Field(gt=0, le=MAX_MOD_BYTES)
    #: Server type and Minecraft version this file was chosen for.
    loader: Loader
    mc_version: McVersion
    #: Projects this file needs (they are in the list too).
    requires: list[ModrinthId] = Field(default_factory=list, max_length=50)
    #: Added because another mod needs it: removed with the last mod that needs it.
    dependency: bool = False

    @model_validator(mode="after")
    def _from_modrinth_cdn(self) -> Mod:
        prefix = f"https://cdn.modrinth.com/data/{self.project_id}/versions/{self.version_id}/"
        if not self.url.startswith(prefix) or not _CDN_FILE.fullmatch(self.url[len(prefix):]):
            raise ValueError("mods are downloaded from Modrinth's CDN only")
        return self


#: Largest resource pack Minecraft accepts.
MAX_PACK_BYTES = 250 * 1024**2


class ResourcePack(_Strict):
    """The pack Minecraft offers to players when they join (docs/ARCHITECTURE.md 3.10).

    Players' Minecraft downloads it from ``url`` and checks its SHA-1: it goes neither through
    the VPS nor through the hosting PC."""

    name: str = Field(min_length=1, max_length=100, pattern=_LINE)
    url: str = Field(min_length=9, max_length=1000, pattern=r"^https://[^\x00-\x20\x7f]+$")
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int = Field(gt=0, le=MAX_PACK_BYTES)
    #: Players who decline it cannot join.
    required: bool = False
    #: Chosen on Modrinth (None: a direct link given by an admin).
    project_id: ModrinthId | None = None
    version_id: ModrinthId | None = None
    version_number: str | None = Field(default=None, max_length=64, pattern=_LINE)


MAX_MODPACK_BYTES = 200 * 1024**2


class Modpack(_Strict):
    """A Modrinth modpack the server was made from (docs/ARCHITECTURE.md 3.11).

    Its mods are in the list of mods like any other. Each host downloads the ``.mrpack`` itself
    from Modrinth's CDN, checks its SHA-512 and copies only its configuration files (never a
    program) into the server folder."""

    project_id: ModrinthId
    version_id: ModrinthId
    name: str = Field(min_length=1, max_length=100, pattern=_LINE)
    version_number: str = Field(min_length=1, max_length=64, pattern=_LINE)
    url: str = Field(max_length=400)
    sha512: str = Field(pattern=r"^[0-9a-f]{128}$")
    size: int = Field(gt=0, le=MAX_MODPACK_BYTES)

    @model_validator(mode="after")
    def _from_modrinth_cdn(self) -> Modpack:
        prefix = f"https://cdn.modrinth.com/data/{self.project_id}/versions/{self.version_id}/"
        if not self.url.startswith(prefix) or not _CDN_PACK.fullmatch(self.url[len(prefix):]):
            raise ValueError("modpacks are downloaded from Modrinth's CDN only")
        return self


MAX_DATAPACKS = 50
MAX_DATAPACK_BYTES = 64 * 1024**2


class Datapack(_Strict):
    """A datapack published on Modrinth, put in the world's ``datapacks`` folder by each host
    (docs/ARCHITECTURE.md 3.12). Same controls as a mod: Modrinth's CDN, SHA-512, allowed PC by PC."""

    project_id: ModrinthId
    version_id: ModrinthId
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.+!@$()`,'\"-]+$")
    name: str = Field(min_length=1, max_length=100, pattern=_LINE)
    version_number: str = Field(min_length=1, max_length=64, pattern=_LINE)
    url: str = Field(max_length=400)
    sha512: str = Field(pattern=r"^[0-9a-f]{128}$")
    size: int = Field(gt=0, le=MAX_DATAPACK_BYTES)
    mc_version: McVersion

    @model_validator(mode="after")
    def _from_modrinth_cdn(self) -> Datapack:
        prefix = f"https://cdn.modrinth.com/data/{self.project_id}/versions/{self.version_id}/"
        if not self.url.startswith(prefix) or not _CDN_ZIP.fullmatch(self.url[len(prefix):]):
            raise ValueError("datapacks are downloaded from Modrinth's CDN only")
        return self


def _unique_datapacks(packs: list[Datapack]) -> list[Datapack]:
    if len({p.project_id for p in packs}) != len(packs):
        raise ValueError("a datapack appears twice")
    return packs


Datapacks = Annotated[list[Datapack], Field(max_length=MAX_DATAPACKS), AfterValidator(_unique_datapacks)]


def datapack_problems(mc_version: str, datapacks: list[Datapack]) -> list[str]:
    return [f"{d.name} {d.version_number} est prévu pour Minecraft {d.mc_version}." for d in datapacks
            if d.mc_version != mc_version]


# --- Configuration files of mods and plugins (docs/ARCHITECTURE.md 3.13) ----------------------

MAX_CONFIG_FILES = 200
MAX_CONFIG_FILE_BYTES = 256 * 1024
MAX_CONFIG_TOTAL_BYTES = 2 * 1024**2
#: Folders that hold settings (never mods, worlds or scripts).
CONFIG_DIRS = frozenset({"config", "defaultconfigs", "plugins"})
#: Paper's settings at the top of the server folder.
CONFIG_ROOT_FILES = frozenset({"bukkit.yml", "spigot.yml", "commands.yml", "permissions.yml", "help.yml"})
CONFIG_SUFFIXES = frozenset({".toml", ".json", ".json5", ".yml", ".yaml", ".properties", ".cfg", ".conf",
                             ".txt", ".snbt", ".ini"})
_CONFIG_PART = re.compile(r"^[A-Za-z0-9_+\-()\[\] ][A-Za-z0-9_.+\-()\[\] ]{0,99}$")
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})


def config_path_problem(path: str) -> str | None:
    """Why ``path`` is not a settings file the group may manage (None: it is one)."""
    parts = path.split("/")
    if len(path) > 300 or any(not _CONFIG_PART.fullmatch(p) or p.rstrip(" .") != p for p in parts):
        return "nom de fichier invalide"
    if any(p.split(".")[0].upper() in _RESERVED for p in parts):
        return "nom réservé par Windows"
    suffix = "." + parts[-1].rpartition(".")[2].lower() if "." in parts[-1] else ""
    if suffix not in CONFIG_SUFFIXES:
        return "ce n'est pas un fichier de réglages"
    if len(parts) == 1:
        return None if path in CONFIG_ROOT_FILES else "hors des dossiers de réglages"
    if parts[0] not in CONFIG_DIRS or (parts[0] == "plugins" and len(parts) < 3):
        return "hors des dossiers de réglages"
    return None


def _check_config_files(value: dict[str, str]) -> dict[str, str]:
    if len(value) > MAX_CONFIG_FILES:
        raise ValueError(f"at most {MAX_CONFIG_FILES} configuration files")
    total = 0
    for path, content in value.items():
        if (problem := config_path_problem(path)) is not None:
            raise ValueError(f"{path}: {problem}")
        size = len(content.encode("utf-8"))
        if size > MAX_CONFIG_FILE_BYTES or "\x00" in content:
            raise ValueError(f"{path}: too big, or not a text file")
        total += size
    if total > MAX_CONFIG_TOTAL_BYTES:
        raise ValueError("configuration files too big altogether")
    return value


#: Settings files written by every host before Minecraft starts: path -> content (UTF-8).
ConfigFiles = Annotated[dict[str, str], AfterValidator(_check_config_files)]


#: How the control plane picks the hosting PC (docs/ARCHITECTURE.md 3.15).
HostChoice = Literal["priority", "best"]


class TransferAddress(_Strict):
    """Where a player is sent with Minecraft's ``/transfer`` during a change of PC (ARCHITECTURE 4.18):
    a one-time address of the relay, valid a few minutes, for that player only."""

    player: PlayerName
    host: str = Field(min_length=3, max_length=255, pattern=r"^[a-z0-9.-]+$")
    port: int = Field(ge=1, le=65535)


class TransferAddressesRequest(_Strict):
    host_id: Id
    epoch: int = Field(ge=1)


#: Dimensions Chunky can pre-generate.
PregenDimension = Literal["overworld", "the_nether", "the_end"]


class PregenProgress(_Strict):
    """Pre-generation of the world by Chunky, as the hosting PC last saw it."""

    dimension: PregenDimension
    radius: int = Field(ge=0, le=100_000)
    state: Literal["running", "done", "cancelled", "no_space"]
    percent: float = Field(default=0.0, ge=0, le=100)
    #: Estimated time left.
    eta_s: int | None = Field(default=None, ge=0)


class SparkProfile(_Strict):
    """A spark profile run from the application (docs/ARCHITECTURE.md 4.17)."""

    state: Literal["running", "done", "failed"]
    #: The report spark uploaded (its viewer), once done.
    url: str | None = Field(default=None, pattern=r"^https://spark\.lucko\.me/[A-Za-z0-9]{5,30}$")


class PerfPoint(_Strict):
    """One point of the performance graphs: averages, and the worst moment, over its span."""

    at: datetime
    cpu_percent: float | None = None
    ram_used_mb: int | None = None
    tps: float | None = None
    tps_min: float | None = None
    mspt: float | None = None
    mspt_max: float | None = None
    players: int | None = None


class ServerPerformance(_Strict):
    hours: int
    #: Span of each point, in seconds.
    step_s: int
    points: list[PerfPoint]


class CrashReport(_Strict):
    """Why Minecraft stopped, as the hosting PC understood it (docs/ARCHITECTURE.md 4.16)."""

    cause: Literal["mod", "missing_dependency", "java", "memory", "world", "watchdog", "unknown"]
    #: One sentence in French, shown as it is.
    message: str = Field(min_length=1, max_length=300, pattern=_LINE)
    #: The mod that seems to be at fault, when one was found.
    mod_project_id: ModrinthId | None = None
    mod_name: str | None = Field(default=None, max_length=100, pattern=_LINE)


class LastCrash(CrashReport):
    at: datetime


def _unique_mods(mods: list[Mod]) -> list[Mod]:
    if len({m.project_id for m in mods}) != len(mods):
        raise ValueError("a project appears twice in the list of mods")
    return mods


Mods = Annotated[list[Mod], Field(max_length=MAX_MODS), AfterValidator(_unique_mods)]


def mod_problems(loader: Loader, mc_version: str, mods: list[Mod], map_enabled: bool) -> list[str]:
    """Why this list of mods cannot run on that server, in French (empty when it can)."""
    problems = []
    if mods and loader not in MOD_LOADERS:
        problems.append("Vanilla ne charge ni mod ni plugin : choisis Paper ou Fabric.")
    present = {m.project_id for m in mods}
    for m in mods:
        if m.loader != loader or m.mc_version != mc_version:
            problems.append(f"{m.name} {m.version_number} est prévu pour {m.loader.value} {m.mc_version}.")
        if any(r not in present for r in m.requires):
            problems.append(f"{m.name} a besoin d'un mod absent de la liste.")
    if map_enabled and not any(m.slug == MAP_MOD_SLUG for m in mods):
        problems.append("La carte du monde a besoin de BlueMap dans la liste des mods.")
    return problems


# --- Auth ---------------------------------------------------------------------

class RegisterRequest(_Strict):
    username: Username
    password: Password
    invite_code: str | None = Field(default=None, max_length=64)


class LoginRequest(_Strict):
    username: Username
    password: Password


class PasswordChange(_Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: Password


class RefreshRequest(_Strict):
    refresh_token: str = Field(min_length=20, max_length=128)


class TokenPair(_Strict):
    access_token: str
    refresh_token: str
    expires_in: int


class UserInfo(_Strict):
    user_id: Id
    username: str
    groups: list[GroupInfo]


# --- Groups -------------------------------------------------------------------

class GroupCreate(_Strict):
    name: DisplayName


class GroupInfo(_Strict):
    group_id: Id
    name: str
    role: Role
    can_host: bool


class MemberInfo(_Strict):
    user_id: Id
    username: str
    role: Role
    can_host: bool
    hosts: list[HostInfo]


class MemberUpdate(_Strict):
    role: Role | None = None
    can_host: bool | None = None


class InviteCreate(_Strict):
    max_uses: int = Field(default=1, ge=1, le=50)
    expires_in_s: int = Field(default=7 * 24 * 3600, ge=60, le=30 * 24 * 3600)


class InviteInfo(_Strict):
    code: str
    expires_at: datetime
    max_uses: int


class JoinRequest(_Strict):
    code: str = Field(min_length=8, max_length=64)


# --- Hosts --------------------------------------------------------------------

class HostRegister(_Strict):
    display_name: DisplayName
    os: Annotated[str, StringConstraints(max_length=64)]
    cpu_cores: int = Field(ge=1, le=512)
    total_ram_mb: int = Field(ge=512, le=4 * 1024 * 1024)


class HostUpdate(_Strict):
    display_name: DisplayName | None = None
    hosting_allowed: bool | None = None
    auto_accept_offers: bool | None = None
    max_ram_mb: int | None = Field(default=None, ge=1024)


class HostInfo(_Strict):
    host_id: Id
    display_name: str
    online: bool
    hosting_allowed: bool
    auto_accept_offers: bool
    max_ram_mb: int | None
    cpu_cores: int
    total_ram_mb: int


class ServerHostInfo(_Strict):
    host_id: Id
    display_name: str
    owner: str
    online: bool
    hosting_allowed: bool
    can_host: bool
    priority: int
    enabled: bool
    #: Last measures of that PC (docs/ARCHITECTURE.md 3.15), when it is online.
    rtt_ms: float | None = None
    upload_kbps: int | None = None
    cpu_score: float | None = None
    #: How good a host it is for this server, from 0 to 100 (automatic choice).
    score: float | None = None


class HostPriority(_Strict):
    priority: int = Field(ge=PRIORITY_MIN, le=PRIORITY_MAX)
    enabled: bool = True


# --- Servers ------------------------------------------------------------------

class ServerCreate(_Strict):
    name: DisplayName
    mc_version: McVersion
    loader: Loader = Loader.PAPER
    memory_mb: int = Field(default=6144, ge=1024, le=65536)
    properties: Properties = Field(default_factory=dict, max_length=64)
    preemption: Preemption = Preemption.WHEN_EMPTY
    #: Delay announced to players before a `countdown` preemption moves the server.
    preemption_countdown_s: int = Field(default=300, ge=30, le=3600)
    #: None = an unclean failover always waits for an admin confirmation.
    auto_failover_after_s: int | None = Field(default=None, ge=60)
    hot_backup_interval_s: int = Field(default=900, ge=60, le=6 * 3600)
    #: The relay starts the server when an allowed player tries to join it while it is stopped.
    start_on_join: bool = False
    #: Stop the server once nobody has played for this long (None = never).
    stop_when_empty_after_s: int | None = Field(default=None, ge=300, le=24 * 3600)
    #: Status page anyone with the address can open in a browser (shows who plays).
    public_status: bool = False
    #: Accept accounts not verified by Microsoft (TLauncher...). Forces the whitelist on.
    allow_offline_accounts: bool = False
    #: With unofficial accounts: True = each player joins through their own personal address;
    #: False = the server's address, the name alone checked against the list (anyone can
    #: take a listed name, since nothing verifies it).
    personal_addresses: bool = True
    #: Minecraft names allowed on the server when the whitelist is on (managed by the group).
    allowed_players: AllowedPlayers = Field(default_factory=list)
    #: Mods (Fabric) or plugins (Paper) from Modrinth, installed on whichever PC hosts.
    mods: Mods = Field(default_factory=list)
    #: World map (BlueMap) in a browser, with the status page.
    map_enabled: bool = False
    resource_pack: ResourcePack | None = None
    modpack: Modpack | None = None
    datapacks: Datapacks = Field(default_factory=list)
    host_choice: HostChoice = "priority"

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_properties(value)


class ServerPatch(_Strict):
    """Applied at the next start of the server (bumps ``config_revision``)."""

    name: DisplayName | None = None
    mc_version: McVersion | None = None
    loader: Loader | None = None
    memory_mb: int | None = Field(default=None, ge=1024, le=65536)
    properties: Properties | None = Field(default=None, max_length=64)
    preemption: Preemption | None = None
    preemption_countdown_s: int | None = Field(default=None, ge=30, le=3600)
    auto_failover_after_s: int | None = Field(default=None, ge=60)
    hot_backup_interval_s: int | None = Field(default=None, ge=60, le=6 * 3600)
    start_on_join: bool | None = None
    stop_when_empty_after_s: int | None = Field(default=None, ge=300, le=24 * 3600)
    public_status: bool | None = None
    allow_offline_accounts: bool | None = None
    personal_addresses: bool | None = None
    allowed_players: AllowedPlayers | None = None
    mods: Mods | None = None
    map_enabled: bool | None = None
    #: null removes the pack.
    resource_pack: ResourcePack | None = None
    #: null: the server no longer follows a modpack (its mods stay in the list).
    modpack: Modpack | None = None
    datapacks: Datapacks | None = None
    #: The whole set of managed settings files (replaces the previous one).
    config_files: ConfigFiles | None = None
    host_choice: HostChoice | None = None

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else validate_properties(value)


class MigrationInfo(_Strict):
    migration_id: Id
    source_host_id: Id
    target_host_id: Id
    reason: MigrationReason
    status: MigrationStatus
    countdown_s: int
    detail: str | None
    created_at: datetime


class ServerStatus(_Strict):
    server_id: Id
    group_id: Id
    name: str
    state: ServerState
    desired_running: bool
    mc_version: str
    loader: Loader
    memory_mb: int
    properties: Properties
    preemption: Preemption
    preemption_countdown_s: int
    auto_failover_after_s: int | None
    hot_backup_interval_s: int
    start_on_join: bool = False
    stop_when_empty_after_s: int | None = None
    #: When the server will stop for lack of players, if nobody joins before.
    stops_when_empty_at: datetime | None = None
    public_status: bool = False
    #: Link to the status page, when it is on.
    status_page_url: str | None = None
    allow_offline_accounts: bool
    personal_addresses: bool = True
    allowed_players: list[str]
    mods: list[Mod] = Field(default_factory=list)
    map_enabled: bool = False
    resource_pack: ResourcePack | None = None
    modpack: Modpack | None = None
    datapacks: list[Datapack] = Field(default_factory=list)
    #: Settings files managed by the group (their content: GET /servers/{id}/config-files).
    config_file_paths: list[str] = Field(default_factory=list)
    host_choice: HostChoice = "priority"
    #: World map in a browser, when it is on (served with the status page).
    map_url: str | None = None
    #: Players refused by the whitelist who asked to join, waiting for an admin.
    pending_access_requests: int = 0
    config_revision: int
    active_host_id: Id | None
    lease_epoch: int
    host_state: HostRuntimeState | None
    players_online: int | None
    head_version_id: Id | None
    last_backup_at: datetime | None
    #: The planned handover in progress, if any.
    migration: MigrationInfo | None = None
    #: Address players type in Minecraft (the VPS relay), the same whatever PC hosts.
    public_address: str | None = None
    address_label: str | None = None
    #: Players currently connected through the relay.
    relay_connections: int = 0
    #: Latest game health reported by the host (ticks per second, ms per tick).
    tps: float | None = None
    mspt: float | None = None
    #: Orphaned server with automatic failover: when another PC will be elected (estimate; a
    #: control-plane restart in between adds its startup quarantine).
    auto_failover_at: datetime | None = None
    #: Pre-generation of the world, while the server runs.
    pregen: PregenProgress | None = None
    #: Latest spark profile of this run of the server.
    profile: SparkProfile | None = None
    #: Latest crash with this configuration (gone once the configuration changes).
    last_crash: LastCrash | None = None


class PlayerAddress(_Strict):
    """Personal address of a player (servers accepting unofficial accounts)."""

    player: str
    address: str


class StopRequestBody(_Strict):
    countdown_s: int = Field(default=30, ge=0, le=600)


class TransferRequest(_Strict):
    #: None = the best eligible host other than the current one.
    target_host_id: Id | None = None
    #: Announced in the game before the current host stops.
    countdown_s: int = Field(default=60, ge=0, le=600)


class EventInfo(_Strict):
    event_id: int
    type: str
    server_id: Id | None
    host_id: Id | None
    actor_user_id: Id | None
    payload: dict[str, Any]
    created_at: datetime


class MetricsPoint(_Strict):
    at: datetime
    host_id: Id
    cpu_percent: float
    ram_used_mb: int
    players_online: int | None
    tps: float | None
    mspt: float | None


# --- Leases -------------------------------------------------------------------

class LeaseAcquire(_Strict):
    host_id: Id
    offer_id: Id


class ImportRequest(_Strict):
    host_id: Id


class LeaseRenew(_Strict):
    host_id: Id
    epoch: int = Field(ge=1)


class LeaseRelease(_Strict):
    host_id: Id
    epoch: int = Field(ge=1)
    final_version_id: Id | None = None


class LeaseGrant(_Strict):
    server_id: Id
    epoch: int
    ttl_s: float
    expires_at: datetime


class HostStateReport(_Strict):
    host_id: Id
    epoch: int = Field(ge=1)
    state: HostRuntimeState
    detail: str | None = Field(default=None, max_length=500)
    #: With "crashed": what went wrong, when the host could tell.
    crash: CrashReport | None = None


# --- Storage ------------------------------------------------------------------

class MissingBlobsRequest(_Strict):
    hashes: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]] = Field(
        max_length=10_000
    )


class MissingBlobsResponse(_Strict):
    missing: list[str]


class VersionCommit(_Strict):
    host_id: Id
    epoch: int = Field(ge=1)
    parent_version_id: Id | None
    kind: VersionKind
    label: str | None = Field(default=None, max_length=100)
    manifest: Manifest


class VersionInfo(_Strict):
    version_id: Id
    parent_version_id: Id | None
    kind: VersionKind
    epoch: int
    host_id: Id | None
    manifest_sha256: str
    total_size: int
    created_at: datetime
    pinned: bool
    label: str | None


class RestoreRequest(_Strict):
    version_id: Id


class DeleteServerRequest(_Strict):
    """The server's exact name, typed again: a slip of the finger cannot delete a world."""

    name: str = Field(min_length=1, max_length=64)


class NewWorldRequest(_Strict):
    """Start the server over with a new world; the old one stays in the history (ARCHITECTURE 3.17)."""

    #: Empty: Minecraft picks one at random.
    seed: WorldSeed = ""
    level_type: LevelType = "normal"
    generate_structures: bool = True


class PinRequest(_Strict):
    pinned: bool
    label: str | None = Field(default=None, max_length=100)


class ServerStorage(_Strict):
    server_id: Id
    name: str
    head_size: int
    versions: int


class StorageUsage(_Strict):
    used_bytes: int
    quota_bytes: int
    servers: list[ServerStorage]


UserInfo.model_rebuild()
MemberInfo.model_rebuild()


# --- Alerts for group admins (docs/API.md section 2 bis) ------------------------------------

class Alert(_Strict):
    #: vps_disk_low | vps_memory_low | vps_restarted | group_quota_high
    code: str
    severity: str = Field(pattern=r"^(warning|critical)$")
    group_id: Id | None = None
    details: dict[str, Any] = Field(default_factory=dict)


# --- Players and chat, seen from any PC of the group (docs/API.md section 4 bis) ----------

class ChatLine(_Strict):
    at: datetime
    #: Who wrote it; None for a line of the server itself (someone joined or left).
    player: PlayerName | None = None
    text: str = Field(max_length=256)


class ActivityReport(_Strict):
    """Sent by the lease holder when players come and go or talk. Kept in memory only."""

    host_id: Id
    epoch: int = Field(ge=1)
    players: list[PlayerName] = Field(default_factory=list, max_length=200)
    lines: list[ChatLine] = Field(default_factory=list, max_length=100)


class ServerActivity(_Strict):
    players: list[str]
    lines: list[ChatLine]
    updated_at: datetime | None = None


class ConsoleLine(_Strict):
    at: datetime
    text: str = Field(max_length=1000)


class ConsoleReport(_Strict):
    """Console lines of the running server, sent by the lease holder so that the group's admins
    and moderators can read them from any PC. Kept in memory only, never written to disk."""

    host_id: Id
    epoch: int = Field(ge=1)
    lines: list[ConsoleLine] = Field(default_factory=list, max_length=200)


class ConsoleLog(_Strict):
    lines: list[ConsoleLine]
    #: Pass it back as ``after`` to get only newer lines.
    next: int
    #: Lease epoch of the host the lines come from (a new one: a new console).
    epoch: int


# --- Access requests (docs/API.md section 4 ter) ---------------------------------------------

class AccessRequest(_Strict):
    """A player the whitelist refused, who asked to join by trying to connect; or a listed
    player with an unverified account, connecting from a place not seen before."""

    player: str
    first_at: datetime
    last_at: datetime
    attempts: int
    #: pending: waiting for an admin; refused: an admin said no (further attempts are counted
    #: without notifying anyone again).
    status: str
    kind: Literal["join", "new_place"] = "join"
    #: Network the attempt came from ("86.247.12.0/24"); empty for requests made before 0.5.0.
    place: str = ""


class AccessAnswer(_Strict):
    """Which request is answered: without ``place``, the player's request to join."""

    place: str | None = Field(default=None, max_length=64)


# --- Statistics (docs/API.md section 4 quater) -----------------------------------------------

class PlayerStat(_Strict):
    player: str
    seconds: float
    last_seen_at: datetime | None


class HostStat(_Strict):
    host_id: Id
    display_name: str
    owner: str
    seconds: float


class HourStat(_Strict):
    #: In the caller's time zone (``utc_offset_min``): 0 = Monday.
    weekday: int
    hour: int
    #: Average number of players while the server was up at that hour (None: never up then).
    average_players: float | None
    online_seconds: float


class ServerStats(_Strict):
    days: int
    online_seconds: float
    players: list[PlayerStat]
    hosts: list[HostStat]
    hours: list[HourStat]


# --- Application updates (docs/API.md section 8 ter) ----------------------------------------

#: Version of the application: "major.minor.patch".
AppVersion = Annotated[str, StringConstraints(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")]
#: Systems the application is published for (0.9.0): Windows, and macOS on Apple chips.
ClientPlatform = Literal["windows", "macos-arm64"]
CLIENT_PLATFORMS: tuple[str, ...] = ("windows", "macos-arm64")


def release_message(version: str, sha256: str, size: int, platform: str | None = None) -> bytes:
    """What the developer's update key signs: the installer's hash *and* its version number, so
    an old installer can never be passed off as a newer one. Since 0.9.0, the system too (except
    for Windows, unchanged so that older applications still recognise it): a Mac never takes a
    Windows installer for its own, nor the reverse."""
    if platform in (None, "windows"):
        return f"mdh-client-release/v1\n{version}\n{sha256}\n{size}\n".encode()
    return f"mdh-client-release/v2\n{platform}\n{version}\n{sha256}\n{size}\n".encode()


def release_file_name(version: str, platform: str | None = None) -> str:
    """The Windows installer, or the zipped macOS application."""
    if platform in (None, "windows"):
        return f"MinecraftDistributedHosting-Setup-{version}.exe"
    return f"MinecraftDistributedHosting-{version}-{platform}.zip"


class ClientRelease(_Strict):
    """Installer published on the control plane (``install.sh publish-client``).

    Signed offline, on the developer's PC: the control plane only stores and serves it, and the
    application checks the signature itself before running anything.
    """

    version: AppVersion
    #: None: Windows (the only system before 0.9.0; left out of the JSON for older applications).
    platform: ClientPlatform | None = None
    size: int = Field(gt=0, le=512 * 1024**2)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    #: Ed25519 signature of :func:`release_message`, base64.
    signature: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]{86}==$")]
    notes: str | None = Field(default=None, max_length=2000)
    published_at: datetime | None = None
