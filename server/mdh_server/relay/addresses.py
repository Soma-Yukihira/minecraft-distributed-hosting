"""Which address leads to which server, and personal addresses of players.

* Every server has a stable ``address_label``: ``<label>.<relay domain>`` (wildcard DNS). When
  the control plane has a single server, the bare relay domain (or the VPS IP) works too.
* Servers accepting unofficial accounts (no Microsoft verification) can only be joined through a
  **personal address** ``<token>.<relay domain>``: the relay lets in only the player the address
  belongs to. This replaces a password mod: nothing to install on the server, works for Paper,
  Fabric and Vanilla. The token travels in clear like the rest of an offline-mode connection.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

from mdh_server.config import Settings

TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
TOKEN_LENGTH = 12  # 60 bits
TOKEN_SHAPE = re.compile(rf"^[{TOKEN_ALPHABET}]{{{TOKEN_LENGTH}}}$")
LABEL_MAX = 30


@dataclass(frozen=True)
class Route:
    server_id: str | None
    #: Set when a personal address was used: only this player may log in through it.
    player: str | None = None


def new_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:LABEL_MAX].strip("-")
    return slug or "serveur"


def unique_label(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    candidate, n = base, 1
    # Never shaped like a personal token, never already used.
    while TOKEN_SHAPE.match(candidate) or conn.execute(
            "SELECT 1 FROM servers WHERE address_label = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base[:LABEL_MAX - 4]}-{n}"
    return candidate


def assign_missing(conn: sqlite3.Connection) -> None:
    """Servers created before addresses existed (schema < 5) get a label from their name and a
    personal address for each allowed player. Idempotent: run at every start-up."""
    for row in conn.execute("SELECT id, name, address_label, allowed_players_json FROM servers").fetchall():
        if row["address_label"] is None:
            conn.execute("UPDATE servers SET address_label = ? WHERE id = ?",
                         (unique_label(conn, row["name"]), row["id"]))
        sync_player_tokens(conn, row["id"], json.loads(row["allowed_players_json"]))


def sync_player_tokens(conn: sqlite3.Connection, server_id: str, players: list[str]) -> None:
    """One personal address per allowed player; existing ones are kept (players keep them)."""
    wanted = {p.lower(): p for p in players}
    existing = {r["player_name"].lower() for r in conn.execute(
        "SELECT player_name FROM player_access WHERE server_id = ?", (server_id,))}
    for key in existing - wanted.keys():
        conn.execute("DELETE FROM player_access WHERE server_id = ? AND player_name = ?", (server_id, key))
    for key in wanted.keys() - existing:
        conn.execute("INSERT INTO player_access (server_id, player_name, token, created_at) VALUES (?, ?, ?, ?)",
                     (server_id, wanted[key], new_token(), time.time()))


def regenerate_token(conn: sqlite3.Connection, server_id: str, player: str) -> str | None:
    token = new_token()
    cur = conn.execute("UPDATE player_access SET token = ?, created_at = ? WHERE server_id = ? AND player_name = ?",
                       (token, time.time(), server_id, player))
    return token if cur.rowcount else None


def _suffix(port: int) -> str:
    return "" if port == 25565 else f":{port}"


def single_server(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute("SELECT id FROM servers LIMIT 2").fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def _server_host(settings: Settings, conn: sqlite3.Connection, srv: sqlite3.Row) -> str:
    return settings.relay_domain if single_server(conn) == srv["id"] else f"{srv['address_label']}.{settings.relay_domain}"


def server_address(settings: Settings, conn: sqlite3.Connection, srv: sqlite3.Row, port: int) -> str:
    return _server_host(settings, conn, srv) + _suffix(port)


def status_page_url(settings: Settings, conn: sqlite3.Connection, srv: sqlite3.Row, port: int) -> str:
    """The server's address opened in a browser: same host, the relay port always written."""
    return f"http://{_server_host(settings, conn, srv)}:{port}/"


def player_address(settings: Settings, token: str, port: int) -> str:
    return f"{token}.{settings.relay_domain}{_suffix(port)}"


def resolve(conn: sqlite3.Connection, settings: Settings, hostname: str) -> Route:
    """Map the address typed by a player to a server (and possibly a player)."""
    domain = settings.relay_domain.lower()
    if hostname.endswith("." + domain):
        label = hostname[: -len(domain) - 1]
        if "." in label:
            return Route(None)
        row = conn.execute("SELECT server_id, player_name FROM player_access WHERE token = ?", (label,)).fetchone()
        if row is not None:
            return Route(row["server_id"], row["player_name"])
        row = conn.execute("SELECT id FROM servers WHERE address_label = ?", (label,)).fetchone()
        return Route(row["id"] if row else None)
    # Bare domain, VPS IP, "localhost"...: unambiguous only with a single server.
    return Route(single_server(conn))
