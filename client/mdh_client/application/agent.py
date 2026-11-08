"""The host agent: everything the PC does on behalf of the group, without any GUI.

Responsibilities
* keep a session with the control plane (login, DPAPI-protected refresh token, WebSocket);
* publish availability and metrics in heartbeats, renew the lease of the hosted server;
* accept hosting offers (automatically or after asking the user), then install and run Minecraft;
* **self-fence**: stop Minecraft as soon as the lease can no longer be proven valid;
* stop gracefully on request, restart after a crash within limits, release the lease when done;
* planned migrations: as the target, prefetch the world without a lease; as the current host,
  announce, stop, upload the final world and release -- or keep hosting if anything fails.

The agent runs on its own asyncio loop (see :mod:`mdh_client.application.runtime`). It reports
to the outside world only through ``emit(event, **payload)``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import random
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from mdh_client import credentials
from mdh_client.config import ClientSettings, Layout
from mdh_client.coordination.lease_guard import LeaseGuard
from mdh_client.application import templates
from mdh_client.minecraft import (anticheat, bluemap, config_files, crash, datapacks, gamerules, ip_bans, jvm, modpack,
                                  pregen, proxy_protocol, resource_pack, server_files)
from mdh_client.minecraft import mods as mod_files
from mdh_client.minecraft.downloads import make_client
from mdh_client.minecraft.modrinth import Modrinth, ModrinthError
from mdh_client.minecraft.installer import Installer, PreparedServer
from mdh_client.minecraft.log_parser import LogEvent
from mdh_client.minecraft.process import CommandRejected, MinecraftProcess, ProcessState
from mdh_client.monitoring import resources
from mdh_client.minecraft.sources import SourceResolver
from mdh_client.networking.api_client import ApiClient, ApiError, ApiUnavailable, check_base_url
from mdh_client.networking.control_channel import ControlChannel
from mdh_client.networking.relay_tunnel import run_tunnel
from mdh_client.synchronization import world_files
from mdh_client.system import benchmark
from mdh_client.system.awake import KeepAwake
from mdh_client.system.updates import ANNOUNCE_SPREAD_S, ReadyUpdate, Updater, is_newer
from mdh_client.synchronization.engine import LeaseContext, LeaseLost, NotEnoughSpace, SyncEngine, SyncError
from mdh_shared import __version__, versions
from mdh_shared.constants import LEASE_SAFETY_MARGIN_S, MIN_SERVER_VERSION
from mdh_shared.models.api import (MAP_MOD_SLUG, MAX_CONFIG_FILE_BYTES, PREGEN_MOD_SLUG, SPARK_MOD_SLUG,
                                   CrashReport, Datapack, Mod, Modpack, PregenProgress, ResourcePack,
                                   SparkProfile, anticheat_enabled, anticheat_slugs, validate_properties)
from mdh_shared.models.enums import Loader
from mdh_shared.protocol import proxy
from mdh_shared.protocol.messages import (
    ActionMessage,
    HeartbeatAck,
    HostingOffer,
    MigrationCancel,
    MigrationRequest,
    PrefetchRequest,
    RelayOpen,
    ServerUpdate,
    GroupUpdate,
    ClientReleaseNotice,
    StopRequest,
)

log = logging.getLogger("mdh.agent")

Emit = Callable[..., None]
AskOffer = Callable[[dict[str, Any]], Awaitable[bool]]
PrepareFn = Callable[[Loader, str, Callable[[str, float | None], None]], Awaitable[PreparedServer]]
CommandFactory = Callable[[PreparedServer, int], list[str]]

OFFER_DECISION_TIMEOUT_S = 25.0
#: How often a group admin's PC asks the VPS for alerts (disk, quota, restart after a crash).
ALERTS_EVERY_S = 300.0
#: Chat lines are sent to the control plane in batches, at most this often.
ACTIVITY_BATCH_S = 1.5
#: Full pass over the group's servers for standby copies (new backups trigger one at once).
STANDBY_REFRESH_S = 600.0
STOP_ANNOUNCE_AT = (1800, 900, 600, 300, 120, 60, 30, 10, 5)
#: Measures for the automatic choice of the host (docs/ARCHITECTURE.md 3.15).
SPEEDTEST_BYTES = 2 * 1024 * 1024
MEASURE_EVERY_S = 6 * 3600
#: After /transfer, time for the players' game to reconnect to the relay before Minecraft stops.
TRANSFER_GRACE_S = 2.0
_TRANSFER_HOST = re.compile(r"[a-z0-9.-]{3,255}")
_PLAYER = re.compile(r"[A-Za-z0-9_]{3,16}")


@dataclass
class Hosted:
    server_id: str
    name: str
    epoch: int
    guard: LeaseGuard
    ttl_s: float
    phase: str = "preparing"        # preparing | starting | running | stopping | fenced
    process: MinecraftProcess | None = None
    restarts: collections.deque[float] = field(default_factory=lambda: collections.deque(maxlen=20))
    stopping: bool = False
    mc_version: str | None = None
    java_major: int | None = None
    task: asyncio.Task | None = None
    monitor: resources.ProcessMonitor | None = None
    server: dict[str, Any] = field(default_factory=dict)
    #: Serialises world uploads (hot backups vs. final backup on stop).
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    hot_task: asyncio.Task | None = None
    prepared: PreparedServer | None = None
    #: The task running the current stop (a second stop request waits for it).
    stop_task: asyncio.Task | None = None
    #: Planned migration this stop hands the server over for (None: plain stop).
    migration_id: str | None = None
    migration_cancelled: bool = False
    #: Ends a stop countdown early (migration cancelled, or the app is closing).
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    #: Players connected through the VPS relay (one task per connection).
    tunnels: set[asyncio.Task] = field(default_factory=set)
    #: Minecraft reads a PROXY protocol header first (Paper): each tunnel must write one.
    proxy_protocol: bool = False
    #: Address of each player seen through the relay (lower-case name): "ban-ip <name>" uses it.
    player_ips: dict[str, str] = field(default_factory=dict)
    #: Minecraft's banned-ips.json of this server, enforced by the tunnels (any server type).
    bans: ip_bans.BanList | None = None
    #: Re-reads the group's configuration after a server_update (one at a time; an update that
    #: arrives meanwhile sets config_dirty and is read by the same task).
    config_task: asyncio.Task | None = None
    config_dirty: bool = False
    #: Chat and comings and goings not yet reported to the control plane (for the other PCs).
    activity_lines: list[dict[str, Any]] = field(default_factory=list)
    activity_task: asyncio.Task | None = None
    #: Console lines not yet sent (read-only view of the console on the other PCs).
    console_lines: list[dict[str, Any]] = field(default_factory=list)
    console_task: asyncio.Task | None = None
    #: Pre-generation by Chunky, as last seen in the console (a PregenProgress), and its world.
    pregen: dict[str, Any] | None = None
    pregen_world: str | None = None
    #: spark profile started from the application (a SparkProfile).
    profile: dict[str, Any] | None = None

    @property
    def lease(self) -> LeaseContext:
        return LeaseContext(self.server["group_id"], self.server_id, self.server["_host_id"], self.epoch)


class HostAgent:
    def __init__(
        self,
        layout: Layout,
        settings: ClientSettings,
        *,
        emit: Emit = lambda *a, **k: None,
        ask_offer: AskOffer | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        prepare: PrepareFn | None = None,
        command_factory: CommandFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        ready_timeout_s: float = 600.0,
        stop_timeout_s: float = 90.0,
        store_credentials: bool = True,
        detect_resources: Callable[[Path], resources.SystemResources] = resources.detect,
        hot_backup_interval_s: float | None = None,
        auto_updates: bool | None = None,
        downloads_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.layout = layout
        self.settings = settings
        self._listener = emit
        self.ask_offer = ask_offer
        self.clock = clock
        self._rtts = benchmark.RoundTrips()
        self.cpu_score: float | None = None
        self.upload_kbps: int | None = None
        self.ready_timeout_s = ready_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self._store_credentials = store_credentials
        self._api_transport = api_transport
        self.api = ApiClient(settings.api_url, transport=api_transport,
                             on_refresh_token=self._remember_refresh_token)
        self._downloads = make_client(downloads_transport)
        self.mod_installer = mod_files.ModInstaller(layout.mod_cache, layout.downloads, self._downloads)
        self.modrinth = Modrinth(self._downloads)
        self._pack_client = resource_pack.make_client(downloads_transport)
        self._prepare = prepare or Installer(layout, settings, self._downloads).prepare
        self._command_factory = command_factory or (
            lambda p, mem: jvm.server_command(str(p.java), str(p.jar), mem))
        self.channel: ControlChannel | None = None
        self.hosted: Hosted | None = None
        self.user: dict[str, Any] | None = None
        self._loader = Loader.PAPER
        self._tasks: list[asyncio.Task] = []
        #: Running prefetches for migrations towards this PC, by migration id.
        self._prefetching: dict[str, asyncio.Task] = {}
        self._detect = detect_resources
        self._hot_interval_override = hot_backup_interval_s
        self._resources = detect_resources(layout.home)
        self.updater = Updater(layout.home / "cache" / "updates", lambda: self.api, on_ready=self._update_ready)
        self._awake = KeepAwake()
        #: Look for new versions: only the installed application can install them.
        self._auto_updates = getattr(sys, "frozen", False) if auto_updates is None else auto_updates
        #: Set right before quitting to install an update: no new hosting from then on.
        self.updating = False
        #: Oldest application the control plane still accepts, when this one is older: it may
        #: log in and update itself, but hosts nothing.
        self.too_old_for: str | None = None
        #: Standby copies (docs/ARCHITECTURE.md 2.6): running refresh per server, and servers to
        #: refresh again once it is done.
        self._standby: dict[str, asyncio.Task] = {}
        self._standby_dirty: set[str] = set()
        self._standby_prepared: set[tuple[str, str]] = set()

    def emit(self, event: str, /, **payload: Any) -> None:
        """Notify the outside world. A faulty listener must never break hosting."""
        try:
            self._listener(event, **payload)
        except Exception:  # noqa: BLE001
            log.exception("event listener failed for %s", event)

    # ================================================================== session

    def _remember_refresh_token(self, token: str | None) -> None:
        if not self._store_credentials:
            return
        if token is None:
            credentials.forget_refresh_token(self.layout.credentials_file)
        else:
            credentials.store_refresh_token(self.layout.credentials_file, token)

    async def check_compatibility(self) -> None:
        """Before logging in: refuse a control plane too old for this application, and note
        when it is this application that is too old (it can still log in to get its update)."""
        info = await self.api.health()
        server_version = info.get("version")
        if versions.older_than(server_version, MIN_SERVER_VERSION):
            raise ApiError(0, "server_too_old",
                           f"control plane {server_version} is older than {MIN_SERVER_VERSION}",
                           {"server_version": server_version, "required": MIN_SERVER_VERSION})
        minimum = info.get("min_client_version")
        self._set_too_old(minimum if minimum and versions.older_than(__version__, minimum) else None)

    def _set_too_old(self, minimum: str | None) -> None:
        if minimum == self.too_old_for:
            return
        self.too_old_for = minimum
        if minimum is not None:
            log.warning("this application (%s) is older than the minimum %s", __version__, minimum)
            self.emit("incompatible", current=__version__, minimum=minimum)
            if self._tasks and self._auto_updates:
                asyncio.create_task(self._update_now())

    def _refused_as_too_old(self) -> None:
        """The control plane was updated during the session and no longer accepts this version."""
        async def recheck() -> None:
            with contextlib.suppress(ApiError):
                await self.check_compatibility()
            if self.too_old_for is None:
                self._set_too_old("plus récente")
        asyncio.create_task(recheck())

    async def _update_now(self, delay_s: float = 0.0) -> None:
        await asyncio.sleep(delay_s)
        try:
            await self.updater.check()
        except Exception as exc:  # noqa: BLE001 - the regular checks try again later
            log.warning("update check failed: %s", exc)

    def _on_release_notice(self, msg: ClientReleaseNotice) -> None:
        """A version was just published: fetch it now (it is verified like any other)."""
        if (not self._auto_updates or msg.platform != self.updater.platform
                or not is_newer(msg.version, self.updater.current)
                or (self.updater.ready and self.updater.ready.version == msg.version)):
            return
        log.info("version %s announced by the control plane", msg.version)
        asyncio.create_task(self._update_now(random.uniform(0, ANNOUNCE_SPREAD_S)))

    @property
    def can_update(self) -> bool:
        """Only the installed application can install a new version of itself."""
        return self._auto_updates

    async def check_for_updates(self) -> ReadyUpdate | None:
        """The "Vérifier les mises à jour" button: now, with the reason when it fails."""
        if not self._auto_updates:
            raise RuntimeError("seule l'application installée peut se mettre à jour")
        return await self.updater.check()

    async def login(self, username: str, password: str) -> dict[str, Any]:
        await self.check_compatibility()
        await self.api.login(username, password)
        self.settings.username = username
        self._save_settings()
        return await self._after_login()

    async def register(self, username: str, password: str, invite_code: str | None) -> dict[str, Any]:
        await self.check_compatibility()
        await self.api.register(username, password, invite_code or None)
        return await self.login(username, password)

    async def restore_session(self) -> dict[str, Any] | None:
        token = credentials.load_refresh_token(self.layout.credentials_file) if self._store_credentials else None
        if not token:
            return None
        await self.check_compatibility()
        self.api.refresh_token = token
        try:
            await self.api.refresh()
        except ApiError:
            return None
        return await self._after_login()

    async def logout(self) -> None:
        await self.shutdown(stop_hosting=True)
        await self.api.logout()
        self.user = None
        self.emit("session", user=None)

    async def _after_login(self) -> dict[str, Any]:
        self.user = await self.api.me()
        await self._ensure_host()
        self.emit("session", user=self.user)
        self._start_background()
        return self.user

    async def _ensure_host(self) -> None:
        mine = {h["host_id"] for h in await self.api.hosts()}
        r = self._resources
        if self.settings.host_id not in mine:
            host = await self.api.register_host(self.settings.display_name, r.os_name,
                                                r.logical_cores, r.total_ram_mb)
            self.settings.host_id = host["host_id"]
            self._save_settings()
        await self.push_host_preferences()

    async def push_host_preferences(self) -> None:
        assert self.settings.host_id
        await self.api.update_host(
            self.settings.host_id,
            display_name=self.settings.display_name,
            hosting_allowed=self.settings.hosting_allowed,
            auto_accept_offers=self.settings.auto_accept_offers,
            max_ram_mb=self.memory_cap_mb(),
        )

    def _save_settings(self) -> None:
        self.settings.save(self.layout.settings_file)

    def memory_cap_mb(self) -> int:
        ceiling = resources.max_allowed_memory_mb(self._resources.total_ram_mb)
        wanted = self.settings.max_ram_mb or resources.suggest_memory_mb(self._resources.total_ram_mb)
        return max(1024, min(wanted, ceiling))

    # ================================================================== background tasks

    def _start_background(self) -> None:
        if self._tasks:
            return
        self.channel = ControlChannel(
            self.api.ws_url,
            access_token=self._fresh_access_token,
            host_id=self.settings.host_id,
            heartbeat_payload=self._heartbeat_payload,
            on_ack=self._on_ack,
            on_message=self._on_message,
            on_connection=lambda online: self.emit("connection", online=online),
            clock=self.clock,
            ssl_context=self.api.ssl_context,
            on_too_old=self._refused_as_too_old,
        )
        self._tasks = [asyncio.create_task(self.channel.run(), name="channel"),
                       asyncio.create_task(self._watchdog(), name="watchdog"),
                       asyncio.create_task(self._stats_loop(), name="stats")]
        self._tasks.append(asyncio.create_task(self._standby_loop(), name="standby"))
        self._tasks.append(asyncio.create_task(self._alerts_loop(), name="alerts"))
        self._tasks.append(asyncio.create_task(self._measures_loop(), name="measures"))
        if self._auto_updates:
            self._tasks.append(asyncio.create_task(self.updater.run(), name="updates"))
            if self.too_old_for is not None:
                asyncio.create_task(self._update_now())  # no need to wait for the first check

    async def _measures_loop(self) -> None:
        """Single-core speed and upload speed of this PC, while it hosts nothing (the measures
        would slow the game down, and the game would distort them)."""
        while True:
            if self.hosted is None and self.settings.host_id:
                if self.cpu_score is None:
                    self.cpu_score = await asyncio.to_thread(benchmark.cpu_score)
                await self.measure_upload()
            await asyncio.sleep(MEASURE_EVERY_S if self.upload_kbps is not None else 600)

    async def measure_upload(self) -> int | None:
        if not self.settings.host_id:
            return None
        data = os.urandom(SPEEDTEST_BYTES)
        start = time.perf_counter()
        try:
            await self.api.speedtest(self.settings.host_id, data)
        except (ApiError, ApiUnavailable) as exc:
            log.info("speed test failed: %s", exc)
            return None
        elapsed = time.perf_counter() - start
        self.upload_kbps = int(len(data) * 8 / 1000 / max(elapsed, 0.001))
        log.info("upload to the VPS: %d kbit/s, single core: %s", self.upload_kbps, self.cpu_score)
        return self.upload_kbps

    async def _fresh_access_token(self) -> str:
        await self.api.refresh()  # rotate on every (re)connection: the WS hello needs a valid one
        assert self.api.access_token
        return self.api.access_token

    def _availability(self) -> dict[str, Any]:
        r = self._detect(self.layout.home)
        return {
            "hosting_allowed": (self.settings.hosting_allowed and self.hosted is None and not self.updating
                                and self.too_old_for is None),
            "free_ram_mb": r.available_ram_mb,
            "free_disk_mb": r.disk_free_mb,
            "relay_ok": True,  # Phase 6: real relay check
            "local_versions": self._local_versions(),
            "rtt_ms": self._rtts.ms,
            "upload_kbps": self.upload_kbps,
            "cpu_score": self.cpu_score,
        }

    def _local_versions(self) -> dict[str, str]:
        found: dict[str, str] = {}
        if self.layout.servers.is_dir():
            engine = self._engine()
            for directory in list(self.layout.servers.iterdir())[:64]:
                # A standby copy is as good as the world itself for the election tie-break.
                version = engine.prefetched_version(directory) or engine.local_version(directory)
                if version:
                    found[directory.name] = version
        return found

    def _engine(self) -> SyncEngine:
        return SyncEngine(self.api, progress=lambda m, f: self.emit("progress", message=m, fraction=f))

    def _heartbeat_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"availability": self._availability()}
        h = self.hosted
        if h is not None and h.phase != "fenced":
            body["leases"] = [{"server_id": h.server_id, "epoch": h.epoch}]
            metrics = self._metrics()
            if metrics:
                body["metrics"] = metrics
        return body

    def _metrics(self) -> dict[str, Any] | None:
        h = self.hosted
        if h is None or h.process is None or not h.process.alive or h.monitor is None:
            return None
        usage = h.monitor.sample()
        if usage is None:
            return None
        r = self._detect(self.layout.home)
        return {"cpu_percent": usage.cpu_percent, "ram_used_mb": usage.rss_mb,
                "ram_free_mb": r.available_ram_mb, "disk_free_mb": r.disk_free_mb,
                "java_version": str(h.java_major) if h.java_major else None, "mc_version": h.mc_version,
                "players_online": len(h.process.players), "tps": h.process.tps, "mspt": h.process.mspt,
                "pregen": _pregen_or_none(h.pregen), "profile": _profile_or_none(h.profile)}

    async def _on_ack(self, ack: HeartbeatAck, sent_at: float) -> None:
        self._rtts.add(self.clock() - sent_at)
        h = self.hosted
        if h is None:
            return
        for status in ack.leases:
            if status.server_id == h.server_id and status.epoch == h.epoch:
                if status.ok and status.expires_in_s is not None:
                    h.guard.granted(sent_at, h.epoch, status.expires_in_s)
                else:
                    log.warning("control plane refused our lease: stopping immediately")
                    h.guard.revoked()

    async def _watchdog(self) -> None:
        """Self-fencing loop: never let Minecraft run on a lease we cannot prove is valid."""
        while True:
            await asyncio.sleep(0.25)
            h = self.hosted
            if h is None or h.phase in ("fenced",) or h.stopping:
                continue
            if h.guard.must_stop():
                await self._fence(h, "le bail d'hébergement n'a pas pu être renouvelé")
                continue
            # WebSocket down but HTTP may still work: renew through the REST fallback.
            if (self.channel is None or not self.channel.connected) and h.guard.remaining_s() < h.ttl_s / 2:
                sent = h.guard.request_sent()
                try:
                    grant = await self.api.renew(h.server_id, self.settings.host_id or "", h.epoch)
                    h.guard.granted(sent, grant["epoch"], grant["ttl_s"])
                except ApiError as exc:
                    if exc.code in ("stale_epoch", "expired"):
                        h.guard.revoked()

    async def _stats_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(2)
            tick += 1
            h = self.hosted
            if h is None or h.process is None or not h.process.alive:
                continue
            usage = h.monitor.sample() if h.monitor else None
            self.emit("stats", server_id=h.server_id, cpu_percent=usage.cpu_percent if usage else None,
                      ram_mb=usage.rss_mb if usage else None, tps=h.process.tps, mspt=h.process.mspt,
                      players=sorted(h.process.players))
            if tick % 30 == 0 and h.phase == "running":
                # Paper answers "tps"; vanilla/Fabric 1.20.3+ answer "tick query".
                with contextlib.suppress(Exception):
                    await h.process.send("tps" if self._loader == Loader.PAPER else "tick query")

    # ================================================================== messages

    async def _on_message(self, msg: Any) -> None:
        if isinstance(msg, HostingOffer):
            asyncio.create_task(self._on_offer(msg))
        elif isinstance(msg, StopRequest):
            h = self.hosted
            if h and h.server_id == msg.server_id:
                if not h.stopping:
                    asyncio.create_task(self.stop_hosting("arrêt demandé", msg.countdown_s))
                elif h.migration_id is not None:
                    h.migration_id = None  # the group stops the server: no handover any more
        elif isinstance(msg, PrefetchRequest):
            self._prefetching[msg.migration_id] = asyncio.create_task(self._on_prefetch(msg))
        elif isinstance(msg, MigrationRequest):
            asyncio.create_task(self._on_migration_request(msg))
        elif isinstance(msg, MigrationCancel):
            self._on_migration_cancel(msg)
        elif isinstance(msg, RelayOpen):
            self._open_tunnel(msg)
        elif isinstance(msg, ActionMessage):
            await self._on_action(msg)
        elif isinstance(msg, GroupUpdate):
            self.emit("group_update", group_id=msg.group_id)
        elif isinstance(msg, ClientReleaseNotice):
            self._on_release_notice(msg)
        elif isinstance(msg, ServerUpdate):
            self.emit("server_update", **msg.model_dump(mode="json"))
            if msg.state == "deleted":
                await self._forget_server(msg.server_id)
                return
            if msg.active_host_id != self.settings.host_id:
                self._want_standby(msg.server_id)  # e.g. a new backup was committed by the host
            h = self.hosted
            if h and h.server_id == msg.server_id and h.phase == "running" and not h.stopping:
                h.config_dirty = True
                if h.config_task is None or h.config_task.done():
                    h.config_task = asyncio.create_task(self._follow_config(h))

    async def _on_offer(self, offer: HostingOffer) -> None:
        if self.hosted is not None or not self.settings.hosting_allowed or self.updating:
            await self._decline(offer, "déjà occupé" if self.hosted else
                                "mise à jour de l'application" if self.updating else "hébergement désactivé")
            return
        try:
            server = await self.api.server(offer.server_id)
        except ApiError as exc:
            await self._decline(offer, exc.code)
            return
        if self.unapproved_mods(server):
            self.emit("mods_approval", server_id=offer.server_id, server_name=server["name"])
            await self._decline(offer, "mods à autoriser sur ce PC")
            return
        # A migration offer was already accepted (or asked) at the prefetch step.
        accept = self.settings.auto_accept_offers or offer.reason in ("resume", "migration")
        if not accept and self.ask_offer is not None:
            try:
                accept = await asyncio.wait_for(
                    self.ask_offer({"server_id": offer.server_id, "name": server["name"],
                                    "mc_version": server["mc_version"], "loader": server["loader"],
                                    "memory_mb": server["memory_mb"], "reason": offer.reason}),
                    min(OFFER_DECISION_TIMEOUT_S, offer.expires_in_s - 2))
            except TimeoutError:
                accept = False
        if not accept:
            await self._decline(offer, "refusé par l'utilisateur")
            return
        await self._host(offer, server)

    async def _decline(self, offer: HostingOffer, reason: str) -> None:
        if self.channel:
            await self.channel.send({"type": "offer_reply", "offer_id": offer.offer_id, "accept": False,
                                     "reason": reason[:200]})

    # ================================================================== relay

    def _open_tunnel(self, msg: RelayOpen) -> None:
        """A player waits on the VPS relay: connect back to it and to the local server."""
        h = self.hosted
        if (h is None or h.server_id != msg.server_id or h.epoch != msg.epoch or h.phase != "running"
                or h.process is None or not h.process.alive or msg.transport != "tcp"
                or (msg.target == "map" and not h.server.get("map_enabled"))):
            log.info("relay connection %s ignored: this PC does not serve that lease", msg.conn_id)
            return  # the relay gives up after a few seconds and tells the player
        task = asyncio.create_task(self._tunnel(h, msg))
        h.tunnels.add(task)
        task.add_done_callback(h.tunnels.discard)

    async def _tunnel(self, h: Hosted, msg: RelayOpen) -> None:
        url = self.api.ws_url.removesuffix("/ws") + f"/relay/data/{msg.conn_id}"
        try:
            port = self.settings.map_port if msg.target == "map" else self.settings.server_port
            ip = ip_bans.normalize(str(msg.peer_ip)) if msg.peer_ip else None
            if msg.target == "minecraft" and h.bans is not None and h.bans.bans(ip):
                # Banned by address (docs/ARCHITECTURE.md 4.22): refused here, on every server type.
                await run_tunnel(url, msg.relay_token, port, self.api.ssl_context, refuse=ip_bans.BANNED_MESSAGE)
                log.info("relay connection %s refused: banned address", msg.conn_id)
                return
            if msg.player and ip:
                h.player_ips[msg.player.lower()] = ip
            # The player's address for Paper; LOCAL when the VPS did not say.
            first = (proxy.header(ip, msg.peer_port, port)
                     if msg.target == "minecraft" and h.proxy_protocol else b"")
            sent, received = await run_tunnel(url, msg.relay_token, port, self.api.ssl_context, first=first)
            log.info("relay connection %s closed (%d kB in, %d kB out)", msg.conn_id, sent // 1024,
                     received // 1024)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one player's connection must not affect hosting
            log.warning("relay connection %s failed: %s", msg.conn_id, exc)

    @staticmethod
    def _close_tunnels(h: Hosted) -> None:
        for task in list(h.tunnels):
            task.cancel()

    # ================================================================== planned migrations

    async def _on_prefetch(self, msg: PrefetchRequest) -> None:
        """Migration towards this PC, step 2: get Minecraft and the world ready in advance."""
        try:
            ok, reason = await self._prefetch(msg)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._engine().discard_prefetch, self.layout.server_dir(msg.server_id))
            return
        finally:
            self._prefetching.pop(msg.migration_id, None)
        if not ok:
            self.emit("migration", server_id=msg.server_id, role="target", phase="refused", reason=reason)
        if self.channel:
            await self.channel.send({"type": "prefetch_done", "migration_id": msg.migration_id, "ok": ok,
                                     "reason": reason[:200] if reason else None})

    async def _prefetch(self, msg: PrefetchRequest) -> tuple[bool, str | None]:
        if self.hosted is not None:
            return False, "ce PC héberge déjà un serveur"
        if not self.settings.hosting_allowed:
            return False, "hébergement désactivé sur ce PC"
        if self.updating:
            return False, "mise à jour de l'application en cours"
        await self._stop_standby(msg.server_id)  # same prefetch area: the migration takes over
        try:
            server = await self.api.server(msg.server_id)
        except ApiError as exc:
            return False, exc.code
        if self.unapproved_mods(server):
            self.emit("mods_approval", server_id=msg.server_id, server_name=server["name"])
            return False, "mods à autoriser sur ce PC"
        if not self.settings.auto_accept_offers and self.ask_offer is not None:
            try:
                accept = await asyncio.wait_for(
                    self.ask_offer({"server_id": msg.server_id, "name": server["name"],
                                    "mc_version": server["mc_version"], "loader": server["loader"],
                                    "memory_mb": server["memory_mb"], "reason": "migration"}),
                    OFFER_DECISION_TIMEOUT_S)
            except TimeoutError:
                accept = False
            if not accept:
                return False, "refusé par l'utilisateur"
        self.emit("migration", server_id=msg.server_id, server_name=server["name"], role="target",
                  phase="prefetching")
        try:
            await self._prepare(Loader(server["loader"]), server["mc_version"],
                                lambda m, f: self.emit("progress", message=m, fraction=f))
            await self.mod_installer.fetch_all(_mods(server),
                                               lambda m, f: self.emit("progress", message=m, fraction=f))
            if msg.version_id:
                engine = self._engine()
                live = self.layout.server_dir(msg.server_id)
                live.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(engine.recover, live)
                version = await self._version(msg.server_id, msg.version_id)
                result = await engine.prefetch(server["group_id"], msg.server_id, version, live)
                log.info("migration prefetch: %s", result)
        except NotEnoughSpace as exc:
            return False, f"pas assez d'espace disque ({exc})"
        except ApiError as exc:
            return False, f"échec du préchargement : {exc.message}"
        except Exception as exc:  # noqa: BLE001 - reported to the VPS, which cancels the migration
            log.exception("prefetch failed")
            return False, f"échec du préchargement : {exc}"
        self.emit("migration", server_id=msg.server_id, server_name=server["name"], role="target",
                  phase="ready")
        return True, None

    async def _on_migration_request(self, msg: MigrationRequest) -> None:
        """Migration away from this PC, steps 3-6: announce, stop, final upload, release."""
        h = self.hosted
        if h is None or h.server_id != msg.server_id or h.stopping or h.phase != "running":
            # Not in a state to hand over (starting, crashed...): keep things as they are.
            with contextlib.suppress(ApiError):
                await self.api.cancel_transfer(msg.server_id)
            return
        self.emit("migration", server_id=h.server_id, server_name=h.name, role="source", phase="countdown",
                  countdown_s=msg.countdown_s, target_host_id=msg.target_host_id)
        await self.stop_hosting("changement d'hôte", msg.countdown_s, migration_id=msg.migration_id)

    def _on_migration_cancel(self, msg: MigrationCancel) -> None:
        task = self._prefetching.pop(msg.migration_id, None)
        if task is not None:
            task.cancel()
        h = self.hosted
        source = h is not None and h.migration_id == msg.migration_id
        if source:
            assert h is not None
            h.migration_cancelled = True
            h.interrupt.set()
        elif task is None:
            # Prefetch already done: the downloaded copy is not needed any more.
            self._engine().discard_prefetch(self.layout.server_dir(msg.server_id))
        self.emit("migration", server_id=msg.server_id, role="source" if source else "target",
                  phase="cancelled", reason=msg.reason)

    async def _version(self, server_id: str, version_id: str) -> dict[str, Any]:
        version = next((v for v in await self.api.versions(server_id, limit=500)
                        if v["version_id"] == version_id), None)
        if version is None:
            raise SyncError("the requested world version is not listed by the VPS")
        return version

    async def _on_action(self, msg: ActionMessage) -> None:
        h = self.hosted
        if h is None or h.server_id != msg.server_id or h.process is None or h.phase != "running":
            return
        a = msg.action
        # Closed list mapped to fixed commands; names already validated by the protocol model.
        if a.kind == "announce":
            await h.process.announce(a.message)
        elif a.kind == "restart":
            asyncio.create_task(self.restart_hosted())
        elif a.kind == "pregen_start":
            await self._start_pregen(h, a.dimension, a.radius)
        elif a.kind == "pregen_cancel":
            for command in pregen.CANCEL_COMMANDS:
                await h.process.send(command)
        elif a.kind == "profile_start":
            # Paper has spark built in; Fabric needs the mod in the list.
            if Loader(h.server["loader"]) == Loader.PAPER or any(m.slug == SPARK_MOD_SLUG for m in _mods(h.server)):
                h.profile = {"state": "running", "url": None}
                await h.process.send(f"spark profiler start --timeout {a.seconds}")
        elif a.kind == "ban_ip":
            with contextlib.suppress(CommandRejected):  # already said in the console
                await self._ban_ip(h, f"ban-ip {a.player}")
        else:
            verb = {"whitelist_add": "whitelist add", "whitelist_remove": "whitelist remove",
                    "op": "op", "deop": "deop", "kick": "kick", "ban": "ban", "pardon": "pardon"}[a.kind]
            await h.process.send(f"{verb} {a.player}")

    async def _ban_ip(self, h: Hosted, command: str) -> None:
        """``ban-ip``, always with the address the relay saw (Minecraft may only see 127.0.0.1). A
        refusal is written in the console, which the admins of other PCs read too."""
        assert h.process is not None
        try:
            commands = ip_bans.ban_commands(command, h.player_ips)
        except CommandRejected as exc:
            self._on_console_line(h, f"[Minecraft Distributed Hosting] commande refusée : {exc}")
            raise
        for c in commands:
            await h.process.send(c)

    async def _start_pregen(self, h: Hosted, dimension: str, radius: int) -> None:
        """Fixed Chunky commands, once this PC has room for what they will write."""
        assert h.process is not None
        if not any(m.slug == PREGEN_MOD_SLUG for m in _mods(h.server)):
            return  # the application only offers it with Chunky in the list
        h.pregen = {"dimension": dimension, "radius": radius, "state": "running", "percent": 0.0, "eta_s": None}
        free = shutil.disk_usage(self.layout.server_dir(h.server_id)).free
        if free < pregen.estimate_bytes(radius) + pregen.DISK_MARGIN_BYTES:
            h.pregen["state"] = "no_space"
            self.emit("error", message="Pas assez de place sur ce PC pour pré-générer le monde : il faudrait "
                                       f"environ {pregen.estimate_bytes(radius) // 1024**2} Mo de libre en plus "
                                       "de la marge.")
            return
        level = str(h.server.get("properties", {}).get("level-name") or "world")
        h.pregen_world = pregen.world_name(Loader(h.server["loader"]), dimension, level)
        for command in pregen.start_commands(h.pregen_world, radius):
            await h.process.send(command)

    def _on_pregen(self, h: Hosted, ev: LogEvent) -> None:
        world = ev.value[0] if isinstance(ev.value, tuple) else ev.value
        if h.pregen is None or h.pregen_world != world:  # a task this PC did not start (continued...)
            h.pregen_world = str(world)
            h.pregen = {"dimension": pregen.dimension_of(str(world)), "radius": 0, "state": "running",
                        "percent": 0.0, "eta_s": None}
        if ev.kind == "pregen":
            _, percent, eta = ev.value  # type: ignore[misc]
            h.pregen.update(state="running", percent=percent, eta_s=eta)
        elif ev.kind == "pregen_done":
            h.pregen.update(state="done", percent=100.0, eta_s=0)
        elif h.pregen["state"] == "running":
            h.pregen.update(state="cancelled", eta_s=None)

    # ================================================================== hosting

    async def _host(self, offer: HostingOffer, server: dict[str, Any]) -> None:
        host_id = self.settings.host_id or ""
        sent = self.clock()  # lease validity is counted from when we *sent* the request
        try:
            grant = await self.api.acquire(offer.server_id, host_id, offer.offer_id)
        except ApiError as exc:
            self.emit("error", message=f"Impossible de prendre l'hébergement : {exc.message}")
            return
        ttl = float(grant["ttl_s"])
        # Keep the margin meaningful when the control plane uses a short TTL (tests).
        guard = LeaseGuard(safety_margin_s=min(LEASE_SAFETY_MARGIN_S, ttl / 3), clock=self.clock)
        guard.granted(sent, grant["epoch"], ttl)
        server = {**server, "_host_id": host_id}
        h = Hosted(server["server_id"], server["name"], grant["epoch"], guard, ttl,
                   mc_version=server["mc_version"], server=server)
        self.hosted = h
        self._loader = Loader(server["loader"])
        if self.channel:
            self.channel.beat_now()
        self._phase(h, "preparing")
        h.task = asyncio.current_task()
        for server_id in list(self._standby):  # hosting comes first; this one's copy is used now
            await self._stop_standby(server_id)
        try:
            await self._report(h, "downloading")
            prepared = await self._prepare(Loader(server["loader"]), server["mc_version"],
                                           lambda m, f: self.emit("progress", message=m, fraction=f))
            h.java_major = prepared.distribution.java_major
            h.prepared = prepared
            await self._sync_world(h)
            await self._install_mods(h, server)
            if not self._prepare_directory(h, server):
                await self._give_up(h, "EULA Minecraft non acceptée sur ce PC")
                return
            await self._launch(h, prepared, server)
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            h.guard.revoked()  # the watchdog fences
        except NotEnoughSpace as exc:
            await self._give_up(h, f"pas assez d'espace disque pour le monde ({exc})")
        except Exception as exc:  # noqa: BLE001 - any setup failure: release so someone else hosts
            log.exception("hosting failed")
            await self._give_up(h, f"échec de préparation : {exc}")

    async def _sync_world(self, h: Hosted) -> None:
        """Bring the latest verified world onto this PC without ever losing local progress."""
        engine = self._engine()
        live = self.layout.server_dir(h.server_id)
        live.mkdir(parents=True, exist_ok=True)
        if await asyncio.to_thread(engine.recover, live):
            self.emit("error", message="Un transfert de monde interrompu a été annulé proprement.")
        status = await self.api.server(h.server_id)
        head_id = status["head_version_id"]
        local_id = engine.local_version(live)
        if await engine.has_local_changes(live):
            if head_id is None or local_id == head_id:
                # Nobody wrote after our last sync: this PC has the newest world (e.g. it crashed or
                # lost the network before its final backup). Save it first, never discard it.
                self.emit("progress", message="Sauvegarde des données de ce PC non encore envoyées",
                          fraction=None)
                async with h.sync_lock:
                    version = await engine.upload(h.lease, live, parent=head_id, kind="final",
                                                  label="Reprise des données de ce PC")
                self._sync_event(h, version)
                return
            archived = await asyncio.to_thread(engine.archive_divergent, live)
            self.emit("error", message="Ce PC avait une version du monde différente de celle du groupe : "
                                       f"elle est conservée dans {archived}.")
        if head_id:
            head = await self._version(h.server_id, head_id)
            result = await engine.materialize(h.server["group_id"], h.server_id, head, live)
            log.info("world ready: %s", result)
            self._sync_event(h, head)

    async def _install_mods(self, h: Hosted, server: dict[str, Any]) -> None:
        """The group's mods or plugins, verified, and nothing else (docs/ARCHITECTURE.md 3.9)."""
        mods = _mods(server)
        blocked = self.unapproved_mods(server)
        if blocked:  # the list changed after the offer was accepted
            raise RuntimeError("mods non autorisés sur ce PC : " + ", ".join(m.name for m in blocked))
        loader = Loader(server["loader"])
        directory = self.layout.server_dir(h.server_id)
        moved = await self.mod_installer.install(directory, loader, mods,
                                                 lambda m, f: self.emit("progress", message=m, fraction=f))
        if moved:
            aside = directory / ".mdh" / "removed-jars"
            self.emit("error", message=f"{len(moved)} fichier(s) JAR non déclaré(s) mis de côté dans {aside} : "
                                       "ils ne seront pas chargés.")
        if server.get("map_enabled") and any(m.slug == MAP_MOD_SLUG for m in mods):
            await asyncio.to_thread(bluemap.configure, directory, loader, self.settings.map_port,
                                    eula_accepted=self.settings.eula_accepted_at is not None)
        if server.get("modpack"):
            pack = Modpack.model_validate(server["modpack"])
            path = await self.mod_installer.fetch_file(pack.url, pack.sha512, pack.size, ".mrpack", pack.name,
                                                       lambda m, f: self.emit("progress", message=m, fraction=f))
            count = await asyncio.to_thread(modpack.apply_overrides, path, directory,
                                            datapacks.level_name(server.get("properties", {})))
            log.info("modpack %s %s: %d configuration files", pack.name, pack.version_number, count)
        await datapacks.install(directory, datapacks.level_name(server.get("properties", {})), _datapacks(server),
                                self.mod_installer, lambda m, f: self.emit("progress", message=m, fraction=f))
        if server.get("config_file_paths"):  # after the modpack's: the group's own settings win
            files = await self.api.config_files(h.server_id)
            await asyncio.to_thread(config_files.apply, directory, files)
        if loader == Loader.PAPER:  # after the group's files: the protection's own block wins
            await asyncio.to_thread(anticheat.configure_paper, directory, server["mc_version"],
                                    anticheat_enabled(mods))
        # Also after the group's files: without it, a tunnel's header would break every connection.
        h.proxy_protocol = await asyncio.to_thread(proxy_protocol.configure, directory, loader,
                                                   server["mc_version"])

    def unapproved_mods(self, server: dict[str, Any]) -> list[Mod]:
        """Mods of that server this PC's user has not allowed yet: it hosts nothing until then."""
        return mod_files.unapproved([*_mods(server), *_datapacks(server)], self.settings.approved_mods)

    def approve_mods(self, mods: list[Mod | Datapack]) -> None:
        known = set(self.settings.approved_mods)
        self.settings.approved_mods += [m.sha512 for m in mods if m.sha512 not in known]
        self._save_settings()

    async def search_mods(self, server: dict[str, Any], query: str) -> list[Any]:
        return await self.modrinth.search(query, Loader(server["loader"]), server["mc_version"])

    async def resolve_mod(self, server: dict[str, Any], ref: str, current: list[Mod]) -> list[Mod]:
        return await self.modrinth.resolve(ref, Loader(server["loader"]), server["mc_version"], current)

    async def anticheat_mods(self, loader: Loader, mc_version: str, current: list[Mod]) -> list[Mod]:
        """What turning the anti-cheat protection on adds to ``current`` (with dependencies)."""
        added: list[Mod] = []
        for slug in anticheat_slugs(loader):
            added += await self.modrinth.resolve(slug, loader, mc_version, [*current, *added])
        return added

    async def search_packs(self, server: dict[str, Any], query: str) -> list[Any]:
        return await self.modrinth.search_packs(query, server["mc_version"])

    async def modrinth_pack(self, server: dict[str, Any], ref: str, required: bool) -> ResourcePack:
        return await self.modrinth.pack(ref, server["mc_version"], required)

    async def pack_from_url(self, url: str, required: bool) -> ResourcePack:
        return await resource_pack.from_url(self._pack_client, url, required)

    # ------------------------------------------------------------------ 0.7.0

    async def search_datapacks(self, server: dict[str, Any], query: str) -> list[Any]:
        return await self.modrinth.search_datapacks(query, server["mc_version"])

    async def datapack(self, server: dict[str, Any], ref: str) -> Datapack:
        return await self.modrinth.datapack(ref, server["mc_version"])

    async def save_datapacks(self, server_id: str, packs: list[Datapack]) -> dict[str, Any]:
        status = await self.api.update_server(server_id, datapacks=[p.model_dump(mode="json") for p in packs])
        self.approve_mods(packs)  # whoever chose them allows them on their own PC
        return status

    async def config_file_list(self, server: dict[str, Any]) -> list[tuple[str, int, bool]]:
        """Settings files of the server: (path, size, managed by the group), from the latest
        version of the world on the VPS and the group's own list."""
        managed = await self.api.config_files(server["server_id"])
        found: dict[str, int] = {}
        if server.get("head_version_id"):
            version = await self._version(server["server_id"], server["head_version_id"])
            for f in await self._engine().files(server["server_id"], version):
                if config_files.is_config(f.path, f.size):
                    found[f.path] = f.size
        for path, content in managed.items():
            found[path] = len(content.encode("utf-8"))
        return sorted((path, size, path in managed) for path, size in found.items())

    async def read_config_file(self, server: dict[str, Any], path: str) -> str:
        managed = await self.api.config_files(server["server_id"])
        if path in managed:
            return managed[path]
        version = await self._version(server["server_id"], server["head_version_id"])
        data = await self._engine().read_file(server["group_id"], server["server_id"], version, path,
                                              MAX_CONFIG_FILE_BYTES)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path} n'est pas un fichier texte (UTF-8) : il ne peut pas être modifié ici.") from exc

    async def save_config_files(self, server_id: str, files: dict[str, str]) -> dict[str, Any]:
        return await self.api.update_server(server_id, config_files=files)

    async def apply_template(self, server: dict[str, Any], key: str) -> tuple[dict[str, Any], list[str]]:
        """The template's mods for the new server; returns its status and the mods with no file."""
        template = templates.by_key(key)
        loader = Loader(server["loader"])
        mods: list[Mod] = []
        missing = []
        for slug in template.mods:
            try:
                mods += await self.modrinth.resolve(slug, loader, server["mc_version"], mods)
            except ModrinthError:
                missing.append(slug)
        status = await self.save_mods(server["server_id"], mods) if mods else server
        return status, missing

    async def search_modpacks(self, query: str) -> list[Any]:
        return await self.modrinth.search_modpacks(query)

    async def import_modpack(self, ref: str) -> modpack.ImportedPack:
        """Download a modpack and read its mods (nothing is saved)."""
        return await modpack.import_pack(self.modrinth, self.mod_installer, ref,
                                         lambda m, f: self.emit("progress", message=m, fraction=f))

    async def save_modpack(self, server: dict[str, Any], imported: modpack.ImportedPack) -> dict[str, Any]:
        """The server becomes that modpack: Fabric, its Minecraft version, its mods."""
        mods = list(imported.mods)
        map_enabled = bool(server.get("map_enabled"))
        if map_enabled and not any(m.slug == MAP_MOD_SLUG for m in mods):
            try:
                mods += await self.modrinth.resolve(MAP_MOD_SLUG, Loader.FABRIC, imported.mc_version, mods)
            except ModrinthError:
                map_enabled = False  # no BlueMap for that version: the map goes off
        if anticheat_enabled(_mods(server)) and not anticheat_enabled(mods):
            with contextlib.suppress(ModrinthError):  # no file for that version: the protection goes off
                mods += await self.anticheat_mods(Loader.FABRIC, imported.mc_version, mods)
        status = await self.api.update_server(server["server_id"], loader=Loader.FABRIC.value,
                                              mc_version=imported.mc_version,
                                              mods=[m.model_dump(mode="json") for m in mods],
                                              map_enabled=map_enabled,
                                              modpack=imported.modpack.model_dump(mode="json"))
        self.approve_mods(mods)
        return status

    async def export_for_players(self, server: dict[str, Any], dest: Path, kind: str) -> list[Any]:
        """The mods players need, as a .mrpack or a .zip of files (docs/ARCHITECTURE.md 3.11)."""
        build = (await SourceResolver(self._downloads).fabric(server["mc_version"])).build
        return await modpack.export_for_players(self.modrinth, self.mod_installer, server, build.split("/")[0],
                                                dest, "zip" if kind == "zip" else "mrpack",
                                                lambda m, f: self.emit("progress", message=m, fraction=f))

    async def mod_updates(self, server: dict[str, Any]) -> tuple[list[Mod], list[Any]]:
        """Newer files on Modrinth for the server's mods: (new list, changes), nothing saved."""
        return await self.modrinth.updates(_mods(server), Loader(server["loader"]), server["mc_version"])

    async def save_mods(self, server_id: str, mods: list[Mod], map_enabled: bool | None = None) -> dict[str, Any]:
        """New list of mods for the group; whoever chose them allows them on their own PC."""
        changes: dict[str, Any] = {"mods": [m.model_dump(mode="json") for m in mods]}
        if map_enabled is not None:
            changes["map_enabled"] = map_enabled
            if map_enabled:
                changes["public_status"] = True  # the map is served with the status page
        status = await self.api.update_server(server_id, **changes)
        self.approve_mods(mods)
        return status

    def _sync_event(self, h: Hosted, version: dict[str, Any]) -> None:
        self.emit("sync", server_id=h.server_id, version_id=version["version_id"],
                  kind=version.get("kind"), at=version.get("created_at"))

    def _prepare_directory(self, h: Hosted, server: dict[str, Any]) -> bool:
        directory = self.layout.server_dir(h.server_id)
        directory.mkdir(parents=True, exist_ok=True)
        offline = bool(server.get("allow_offline_accounts"))
        if ip_bans.drop_loopback(directory):
            log.warning("ban of 127.0.0.1 removed: behind the relay it would have shut every player out")
        h.bans = ip_bans.BanList(directory)
        if server_files.reset_identities_if_auth_mode_changed(directory, offline):
            log.warning("account verification mode changed: whitelist identities rebuilt")
        # Chunky cannot pre-generate while Minecraft pauses an empty server (1.21.2+).
        chunky = {"pause-when-empty-seconds": 0} if any(m.slug == PREGEN_MOD_SLUG for m in _mods(server)) else {}
        server_files.write_properties(directory / "server.properties",
                                      {**_group_properties(server), **chunky,
                                       **resource_pack.properties(server.get("resource_pack"))},
                                      port=self.settings.server_port, allow_offline_accounts=offline)
        if offline:
            server_files.write_offline_whitelist(directory, server.get("allowed_players", []))
        eula = directory / "eula.txt"
        if self.settings.eula_accepted_at:
            server_files.write_eula(eula, self.settings.eula_accepted_at)
            return True
        self.emit("eula_required")
        return False

    async def _launch(self, h: Hosted, prepared: PreparedServer, server: dict[str, Any]) -> None:
        if h.guard.must_stop():
            await self._fence(h, "bail expiré avant le démarrage")
            return
        memory = min(int(server["memory_mb"]), self.memory_cap_mb())
        command = self._command_factory(prepared, memory)
        proc = MinecraftProcess(
            command, self.layout.server_dir(h.server_id),
            on_line=lambda line: self._on_console_line(h, line),
            on_event=lambda ev: self._on_log_event(h, ev),
            on_state=lambda st: self._on_process_state(h, st),
            below_normal_priority=self.settings.cpu_priority == "below_normal",
            cpu_limit_percent=self.settings.cpu_limit_percent,
        )
        h.process = proc
        self._phase(h, "starting")
        await self._report(h, "starting")
        await proc.start()
        h.monitor = resources.ProcessMonitor(proc.pid) if proc.pid else None
        if await proc.wait_ready(self.ready_timeout_s):
            if h.stopping:
                return  # a stop was requested during startup: it is already under way
            await self._apply_whitelist(h)
            await self._apply_game_rules(h, h.server.get("properties", {}))
            self._phase(h, "running")
            await self._report(h, "running")
            if h.hot_task is None:
                h.hot_task = asyncio.create_task(self._hot_backups(h))
        elif proc.alive:
            # Hung during startup. A deliberate kill is not a "crash", so give up explicitly.
            proc.kill()
            await proc.wait_exit()
            await self._give_up(h, f"le serveur n'a pas démarré en {int(self.ready_timeout_s)} s")
        # else: it exited by itself -> CRASHED state -> _after_crash decides (EULA, port, retry)

    async def _follow_config(self, h: Hosted) -> None:
        """Apply a new list of allowed players, and options that are game rules, while the game runs.

        The relay lets a player in as soon as the list changes; without this, Minecraft would keep
        refusing them until the next restart. Game rules (PvP...) take effect at once too.
        Everything else in the configuration still waits for the restart, as does a change of
        whitelist or account mode (identities change with it).
        """
        while h.config_dirty and self.hosted is h and not h.stopping:
            h.config_dirty = False
            try:
                await self._apply_live_config(h)
            except (ApiError, RuntimeError, OSError) as exc:  # retried on the next server_update
                log.warning("live configuration not applied: %s", exc)

    async def _apply_live_config(self, h: Hosted) -> None:
        latest = await self.api.server(h.server_id)
        running = h.server
        if self.hosted is not h or latest.get("config_revision") == running.get("config_revision"):
            return
        props, applied = latest.get("properties", {}), running.get("properties", {})
        changed = {key: props[key] for key in gamerules.rule_names(h.mc_version)
                   if key in props and props[key] != applied.get(key)}
        if changed:
            if not await self._apply_game_rules(h, changed):
                raise RuntimeError("game rules not applied")  # retried on the next server_update
            running = h.server = {**running, "properties": {**applied, **changed}}
            log.info("game rules applied without restart: %s", ", ".join(sorted(changed)))
        offline = bool(running.get("allow_offline_accounts"))
        players = latest.get("allowed_players", [])
        if (bool(latest.get("allow_offline_accounts")) != offline
                or bool(latest["properties"].get("white-list")) != bool(running["properties"].get("white-list"))
                or players == running.get("allowed_players")):
            # Nothing to apply now (the rest waits for the restart): remember we saw this revision.
            h.server = {**running, "config_revision": latest.get("config_revision")}
            return
        h.server = {**running, "allowed_players": players, "config_revision": latest.get("config_revision")}
        try:
            if offline:
                directory = self.layout.server_dir(h.server_id)
                await asyncio.to_thread(server_files.write_offline_whitelist, directory, players)
                if h.process is not None:
                    # enforce-whitelist is forced in this mode: whoever was removed is kicked.
                    await h.process.send("whitelist reload")
            else:
                await self._apply_whitelist(h)
        except BaseException:
            h.server = running  # not applied: the next server_update tries again
            raise
        log.info("allowed players applied without restart (%d)", len(players))

    async def _apply_game_rules(self, h: Hosted, properties: dict[str, Any]) -> bool:
        """Options Minecraft ignores in server.properties since 1.21.9 (PvP...): fixed commands."""
        if h.process is None:
            return False
        try:
            for command in gamerules.commands(properties, h.mc_version, h.server.get("loader")):
                await h.process.send(command)
        except (RuntimeError, OSError) as exc:
            log.warning("game rules not applied: %s", exc)
            return False
        return True

    async def _apply_whitelist(self, h: Hosted) -> None:
        """Make the server's whitelist match the group's list of allowed players."""
        srv = h.server
        if srv.get("allow_offline_accounts"):
            return  # already written with offline UUIDs before startup (see write_offline_whitelist)
        if not srv.get("properties", {}).get("white-list") or h.process is None:
            return
        wanted = {name.lower(): name for name in srv.get("allowed_players", [])}
        current = {name.lower(): name for name in
                   server_files.whitelisted_names(self.layout.server_dir(h.server_id))}
        try:
            for key, name in wanted.items():
                if key not in current:
                    await h.process.send(f"whitelist add {name}")
            for key, name in current.items():
                if key not in wanted:
                    await h.process.send(f"whitelist remove {name}")
        except (RuntimeError, OSError) as exc:
            log.warning("whitelist sync failed: %s", exc)

    async def _hot_backups(self, h: Hosted) -> None:
        interval = self._hot_interval_override or float(h.server.get("hot_backup_interval_s", 900))
        while True:
            await asyncio.sleep(interval)
            if self.hosted is not h or h.stopping or h.phase != "running":
                continue
            try:
                await self.backup(h, kind="hot")
            except LeaseLost:
                h.guard.revoked()
                return
            except Exception as exc:  # noqa: BLE001 - a failed hot backup must not stop the game
                log.warning("hot backup failed: %s", exc)
                self.emit("error", message=f"Sauvegarde automatique échouée (nouvel essai plus tard) : {exc}")

    async def backup(self, h: Hosted, kind: str = "hot", label: str | None = None) -> dict | None:
        """Consistent copy while the game runs: save-off, flush, copy, save-on, then upload the copy."""
        proc = h.process
        if proc is None or not proc.alive:
            return None
        engine = self._engine()
        live = self.layout.server_dir(h.server_id)
        snapshot = engine.state_dir(live) / "snapshot"
        async with h.sync_lock:
            await proc.send("save-off")
            try:
                if not await proc.save_all_flush(120):
                    raise SyncError("Minecraft n'a pas confirmé la sauvegarde")
                await asyncio.to_thread(engine.refresh_snapshot, live, snapshot)
            finally:
                with contextlib.suppress(Exception):
                    await proc.send("save-on")
            version = await engine.upload(h.lease, snapshot, parent=engine.local_version(live), kind=kind,
                                          live=live, label=label)
        self._sync_event(h, version)
        return version

    def _on_log_event(self, h: Hosted, ev: LogEvent) -> None:
        if ev.kind in ("pregen", "pregen_done", "pregen_stopped"):
            self._on_pregen(h, ev)
            return
        if ev.kind == "spark_url":
            h.profile = {"state": "done", "url": ev.value}
            return
        if ev.kind == "spark_failed":
            h.profile = {"state": "failed", "url": None}
            return
        if ev.kind in ("joined", "left", "list") and h.process is not None:
            self.emit("players", server_id=h.server_id, players=sorted(h.process.players))
        now = datetime.now(UTC).isoformat()
        if ev.kind == "chat":
            player, text = ev.value  # type: ignore[misc]
            h.activity_lines.append({"at": now, "player": player, "text": text})
        elif ev.kind == "joined":
            h.activity_lines.append({"at": now, "text": f"{ev.value} a rejoint la partie"})
        elif ev.kind == "left":
            h.activity_lines.append({"at": now, "text": f"{ev.value} a quitté la partie"})
        elif ev.kind != "list":
            return
        del h.activity_lines[:-100]
        if h.activity_task is None or h.activity_task.done():
            h.activity_task = asyncio.get_running_loop().create_task(self._report_activity(h))

    def _on_console_line(self, h: Hosted, line: str) -> None:
        self.emit("log", server_id=h.server_id, line=line)
        h.console_lines.append({"at": datetime.now(UTC).isoformat(), "text": line[:1000]})
        del h.console_lines[:-1000]
        if h.console_task is None or h.console_task.done():
            h.console_task = asyncio.get_running_loop().create_task(self._report_console(h))

    async def _report_console(self, h: Hosted) -> None:
        """The console, read-only, for the group's admins and moderators on other PCs."""
        await asyncio.sleep(ACTIVITY_BATCH_S)
        while self.hosted is h and h.process is not None and h.console_lines:
            lines, h.console_lines = h.console_lines[:200], h.console_lines[200:]
            try:
                await self.api.report_console(h.server_id, self.settings.host_id or "", h.epoch, lines)
            except ApiError as exc:
                log.info("console not reported: %s", exc)
                if exc.status in (403, 409):
                    return  # no longer the holder: nobody should read this console any more
            if h.console_lines:
                await asyncio.sleep(ACTIVITY_BATCH_S)

    async def _report_activity(self, h: Hosted) -> None:
        """Who is in game and what they say, for the group's other PCs (a few seconds late)."""
        await asyncio.sleep(ACTIVITY_BATCH_S)
        while self.hosted is h and h.process is not None:
            lines, h.activity_lines = h.activity_lines, []
            try:
                await self.api.report_activity(h.server_id, self.settings.host_id or "", h.epoch,
                                               sorted(h.process.players), lines)
            except ApiError as exc:
                log.info("activity not reported: %s", exc)
            if not h.activity_lines:
                return
            await asyncio.sleep(ACTIVITY_BATCH_S)

    def _on_process_state(self, h: Hosted, state: ProcessState) -> None:
        if state == ProcessState.CRASHED and not h.stopping and h.phase != "fenced":
            asyncio.get_running_loop().create_task(self._after_crash(h))

    async def _after_crash(self, h: Hosted) -> None:
        proc = h.process
        assert proc is not None
        if proc.eula_required:
            await self._give_up(h, "EULA Minecraft non acceptée")
            return
        if proc.bind_failed:
            await self._give_up(h, f"le port {self.settings.server_port} est déjà utilisé sur ce PC")
            return
        tail = " | ".join(list(proc.lines)[-3:])
        try:
            diagnosis: CrashReport | None = await asyncio.to_thread(
                crash.analyze, self.layout.server_dir(h.server_id), list(proc.lines), _mods(h.server),
                Loader(h.server["loader"]), crash.since_start(proc.started_at), proc.exit_code)
        except Exception:  # never let the explanation stand in the way of the restart
            log.exception("crash analysis failed")
            diagnosis = None
        if diagnosis is not None:
            self.emit("error", message=f"Minecraft s'est arrêté brutalement. {diagnosis.message}")
        await self._report(h, "crashed", f"code {proc.exit_code}: {tail}", crash=diagnosis)
        now = self.clock()
        while h.restarts and now - h.restarts[0] > self.settings.restart_window_s:
            h.restarts.popleft()
        if (self.settings.auto_restart and len(h.restarts) < self.settings.max_restarts
                and not h.guard.must_stop() and self.hosted is h):
            h.restarts.append(now)
            self.emit("error", message=f"Minecraft s'est arrêté brutalement, redémarrage "
                                       f"({len(h.restarts)}/{self.settings.max_restarts})")
            self._phase(h, "starting")
            await self._report(h, "starting")
            await proc.start()
            h.monitor = resources.ProcessMonitor(proc.pid) if proc.pid else None
            if await proc.wait_ready(self.ready_timeout_s):
                await self._apply_whitelist(h)
                await self._apply_game_rules(h, h.server.get("properties", {}))
                self._phase(h, "running")
                await self._report(h, "running")
            return
        # The world on disk is the newest state: save it before handing over (older versions
        # stay on the VPS should it turn out to be damaged).
        await self._give_up(h, "trop de plantages successifs", upload_world=True)

    async def _give_up(self, h: Hosted, reason: str, upload_world: bool = False) -> None:
        """Hand the server back to the group (the control plane elects someone else)."""
        self.emit("error", message=f"Hébergement abandonné : {reason}")
        await self._report(h, "crashed", reason)
        final = await self._final_backup(h, "Après plantages") if upload_world else None
        if final is False:
            self._forget(h)  # could not save: the lease expires, this PC's data is resumed later
            return
        await self._release(h, final or None)

    def _forget(self, h: Hosted) -> None:
        """Stop considering ourselves the host without releasing (the lease will expire)."""
        self._close_tunnels(h)
        if self.hosted is h:
            self.hosted = None
        self._phase(h, "released")

    async def _final_backup(self, h: Hosted, label: str | None = None) -> str | Literal[False]:
        """Upload the stopped world. Returns the version id, "" if nothing to do, False on failure."""
        if h.hot_task is not None:
            h.hot_task.cancel()
        engine = self._engine()
        live = self.layout.server_dir(h.server_id)
        if not await engine.has_local_changes(live):
            return ""
        self._phase(h, "saving")
        for attempt in range(5):
            try:
                async with h.sync_lock:
                    version = await engine.upload(h.lease, live, parent=engine.local_version(live),
                                                  kind="final", label=label)
                self._sync_event(h, version)
                return version["version_id"]
            except LeaseLost:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("final backup attempt %d failed: %s", attempt + 1, exc)
                if h.guard.must_stop():
                    break
                await asyncio.sleep(min(5 * (attempt + 1), 20))
        self.emit("error", message="La sauvegarde finale n'a pas pu être envoyée. Le monde reste sur ce PC "
                                   "et sera envoyé dès qu'il hébergera de nouveau.")
        return False

    async def _release(self, h: Hosted, final_version_id: str | None = None) -> None:
        try:
            await self.api.release(h.server_id, self.settings.host_id or "", h.epoch, final_version_id)
        except ApiError as exc:
            log.warning("release failed (%s): the lease will simply expire", exc)
        self._close_tunnels(h)
        if self.hosted is h:
            self.hosted = None
        self._phase(h, "released")

    async def _fence(self, h: Hosted, reason: str) -> None:
        log.error("SELF-FENCING server %s: %s", h.server_id, reason)
        self._phase(h, "fenced")
        self.emit("fenced", server_id=h.server_id, reason=reason)
        if h.hot_task is not None:
            h.hot_task.cancel()
        self._close_tunnels(h)
        if h.task is not None and h.task is not asyncio.current_task() and not h.task.done():
            h.task.cancel()
        proc = h.process
        if proc is not None and proc.alive:
            with contextlib.suppress(Exception):
                await proc.announce("Connexion au serveur central perdue : arrêt de sécurité du serveur.")
            await proc.stop(timeout=15)  # kicks players now, saves locally, kills after 15 s
        if self.hosted is h:
            self.hosted = None

    async def stop_hosting(self, reason: str, countdown_s: int = 0, migration_id: str | None = None) -> None:
        h = self.hosted
        if h is None:
            return
        if h.stopping:
            # Already stopping (e.g. the app closes during a countdown): skip what is left of the
            # countdown and wait for that stop to finish.
            h.interrupt.set()
            if h.stop_task is not None and h.stop_task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(h.stop_task)
            return
        h.stopping = True
        h.stop_task = asyncio.current_task()
        h.migration_id = migration_id
        h.migration_cancelled = False
        h.interrupt.clear()
        self._phase(h, "handing_over" if migration_id else "stopping")
        await self._report(h, "stopping")
        proc = h.process
        if proc is not None and proc.alive and countdown_s > 0:
            await self._countdown(h, proc, reason, countdown_s)
            if h.migration_cancelled:
                await self._resume_running(h)
                return
        if proc is not None and proc.alive and h.migration_id is not None:
            with contextlib.suppress(Exception):
                await proc.announce("Changement de PC en cours : votre jeu devrait se reconnecter tout seul "
                                    "(sinon, reconnectez-vous à la même adresse dans une minute)."
                                    if _transfers(h.mc_version) else
                                    "Changement de PC en cours : reconnectez-vous à la même adresse "
                                    "dans une minute.")
            await self._transfer_players(h, proc)
        async with h.sync_lock:  # let a running hot backup finish first
            if proc is not None and proc.alive:
                await proc.stop(self.stop_timeout_s)
        final = await self._final_backup(h)
        if h.migration_id is not None and (final is False or h.migration_cancelled):
            await self._resume_after_migration(h, final)
            return
        if final is False:
            self._forget(h)  # the lease expires on its own; this PC keeps the newest world
            return
        await self._release(h, final or None)

    async def _transfer_players(self, h: Hosted, proc: MinecraftProcess) -> None:
        """Send every player in game to a one-time address of the relay (docs/ARCHITECTURE.md 4.18):
        their game reconnects at once, and the relay keeps them waiting until the new PC runs."""
        if not _transfers(h.mc_version):
            return
        try:
            targets = await self.api.transfer_addresses(h.server_id, self.settings.host_id or "", h.epoch)
        except (ApiError, ApiUnavailable) as exc:
            log.info("no transfer addresses: %s", exc)
            return
        sent = 0
        for t in targets:
            host, port, player = str(t.get("host", "")), t.get("port"), str(t.get("player", ""))
            if not (_TRANSFER_HOST.fullmatch(host) and _PLAYER.fullmatch(player) and isinstance(port, int)
                    and 1 <= port <= 65535):
                continue
            with contextlib.suppress(Exception):
                await proc.send(f"transfer {host} {port} {player}")
                sent += 1
        if sent:
            log.info("%d player(s) sent to the relay before the handover", sent)
            await asyncio.sleep(TRANSFER_GRACE_S)

    async def _countdown(self, h: Hosted, proc: MinecraftProcess, reason: str, countdown_s: int) -> None:
        """Warn the players in the game; returns early if ``h.interrupt`` is set."""
        remaining = countdown_s
        marks = [countdown_s] + [m for m in STOP_ANNOUNCE_AT if m < countdown_s]
        for mark in marks:
            if await self._interruptible_sleep(h, remaining - mark):
                return
            remaining = mark
            if h.migration_id and _transfers(h.mc_version):
                text = f"Le serveur change de PC dans {mark} s : votre jeu devrait se reconnecter tout seul."
            elif h.migration_id:
                text = f"Le serveur change de PC dans {mark} s : reconnectez-vous à la même adresse une minute après."
            else:
                text = f"Le serveur s'arrête dans {mark} s ({reason})."
            with contextlib.suppress(Exception):
                await proc.announce(text)
        await self._interruptible_sleep(h, remaining)

    @staticmethod
    async def _interruptible_sleep(h: Hosted, seconds: float) -> bool:
        try:
            await asyncio.wait_for(h.interrupt.wait(), max(0.0, seconds))
            return True
        except TimeoutError:
            return False

    async def _resume_running(self, h: Hosted) -> None:
        """The migration was cancelled during the countdown: the game simply goes on here."""
        h.stopping = False
        h.migration_id = None
        h.stop_task = None
        if h.process is not None:
            with contextlib.suppress(Exception):
                await h.process.announce("Changement de PC annulé : la partie continue.")
        self._phase(h, "running")
        await self._report(h, "running")

    async def _resume_after_migration(self, h: Hosted, final: str | bool) -> None:
        """Minecraft is stopped but the handover will not happen: host again (same lease).

        Either the migration was cancelled meanwhile, or the final world could not be uploaded
        (then it is still only on this PC, so this PC must be the one to carry on).
        """
        h.migration_id = None
        if final is False:
            with contextlib.suppress(ApiError):
                await self.api.cancel_transfer(h.server_id)  # the VPS waits for our release
        if h.guard.must_stop() or self.hosted is not h or h.prepared is None:
            if final is False:
                self._forget(h)
            else:
                await self._release(h, final or None)
            return
        self.emit("error", message="Le changement de PC n'a pas abouti : le serveur redémarre sur ce PC.")
        h.stopping = False
        h.stop_task = None
        h.hot_task = None
        await self._launch(h, h.prepared, h.server)

    async def restart_hosted(self) -> None:
        """Restart Minecraft with the group's *current* configuration (mods, map, version,
        properties...), keeping the lease: what "applied at the next start" waits for."""
        h = self.hosted
        if h is None or h.process is None or h.phase != "running" or h.stopping:
            return
        try:
            server = {**await self.api.server(h.server_id), "_host_id": h.server["_host_id"]}
        except ApiError as exc:
            log.warning("restart with the configuration in use (latest not readable: %s)", exc)
            server = h.server
        blocked = self.unapproved_mods(server)
        if blocked:
            self.emit("mods_approval", server_id=h.server_id, server_name=h.name)
            self.emit("error", message="Redémarrage annulé : des mods ne sont pas encore autorisés sur ce PC. "
                                       "Le serveur continue avec la configuration actuelle.")
            return
        h.stopping = True
        await h.process.announce("Redémarrage du serveur dans 5 s.")
        await asyncio.sleep(5)
        await h.process.stop(self.stop_timeout_s)
        h.stopping = False
        if self.hosted is not h or h.guard.must_stop():
            return  # fenced meanwhile: the watchdog takes it from here
        try:
            h.server = server
            self._loader = Loader(server["loader"])
            prepared = await self._prepare(Loader(server["loader"]), server["mc_version"],
                                           lambda m, f: self.emit("progress", message=m, fraction=f))
            h.prepared, h.mc_version = prepared, server["mc_version"]
            h.java_major = prepared.distribution.java_major
            await self._install_mods(h, server)
            if not self._prepare_directory(h, server):
                await self._give_up(h, "EULA Minecraft non acceptée sur ce PC", upload_world=True)
                return
            await self._launch(h, prepared, server)
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            h.guard.revoked()
        except Exception as exc:  # noqa: BLE001 - the world on disk is complete: save it, hand over
            log.exception("restart failed")
            await self._give_up(h, f"échec du redémarrage : {exc}", upload_world=True)

    async def _report(self, h: Hosted, state: str, detail: str | None = None,
                      crash: CrashReport | None = None) -> None:
        try:
            await self.api.host_state(h.server_id, self.settings.host_id or "", h.epoch, state, detail,
                                      crash.model_dump(mode="json") if crash else None)
        except ApiError as exc:
            log.info("state report %s failed: %s", state, exc)
            if exc.code in ("stale_epoch",):
                h.guard.revoked()

    def _phase(self, h: Hosted, phase: str) -> None:
        if phase not in ("released",):
            h.phase = phase
        if phase in ("released", "fenced"):
            self._awake.release()
        else:
            self._awake.hold()  # asleep, this PC would stop the server for everybody
        self.emit("hosting", server_id=h.server_id, server_name=h.name, phase=phase, epoch=h.epoch)

    # ================================================================== user actions

    async def set_api_url(self, url: str) -> None:
        """Point the client to another control plane (only while logged out)."""
        url = check_base_url(url)
        if url == self.api.address:
            return
        if self.user is not None:
            raise RuntimeError("déconnecte-toi avant de changer de serveur central")
        await self.api.close()
        self.api = ApiClient(url, transport=self._api_transport,
                             on_refresh_token=self._remember_refresh_token)
        self.settings.api_url = url
        self.settings.host_id = None  # machine ids are per control plane
        self._save_settings()

    async def available_versions(self, loader: Loader) -> list[str]:
        """Stable versions offered for a loader, newest first."""
        resolver = SourceResolver(self._downloads)
        if loader == Loader.PAPER:
            versions = [v for v in await resolver.paper_versions() if "-" not in v]
        else:
            versions = await resolver.release_versions()
        return versions

    def local_world_size(self, server_id: str) -> int:
        directory = self.layout.server_dir(server_id)
        if not directory.exists():
            return 0
        excluded = {"libraries", "versions", "cache", "logs", "crash-reports", ".fabric", "bundler"}
        total = 0
        for child in directory.iterdir():
            if child.is_dir() and child.name not in excluded:
                total += sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
        return total

    # ================================================================== worlds from elsewhere

    async def import_world(self, server_id: str, source: Path) -> tuple[dict, world_files.ImportSummary]:
        """Replace the group's world by ``source`` (a world, an old server, or a .zip of either).

        Under an import lease (docs/ARCHITECTURE.md 3.8): the server must be stopped, this PC is
        then the only writer, and the previous world stays in the history.
        """
        if self.hosted is not None:
            raise RuntimeError("ce PC héberge un serveur : arrête-le d'abord")
        server = await self.api.server(server_id)
        staging = self.layout.home / "cache" / "import" / server_id
        self.emit("progress", message="Préparation du monde à importer", fraction=None)
        try:
            summary = await asyncio.to_thread(world_files.prepare_import, source, staging)
            host_id = self.settings.host_id or ""
            grant = await self.api.start_import(server_id, host_id)
            epoch = grant["epoch"]
            keep = asyncio.create_task(self._keep_lease(server_id, epoch, float(grant["ttl_s"])))
            version: dict | None = None
            try:
                version = await self._engine().upload(
                    LeaseContext(server["group_id"], server_id, host_id, epoch), staging,
                    parent=server["head_version_id"], kind="manual",
                    label=f"Monde importé : {summary.world_name}"[:64])
            finally:
                keep.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keep
                with contextlib.suppress(ApiError):
                    await self.api.release(server_id, host_id, epoch,
                                           version["version_id"] if version else None)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        log.info("world imported into %s: %s", server_id, summary)
        return version, summary

    async def _keep_lease(self, server_id: str, epoch: int, ttl_s: float) -> None:
        while True:
            await asyncio.sleep(ttl_s / 3)
            try:
                await self.api.renew(server_id, self.settings.host_id or "", epoch)
            except ApiError as exc:
                log.warning("import lease of %s not renewed: %s", server_id, exc)

    async def export_world(self, server_id: str, version_id: str, dest: Path) -> int:
        """Write a version of the world as a .zip that opens in single-player."""
        server = await self.api.server(server_id)
        version = await self._version(server_id, version_id)
        work = self.layout.home / "cache" / "export" / server_id
        await asyncio.to_thread(shutil.rmtree, work, True)
        try:
            await self._engine().rebuild(server["group_id"], server_id, version, work,
                                         reuse=self.layout.server_dir(server_id))
            return await asyncio.to_thread(world_files.export_world, work, dest, server["name"])
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)

    async def backup_now(self) -> dict | None:
        h = self.hosted
        if h is None or h.phase != "running":
            raise RuntimeError("ce PC n'héberge pas de serveur en ce moment")
        return await self.backup(h, kind="manual", label="Sauvegarde manuelle")

    async def send_console(self, command: str) -> None:
        h = self.hosted
        if h is None or h.process is None or not h.process.alive:
            raise RuntimeError("ce PC n'héberge pas de serveur en ce moment")
        await self._ban_ip(h, command)  # any other command goes through unchanged

    def accept_eula(self) -> None:
        self.settings.eula_accepted_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._save_settings()

    async def update_settings(self, **changes: Any) -> None:
        for key, value in changes.items():
            if not hasattr(self.settings, key):
                raise AttributeError(key)
            setattr(self.settings, key, value)
        self._save_settings()
        if self.user is not None:
            await self.push_host_preferences()

    async def _alerts_loop(self) -> None:
        """Alerts for group admins (only admins get any): emitted when they change."""
        last: list[dict] | None = None
        while True:
            try:
                alerts = await self.api.alerts()
                if alerts != last:
                    last = alerts
                    self.emit("alerts", alerts=alerts)
            except ApiError as exc:
                log.info("alerts not fetched: %s", exc)
            await asyncio.sleep(ALERTS_EVERY_S)

    async def _forget_server(self, server_id: str) -> None:
        """The group deleted that server (owner only, stopped): its copy on this PC goes too."""
        await self._stop_standby(server_id)
        if self.hosted is not None and self.hosted.server_id == server_id:
            return  # cannot happen (the server was stopped); never pull the floor from under Minecraft
        folder = self.layout.server_dir(server_id)
        if folder.exists():
            await asyncio.to_thread(shutil.rmtree, folder, True)
            log.info("deleted server %s: local copy removed", server_id)

    # ================================================================== standby copies

    async def _standby_loop(self) -> None:
        """Now and then, look for running servers this PC could take over (docs/ARCHITECTURE.md 2.6).

        Between two passes, every new backup of a server triggers a refresh (``server_update``).
        """
        while True:
            try:
                if self._standby_enabled():
                    me = await self.api.me()
                    for group in me["groups"]:
                        if not (group["can_host"] or group["role"] in ("owner", "admin")):
                            continue
                        for srv in await self.api.servers(group["group_id"]):
                            if srv["desired_running"] and srv.get("active_host_id") != self.settings.host_id:
                                self._want_standby(srv["server_id"])
            except ApiError as exc:
                log.info("standby pass skipped: %s", exc)
            await asyncio.sleep(STANDBY_REFRESH_S)

    def _standby_enabled(self) -> bool:
        return (self.settings.standby_copy and self.settings.hosting_allowed and not self.updating
                and self.hosted is None)

    def _want_standby(self, server_id: str) -> None:
        if not self._standby_enabled():
            return
        self._standby_dirty.add(server_id)
        task = self._standby.get(server_id)
        if task is None or task.done():
            self._standby[server_id] = asyncio.create_task(self._standby_refresh(server_id),
                                                           name=f"standby-{server_id[:8]}")

    async def _stop_standby(self, server_id: str) -> None:
        self._standby_dirty.discard(server_id)
        task = self._standby.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _standby_refresh(self, server_id: str) -> None:
        try:
            while server_id in self._standby_dirty:
                self._standby_dirty.discard(server_id)
                try:
                    await self._standby_once(server_id)
                except NotEnoughSpace as exc:
                    log.warning("no standby copy of %s: not enough disk space (%s)", server_id, exc)
                    return
                except ApiError as exc:
                    log.info("standby copy of %s not refreshed: %s", server_id, exc)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a standby copy is a bonus, never a failure
                    log.exception("standby copy of %s failed", server_id)
                    return
        finally:
            if self._standby.get(server_id) is asyncio.current_task():
                del self._standby[server_id]

    async def _standby_once(self, server_id: str) -> None:
        """Bring the prefetch area of ``server_id`` to the group's latest backup, without a lease."""
        if not self._standby_enabled() or self._prefetching:
            return
        server = await self.api.server(server_id)
        head_id = server["head_version_id"]
        if (not server["desired_running"] or head_id is None
                or server.get("active_host_id") == self.settings.host_id):
            return
        engine = SyncEngine(self.api)  # quiet: no progress bar for a background copy
        live = self.layout.server_dir(server_id)
        if head_id in (engine.local_version(live), engine.prefetched_version(live)):
            return
        key = (server["loader"], server["mc_version"])
        if key not in self._standby_prepared:  # Java and the server JAR, checked once per session
            await self._prepare(Loader(server["loader"]), server["mc_version"], lambda m, f: None)
            self._standby_prepared.add(key)
        await self.mod_installer.fetch_all(_mods(server))  # downloaded and checked, not run
        for pack in _datapacks(server):
            await self.mod_installer.fetch_file(pack.url, pack.sha512, pack.size, ".zip", pack.name)
        live.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(engine.recover, live)
        version = await self._version(server_id, head_id)
        result = await engine.prefetch(server["group_id"], server_id, version, live)
        log.info("standby copy of %s at %s: %s", server_id, head_id, result)
        self.emit("standby", server_id=server_id, version_id=head_id, at=time.time())

    # ================================================================== updates

    def _update_ready(self, ready: ReadyUpdate) -> None:
        self.emit("update", version=ready.version, notes=ready.notes)

    @property
    def busy(self) -> bool:
        """Hosting, or getting ready to host: not the moment to restart the application."""
        return self.hosted is not None or bool(self._prefetching)

    async def prepare_update(self) -> ReadyUpdate:
        """Last checks before quitting to install: from now on, this PC hosts nothing new."""
        ready = await asyncio.to_thread(self.updater.verify_ready)
        self.updating = True
        return ready

    async def shutdown(self, stop_hosting: bool = True) -> None:
        for task in list(self._prefetching.values()):
            task.cancel()
        for server_id in list(self._standby):
            await self._stop_standby(server_id)
        if stop_hosting and self.hosted is not None:
            if self.hosted.phase == "preparing" and self.hosted.task:
                self.hosted.task.cancel()
                await self._release(self.hosted)
            else:
                await self.stop_hosting("fermeture de l'application", countdown_s=10)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        self._awake.release()

    async def close(self) -> None:
        await self.shutdown()
        await self.api.close()
        await self._downloads.aclose()
        await self._pack_client.aclose()


def _group_properties(server: dict[str, Any]) -> dict[str, Any]:
    """The group's server.properties settings, checked again here: whoever controls the VPS still
    cannot set a key the API refuses (rcon, server-ip...) or slip a line break into one."""
    kept: dict[str, Any] = {}
    for key, value in (server.get("properties") or {}).items():
        try:
            kept.update(validate_properties({key: value}))
        except (ValueError, TypeError):
            log.warning("server.properties setting %r from the control plane refused", key)
    return kept


def _mods(server: dict[str, Any]) -> list[Mod]:
    return [Mod.model_validate(m) for m in server.get("mods", [])]


def _transfers(mc_version: str | None) -> bool:
    """Minecraft's /transfer exists since 1.20.5 (and in every 26.x version)."""
    try:
        return tuple(int(p) for p in (mc_version or "").split(".")) >= (1, 20, 5)
    except ValueError:
        return False


def _datapacks(server: dict[str, Any]) -> list[Datapack]:
    return [Datapack.model_validate(d) for d in server.get("datapacks", [])]


def _profile_or_none(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    try:
        return SparkProfile.model_validate(profile).model_dump(mode="json")
    except ValueError:
        return None


def _pregen_or_none(progress: dict[str, Any] | None) -> dict[str, Any] | None:
    """Never let a strange console line make the VPS refuse the heartbeat (and so the lease)."""
    if progress is None:
        return None
    try:
        return PregenProgress.model_validate(progress).model_dump(mode="json")
    except ValueError:
        log.warning("pre-generation progress not reported: %r", progress)
        return None
