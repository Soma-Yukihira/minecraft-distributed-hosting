"""The coordinator: elections, hosting offers, leases and server lifecycle.

Rules followed everywhere in this module:

* Every decision re-reads the database state first, and writes inside one ``BEGIN IMMEDIATE``
  transaction **without awaiting in between**. Messages are sent only after the commit.
  Combined with the single process, two decisions can never interleave.
* The lease (``coordination.lease``) is the only source of truth for "who may write the world".
  Server states are a user-facing view derived from it.
* Nothing here ever deletes a world version or hands the world to a new host while the previous
  one may still be writing (expiry + grace, enforced by ``lease.acquire``).
* A planned migration (docs/ARCHITECTURE.md 3.5) only hands the world over after the current
  host *released* its lease, i.e. after its final commit. Until then any failure cancels the
  migration and the current host simply keeps hosting.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mdh_server import events
from mdh_server.perf import PerfRecorder
from mdh_server.stats import FLUSH_EVERY_S, StatsRecorder
from mdh_server.api.errors import ApiError, conflict
from mdh_server.context import AppContext
from mdh_server.coordination import lease as lease_rules
from mdh_server.coordination.lease import LeaseError, LeaseState
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.coordination.lease_store import save as save_lease
from mdh_server.database.db import immediate
from mdh_server.election.scoring import (BEST_PREEMPT_MARGIN, Candidate, Requirements, performance_score,
                                         rank_candidates, should_preempt)
from mdh_server.realtime import Session
from mdh_server.relay import addresses
from mdh_shared.constants import OFFER_TIMEOUT_S
from mdh_shared.models.api import CrashReport, LastCrash, MigrationInfo, ServerStatus
from mdh_shared.models.enums import (
    HostRuntimeState,
    Loader,
    MigrationReason,
    MigrationStatus,
    Preemption,
    ServerState,
)
from mdh_shared.protocol.messages import (
    Availability,
    Heartbeat,
    HeartbeatAck,
    HostingOffer,
    HostMetrics,
    LeaseStatus,
    MigrationCancel,
    MigrationRequest,
    OfferReply,
    PrefetchDone,
    PrefetchRequest,
    ServerUpdate,
    StopRequest,
)

log = logging.getLogger("mdh.coordinator")

#: States in which a live host holds the lease.
HELD_STATES = frozenset(
    {ServerState.ASSIGNED, ServerState.STARTING, ServerState.RUNNING, ServerState.STOPPING,
     ServerState.MIGRATING}
)
RUNTIME_TO_STATE = {
    HostRuntimeState.DOWNLOADING: ServerState.ASSIGNED,
    HostRuntimeState.STARTING: ServerState.STARTING,
    HostRuntimeState.RUNNING: ServerState.RUNNING,
    HostRuntimeState.STOPPING: ServerState.STOPPING,
}
#: Missing renewals for this many heartbeat intervals marks the host unreachable.
UNREACHABLE_AFTER_INTERVALS = 2.5
METRICS_HISTORY = 360  # one hour at one point every 10 s
#: A member removed while hosting (docs/ARCHITECTURE.md 4.21): countdown announced to the players
#: before the server moves or stops, how often to try again, and when the lease is no longer
#: renewed (a PC that ignores the requests still loses the server, by its lease).
DEPARTURE_COUNTDOWN_S = 60
DEPARTURE_RETRY_S = 60.0
DEPARTURE_DEADLINE_S = 1800.0


class ProtocolError(Exception):
    """The peer broke the WebSocket protocol; its session is closed."""


@dataclass
class Presence:
    host_id: str
    user_id: str
    session_id: str
    connected_at: float = 0.0
    seq: int = -1
    availability: Availability | None = None
    metrics: HostMetrics | None = None


def _ts(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


class Coordinator:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.presence: dict[str, Presence] = {}
        self.runtime: dict[str, HostRuntimeState] = {}
        self.metrics: dict[str, deque[tuple[float, str, HostMetrics]]] = {}
        #: Latest crash explained by the host: (when, config revision, report). Memory only.
        self.crashes: dict[str, tuple[float, int, CrashReport]] = {}
        self._last_renewal: dict[str, float] = {}
        #: Hosts skipped for the current election round of a server (declined / timed out).
        self._excluded: dict[str, set[str]] = {}
        #: Servers for which "no eligible host" is already journaled (avoid one event per tick).
        self._waiting_noted: set[str] = set()
        #: No automatic preemption of a server before this time (after a failed attempt).
        self._preempt_after: dict[str, float] = {}
        #: Since when a running server set to "stop when empty" has had nobody on it.
        self._empty_since: dict[str, float] = {}
        #: Servers whose lease is held by an admin's PC uploading a world (no Minecraft running).
        self._importing: set[str] = set()
        #: Players and last chat lines reported by the holder, by server (api/routes_admin.py).
        self.activity: dict[str, Any] = {}
        #: Console lines reported by the holder, by server (api/routes_admin.py), memory only.
        self.console: dict[str, Any] = {}
        #: Play time, busy hours, hosting time (docs/ARCHITECTURE.md 4.13), written now and then.
        self.stats = StatsRecorder()
        self.perf = PerfRecorder()
        #: Last attempt to move a server off the PC of a leaving member.
        self._departure_tried: dict[str, float] = {}

    # ------------------------------------------------------------------ helpers

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.conn

    def _server(self, server_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        if row is None:
            raise KeyError(server_id)
        return row

    def _set_state(self, server_id: str, state: ServerState) -> None:
        self.conn.execute("UPDATE servers SET state = ? WHERE id = ?", (state.value, server_id))

    def _event(self, srv: sqlite3.Row, type: str, **kwargs) -> None:
        events.record(self.conn, group_id=srv["group_id"], server_id=srv["id"], type=type,
                      now=self.ctx.now(), **kwargs)

    def active_migration(self, server_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM migrations WHERE server_id = ?"
            " AND status IN ('prefetching', 'stopping', 'handover')", (server_id,)).fetchone()

    def _set_migration(self, mig: sqlite3.Row, status: MigrationStatus, detail: str | None = None) -> None:
        self.conn.execute(
            "UPDATE migrations SET status = ?, detail = COALESCE(?, detail), updated_at = ? WHERE id = ?",
            (status.value, detail, self.ctx.now(), mig["id"]))

    def _held_state(self, server_id: str) -> ServerState:
        """User-facing state of a server whose host keeps its lease (from what it last reported)."""
        runtime = self.runtime.get(server_id, HostRuntimeState.RUNNING)
        return RUNTIME_TO_STATE.get(runtime, ServerState.RUNNING)

    def _isolated(self, lease: LeaseState, now: float) -> bool:
        policy = self.ctx.settings.lease
        out_of_quarantine = now >= self.ctx.started_at + policy.startup_quarantine_s
        return lease.holder is None or (lease.is_isolated_from(now, policy) and out_of_quarantine)

    def status(self, server_id: str) -> ServerStatus:
        srv = self._server(server_id)
        lease = load_lease(self.conn, server_id)
        now = self.ctx.now()
        last_backup = self.conn.execute(
            "SELECT MAX(created_at) FROM world_versions WHERE server_id = ?", (server_id,)
        ).fetchone()[0]
        history = self.metrics.get(server_id)
        latest = history[-1][2] if history else None
        players = latest.players_online if latest else None
        active = lease.holder if lease.is_active(now) else None
        mig = self.active_migration(server_id)
        migration = None if mig is None else MigrationInfo(
            migration_id=mig["id"], source_host_id=mig["source_host_id"],
            target_host_id=mig["target_host_id"], reason=MigrationReason(mig["reason"]),
            status=MigrationStatus(mig["status"]), countdown_s=mig["countdown_s"],
            detail=mig["detail"], created_at=_ts(mig["created_at"]))
        return ServerStatus(
            server_id=srv["id"],
            group_id=srv["group_id"],
            name=srv["name"],
            state=ServerState(srv["state"]),
            desired_running=bool(srv["desired_running"]),
            mc_version=srv["mc_version"],
            loader=Loader(srv["loader"]),
            memory_mb=srv["memory_mb"],
            properties=json.loads(srv["properties_json"]),
            preemption=Preemption(srv["preemption"]),
            preemption_countdown_s=srv["preemption_countdown_s"],
            auto_failover_after_s=srv["auto_failover_after_s"],
            hot_backup_interval_s=srv["hot_backup_interval_s"],
            start_on_join=bool(srv["start_on_join"]),
            stop_when_empty_after_s=srv["stop_when_empty_after_s"],
            stops_when_empty_at=_ts(self._empty_since[server_id] + srv["stop_when_empty_after_s"])
            if server_id in self._empty_since and srv["stop_when_empty_after_s"] else None,
            public_status=bool(srv["public_status"]),
            status_page_url=addresses.status_page_url(self.ctx.settings, self.conn, srv, self.ctx.relay.port)
            if srv["public_status"] and self.ctx.settings.relay_enabled else None,
            allow_offline_accounts=bool(srv["allow_offline_accounts"]),
            personal_addresses=bool(srv["personal_addresses"]),
            allowed_players=json.loads(srv["allowed_players_json"]),
            mods=json.loads(srv["mods_json"]),
            resource_pack=json.loads(srv["resource_pack_json"]) if srv["resource_pack_json"] else None,
            modpack=json.loads(srv["modpack_json"]) if srv["modpack_json"] else None,
            datapacks=json.loads(srv["datapacks_json"]),
            config_file_paths=sorted(json.loads(srv["config_files_json"])),
            host_choice=srv["host_choice"],
            map_enabled=bool(srv["map_enabled"]),
            map_url=addresses.status_page_url(self.ctx.settings, self.conn, srv, self.ctx.relay.port) + "map/"
            if srv["map_enabled"] and srv["public_status"] and self.ctx.settings.relay_enabled else None,
            pending_access_requests=self.conn.execute(
                "SELECT COUNT(*) FROM access_requests WHERE server_id = ? AND status = 'pending'",
                (server_id,)).fetchone()[0],
            config_revision=srv["config_revision"],
            active_host_id=active,
            lease_epoch=lease.epoch,
            host_state=self.runtime.get(server_id) if active else None,
            players_online=players if active else None,
            head_version_id=srv["head_version_id"],
            last_backup_at=_ts(last_backup),
            migration=migration,
            public_address=(addresses.server_address(self.ctx.settings, self.conn, srv, self.ctx.relay.port)
                            if self.ctx.settings.relay_enabled else None),
            address_label=srv["address_label"],
            relay_connections=self.ctx.relay.connections(server_id),
            tps=latest.tps if active and latest else None,
            mspt=latest.mspt if active and latest else None,
            auto_failover_at=_ts(self._auto_failover_at(srv, lease)),
            pregen=latest.pregen if active and latest else None,
            profile=latest.profile if active and latest else None,
            last_crash=self._last_crash(srv),
        )

    def _last_crash(self, srv: sqlite3.Row) -> LastCrash | None:
        """Shown until the configuration changes (an admin removed the mod, updated it...)."""
        crash = self.crashes.get(srv["id"])
        if crash is None or crash[1] != srv["config_revision"]:
            return None
        at, _, report = crash
        return LastCrash(at=_ts(at), **report.model_dump())

    def _auto_failover_at(self, srv: sqlite3.Row, lease: LeaseState) -> float | None:
        """Same deadline as the sweep in tick() uses."""
        if (srv["state"] != ServerState.ORPHANED.value or not srv["desired_running"]
                or srv["auto_failover_after_s"] is None or lease.expires_at is None):
            return None
        return lease.expires_at + max(self.ctx.settings.lease.grace_s, srv["auto_failover_after_s"])

    def forget_server(self, server_id: str) -> None:
        """The server was deleted: drop what is kept about it in memory."""
        for table in (self.metrics, self.crashes, self._last_renewal, self._excluded, self._preempt_after,
                      self._empty_since, self.activity, self.console):
            table.pop(server_id, None)
        self._waiting_noted.discard(server_id)
        self._importing.discard(server_id)
        self.stats.forget(server_id)

    async def broadcast(self, server_id: str) -> None:
        self.ctx.relay.server_changed(server_id)  # players waiting on "Connexion…" look again
        status = self.status(server_id)
        await self.ctx.hub.send_to_group(
            self.conn,
            status.group_id,
            ServerUpdate(server_id=server_id, state=status.state.value,
                         active_host_id=status.active_host_id, host_state=status.host_state),
        )

    # ------------------------------------------------------------------ presence

    async def host_connected(self, session: Session) -> None:
        assert session.host_id is not None
        self.presence[session.host_id] = Presence(session.host_id, session.user_id, session.session_id,
                                                  connected_at=self.ctx.now())
        self.conn.execute("UPDATE hosts SET last_seen_at = ? WHERE id = ?",
                          (self.ctx.now(), session.host_id))
        await self._offer_resume(session.host_id)

    def host_disconnected(self, session: Session) -> None:
        # Server states are not changed here: a reconnect is common, and liveness is decided by
        # lease renewals in tick().
        p = self.presence.get(session.host_id or "")
        if p is not None and p.session_id == session.session_id:
            del self.presence[session.host_id]  # type: ignore[arg-type]

    async def heartbeat(self, session: Session, msg: Heartbeat) -> HeartbeatAck:
        p = self.presence.get(session.host_id or "")
        if p is None or p.session_id != session.session_id:
            raise ProtocolError("heartbeat from a session that is not the host's current one")
        if msg.seq <= p.seq:
            raise ProtocolError("heartbeat sequence numbers must strictly increase")
        p.seq = msg.seq
        if msg.availability is not None:
            p.availability = msg.availability
        if msg.metrics is not None:
            p.metrics = msg.metrics
        now = self.ctx.now()
        statuses: list[LeaseStatus] = []
        restored: list[str] = []
        for ref in msg.leases:
            try:
                state, was_restored = self._renew(ref.server_id, p.host_id, ref.epoch)
            except (LeaseError, KeyError):
                statuses.append(LeaseStatus(server_id=ref.server_id, epoch=ref.epoch, ok=False))
                continue
            assert state.expires_at is not None
            statuses.append(LeaseStatus(server_id=ref.server_id, epoch=ref.epoch, ok=True,
                                        expires_in_s=state.expires_at - now))
            if msg.metrics is not None:
                self.metrics.setdefault(ref.server_id, deque(maxlen=METRICS_HISTORY)).append(
                    (now, p.host_id, msg.metrics))
                activity = self.activity.get(ref.server_id)
                self.stats.observe(ref.server_id, p.host_id, list(activity.players) if activity else [],
                                   msg.metrics.players_online, now)
                self.perf.observe(ref.server_id, msg.metrics, now)
            if was_restored:
                restored.append(ref.server_id)
        for server_id in restored:
            await self.broadcast(server_id)
        return HeartbeatAck(seq=msg.seq, leases=tuple(statuses))

    async def offer_reply(self, session: Session, msg: OfferReply) -> None:
        if msg.accept:
            return  # acceptance is the HTTP lease acquisition itself
        offer = self.conn.execute(
            "SELECT * FROM offers WHERE id = ? AND host_id = ? AND status = 'pending'",
            (msg.offer_id, session.host_id),
        ).fetchone()
        if offer is None:
            return
        srv = self._server(offer["server_id"])
        with immediate(self.conn):
            self.conn.execute("UPDATE offers SET status = 'declined' WHERE id = ?", (offer["id"],))
            self._event(srv, "offer_declined", host_id=session.host_id, reason=msg.reason)
        self._excluded.setdefault(srv["id"], set()).add(offer["host_id"])
        if offer["reason"] == "election":
            await self._elect(srv["id"])
        elif offer["reason"] == "migration":
            await self._migration_failed(srv["id"], "target_declined")

    # ------------------------------------------------------------------ user actions

    async def request_start(self, server_id: str, actor_user_id: str | None,
                            player: str | None = None) -> None:
        """Someone wants the server up: a member (``actor_user_id``) or, through the relay, a
        player trying to join a sleeping server (``player``)."""
        srv = self._server(server_id)
        state = ServerState(srv["state"])
        if state == ServerState.ORPHANED:
            raise conflict(
                "needs_failover_confirmation",
                "the previous host disappeared without a final save: an admin must confirm "
                "restarting from the last backup",
            )
        with immediate(self.conn):
            self.conn.execute("UPDATE servers SET desired_running = 1 WHERE id = ?", (server_id,))
            self._event(srv, "start_requested", actor_user_id=actor_user_id,
                        **({"player": player} if player else {}))
        if state == ServerState.OFFLINE:
            self._excluded.pop(server_id, None)
            self._waiting_noted.discard(server_id)
            await self._elect(server_id)
        await self.broadcast(server_id)

    async def request_stop(self, server_id: str, actor_user_id: str | None, countdown_s: int,
                           reason: str | None = None) -> None:
        srv = self._server(server_id)
        state = ServerState(srv["state"])
        mig = self.active_migration(server_id)
        holder = load_lease(self.conn, server_id).holder
        with immediate(self.conn):
            self.conn.execute("UPDATE servers SET desired_running = 0 WHERE id = ?", (server_id,))
            if mig is not None:
                # Stopping wins over a planned migration: the current host stops for good (a host
                # in the middle of migrating out turns it into a plain stop when it gets the request).
                self._set_migration(mig, MigrationStatus.CANCELLED, "stop_requested")
                self._event(srv, "migration_cancelled", host_id=mig["source_host_id"],
                            target_host_id=mig["target_host_id"], detail="stop_requested")
            if state == ServerState.ELECTING or (state == ServerState.MIGRATING and holder is None):
                self.conn.execute(
                    "UPDATE offers SET status = 'expired' WHERE server_id = ? AND status = 'pending'",
                    (server_id,))
                self._set_state(server_id, ServerState.OFFLINE)
            elif state in HELD_STATES:
                self._set_state(server_id, ServerState.STOPPING)
            self._event(srv, "stop_requested", actor_user_id=actor_user_id, countdown_s=countdown_s,
                        **({"reason": reason} if reason else {}))
        if mig is not None and mig["status"] != MigrationStatus.HANDOVER:
            await self.ctx.hub.send_to_host(mig["target_host_id"], MigrationCancel(
                server_id=server_id, migration_id=mig["id"], reason="stop_requested"))
        if holder is not None and (state in HELD_STATES or state == ServerState.UNREACHABLE):
            await self.ctx.hub.send_to_host(
                holder, StopRequest(server_id=server_id, reason="requested", countdown_s=countdown_s))
        await self.broadcast(server_id)

    async def request_transfer(self, server_id: str, *, target_host_id: str | None, countdown_s: int,
                               reason: MigrationReason, actor_user_id: str | None) -> str:
        """Start a planned migration of a running server (docs/ARCHITECTURE.md 3.5, step 1)."""
        srv = self._server(server_id)
        now = self.ctx.now()
        lease = load_lease(self.conn, server_id)
        if self.active_migration(server_id) is not None:
            raise conflict("migration_in_progress", "a change of host is already in progress")
        if ServerState(srv["state"]) != ServerState.RUNNING or not lease.is_active(now):
            raise conflict("not_running", "only a running server can change host")
        source = lease.holder
        assert source is not None
        if target_host_id == source:
            raise conflict("already_hosting", "this PC already hosts the server")
        result = rank_candidates(self._candidates(srv),
                                 self._requirements(srv, excluded=frozenset({source})))
        eligible = [r.host_id for r in result.ranked]
        if target_host_id is None:
            if not eligible:
                raise ApiError(409, "no_eligible_host", "no other PC can host this server right now",
                               details={"rejected": {h: [r.value for r in reasons]
                                                     for h, reasons in result.rejected.items()}})
            target = eligible[0]
        elif target_host_id in eligible:
            target = target_host_id
        elif target_host_id in result.rejected:
            raise ApiError(409, "target_not_eligible", "this PC cannot host the server right now",
                           details={"reasons": [r.value for r in result.rejected[target_host_id]]})
        else:
            raise ApiError(404, "not_found", "host not found")

        migration_id = uuid.uuid4().hex
        with immediate(self.conn):
            self.conn.execute(
                "INSERT INTO migrations (id, server_id, source_host_id, target_host_id, reason, status,"
                " countdown_s, actor_user_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'prefetching', ?, ?, ?, ?)",
                (migration_id, server_id, source, target, reason.value, countdown_s, actor_user_id, now, now))
            self._set_state(server_id, ServerState.MIGRATING)
            self._event(srv, "migration_started", host_id=source, actor_user_id=actor_user_id,
                        target_host_id=target, reason=reason.value, countdown_s=countdown_s)
        sent = await self.ctx.hub.send_to_host(target, PrefetchRequest(
            server_id=server_id, migration_id=migration_id, version_id=srv["head_version_id"]))
        if not sent:
            await self._cancel_migration(server_id, "target_offline")
            raise conflict("target_offline", "the chosen PC is not connected")
        await self.broadcast(server_id)
        return migration_id

    async def cancel_transfer(self, server_id: str, actor_user_id: str) -> None:
        mig = self.active_migration(server_id)
        if mig is None:
            raise conflict("no_migration", "no change of host is in progress")
        if mig["status"] == MigrationStatus.HANDOVER:
            raise conflict("too_late", "the current host already handed the world over")
        await self._cancel_migration(server_id, "cancelled_by_user", actor_user_id=actor_user_id)

    async def prefetch_done(self, session: Session, msg: PrefetchDone) -> None:
        """Step 2 done: the target has (most of) the world; ask the current host to hand over."""
        mig = self.conn.execute("SELECT * FROM migrations WHERE id = ?", (msg.migration_id,)).fetchone()
        if (mig is None or mig["target_host_id"] != session.host_id
                or mig["status"] != MigrationStatus.PREFETCHING):
            return  # stale (cancelled meanwhile) or not addressed to this host
        server_id = mig["server_id"]
        if not msg.ok:
            await self._cancel_migration(server_id, "prefetch_failed", reason=msg.reason)
            return
        srv = self._server(server_id)
        with immediate(self.conn):
            self._set_migration(mig, MigrationStatus.STOPPING)
            self._event(srv, "migration_prefetched", host_id=mig["target_host_id"])
        sent = await self.ctx.hub.send_to_host(mig["source_host_id"], MigrationRequest(
            server_id=server_id, migration_id=mig["id"], target_host_id=mig["target_host_id"],
            countdown_s=mig["countdown_s"]))
        if not sent:
            await self._cancel_migration(server_id, "source_unreachable")
            return
        await self.broadcast(server_id)

    async def _cancel_migration(self, server_id: str, detail: str, *, actor_user_id: str | None = None,
                                reason: str | None = None) -> bool:
        """Abandon a migration before the handover: the current host keeps (or resumes) hosting."""
        mig = self.active_migration(server_id)
        if mig is None or mig["status"] == MigrationStatus.HANDOVER:
            return False
        srv = self._server(server_id)
        with immediate(self.conn):
            self._set_migration(mig, MigrationStatus.CANCELLED, detail)
            if ServerState(srv["state"]) == ServerState.MIGRATING:
                # A source already stopping for the handover restarts Minecraft right away.
                stopping = self.runtime.get(server_id) == HostRuntimeState.STOPPING
                self._set_state(server_id, ServerState.STARTING if stopping else self._held_state(server_id))
            self._event(srv, "migration_cancelled", host_id=mig["source_host_id"], actor_user_id=actor_user_id,
                        target_host_id=mig["target_host_id"], detail=detail, reason=reason)
        self._preempt_after[server_id] = self.ctx.now() + self.ctx.settings.preemption_retry_s
        cancel = MigrationCancel(server_id=server_id, migration_id=mig["id"], reason=detail)
        await self.ctx.hub.send_to_host(mig["source_host_id"], cancel)
        await self.ctx.hub.send_to_host(mig["target_host_id"], cancel)
        await self.broadcast(server_id)
        return True

    async def _offer_migration(self, mig: sqlite3.Row) -> None:
        """Steps 6-7: the source released its lease after its final commit; offer the target."""
        server_id = mig["server_id"]
        srv = self._server(server_id)
        offer_id = uuid.uuid4().hex
        now = self.ctx.now()
        with immediate(self.conn):
            self._set_migration(mig, MigrationStatus.HANDOVER)
            self.conn.execute(
                "INSERT INTO offers (id, server_id, host_id, created_at, expires_at, status, reason)"
                " VALUES (?, ?, ?, ?, ?, 'pending', 'migration')",
                (offer_id, server_id, mig["target_host_id"], now, now + OFFER_TIMEOUT_S))
            self._event(srv, "migration_handover", host_id=mig["target_host_id"],
                        version_id=srv["head_version_id"])
        sent = await self.ctx.hub.send_to_host(mig["target_host_id"], HostingOffer(
            offer_id=offer_id, server_id=server_id, reason="migration", expires_in_s=OFFER_TIMEOUT_S,
            prefetch_version_id=srv["head_version_id"]))
        if not sent:
            await self._migration_failed(server_id, "target_offline")

    async def _migration_failed(self, server_id: str, detail: str) -> None:
        """The target could not take over after the handover: back to a normal election.

        Nothing is lost: the source's final world is the head. The target is skipped once, so
        the election usually gives the server back to the previous host.
        """
        mig = self.active_migration(server_id)
        if mig is None:
            return
        srv = self._server(server_id)
        holder = load_lease(self.conn, server_id).holder
        with immediate(self.conn):
            self._set_migration(mig, MigrationStatus.FAILED, detail)
            self.conn.execute(
                "UPDATE offers SET status = 'expired' WHERE server_id = ? AND status = 'pending'"
                " AND reason = 'migration'", (server_id,))
            if ServerState(srv["state"]) == ServerState.MIGRATING and holder is None:
                self._set_state(server_id, ServerState.OFFLINE)
            self._event(srv, "migration_failed", host_id=mig["target_host_id"], detail=detail)
        self._preempt_after[server_id] = self.ctx.now() + self.ctx.settings.preemption_retry_s
        self._excluded[server_id] = {mig["target_host_id"]}
        await self._elect(server_id)
        await self.broadcast(server_id)

    async def confirm_failover(self, server_id: str, actor_user_id: str) -> None:
        srv = self._server(server_id)
        if ServerState(srv["state"]) != ServerState.ORPHANED:
            raise conflict("not_orphaned", "failover confirmation only applies to an orphaned server")
        lease = load_lease(self.conn, server_id)
        now = self.ctx.now()
        if not self._isolated(lease, now):
            assert lease.expires_at is not None
            wait = max(lease.expires_at + self.ctx.settings.lease.grace_s,
                       self.ctx.started_at + self.ctx.settings.lease.startup_quarantine_s) - now
            raise ApiError(409, "not_isolated_yet",
                           f"the previous host may still be writing; retry in {math.ceil(wait)} s",
                           headers={"Retry-After": str(math.ceil(wait))})
        with immediate(self.conn):
            self.conn.execute(
                "UPDATE offers SET status = 'expired' WHERE server_id = ? AND status = 'pending'",
                (server_id,))
            self.conn.execute(
                "UPDATE servers SET desired_running = 1, state = 'offline' WHERE id = ?", (server_id,))
            self._event(srv, "failover_confirmed", actor_user_id=actor_user_id,
                        previous_host_id=lease.holder, previous_epoch=lease.epoch,
                        restart_from_version=srv["head_version_id"])
        self._excluded.pop(server_id, None)
        self._waiting_noted.discard(server_id)
        await self._elect(server_id)
        await self.broadcast(server_id)

    # ------------------------------------------------------------------ host actions

    async def acquire(self, server_id: str, host_id: str, offer_id: str) -> LeaseState:
        offer = self.conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if offer is None or offer["server_id"] != server_id or offer["host_id"] != host_id:
            raise ApiError(410, "offer_invalid", "this hosting offer does not exist for this host")
        now = self.ctx.now()
        if offer["status"] != "pending" or offer["expires_at"] <= now:
            raise ApiError(410, "offer_expired", "this hosting offer is no longer valid")
        srv = self._server(server_id)
        state = ServerState(srv["state"])
        expected = {"election": ServerState.ELECTING, "resume": ServerState.ORPHANED,
                    "migration": ServerState.MIGRATING}[offer["reason"]]
        if state != expected:
            raise conflict("unexpected_state", f"server is {state.value}")
        with immediate(self.conn):
            new = lease_rules.acquire(
                load_lease(self.conn, server_id), host_id, now, self.ctx.settings.lease,
                control_plane_started_at=self.ctx.started_at,
            )
            save_lease(self.conn, new, now, granted=True)
            self.conn.execute("UPDATE offers SET status = 'accepted' WHERE id = ?", (offer_id,))
            self.conn.execute(
                "UPDATE offers SET status = 'expired' WHERE server_id = ? AND status = 'pending'",
                (server_id,))
            self._set_state(server_id, ServerState.ASSIGNED)
            if offer["reason"] == "migration":
                self.conn.execute(
                    "UPDATE migrations SET target_epoch = ?, updated_at = ? WHERE server_id = ?"
                    " AND status = 'handover' AND target_host_id = ?", (new.epoch, now, server_id, host_id))
            self._event(srv, "lease_granted", host_id=host_id, epoch=new.epoch,
                        resumed=offer["reason"] == "resume", migration=offer["reason"] == "migration")
        self._last_renewal[server_id] = now
        self.runtime[server_id] = HostRuntimeState.DOWNLOADING
        self._excluded.pop(server_id, None)
        self._waiting_noted.discard(server_id)
        await self.broadcast(server_id)
        return new

    async def grant_import(self, server_id: str, host_id: str, actor_user_id: str) -> LeaseState:
        """Lease for an admin's PC to upload a world from elsewhere (docs/ARCHITECTURE.md 3.8).

        Same rules as hosting: a new epoch, only once nobody else can write, renewed and released
        the same way. Minecraft never runs under it, and nothing is offered to players.
        """
        srv = self._server(server_id)
        if (ServerState(srv["state"]) != ServerState.OFFLINE or srv["desired_running"]
                or self.active_migration(server_id) is not None):
            raise conflict("server_busy", "stop the server before importing a world")
        now = self.ctx.now()
        with immediate(self.conn):
            new = lease_rules.acquire(load_lease(self.conn, server_id), host_id, now,
                                      self.ctx.settings.lease,
                                      control_plane_started_at=self.ctx.started_at)
            save_lease(self.conn, new, now, granted=True)
            self._set_state(server_id, ServerState.ASSIGNED)
            self._event(srv, "import_started", host_id=host_id, epoch=new.epoch,
                        actor_user_id=actor_user_id)
        self._importing.add(server_id)
        self._last_renewal[server_id] = now
        self.runtime[server_id] = HostRuntimeState.DOWNLOADING
        await self.broadcast(server_id)
        return new

    async def renew(self, server_id: str, host_id: str, epoch: int) -> LeaseState:
        state, restored = self._renew(server_id, host_id, epoch)
        if restored:
            await self.broadcast(server_id)
        return state

    def _renew(self, server_id: str, host_id: str, epoch: int) -> tuple[LeaseState, bool]:
        now = self.ctx.now()
        restored = False
        leaving = self.conn.execute(
            "SELECT m.leaving_at FROM memberships m JOIN hosts h ON h.user_id = m.user_id"
            " JOIN servers s ON s.group_id = m.group_id WHERE h.id = ? AND s.id = ?",
            (host_id, server_id)).fetchone()
        if leaving is not None and leaving[0] is not None and now - leaving[0] > DEPARTURE_DEADLINE_S:
            raise lease_rules.LeaseExpired("the owner of this PC was removed from the group")
        with immediate(self.conn):
            new = lease_rules.renew(load_lease(self.conn, server_id), host_id, epoch, now,
                                    self.ctx.settings.lease)
            save_lease(self.conn, new, now, granted=False)
            srv = self._server(server_id)
            if ServerState(srv["state"]) == ServerState.UNREACHABLE:
                runtime = self.runtime.get(server_id, HostRuntimeState.DOWNLOADING)
                self._set_state(server_id, RUNTIME_TO_STATE.get(runtime, ServerState.ASSIGNED))
                self._event(srv, "host_reachable_again", host_id=host_id, epoch=epoch)
                restored = True
        self._last_renewal[server_id] = now
        return new, restored

    async def release(self, server_id: str, host_id: str, epoch: int,
                      final_version_id: str | None) -> None:
        srv = self._server(server_id)
        mig = self.active_migration(server_id)
        # The source of a migration releases after its final commit: that is the handover.
        handover = (mig is not None and mig["status"] == MigrationStatus.STOPPING
                    and mig["source_host_id"] == host_id and bool(srv["desired_running"]))
        with immediate(self.conn):
            new = lease_rules.release(load_lease(self.conn, server_id), host_id, epoch)
            save_lease(self.conn, new, self.ctx.now(), granted=False)
            self._set_state(server_id, ServerState.MIGRATING if handover else ServerState.OFFLINE)
            self._event(srv, "lease_released", host_id=host_id, epoch=epoch,
                        final_version_id=final_version_id)
            if mig is not None and not handover:
                # Released for another reason (app closed, crashes, the target gave up): the
                # migration ends here and a normal election follows.
                failed = mig["status"] == MigrationStatus.HANDOVER
                detail = "target_failed" if failed else "source_stopped"
                self._set_migration(mig, MigrationStatus.FAILED if failed else MigrationStatus.CANCELLED,
                                    detail)
                self._event(srv, "migration_failed" if failed else "migration_cancelled",
                            host_id=host_id, target_host_id=mig["target_host_id"], detail=detail)
        self.runtime.pop(server_id, None)
        self._last_renewal.pop(server_id, None)
        self._importing.discard(server_id)
        self.stats.forget(server_id)
        self.ctx.relay.cut_server(server_id, "lease released")
        if mig is not None and not handover:
            self._preempt_after[server_id] = self.ctx.now() + self.ctx.settings.preemption_retry_s
            if mig["status"] != MigrationStatus.HANDOVER and mig["target_host_id"] != host_id:
                await self.ctx.hub.send_to_host(mig["target_host_id"], MigrationCancel(
                    server_id=server_id, migration_id=mig["id"], reason="source_stopped"))
        if handover:
            assert mig is not None
            await self._offer_migration(mig)
        elif self._server(server_id)["desired_running"]:
            # The host left (closed the app, gave up after crashes...): find someone else.
            self._excluded[server_id] = {host_id}
            await self._elect(server_id)
        await self.broadcast(server_id)

    async def report_state(self, server_id: str, host_id: str, epoch: int,
                           runtime: HostRuntimeState, detail: str | None,
                           crash: CrashReport | None = None) -> None:
        now = self.ctx.now()
        lease_rules.check_fence(load_lease(self.conn, server_id), host_id, epoch, now)
        srv = self._server(server_id)
        current = ServerState(srv["state"])
        previous_runtime = self.runtime.get(server_id)
        self.runtime[server_id] = runtime
        target: ServerState | None = None
        # While migrating, the state stays "migrating" until the handover is over.
        if runtime != HostRuntimeState.CRASHED and current in HELD_STATES and current != ServerState.MIGRATING:
            if not (current == ServerState.STOPPING and runtime != HostRuntimeState.STOPPING):
                target = RUNTIME_TO_STATE[runtime]
        mig = self.active_migration(server_id)
        completed = (mig is not None and mig["status"] == MigrationStatus.HANDOVER
                     and mig["target_host_id"] == host_id and runtime == HostRuntimeState.RUNNING)
        with immediate(self.conn):
            if target is not None and target != current:
                self._set_state(server_id, target)
            if runtime != previous_runtime or runtime == HostRuntimeState.CRASHED:
                extra = {} if crash is None else {"cause": crash.cause, "mod_name": crash.mod_name,
                                                  "explanation": crash.message}
                self._event(srv, f"host_{runtime.value}", host_id=host_id, epoch=epoch, detail=detail, **extra)
            if crash is not None:
                self.crashes[server_id] = (now, srv["config_revision"], crash)
            if completed:
                assert mig is not None
                self._set_migration(mig, MigrationStatus.COMPLETED)
                self._event(srv, "migration_completed", host_id=host_id,
                            source_host_id=mig["source_host_id"],
                            duration_s=round(now - mig["created_at"], 1))
        await self.broadcast(server_id)

    # ------------------------------------------------------------------ election

    def _candidates(self, srv: sqlite3.Row) -> list[Candidate]:
        rows = self.conn.execute(
            "SELECT h.id, h.hosting_allowed, h.max_ram_mb, h.cpu_cores, m.can_host,"
            " COALESCE(p.priority, 50) AS priority, COALESCE(p.enabled, 1) AS enabled"
            " FROM hosts h JOIN memberships m ON m.user_id = h.user_id AND m.group_id = ?"
            " LEFT JOIN server_host_prefs p ON p.host_id = h.id AND p.server_id = ?",
            (srv["group_id"], srv["id"]),
        ).fetchall()
        candidates = []
        for r in rows:
            presence = self.presence.get(r["id"])
            online = presence is not None and self.ctx.hub.is_online(r["id"])
            avail = presence.availability if presence else None
            free_ram = avail.free_ram_mb if avail else 0
            if r["max_ram_mb"] is not None and srv["memory_mb"] > r["max_ram_mb"]:
                free_ram = 0  # the owner capped what this PC may give to Minecraft
            candidates.append(Candidate(
                host_id=r["id"],
                priority=r["priority"],
                online=online,
                hosting_allowed=bool(r["hosting_allowed"]) and bool(avail and avail.hosting_allowed),
                can_host=bool(r["can_host"]),
                enabled_for_server=bool(r["enabled"]),
                free_ram_mb=free_ram,
                free_disk_mb=avail.free_disk_mb if avail else 0,
                cpu_cores=r["cpu_cores"],
                latency_ms=avail.rtt_ms if avail else None,
                relay_ok=bool(avail and avail.relay_ok),
                local_version_id=avail.local_versions.get(srv["id"]) if avail else None,
                cpu_score=avail.cpu_score if avail else None,
                upload_kbps=avail.upload_kbps if avail else None,
            ))
        return candidates

    def _requirements(self, srv: sqlite3.Row, excluded: frozenset[str] | None = None) -> Requirements:
        size = 0
        if srv["head_version_id"]:
            size = self.conn.execute("SELECT total_size FROM world_versions WHERE id = ?",
                                     (srv["head_version_id"],)).fetchone()[0]
        return Requirements(
            memory_mb=srv["memory_mb"],
            world_size_mb=math.ceil(size / 1024**2),
            head_version_id=srv["head_version_id"],
            excluded_host_ids=(frozenset(self._excluded.get(srv["id"], ())) if excluded is None
                               else excluded),
        )

    async def _elect(self, server_id: str) -> str | None:
        """Offer the server to the best eligible host, or record that none is available."""
        srv = self._server(server_id)
        state = ServerState(srv["state"])
        if not srv["desired_running"] or state not in (ServerState.OFFLINE, ServerState.ELECTING):
            return None
        result = rank_candidates(self._candidates(srv), self._requirements(srv), srv["host_choice"])
        winner = result.winner
        if winner is None:
            with immediate(self.conn):
                if state != ServerState.OFFLINE:
                    self._set_state(server_id, ServerState.OFFLINE)
                if server_id not in self._waiting_noted:
                    self._event(srv, "no_eligible_host",
                                rejected={h: [r.value for r in reasons]
                                          for h, reasons in result.rejected.items()})
            self._waiting_noted.add(server_id)
            if state != ServerState.OFFLINE:
                await self.broadcast(server_id)
            return None

        offer_id = uuid.uuid4().hex
        now = self.ctx.now()
        with immediate(self.conn):
            self.conn.execute(
                "INSERT INTO offers (id, server_id, host_id, created_at, expires_at, status, reason)"
                " VALUES (?, ?, ?, ?, ?, 'pending', 'election')",
                (offer_id, server_id, winner, now, now + OFFER_TIMEOUT_S))
            self._set_state(server_id, ServerState.ELECTING)
            self._event(srv, "offer_sent", host_id=winner,
                        ranking=[r.host_id for r in result.ranked])
        self._waiting_noted.discard(server_id)
        sent = await self.ctx.hub.send_to_host(winner, HostingOffer(
            offer_id=offer_id, server_id=server_id, reason="election",
            expires_in_s=OFFER_TIMEOUT_S, prefetch_version_id=srv["head_version_id"]))
        if not sent:
            with immediate(self.conn):
                self.conn.execute("UPDATE offers SET status = 'expired' WHERE id = ?", (offer_id,))
            self._excluded.setdefault(server_id, set()).add(winner)
            return await self._elect(server_id)
        if state != ServerState.ELECTING:
            await self.broadcast(server_id)
        return winner

    async def _offer_resume(self, host_id: str) -> None:
        """A host that vanished comes back: let it resume and upload what it played."""
        rows = self.conn.execute(
            "SELECT s.id FROM servers s JOIN leases l ON l.server_id = s.id"
            " WHERE l.holder_host_id = ? AND s.state = 'orphaned' AND NOT EXISTS"
            " (SELECT 1 FROM offers o WHERE o.server_id = s.id AND o.status = 'pending')",
            (host_id,)).fetchall()
        for row in rows:
            srv = self._server(row["id"])
            offer_id = uuid.uuid4().hex
            now = self.ctx.now()
            with immediate(self.conn):
                self.conn.execute(
                    "INSERT INTO offers (id, server_id, host_id, created_at, expires_at, status, reason)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', 'resume')",
                    (offer_id, srv["id"], host_id, now, now + OFFER_TIMEOUT_S))
                self._event(srv, "resume_offered", host_id=host_id)
            await self.ctx.hub.send_to_host(host_id, HostingOffer(
                offer_id=offer_id, server_id=srv["id"], reason="resume",
                expires_in_s=OFFER_TIMEOUT_S, prefetch_version_id=srv["head_version_id"]))

    # ------------------------------------------------------------------ periodic sweep

    async def tick(self) -> None:
        now = self.ctx.now()
        policy = self.ctx.settings.lease
        self.ctx.relay.sweep()
        if self.stats.flushed_at is None or now - self.stats.flushed_at >= FLUSH_EVERY_S:
            self.stats.flush(self.conn, now)
            self.perf.flush(self.conn, now)

        for offer in self.conn.execute(
            "SELECT * FROM offers WHERE status = 'pending' AND expires_at <= ?", (now,)
        ).fetchall():
            srv = self._server(offer["server_id"])
            with immediate(self.conn):
                self.conn.execute("UPDATE offers SET status = 'expired' WHERE id = ?", (offer["id"],))
                self._event(srv, "offer_expired", host_id=offer["host_id"])
            self._excluded.setdefault(srv["id"], set()).add(offer["host_id"])
            if offer["reason"] == "election":
                await self._elect(srv["id"])
            elif offer["reason"] == "migration":
                await self._migration_failed(srv["id"], "offer_expired")

        await self._sweep_migrations(now)

        unreachable_after = UNREACHABLE_AFTER_INTERVALS * self.ctx.settings.heartbeat_interval_s
        for r in self.conn.execute(
            "SELECT s.id, s.state, s.desired_running, s.auto_failover_after_s,"
            " l.holder_host_id, l.expires_at, l.epoch"
            " FROM servers s JOIN leases l ON l.server_id = s.id WHERE l.holder_host_id IS NOT NULL"
        ).fetchall():
            state = ServerState(r["state"])
            srv = self._server(r["id"])
            if now >= r["expires_at"]:
                if r["id"] in self._importing:
                    # The importing PC vanished: nothing ran, whatever it committed is the head.
                    self._importing.discard(r["id"])
                    with immediate(self.conn):
                        freed = lease_rules.release(load_lease(self.conn, r["id"]), r["holder_host_id"],
                                                    r["epoch"])
                        save_lease(self.conn, freed, now, granted=False)
                        self._set_state(r["id"], ServerState.OFFLINE)
                        self._event(srv, "import_abandoned", host_id=r["holder_host_id"], epoch=r["epoch"])
                    self.runtime.pop(r["id"], None)
                    if srv["desired_running"]:
                        await self._elect(r["id"])
                    await self.broadcast(r["id"])
                elif state in HELD_STATES or state == ServerState.UNREACHABLE:
                    await self._cancel_migration(r["id"], "source_lost")
                    with immediate(self.conn):
                        self._set_state(r["id"], ServerState.ORPHANED)
                        self._event(srv, "lease_expired", host_id=r["holder_host_id"],
                                    epoch=r["epoch"], last_version_id=srv["head_version_id"])
                    self.runtime.pop(r["id"], None)
                    await self.broadcast(r["id"])
                    # The holder may already be back online (it reconnected while its lease was
                    # still valid): it will not "connect" again, so offer the resume now.
                    if self.ctx.hub.is_online(r["holder_host_id"]):
                        await self._offer_resume(r["holder_host_id"])
                elif (state == ServerState.ORPHANED and r["desired_running"]
                      and r["auto_failover_after_s"] is not None
                      and now >= r["expires_at"] + max(policy.grace_s, r["auto_failover_after_s"])
                      and self._isolated(load_lease(self.conn, r["id"]), now)):
                    with immediate(self.conn):
                        self.conn.execute(
                            "UPDATE offers SET status = 'expired' WHERE server_id = ? AND status = 'pending'",
                            (r["id"],))
                        self._set_state(r["id"], ServerState.OFFLINE)
                        self._event(srv, "auto_failover", previous_host_id=r["holder_host_id"],
                                    previous_epoch=r["epoch"])
                    self._excluded.pop(r["id"], None)
                    await self._elect(r["id"])
                    await self.broadcast(r["id"])
            elif state in HELD_STATES:
                last = self._last_renewal.get(r["id"], self.ctx.started_at)
                if now - last > unreachable_after:
                    await self._cancel_migration(r["id"], "source_unreachable")
                    with immediate(self.conn):
                        self._set_state(r["id"], ServerState.UNREACHABLE)
                        self._event(srv, "host_unreachable", host_id=r["holder_host_id"],
                                    seconds_since_renewal=round(now - last, 1))
                    await self.broadcast(r["id"])

        for r in self.conn.execute(
            "SELECT id FROM servers WHERE desired_running = 1 AND state = 'offline'"
        ).fetchall():
            await self._elect(r["id"])

        for r in self.conn.execute(
            "SELECT id FROM servers WHERE state = 'running' AND preemption != 'never'"
        ).fetchall():
            await self._maybe_preempt(r["id"], now)

        await self._stop_empty_servers(now)
        await self._departures(now)

    # ------------------------------------------------------------------ members leaving

    def hosted_by(self, group_id: str, user_id: str) -> list[str]:
        """Servers of the group whose lease a PC of that member holds right now. An expired lease
        does not count: that PC can no longer write (a vanished PC leaves an orphaned server,
        which an admin restarts from the last backup), so it must not hold the departure up."""
        return [r[0] for r in self.conn.execute(
            "SELECT l.server_id FROM leases l JOIN servers s ON s.id = l.server_id"
            " JOIN hosts h ON h.id = l.holder_host_id WHERE s.group_id = ? AND h.user_id = ? AND l.expires_at > ?",
            (group_id, user_id, self.ctx.now()))]

    async def start_departure(self, group_id: str, user_id: str, actor_user_id: str) -> None:
        """A member removed (or leaving) while hosting: no new hosting for them, and their servers
        move to another PC, or stop when none can take them. ``_departures`` ends it."""
        now = self.ctx.now()
        with immediate(self.conn):
            self.conn.execute(
                "UPDATE memberships SET can_host = 0, leaving_at = COALESCE(leaving_at, ?),"
                " leaving_by = COALESCE(leaving_by, ?) WHERE group_id = ? AND user_id = ?",
                (now, actor_user_id, group_id, user_id))
            events.record(self.conn, group_id=group_id, type="member_leaving", now=now,
                          actor_user_id=actor_user_id, member_id=user_id)
        await self._move_off(group_id, user_id, actor_user_id, now)
        await self.ctx.hub.group_changed(self.conn, group_id)

    async def _move_off(self, group_id: str, user_id: str, actor_user_id: str | None, now: float) -> None:
        for server_id in self.hosted_by(group_id, user_id):
            srv = self._server(server_id)
            if (self.active_migration(server_id) is not None or not srv["desired_running"]
                    or now - self._departure_tried.get(server_id, -math.inf) < DEPARTURE_RETRY_S):
                continue  # already moving or stopping, or tried a moment ago
            if ServerState(srv["state"]) != ServerState.RUNNING:
                continue  # starting, or its PC is unreachable: tried again by the sweep
            self._departure_tried[server_id] = now
            try:
                await self.request_transfer(server_id, target_host_id=None, countdown_s=DEPARTURE_COUNTDOWN_S,
                                            reason=MigrationReason.TRANSFER, actor_user_id=actor_user_id)
            except ApiError:  # no other PC can take it right now
                await self.request_stop(server_id, actor_user_id, DEPARTURE_COUNTDOWN_S, reason="member_leaving")

    async def _departures(self, now: float) -> None:
        for r in self.conn.execute(
                "SELECT group_id, user_id, leaving_by FROM memberships WHERE leaving_at IS NOT NULL").fetchall():
            if self.hosted_by(r["group_id"], r["user_id"]):
                await self._move_off(r["group_id"], r["user_id"], r["leaving_by"], now)
                continue
            with immediate(self.conn):
                self.conn.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?",
                                  (r["group_id"], r["user_id"]))
                events.record(self.conn, group_id=r["group_id"], type="member_removed", now=now,
                              actor_user_id=r["leaving_by"], member_id=r["user_id"])
            await self.ctx.hub.group_changed(self.conn, r["group_id"], r["user_id"])

    def _nobody_plays(self, server_id: str, now: float) -> bool:
        """Known to be empty: the host reported no player recently, and none comes through the
        relay. Unknown (no fresh report) is never taken for empty."""
        history = self.metrics.get(server_id)
        fresh = 3 * self.ctx.settings.heartbeat_interval_s
        pregen = history[-1][2].pregen if history else None
        if pregen is not None and pregen.state == "running":
            return False  # the world is being pre-generated: nobody plays, but it is not idle
        return (bool(history) and now - history[-1][0] <= fresh and history[-1][2].players_online == 0
                and self.ctx.relay.players(server_id) == 0)

    async def _stop_empty_servers(self, now: float) -> None:
        """"Stop when empty" (docs/ARCHITECTURE.md 4.7): a clean stop, with its final backup."""
        watched = self.conn.execute(
            "SELECT id, stop_when_empty_after_s FROM servers WHERE state = 'running' AND desired_running = 1"
            " AND stop_when_empty_after_s IS NOT NULL").fetchall()
        for server_id in set(self._empty_since) - {r["id"] for r in watched}:
            del self._empty_since[server_id]
        for r in watched:
            if not self._nobody_plays(r["id"], now):
                self._empty_since.pop(r["id"], None)
                continue
            since = self._empty_since.setdefault(r["id"], now)
            if now - since >= r["stop_when_empty_after_s"]:
                del self._empty_since[r["id"]]
                log.info("stopping %s: nobody played for %ss", r["id"], r["stop_when_empty_after_s"])
                await self.request_stop(r["id"], None, countdown_s=0, reason="empty")

    async def _sweep_migrations(self, now: float) -> None:
        """Cancel migrations that cannot finish before the handover (the source keeps hosting)."""
        for mig in self.conn.execute(
            "SELECT * FROM migrations WHERE status IN ('prefetching', 'stopping')"
        ).fetchall():
            if not self.ctx.hub.is_online(mig["target_host_id"]):
                await self._cancel_migration(mig["server_id"], "target_offline")
            elif (mig["status"] == MigrationStatus.PREFETCHING
                  and now - mig["created_at"] > self.ctx.settings.migration_prefetch_timeout_s):
                await self._cancel_migration(mig["server_id"], "prefetch_timeout")

    async def _maybe_preempt(self, server_id: str, now: float) -> None:
        """A higher-priority PC is back (docs/ARCHITECTURE.md 3.6): move the server to it."""
        if now < self._preempt_after.get(server_id, 0.0) or self.active_migration(server_id) is not None:
            return
        srv = self._server(server_id)
        lease = load_lease(self.conn, server_id)
        if lease.holder is None or not lease.is_active(now):
            return
        candidates = self._candidates(srv)
        priorities = {c.host_id: c.priority for c in candidates}
        mode = srv["host_choice"]
        req = self._requirements(srv, excluded=frozenset({lease.holder}))
        ranked = rank_candidates(candidates, req, mode).ranked
        if not ranked:
            return
        if mode == "best":
            # Clearly better than the PC running it (whose memory already goes to Minecraft).
            current = next((c for c in candidates if c.host_id == lease.holder), None)
            here = performance_score(current, req, extra_ram_mb=srv["memory_mb"]) if current else 0.0
            if ranked[0].score - here < BEST_PREEMPT_MARGIN:
                return
        elif not should_preempt(priorities.get(lease.holder, 50), ranked[0].priority):
            return
        if self.conn.execute(
            "SELECT 1 FROM migrations WHERE server_id = ? AND reason = 'transfer' AND status = 'completed'"
            " AND source_host_id = ? AND target_host_id = ? AND target_epoch = ?",
            (server_id, ranked[0].host_id, lease.holder, lease.epoch),
        ).fetchone():
            return  # someone deliberately moved the server off that PC: respect it for this session
        challenger = self.presence.get(ranked[0].host_id)
        if challenger is None or now - challenger.connected_at < self.ctx.settings.preemption_stable_s:
            return  # wait until it is clearly back, not just flapping
        policy = Preemption(srv["preemption"])
        countdown = srv["preemption_countdown_s"]
        if policy == Preemption.WHEN_EMPTY:
            history = self.metrics.get(server_id)
            fresh = 3 * self.ctx.settings.heartbeat_interval_s
            if not history or now - history[-1][0] > fresh or history[-1][2].players_online != 0:
                return
            pregen = history[-1][2].pregen
            if pregen is not None and pregen.state == "running":
                return  # nobody plays, but Chunky would start over on the other PC
            countdown = 0
        try:
            await self.request_transfer(server_id, target_host_id=ranked[0].host_id, countdown_s=countdown,
                                        reason=MigrationReason.PREEMPTION, actor_user_id=None)
        except ApiError as exc:
            log.info("preemption of %s not started: %s", server_id, exc.code)
            self._preempt_after[server_id] = now + self.ctx.settings.preemption_retry_s
