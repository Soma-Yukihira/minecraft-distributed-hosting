"""Minecraft relay: one public TCP port on the VPS, routed to the PC that holds the lease.

Path of a player connection (docs/ARCHITECTURE.md 4.2):

1. the player connects to the VPS and sends its handshake (typed address, status or login);
2. the relay finds the server, checks access (personal addresses for unofficial accounts) and
   asks the current lease holder, over its control WebSocket, to open a *data* WebSocket;
3. the host connects back (outgoing HTTPS: works behind CGNAT and closed boxes), presents the
   one-time token, and the relay glues the two streams together.

Barrier 4 of docs/ARCHITECTURE.md 2.1: the relay only ever forwards to the holder of the
*current* lease, and cuts every tunnel of an epoch as soon as that lease ends.

When no PC can take players, the relay answers by itself: a server-list status explaining why,
and a readable refusal on login. While a PC is about to take them (the server starts, or changes
PC), a player logging in is *held* instead (docs/ARCHITECTURE.md 4.18): the relay keeps their
client waiting on "Connexion…" with login queries it answers at once, then lets them in.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from mdh_server import access
from mdh_server.api.errors import ApiError
from mdh_server.api.ratelimit import RateLimiter
from mdh_server.coordination.lease import LeaseState
from mdh_server.coordination.lease_store import load as load_lease
from mdh_server.relay import addresses, status_page
from mdh_shared import versions
from mdh_shared.models.api import TransferAddress
from mdh_shared.models.enums import HostRuntimeState, ServerState
from mdh_shared.protocol import minecraft as mc
from mdh_shared.protocol.messages import RelayOpen

if TYPE_CHECKING:
    from mdh_server.context import AppContext

log = logging.getLogger("mdh.relay")

HANDSHAKE_TIMEOUT_S = 5.0
ATTACH_TIMEOUT_S = 10.0
CHUNK = 64 * 1024
#: How long a player may wait on "Connexion…" for the server to come back.
HOLD_MAX_S = 300.0
#: One query every this many seconds (the client gives up after 30 s of silence).
HOLD_QUERY_EVERY_S = 5.0
HOLD_ANSWER_TIMEOUT_S = 10.0
MAX_HELD_PER_SERVER = 100
#: States after which the server will take players again by itself.
HOLD_STATES = frozenset({ServerState.ELECTING, ServerState.ASSIGNED, ServerState.STARTING, ServerState.MIGRATING})
#: Validity of a transfer address (ARCHITECTURE 4.18).
TRANSFER_VALID_S = 300.0
TRANSFER_PREFIX = "t"
#: First application that reads the player's address in ``relay_open`` (PROXY protocol).
PROXY_PROTOCOL_CLIENT = "0.13.0"

STATE_TEXT = {
    ServerState.OFFLINE: "Serveur arrêté : démarre-le depuis l'application du groupe.",
    ServerState.ELECTING: "Le serveur démarre, reviens dans une minute.",
    ServerState.ASSIGNED: "Le serveur démarre, reviens dans une minute.",
    ServerState.STARTING: "Le serveur démarre, reviens dans une minute.",
    ServerState.MIGRATING: "Le serveur change de PC, reviens dans une minute.",
    ServerState.STOPPING: "Le serveur s'arrête.",
    ServerState.UNREACHABLE: "Le PC hôte ne répond plus.",
    ServerState.ORPHANED: "Le PC hôte a disparu : un admin du groupe doit relancer le serveur.",
}
#: A sleeping server set to start when a player joins (docs/ARCHITECTURE.md 4.7).
ASLEEP = "En veille : connecte-toi pour le démarrer."
WAKING = "Le serveur démarre pour toi (environ une minute) : reconnecte-toi dans un instant."
UNKNOWN_ADDRESS = "Adresse inconnue : vérifie l'adresse du serveur dans l'application."
PERSONAL_NEEDED = ("Ce serveur accepte les comptes non officiels : connecte-toi avec ton adresse "
                   "personnelle (demande-la à un admin du groupe).")


class _Prefixed:
    """A reader whose first bytes were already read (to tell a browser from Minecraft)."""

    def __init__(self, prefix: bytes, reader: asyncio.StreamReader) -> None:
        self._prefix, self._reader = prefix, reader

    async def readexactly(self, n: int) -> bytes:
        taken, self._prefix = self._prefix[:n], self._prefix[n:]
        return taken + (await self._reader.readexactly(n - len(taken)) if n > len(taken) else b"")


@dataclass(eq=False)
class Tunnel:
    conn_id: str
    server_id: str
    host_id: str
    epoch: int
    token: str
    peer: str
    player: str | None
    attached: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.monotonic)
    bytes_up: int = 0
    bytes_down: int = 0
    #: minecraft (a player or a server-list ping) or map (a browser showing the world map).
    kind: str = "minecraft"


class Relay:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.tunnels: dict[str, Tunnel] = {}
        self._server: asyncio.base_events.Server | None = None
        self._per_ip: collections.Counter[str] = collections.Counter()
        self._limiter = RateLimiter()
        #: One-time addresses for players sent by /transfer: label -> (server, player, expiry).
        self.transfers: dict[str, tuple[str, str, float]] = {}
        #: Players waiting on "Connexion…", per server.
        self.held: collections.Counter[str] = collections.Counter()
        #: One event per waiting player, set when their server changes (see server_changed).
        self._waiting: dict[str, set[asyncio.Event]] = {}
        self.hold_max_s = HOLD_MAX_S
        self.hold_query_every_s = HOLD_QUERY_EVERY_S

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        s = self.ctx.settings
        self._server = await asyncio.start_server(self._accept, s.relay_bind, s.relay_port)
        log.info("relay listening on %s:%s (domain %s)", s.relay_bind, self.port, s.relay_domain)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self.tunnels.values()):
            self._cut(t, "control plane stopping")
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self.ctx.settings.relay_port

    def connections(self, server_id: str) -> int:
        return sum(1 for t in self.tunnels.values() if t.server_id == server_id and t.kind == "minecraft")

    def players(self, server_id: str) -> int:
        """Players in game through the relay (server-list pings excluded)."""
        return sum(1 for t in self.tunnels.values() if t.server_id == server_id and t.player is not None)

    # ------------------------------------------------------------------ lease binding

    def transfer_addresses(self, server_id: str) -> list[TransferAddress]:
        """A one-time address for each player in game on that server, for Minecraft's /transfer."""
        now = self.ctx.now()
        found: dict[str, TransferAddress] = {}
        for t in self.tunnels.values():
            if t.server_id != server_id or t.player is None or t.player.lower() in found:
                continue
            label = TRANSFER_PREFIX + addresses.new_token() + addresses.new_token()[:3]
            self.transfers[label] = (server_id, t.player, now + TRANSFER_VALID_S)
            found[t.player.lower()] = TransferAddress(player=t.player, host=f"{label}.{self.ctx.settings.relay_domain}",
                                                      port=self.port)
        return list(found.values())

    def _route(self, hostname: str) -> addresses.Route:
        domain = "." + self.ctx.settings.relay_domain.lower()
        entry = self.transfers.get(hostname[: -len(domain)]) if hostname.endswith(domain) else None
        if entry is not None and entry[2] > self.ctx.now():
            return addresses.Route(entry[0], entry[1])  # like a personal address: that player only
        return addresses.resolve(self.ctx.conn, self.ctx.settings, hostname)

    def sweep(self) -> None:
        """Cut every tunnel whose host no longer holds the current lease."""
        now = self.ctx.now()
        for label in [k for k, (_, _, until) in self.transfers.items() if until <= now]:
            del self.transfers[label]
        leases: dict[str, LeaseState] = {}  # one read per server, not per player
        for t in list(self.tunnels.values()):
            lease = leases.get(t.server_id) or leases.setdefault(t.server_id, load_lease(self.ctx.conn, t.server_id))
            if lease.holder != t.host_id or lease.epoch != t.epoch or not lease.is_active(now):
                self._cut(t, f"lease epoch {t.epoch} ended")

    def cut_server(self, server_id: str, reason: str) -> None:
        for t in list(self.tunnels.values()):
            if t.server_id == server_id:
                self._cut(t, reason)

    def cut_server_player(self, server_id: str, player: str) -> None:
        """A personal address was replaced: whoever used the old one is disconnected."""
        for t in list(self.tunnels.values()):
            if t.server_id == server_id and t.player is not None and t.player.lower() == player.lower():
                self._cut(t, "personal address replaced")

    def _cut(self, t: Tunnel, reason: str) -> None:
        log.info("cutting tunnel %s (%s): %s", t.conn_id, t.player or t.peer, reason)
        if t.task is not None and not t.task.done():
            t.task.cancel()

    # ------------------------------------------------------------------ data side (host)

    def claim(self, conn_id: str, token: str) -> Tunnel | None:
        """The host's data WebSocket presents its one-time token."""
        t = self.tunnels.get(conn_id)
        if t is None or t.attached.done() or not hmac.compare_digest(t.token, token):
            return None
        return t

    # ------------------------------------------------------------------ player side

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        s = self.ctx.settings
        if (len(self.tunnels) >= s.relay_max_connections or self._per_ip[peer] >= s.relay_max_per_ip
                or not self._limiter.hit(f"relay:{peer}", per_minute=s.relay_connections_per_minute,
                                         burst=s.relay_max_per_ip * 2)):
            writer.close()
            return
        self._per_ip[peer] += 1
        try:
            await self._serve(reader, writer, peer)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, mc.ProtocolError) as exc:
            log.debug("relay connection from %s ended: %s", peer, exc)
        except asyncio.CancelledError:
            pass  # cut (lease ended) or shutdown
        except Exception:  # noqa: BLE001 - one bad connection must never stop the relay
            log.exception("relay connection from %s failed", peer)
        finally:
            self._per_ip[peer] -= 1
            if self._per_ip[peer] <= 0:
                del self._per_ip[peer]
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str) -> None:
        # A Minecraft handshake starts with its length then 0x00 (or a 2-byte length, high bit
        # set); an HTTP request with its method in capitals ("GET", "HEAD"...): never ambiguous.
        start = await asyncio.wait_for(reader.readexactly(2), HANDSHAKE_TIMEOUT_S)
        if start.isalpha() and start.isupper():
            await self._status_page(start, reader, writer, peer)
            return
        raw, hs = await asyncio.wait_for(mc.read_handshake(_Prefixed(start, reader)), HANDSHAKE_TIMEOUT_S)
        route = self._route(hs.hostname)
        srv = None if route.server_id is None else self.ctx.conn.execute(
            "SELECT * FROM servers WHERE id = ?", (route.server_id,)).fetchone()

        if hs.next_state == mc.STATUS:
            if srv is not None and self._routable(srv):
                await self._tunnel(srv, raw, reader, writer, peer, None)
            else:
                await self._local_status(srv, hs, reader, writer)
            return

        raw_login, packet_id, payload = await asyncio.wait_for(
            mc.read_packet(reader, mc.MAX_LOGIN_START_BYTES), HANDSHAKE_TIMEOUT_S)
        name = mc.parse_login_start(packet_id, payload)
        refusal = self._refusal(srv, route, name, peer)
        if refusal is None and access.whitelist_on(srv) and not access.allowed(srv, name):
            refusal = await self._ask_access(srv, name, peer)
        if refusal is None and route.player is None and access.checks_places(srv):
            refusal = await self._check_place(srv, name, peer)
        if refusal is None and not self._routable(srv):
            woken = await self._wake(srv, name)
            srv = self.ctx.conn.execute("SELECT * FROM servers WHERE id = ?", (srv["id"],)).fetchone()
            if hs.protocol >= mc.MIN_PLUGIN_PROTOCOL and self._holdable(srv):
                srv = await self._hold(srv, name, reader, writer)
            if srv is None or not self._routable(srv):
                refusal = woken or STATE_TEXT.get(ServerState(srv["state"]) if srv else ServerState.OFFLINE,
                                                  "Serveur indisponible.")
        if refusal is not None:
            # The typed host name may be a personal address (a secret): log its kind, not its value.
            kind = "personal" if route.player else ("server" if srv is not None else "unknown")
            shown = refusal.startswith(access.REVEAL.split("{")[0])  # holds the address: not logged
            log.info("relay refused %s from %s (%s address): %s", name, peer, kind,
                     "personal address shown" if shown else refusal)
            writer.write(mc.login_disconnect(refusal))
            await writer.drain()
            return
        log.info("relay: %s from %s joins server %s", name, peer, srv["id"])
        if route.player is not None and access.personal_only(srv):
            access.delivered(self.ctx.conn, srv["id"], name)
        if hs.next_state == mc.TRANSFER:
            raw = mc.login_handshake(hs)  # the server needs no "accepts-transfers"
        await self._tunnel(srv, raw + raw_login, reader, writer, peer, name)

    def _holdable(self, srv) -> bool:
        return bool(srv["desired_running"]) and ServerState(srv["state"]) in HOLD_STATES

    async def _hold(self, srv, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Keep a player on "Connexion…" until a PC takes players (or it is clear none will).

        Each round sends one login query and reads its answer before anything else: when the
        player is let in, no answer is left in flight that the server would not expect."""
        server_id = srv["id"]
        if self.held[server_id] >= MAX_HELD_PER_SERVER:
            return srv
        self.held[server_id] += 1
        changed = asyncio.Event()
        self._waiting.setdefault(server_id, set()).add(changed)
        log.info("relay: %s waits for server %s (%s)", name, server_id, srv["state"])
        deadline = time.monotonic() + self.hold_max_s
        message_id = 0
        try:
            while time.monotonic() < deadline:
                message_id += 1
                writer.write(mc.login_plugin_request(message_id))
                await writer.drain()
                _, packet_id, payload = await asyncio.wait_for(mc.read_packet(reader, 1024), HOLD_ANSWER_TIMEOUT_S)
                if mc.parse_plugin_response(packet_id, payload) != message_id:
                    raise mc.ProtocolError("answer to another query")
                next_query = time.monotonic() + self.hold_query_every_s
                while (left := min(next_query, deadline) - time.monotonic()) > 0:
                    changed.clear()  # before reading: a change from now on wakes us up
                    srv = self.ctx.conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
                    if srv is None or self._routable(srv):
                        return srv
                    if not self._holdable(srv):
                        return srv
                    # Woken by the coordinator when the server changes, else read again at the
                    # next query: no more reading the database twice a second per player.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(changed.wait(), left)
            return srv
        finally:
            waiting = self._waiting.get(server_id)
            if waiting is not None:
                waiting.discard(changed)
                if not waiting:
                    del self._waiting[server_id]
            self.held[server_id] -= 1
            if self.held[server_id] <= 0:
                del self.held[server_id]

    def server_changed(self, server_id: str) -> None:
        """Called by the coordinator each time a server's state is broadcast: the players
        waiting for it check at once whether they can go in."""
        for event in self._waiting.get(server_id, ()):
            event.set()

    def _refusal(self, srv, route: addresses.Route, name: str, peer: str) -> str | None:
        if srv is None:
            return UNKNOWN_ADDRESS
        if route.player is not None and route.player.lower() != name.lower():
            return f"Cette adresse personnelle est celle de {route.player} : utilise la tienne."
        if access.personal_only(srv) and route.player is None and access.allowed(srv, name):
            token = access.address_to_show(self.ctx.conn, srv, name, access.place_of(peer), self.ctx.now())
            if token is None:
                return PERSONAL_NEEDED
            return access.REVEAL.format(address=addresses.player_address(self.ctx.settings, token, self.port))
        return None

    async def _ask_access(self, srv, name: str, peer: str) -> str:
        """A player the whitelist refuses: the attempt is their request to join."""
        if not access.PLAYER_NAME.fullmatch(name) or not self._limiter.hit(
                f"access:{peer}", per_minute=2, burst=5):
            return access.ASKED_PERSONAL if access.personal_only(srv) else access.ASKED
        message, new = access.record(self.ctx.conn, srv, name, self.ctx.now(), access.place_of(peer))
        if new:
            log.info("relay: %s asks to join server %s", name, srv["id"])
            await self.ctx.coordinator.broadcast(srv["id"])
        return message

    async def _check_place(self, srv, name: str, peer: str) -> str | None:
        """Nothing verifies an unofficial account's name: a listed player must come from a known
        place, or an admin confirms the new one."""
        refusal, new = access.check_place(self.ctx.conn, srv, name, access.place_of(peer), self.ctx.now())
        if new:
            log.info("relay: %s connects to server %s from a new place", name, srv["id"])
            await self.ctx.coordinator.broadcast(srv["id"])
        return refusal

    async def _status_page(self, start: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           peer: str) -> None:
        """One HTTP request from a browser, answered with the status page, then closed (or the
        world map, carried to the host's BlueMap)."""
        request = await status_page.read_request(reader, start, HANDSHAKE_TIMEOUT_S)
        if request is None:
            return
        head_only = request.method == "HEAD"
        if request.method not in ("GET", "HEAD"):
            writer.write(status_page.response("405 Method Not Allowed", b""))
        else:
            route = addresses.resolve(self.ctx.conn, self.ctx.settings, request.hostname)
            srv = None if route.server_id is None else self.ctx.conn.execute(
                "SELECT * FROM servers WHERE id = ?", (route.server_id,)).fetchone()
            if srv is None or not srv["public_status"]:
                writer.write(status_page.response("404 Not Found", status_page.NOT_FOUND, head_only=head_only))
            elif request.path in ("/map", "/map/") or request.path.startswith("/map/"):
                if not srv["map_enabled"]:
                    writer.write(status_page.response("404 Not Found", status_page.NOT_FOUND, head_only=head_only))
                elif request.path == "/map":
                    writer.write(status_page.redirect("/map/"))
                elif status_page.map_request(request) is None:
                    writer.write(status_page.response("404 Not Found", status_page.NOT_FOUND, head_only=head_only))
                elif not self._routable(srv):
                    writer.write(status_page.response("503 Service Unavailable", status_page.MAP_OFFLINE,
                                                      head_only=head_only))
                else:
                    await self._map(srv, request, reader, writer, peer)
                    return
            else:
                body = status_page.render(self._page_info(srv))
                writer.write(status_page.response("200 OK", body, head_only=head_only))
        await writer.drain()

    async def _map(self, srv, first: status_page.HttpRequest, reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter, peer: str) -> None:
        """A browser showing the world map: its requests (GET and HEAD under /map/ only, with a
        few harmless headers) go to BlueMap on the host, through one tunnel per connection."""
        lease = load_lease(self.ctx.conn, srv["id"])
        assert lease.holder is not None
        t = Tunnel(uuid.uuid4().hex, srv["id"], lease.holder, lease.epoch, secrets.token_urlsafe(24), peer, None,
                   task=asyncio.current_task(), kind="map")
        self.tunnels[t.conn_id] = t
        try:
            sent = await self.ctx.hub.send_to_host(t.host_id, RelayOpen(
                conn_id=t.conn_id, server_id=t.server_id, epoch=t.epoch, relay_token=t.token, target="map"))
            ws: WebSocket | None = None
            if sent:
                with contextlib.suppress(TimeoutError):
                    ws = await asyncio.wait_for(asyncio.shield(t.attached), ATTACH_TIMEOUT_S)
            if ws is None:
                # Online, but the host does not serve the map: BlueMap starts with the server.
                writer.write(status_page.response("503 Service Unavailable", status_page.MAP_NOT_ACTIVE))
                await writer.drain()
                return

            async def requests() -> None:
                request: status_page.HttpRequest | None = first
                while request is not None:
                    forwarded = status_page.map_request(request)
                    if forwarded is None:
                        return  # anything else ends the connection; the browser opens another
                    t.bytes_up += len(forwarded)
                    await ws.send_bytes(forwarded)
                    request = await status_page.read_request(reader, None, status_page.MAP_IDLE_S)

            async def responses() -> None:
                while t.bytes_down < status_page.MAP_MAX_BYTES:
                    data = await ws.receive_bytes()
                    t.bytes_down += len(data)
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if tasks[0] in done and not tasks[1].done():
                    # No more requests: let the last answer finish, briefly.
                    await asyncio.wait([tasks[1]], timeout=status_page.MAP_LAST_ANSWER_S)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionError,
                                             RuntimeError, KeyError, Exception):
                        await task
            if t.bytes_down == 0:
                # The host answered but BlueMap did not (it starts a little after the game).
                with contextlib.suppress(ConnectionError, RuntimeError):
                    writer.write(status_page.response("503 Service Unavailable", status_page.MAP_STARTING))
                    await writer.drain()
        finally:
            self.tunnels.pop(t.conn_id, None)
            if not t.attached.done():
                t.attached.cancel()
            t.finished.set()

    def _page_info(self, srv) -> status_page.PageInfo:
        status = self.ctx.coordinator.status(srv["id"])
        online = self._routable(srv)
        asleep = (srv["start_on_join"] and not srv["desired_running"]
                  and ServerState(srv["state"]) == ServerState.OFFLINE)
        activity = self.ctx.coordinator.activity.get(srv["id"])
        return status_page.PageInfo(
            name=srv["name"], online=online,
            state_text="En ligne" if online else (
                "En veille : connecte-toi dans Minecraft pour le démarrer" if asleep else
                STATE_TEXT.get(ServerState(srv["state"]), "Indisponible").rstrip(".")),
            players=list(activity.players) if online and activity else [],
            players_count=status.players_online if online else None,
            address=status.public_address, version=f"{srv['loader'].capitalize()} {srv['mc_version']}",
            needs_personal_address=access.personal_only(srv), has_map=bool(srv["map_enabled"]))

    async def _wake(self, srv, name: str) -> str | None:
        """Start a sleeping server for a player allowed on it. Returns what to tell the player.

        Server-list pings never wake it (the list refreshes on its own); a login attempt does.
        With the whitelist on, only a listed player can: anyone else cannot make a PC host.
        """
        if (not srv["start_on_join"] or srv["desired_running"]
                or ServerState(srv["state"]) != ServerState.OFFLINE):
            return None
        if json.loads(srv["properties_json"]).get("white-list") or srv["allow_offline_accounts"]:
            allowed = {p.lower() for p in json.loads(srv["allowed_players_json"])}
            if name.lower() not in allowed:
                return None
        if not self._limiter.hit(f"wake:{srv['id']}", per_minute=1, burst=2):
            return WAKING  # just woken by someone else: same answer, no second request
        try:
            await self.ctx.coordinator.request_start(srv["id"], None, player=name)
        except ApiError as exc:
            log.info("relay could not wake %s: %s", srv["id"], exc.code)
            return None
        log.info("relay: %s woke server %s up", name, srv["id"])
        return WAKING

    def _routable(self, srv) -> bool:
        """Players are sent to a PC only while it holds the lease and Minecraft is up."""
        lease = load_lease(self.ctx.conn, srv["id"])
        return (lease.is_active(self.ctx.now())
                and self.ctx.coordinator.runtime.get(srv["id"]) == HostRuntimeState.RUNNING)

    async def _local_status(self, srv, hs: mc.Handshake, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        _, packet_id, _ = await asyncio.wait_for(mc.read_packet(reader, 16), HANDSHAKE_TIMEOUT_S)
        if packet_id != 0x00:
            return
        if srv is None:
            text, version = UNKNOWN_ADDRESS, "?"
        else:
            asleep = (srv["start_on_join"] and not srv["desired_running"]
                      and ServerState(srv["state"]) == ServerState.OFFLINE)
            text = f"{srv['name']} — " + (ASLEEP if asleep else
                                          STATE_TEXT.get(ServerState(srv["state"]), "indisponible"))
            version = srv["mc_version"]
        writer.write(mc.status_response(text, version_name=version, protocol=hs.protocol))
        await writer.drain()
        with contextlib.suppress(TimeoutError, asyncio.IncompleteReadError, mc.ProtocolError):
            _, packet_id, payload = await asyncio.wait_for(mc.read_packet(reader, 16), HANDSHAKE_TIMEOUT_S)
            if packet_id == 0x01:
                writer.write(mc.pong(payload))
                await writer.drain()

    async def _tunnel(self, srv, initial: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      peer: str, player: str | None) -> None:
        lease = load_lease(self.ctx.conn, srv["id"])
        assert lease.holder is not None
        t = Tunnel(uuid.uuid4().hex, srv["id"], lease.holder, lease.epoch, secrets.token_urlsafe(24), peer, player,
                   task=asyncio.current_task())
        self.tunnels[t.conn_id] = t
        try:
            sent = await self.ctx.hub.send_to_host(t.host_id, RelayOpen(
                conn_id=t.conn_id, server_id=t.server_id, epoch=t.epoch, relay_token=t.token,
                **self._peer_fields(t.host_id, writer, player)))
            ws: WebSocket | None = None
            if sent:
                with contextlib.suppress(TimeoutError):
                    ws = await asyncio.wait_for(asyncio.shield(t.attached), ATTACH_TIMEOUT_S)
            if ws is None:
                if player is not None:
                    writer.write(mc.login_disconnect("Le PC hôte ne répond pas, réessaie dans un instant."))
                    await writer.drain()
                return
            await ws.send_bytes(initial)
            await self._pump(t, reader, writer, ws)
        finally:
            self.tunnels.pop(t.conn_id, None)
            if not t.attached.done():
                t.attached.cancel()
            t.finished.set()
            if player is not None:
                log.info("relay: %s left server %s (%d s, %d kB up, %d kB down)", player, t.server_id,
                         time.monotonic() - t.started, t.bytes_up // 1024, t.bytes_down // 1024)

    def _peer_fields(self, host_id: str, writer: asyncio.StreamWriter, player: str | None) -> dict:
        """The player's address for the host (PROXY protocol, docs/ARCHITECTURE.md 4.22), only to
        applications that know these fields: an older one would refuse the whole message."""
        session = self.ctx.hub.host_session(host_id)
        peer = writer.get_extra_info("peername") or ()
        if (session is None or len(peer) < 2
                or versions.older_than(session.client_version, PROXY_PROTOCOL_CLIENT)):
            return {}
        # A name Minecraft would not accept (a modified client) is simply not passed on.
        named = player if player is not None and access.PLAYER_NAME.fullmatch(player) else None
        return {"peer_ip": peer[0], "peer_port": peer[1], "player": named}

    @staticmethod
    async def _pump(t: Tunnel, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws: WebSocket) -> None:
        async def up() -> None:
            while data := await reader.read(CHUNK):
                t.bytes_up += len(data)
                await ws.send_bytes(data)

        async def down() -> None:
            while True:
                data = await ws.receive_bytes()
                t.bytes_down += len(data)
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionError,
                                         RuntimeError, KeyError, Exception):
                    await task
