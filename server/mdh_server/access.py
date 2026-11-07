"""Access requests: a player the whitelist refuses asks to join just by trying to connect
(docs/ARCHITECTURE.md 4.12).

The relay records the name and tells the player; the group's admins see the request in the
application and accept it in one click (the name joins the list of allowed players) or refuse
it. Nothing is trusted from the request but a well-formed name: with verified accounts,
Microsoft vouches for it at login; with unofficial accounts, accepting creates a personal
address. The player sees it by reconnecting through the server's address from the network they
asked from, until they first use it (7 days at most); an admin can also hand it over.

Unofficial accounts joining through the server's address (``personal_addresses`` off): nothing
verifies the name, so the relay also remembers **where** each player connects from (the network
of their address, see :func:`place_of`). The first connection of a listed player is trusted;
afterwards, a connection from an unknown place becomes a "new place" request that an admin
accepts or refuses, and the player is let in only once it is accepted.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3

from mdh_server import events
from mdh_server.database.db import immediate
from mdh_server.relay import addresses

#: Pending requests kept per server (a flood of names stops there).
MAX_PENDING = 50
#: Requests older than this are forgotten (and may be made again).
KEEP_S = 30 * 24 * 3600
#: Same bound as the API's list of allowed players.
MAX_ALLOWED_PLAYERS = 200
#: Names Minecraft accepts: anything else is refused without being recorded.
PLAYER_NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
#: Places remembered per player (the least recently used is forgotten first).
MAX_PLACES = 20
#: A place not used for this long is forgotten.
PLACE_KEEP_S = 400 * 24 * 3600
#: An accepted player may see their personal address through the server's address this long.
REVEAL_S = 7 * 24 * 3600


class ListFull(Exception):
    pass

ASKED = ("Tu n'es pas encore sur la liste des joueurs : ta demande est envoyée aux administrateurs "
         "du groupe. Reconnecte-toi quand ils l'auront acceptée.")
ASKED_PERSONAL = ("Tu n'es pas encore sur la liste des joueurs : ta demande est envoyée aux "
                  "administrateurs du groupe. Reconnecte-toi à cette même adresse quand ils l'auront "
                  "acceptée : ton adresse personnelle s'affichera.")
REVEAL = ("Ta demande est acceptée ! Ton adresse personnelle, à toi seul : {address} — Dans "
          "Multijoueur, ajoute un serveur avec cette adresse et connecte-toi toujours avec elle. "
          "Ne la donne à personne.")
REFUSED = "Les administrateurs du groupe ont refusé ta demande d'accès à ce serveur."
NEW_PLACE = ("Tu te connectes depuis un nouvel endroit : un administrateur du groupe doit confirmer que "
             "c'est bien toi. Reconnecte-toi quand il aura accepté.")
PLACE_REFUSED = ("Les administrateurs du groupe n'ont pas reconnu cette connexion depuis cet endroit : "
                 "elle est refusée.")


def personal_only(srv: sqlite3.Row) -> bool:
    """Unofficial accounts joining only through their personal address."""
    return bool(srv["allow_offline_accounts"] and srv["personal_addresses"])


def checks_places(srv: sqlite3.Row) -> bool:
    """Unofficial accounts joining through the server's address: only the place vouches for them."""
    return bool(srv["allow_offline_accounts"] and not srv["personal_addresses"])


def place_of(ip: str) -> str:
    """The network a connection comes from: /24 in IPv4, /48 in IPv6 (roughly one household,
    or a small part of an internet provider's addresses)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "?"
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 48}", strict=False))


def whitelist_on(srv: sqlite3.Row) -> bool:
    return bool(json.loads(srv["properties_json"]).get("white-list") or srv["allow_offline_accounts"])


def allowed(srv: sqlite3.Row, name: str) -> bool:
    return name.lower() in {p.lower() for p in json.loads(srv["allowed_players_json"])}


def purge(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM access_requests WHERE last_at < ?", (now - KEEP_S,))
    conn.execute("DELETE FROM known_places WHERE last_at < ?", (now - PLACE_KEEP_S,))


def _pending(conn: sqlite3.Connection, server_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM access_requests WHERE server_id = ? AND status = 'pending'",
                        (server_id,)).fetchone()[0]


def record(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, now: float,
           place: str = "") -> tuple[str, bool]:
    """Note an attempt by ``name``. Returns (what to tell the player, new pending request)."""
    with immediate(conn):
        purge(conn, now)
        # One request per name, from wherever it comes: a refused name stays refused.
        row = conn.execute("SELECT status, place FROM access_requests"
                           " WHERE server_id = ? AND player_name = ? AND kind = 'join'",
                           (srv["id"], name)).fetchone()
        if row is not None:
            conn.execute("UPDATE access_requests SET last_at = ?, attempts = attempts + 1"
                         " WHERE server_id = ? AND player_name = ? AND place = ?",
                         (now, srv["id"], name, row["place"]))
            return (REFUSED if row["status"] == "refused" else _asked(srv)), False
        if _pending(conn, srv["id"]) >= MAX_PENDING:
            return _asked(srv), False  # same answer: the player cannot tell the list is full
        conn.execute("INSERT OR REPLACE INTO access_requests"
                     " (server_id, player_name, place, kind, first_at, last_at)"
                     " VALUES (?, ?, ?, 'join', ?, ?)", (srv["id"], name, place, now, now))
        events.record(conn, group_id=srv["group_id"], server_id=srv["id"], type="access_requested",
                      now=now, player=name)
    return _asked(srv), True


def check_place(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, place: str,
                now: float) -> tuple[str | None, bool]:
    """A listed player joining through the server's address with an unverified account.

    Returns (refusal or None, new pending request). Their first connection is trusted, as when
    the player was added to the list; later ones must come from a place already known."""
    with immediate(conn):
        purge(conn, now)
        known = {r["place"] for r in conn.execute(
            "SELECT place FROM known_places WHERE server_id = ? AND player_name = ?", (srv["id"], name))}
        if not known or place in known:
            _learn(conn, srv["id"], name, place, now)
            return None, False
        row = conn.execute("SELECT status FROM access_requests WHERE server_id = ? AND player_name = ?"
                           " AND place = ? AND kind = 'new_place'", (srv["id"], name, place)).fetchone()
        if row is not None:
            conn.execute("UPDATE access_requests SET last_at = ?, attempts = attempts + 1"
                         " WHERE server_id = ? AND player_name = ? AND place = ?", (now, srv["id"], name, place))
            return (PLACE_REFUSED if row["status"] == "refused" else NEW_PLACE), False
        if _pending(conn, srv["id"]) >= MAX_PENDING:
            return NEW_PLACE, False
        conn.execute("INSERT OR REPLACE INTO access_requests"
                     " (server_id, player_name, place, kind, first_at, last_at)"
                     " VALUES (?, ?, ?, 'new_place', ?, ?)", (srv["id"], name, place, now, now))
        events.record(conn, group_id=srv["group_id"], server_id=srv["id"], type="new_place_requested",
                      now=now, player=name, place=place)
    return NEW_PLACE, True


def _learn(conn: sqlite3.Connection, server_id: str, name: str, place: str, now: float) -> None:
    if not place:
        return  # a request older than places (0.4.x): the next connection is trusted instead
    conn.execute("INSERT INTO known_places (server_id, player_name, place, first_at, last_at)"
                 " VALUES (?, ?, ?, ?, ?)"
                 " ON CONFLICT (server_id, player_name, place) DO UPDATE SET last_at = excluded.last_at",
                 (server_id, name, place, now, now))
    conn.execute("DELETE FROM known_places WHERE server_id = ? AND player_name = ? AND place NOT IN ("
                 " SELECT place FROM known_places WHERE server_id = ? AND player_name = ?"
                 " ORDER BY last_at DESC LIMIT ?)", (server_id, name, server_id, name, MAX_PLACES))


def forget_removed(conn: sqlite3.Connection, server_id: str, players: list[str]) -> None:
    """Players taken off the list lose their places (back on it, they start afresh)."""
    kept = {p.lower() for p in players}
    for table, extra in (("known_places", ""), ("access_requests", " AND kind = 'new_place'")):
        for r in conn.execute(f"SELECT DISTINCT player_name FROM {table} WHERE server_id = ?{extra}",
                              (server_id,)).fetchall():
            if r["player_name"].lower() not in kept:
                conn.execute(f"DELETE FROM {table} WHERE server_id = ? AND player_name = ?{extra}",
                             (server_id, r["player_name"]))


def _asked(srv: sqlite3.Row) -> str:
    return ASKED_PERSONAL if personal_only(srv) else ASKED


def _find(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, place: str | None) -> sqlite3.Row | None:
    """A request to join (``place`` None: older applications), or the one from that place."""
    if place is None:
        return conn.execute("SELECT * FROM access_requests WHERE server_id = ? AND player_name = ?"
                            " AND kind = 'join'", (srv["id"], name)).fetchone()
    return conn.execute("SELECT * FROM access_requests WHERE server_id = ? AND player_name = ? AND place = ?",
                        (srv["id"], name, place)).fetchone()


def accept(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, actor_user_id: str, now: float,
           place: str | None = None) -> bool:
    """Add the player to the allowed list (applied at once by the host), or trust the new place
    they connected from. False if no such request."""
    with immediate(conn):
        row = _find(conn, srv, name, place)
        if row is None:
            return False
        name = row["player_name"]  # as the player typed it
        if row["kind"] == "new_place":
            _learn(conn, srv["id"], name, row["place"], now)
            conn.execute("DELETE FROM access_requests WHERE server_id = ? AND player_name = ? AND place = ?",
                         (srv["id"], name, row["place"]))
            events.record(conn, group_id=srv["group_id"], server_id=srv["id"], type="new_place_accepted",
                          now=now, actor_user_id=actor_user_id, player=name, place=row["place"])
            return True
        players = json.loads(srv["allowed_players_json"])
        if name.lower() not in {p.lower() for p in players}:
            if len(players) >= MAX_ALLOWED_PLAYERS:
                raise ListFull
            players.append(name)
            conn.execute("UPDATE servers SET allowed_players_json = ?, config_revision = config_revision + 1"
                         " WHERE id = ?", (json.dumps(players), srv["id"]))
            addresses.sync_player_tokens(conn, srv["id"], players)
        _learn(conn, srv["id"], name, row["place"], now)  # where they asked from is theirs
        # Shown to them through the server's address, from that place only (see address_to_show).
        conn.execute("UPDATE player_access SET reveal_until = ? WHERE server_id = ? AND lower(player_name) = ?",
                     (now + REVEAL_S, srv["id"], name.lower()))
        conn.execute("DELETE FROM access_requests WHERE server_id = ? AND player_name = ? AND kind = 'join'",
                     (srv["id"], name))
        events.record(conn, group_id=srv["group_id"], server_id=srv["id"], type="access_accepted", now=now,
                      actor_user_id=actor_user_id, player=name)
    return True


def address_to_show(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, place: str, now: float) -> str | None:
    """The personal token of a player accepted from the access requests, if they may see it here:
    within REVEAL_S of the acceptance, before they first use it, and from a network known as
    theirs (the one they asked from). Anyone else typing that name gets the usual refusal."""
    row = conn.execute("SELECT token FROM player_access WHERE server_id = ? AND lower(player_name) = ?"
                       " AND reveal_until > ?", (srv["id"], name.lower(), now)).fetchone()
    if row is None or not conn.execute(
            "SELECT 1 FROM known_places WHERE server_id = ? AND lower(player_name) = ? AND place = ?",
            (srv["id"], name.lower(), place)).fetchone():
        return None
    return row["token"]


def delivered(conn: sqlite3.Connection, server_id: str, name: str) -> None:
    """The player joined with their personal address: the server's address stops showing it."""
    with immediate(conn):
        conn.execute("UPDATE player_access SET reveal_until = NULL WHERE server_id = ? AND lower(player_name) = ?"
                     " AND reveal_until IS NOT NULL", (server_id, name.lower()))


def refuse(conn: sqlite3.Connection, srv: sqlite3.Row, name: str, actor_user_id: str, now: float,
           place: str | None = None) -> bool:
    with immediate(conn):
        row = _find(conn, srv, name, place)
        if row is None:
            return False
        conn.execute("UPDATE access_requests SET status = 'refused' WHERE server_id = ? AND player_name = ?"
                     " AND place = ?", (srv["id"], row["player_name"], row["place"]))
        new_place = row["kind"] == "new_place"
        events.record(conn, group_id=srv["group_id"], server_id=srv["id"],
                      type="new_place_refused" if new_place else "access_refused", now=now,
                      actor_user_id=actor_user_id, player=row["player_name"],
                      **({"place": row["place"]} if new_place else {}))
    return True
