"""WebSocket messages exchanged between hosts and the control plane (docs/API.md section 8).

Every frame is a JSON object with a ``type`` discriminator. Parsing is strict: unknown types,
unknown fields and oversized frames are rejected.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from mdh_shared.constants import MAX_WS_MESSAGE_BYTES
from mdh_shared.models.api import Id, PlayerName, PregenDimension, PregenProgress, SparkProfile
from mdh_shared.models.enums import HostRuntimeState


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LeaseRef(_Msg):
    server_id: Id
    epoch: int = Field(ge=1)


class HostMetrics(_Msg):
    """Deliberately minimal: no host name, local IP or hardware identifiers."""

    cpu_percent: float = Field(ge=0, le=100)
    ram_used_mb: int = Field(ge=0)
    ram_free_mb: int = Field(ge=0)
    disk_free_mb: int = Field(ge=0)
    java_version: str | None = Field(default=None, max_length=32)
    mc_version: str | None = Field(default=None, max_length=32)
    players_online: int | None = Field(default=None, ge=0)
    tps: float | None = Field(default=None, ge=0, le=100)
    mspt: float | None = Field(default=None, ge=0)
    pregen: PregenProgress | None = None
    profile: SparkProfile | None = None


class Availability(_Msg):
    hosting_allowed: bool
    free_ram_mb: int = Field(ge=0)
    free_disk_mb: int = Field(ge=0)
    relay_ok: bool
    local_versions: dict[Id, Id] = Field(default_factory=dict, max_length=64)
    #: Measured by the PC (docs/ARCHITECTURE.md 3.15): round trip to the VPS, upload speed to
    #: the VPS, single-core speed (1.0 = a recent desktop).
    rtt_ms: float | None = Field(default=None, ge=0, le=60_000)
    upload_kbps: int | None = Field(default=None, ge=0, le=10_000_000)
    cpu_score: float | None = Field(default=None, ge=0, le=100)


# --- Client -> control plane --------------------------------------------------------------

class Hello(_Msg):
    type: Literal["hello"] = "hello"
    access_token: str = Field(min_length=20, max_length=128)
    host_id: Id | None = None
    client_version: str = Field(max_length=32)


class Heartbeat(_Msg):
    type: Literal["heartbeat"] = "heartbeat"
    seq: int = Field(ge=0)
    leases: tuple[LeaseRef, ...] = Field(default=(), max_length=16)
    metrics: HostMetrics | None = None
    availability: Availability | None = None


class OfferReply(_Msg):
    type: Literal["offer_reply"] = "offer_reply"
    offer_id: Id
    accept: bool
    reason: str | None = Field(default=None, max_length=200)


class PrefetchDone(_Msg):
    """The migration target downloaded the current world (or could not / would not)."""

    type: Literal["prefetch_done"] = "prefetch_done"
    migration_id: Id
    ok: bool
    reason: str | None = Field(default=None, max_length=200)


ClientMessage = Annotated[Hello | Heartbeat | OfferReply | PrefetchDone, Field(discriminator="type")]


# --- Control plane -> client --------------------------------------------------------------

class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    session_id: Id
    server_time: float
    heartbeat_interval_s: float


class LeaseStatus(_Msg):
    server_id: Id
    epoch: int
    ok: bool
    expires_in_s: float | None = None


class HeartbeatAck(_Msg):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    seq: int
    leases: tuple[LeaseStatus, ...] = ()


class HostingOffer(_Msg):
    type: Literal["hosting_offer"] = "hosting_offer"
    offer_id: Id
    server_id: Id
    reason: str
    expires_in_s: float
    prefetch_version_id: Id | None


class MigrationRequest(_Msg):
    """To the current host: announce, stop, upload the final world, release the lease."""

    type: Literal["migration_request"] = "migration_request"
    server_id: Id
    migration_id: Id
    target_host_id: Id
    countdown_s: int = Field(ge=0, le=3600)


class PrefetchRequest(_Msg):
    """To the migration target: download the current world now, without any lease."""

    type: Literal["prefetch_request"] = "prefetch_request"
    server_id: Id
    migration_id: Id
    version_id: Id | None


class MigrationCancel(_Msg):
    """To both ends: the migration is abandoned; the current host keeps (or resumes) hosting."""

    type: Literal["migration_cancel"] = "migration_cancel"
    server_id: Id
    migration_id: Id
    reason: str = Field(max_length=200)


class StopRequest(_Msg):
    type: Literal["stop_request"] = "stop_request"
    server_id: Id
    reason: str
    countdown_s: int = Field(ge=0, le=600)


class Announce(_Msg):
    kind: Literal["announce"] = "announce"
    #: One line of text: no line break or control character could ever start another command.
    message: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")


class PlayerAction(_Msg):
    kind: Literal["whitelist_add", "whitelist_remove", "op", "deop", "kick", "ban", "pardon"]
    player: PlayerName


class Restart(_Msg):
    kind: Literal["restart"] = "restart"


class PregenStart(_Msg):
    """Pre-generate a square around the world spawn with Chunky (fixed commands on the host)."""

    kind: Literal["pregen_start"] = "pregen_start"
    dimension: PregenDimension = "overworld"
    radius: int = Field(ge=100, le=20_000)


class PregenCancel(_Msg):
    kind: Literal["pregen_cancel"] = "pregen_cancel"


class ProfileStart(_Msg):
    """Profile the server with spark for a while (fixed command on the host)."""

    kind: Literal["profile_start"] = "profile_start"
    seconds: int = Field(default=60, ge=30, le=300)


#: Closed list of remote actions. There is intentionally no "run arbitrary command" action.
ServerAction = Annotated[Announce | PlayerAction | Restart | PregenStart | PregenCancel | ProfileStart,
                         Field(discriminator="kind")]


class ActionMessage(_Msg):
    type: Literal["action"] = "action"
    server_id: Id
    action_id: Id
    action: ServerAction


class RelayOpen(_Msg):
    """To the lease holder: a player is waiting on the relay; open the data WebSocket for it."""

    type: Literal["relay_open"] = "relay_open"
    conn_id: Id
    server_id: Id
    epoch: int
    #: One-time secret to present on ``/relay/data/{conn_id}`` (valid a few seconds).
    relay_token: str = Field(min_length=20, max_length=64)
    transport: Literal["tcp", "udp"] = "tcp"
    #: minecraft: a player; map: a browser showing the world map (BlueMap's local web server).
    target: Literal["minecraft", "map"] = "minecraft"


class ServerUpdate(_Msg):
    type: Literal["server_update"] = "server_update"
    server_id: Id
    state: str
    active_host_id: Id | None
    host_state: HostRuntimeState | None = None


class GroupUpdate(_Msg):
    """Members, roles or servers of a group changed: the application reads the group again (0.10.0).

    Older applications ignore this type of message (they skip what they cannot read)."""

    type: Literal["group_update"] = "group_update"
    group_id: Id


ServerMessage = Annotated[
    Welcome
    | HeartbeatAck
    | HostingOffer
    | MigrationRequest
    | PrefetchRequest
    | MigrationCancel
    | StopRequest
    | ActionMessage
    | RelayOpen
    | ServerUpdate
    | GroupUpdate,
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


class MessageTooLarge(ValueError):
    pass


def _check_size(raw: str | bytes) -> None:
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_WS_MESSAGE_BYTES:
        raise MessageTooLarge(f"frame of {size} bytes exceeds {MAX_WS_MESSAGE_BYTES}")


def parse_client_message(raw: str | bytes) -> ClientMessage:
    _check_size(raw)
    return _client_adapter.validate_json(raw)


def parse_server_message(raw: str | bytes) -> ServerMessage:
    _check_size(raw)
    return _server_adapter.validate_json(raw)


def encode(message: _Msg) -> str:
    return message.model_dump_json()
