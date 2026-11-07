"""Enumerations shared by the API, the database and the client."""

from enum import StrEnum


class Loader(StrEnum):
    VANILLA = "vanilla"
    PAPER = "paper"
    FABRIC = "fabric"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    #: Runs the game (kick, ban, pardon, announce) but changes no configuration nor backup.
    MODERATOR = "moderator"
    MEMBER = "member"

    @property
    def is_admin(self) -> bool:
        return self in (Role.OWNER, Role.ADMIN)

    @property
    def can_moderate(self) -> bool:
        return self is not Role.MEMBER


class ServerState(StrEnum):
    """Lifecycle of a logical server (docs/ARCHITECTURE.md 2.4)."""

    OFFLINE = "offline"
    ELECTING = "electing"          # a hosting offer is pending
    ASSIGNED = "assigned"          # lease granted, host is downloading the world
    STARTING = "starting"
    RUNNING = "running"
    MIGRATING = "migrating"        # planned handover in progress
    STOPPING = "stopping"          # final snapshot being taken and uploaded
    UNREACHABLE = "unreachable"    # heartbeats missing, lease not yet expired
    ORPHANED = "orphaned"          # lease expired without a final commit: needs a decision


class HostRuntimeState(StrEnum):
    """States reported by the host holding the lease."""

    DOWNLOADING = "downloading"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


class VersionKind(StrEnum):
    FINAL = "final"        # taken after the Minecraft process exited
    HOT = "hot"            # taken with save-off / save-all flush while running
    MANUAL = "manual"      # explicit user backup
    RESTORE = "restore"    # new head created from an older version


class Preemption(StrEnum):
    """What happens when a higher-priority host comes back online."""

    NEVER = "never"
    WHEN_EMPTY = "when_empty"
    COUNTDOWN = "countdown"


class MigrationReason(StrEnum):
    TRANSFER = "transfer"          # asked by a user
    PREEMPTION = "preemption"      # a higher-priority host came back


class MigrationStatus(StrEnum):
    """Planned handover (docs/ARCHITECTURE.md 3.5)."""

    PREFETCHING = "prefetching"    # the target downloads the current world, the game goes on
    STOPPING = "stopping"          # the source announces, stops and uploads its final world
    HANDOVER = "handover"          # lease released: the target was offered the server
    COMPLETED = "completed"        # the target runs the server
    CANCELLED = "cancelled"        # stopped before the source released: it keeps hosting
    FAILED = "failed"              # the target could not take over: normal election

    @property
    def active(self) -> bool:
        return self in (MigrationStatus.PREFETCHING, MigrationStatus.STOPPING, MigrationStatus.HANDOVER)
