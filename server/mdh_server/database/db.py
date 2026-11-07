"""SQLite connection and schema management.

One control-plane process owns the database. Durability settings are deliberately strict:
a lease grant that is acknowledged to a host must survive a power loss of the VPS, otherwise
the lease could be handed out twice after a reboot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

#: ``schema.sql`` is the v1 baseline; each later version is a list of statements applied in order.
#: Existing databases are never recreated: they are upgraded in place, one version per transaction.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE servers ADD COLUMN allow_offline_accounts INTEGER NOT NULL DEFAULT 0"
        " CHECK (allow_offline_accounts IN (0, 1))",
        "ALTER TABLE servers ADD COLUMN allowed_players_json TEXT NOT NULL DEFAULT '[]'",
    ],
    3: [
        # Retention deletes old versions: their children may lose their parent pointer (set to
        # NULL), but every other column stays immutable.
        "DROP TRIGGER world_versions_immutable",
        "CREATE TRIGGER world_versions_immutable"
        " BEFORE UPDATE OF server_id, epoch, host_id, kind, manifest_sha256, total_size, created_at"
        " ON world_versions BEGIN SELECT RAISE(ABORT, 'world versions are immutable'); END",
        "CREATE TRIGGER world_versions_parent_only_cleared"
        " BEFORE UPDATE OF parent_id ON world_versions WHEN NEW.parent_id IS NOT NULL"
        " BEGIN SELECT RAISE(ABORT, 'world versions are immutable'); END",
        "CREATE INDEX world_versions_parent ON world_versions (parent_id)",
    ],
    4: [
        # Planned handovers (docs/ARCHITECTURE.md 3.5): one row per attempt, kept as a journal.
        "CREATE TABLE migrations ("
        " id TEXT PRIMARY KEY,"
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " source_host_id TEXT NOT NULL,"
        " target_host_id TEXT NOT NULL,"
        " reason TEXT NOT NULL CHECK (reason IN ('transfer', 'preemption')),"
        " status TEXT NOT NULL CHECK (status IN"
        "  ('prefetching', 'stopping', 'handover', 'completed', 'cancelled', 'failed')),"
        " countdown_s INTEGER NOT NULL CHECK (countdown_s >= 0),"
        # Lease epoch the target got: a voluntary transfer is not undone by preemption while
        # that epoch lasts.
        " target_epoch INTEGER,"
        " actor_user_id TEXT,"
        " detail TEXT,"
        " created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL)",
        # At most one migration in progress per server, enforced by the database itself.
        "CREATE UNIQUE INDEX migrations_active ON migrations (server_id)"
        " WHERE status IN ('prefetching', 'stopping', 'handover')",
        "ALTER TABLE servers ADD COLUMN preemption_countdown_s INTEGER NOT NULL DEFAULT 300"
        " CHECK (preemption_countdown_s BETWEEN 30 AND 3600)",
    ],
    5: [
        # Relay addresses (docs/ARCHITECTURE.md 4.2). Labels of existing servers are filled in
        # from their names at start-up (relay.addresses.assign_missing_labels).
        "ALTER TABLE servers ADD COLUMN address_label TEXT",
        "CREATE UNIQUE INDEX servers_address_label ON servers (address_label)",
        # Personal addresses: the relay lets only that player in through it.
        "CREATE TABLE player_access ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " player_name TEXT NOT NULL COLLATE NOCASE,"
        " token TEXT NOT NULL UNIQUE,"
        " created_at REAL NOT NULL,"
        " PRIMARY KEY (server_id, player_name))",
    ],
    6: [
        # Wake-up by the first player and sleep once empty (docs/ARCHITECTURE.md 4.7).
        "ALTER TABLE servers ADD COLUMN start_on_join INTEGER NOT NULL DEFAULT 0"
        " CHECK (start_on_join IN (0, 1))",
        "ALTER TABLE servers ADD COLUMN stop_when_empty_after_s INTEGER"
        " CHECK (stop_when_empty_after_s IS NULL OR stop_when_empty_after_s > 0)",  # API: >= 5 min
        # Status page in a browser, served by the relay (docs/ARCHITECTURE.md 4.9).
        "ALTER TABLE servers ADD COLUMN public_status INTEGER NOT NULL DEFAULT 0"
        " CHECK (public_status IN (0, 1))",
    ],
    7: [
        # Mods and plugins from Modrinth (docs/ARCHITECTURE.md 3.9), world map (4.11).
        "ALTER TABLE servers ADD COLUMN mods_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE servers ADD COLUMN map_enabled INTEGER NOT NULL DEFAULT 0"
        " CHECK (map_enabled IN (0, 1))",
        # Moderators: the CHECK of a column cannot be changed in place, so the table is rebuilt
        # (nothing references it; every row is copied as it is).
        "CREATE TABLE memberships_v7 ("
        " group_id TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,"
        " user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,"
        " role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'moderator', 'member')),"
        " can_host INTEGER NOT NULL DEFAULT 1 CHECK (can_host IN (0, 1)),"
        " joined_at REAL NOT NULL,"
        " PRIMARY KEY (group_id, user_id))",
        "INSERT INTO memberships_v7 (group_id, user_id, role, can_host, joined_at)"
        " SELECT group_id, user_id, role, can_host, joined_at FROM memberships",
        "DROP TABLE memberships",
        "ALTER TABLE memberships_v7 RENAME TO memberships",
        # Players the whitelist refused, who asked to join (docs/ARCHITECTURE.md 4.12).
        "CREATE TABLE access_requests ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " player_name TEXT NOT NULL COLLATE NOCASE,"
        " status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'refused')),"
        " first_at REAL NOT NULL,"
        " last_at REAL NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),"
        " PRIMARY KEY (server_id, player_name))",
        # Statistics (docs/ARCHITECTURE.md 4.13): seconds per UTC day, written every few minutes.
        "CREATE TABLE play_stats ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " player_name TEXT NOT NULL COLLATE NOCASE,"
        " day TEXT NOT NULL,"
        " seconds REAL NOT NULL DEFAULT 0 CHECK (seconds >= 0),"
        " last_at REAL NOT NULL,"
        " PRIMARY KEY (server_id, player_name, day))",
        "CREATE TABLE host_stats ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " host_id TEXT NOT NULL REFERENCES hosts (id) ON DELETE CASCADE,"
        " day TEXT NOT NULL,"
        " seconds REAL NOT NULL DEFAULT 0 CHECK (seconds >= 0),"
        " PRIMARY KEY (server_id, host_id, day))",
        "CREATE TABLE hour_stats ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " day TEXT NOT NULL,"
        " hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),"
        " online_seconds REAL NOT NULL DEFAULT 0 CHECK (online_seconds >= 0),"
        " player_seconds REAL NOT NULL DEFAULT 0 CHECK (player_seconds >= 0),"
        " PRIMARY KEY (server_id, day, hour))",
    ],
    8: [
        # 0.4.2: unofficial accounts may join through the server's address (name checked only).
        "ALTER TABLE servers ADD COLUMN personal_addresses INTEGER NOT NULL DEFAULT 1"
        " CHECK (personal_addresses IN (0, 1))",
    ],
    9: [
        # 0.5.0: where each player usually connects from (unofficial accounts through the
        # server's address), and requests from a new place next to the requests to join.
        "CREATE TABLE known_places ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " player_name TEXT NOT NULL COLLATE NOCASE,"
        " place TEXT NOT NULL,"
        " first_at REAL NOT NULL,"
        " last_at REAL NOT NULL,"
        " PRIMARY KEY (server_id, player_name, place))",
        "CREATE TABLE access_requests_v9 ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " player_name TEXT NOT NULL COLLATE NOCASE,"
        " place TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT 'join' CHECK (kind IN ('join', 'new_place')),"
        " status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'refused')),"
        " first_at REAL NOT NULL,"
        " last_at REAL NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),"
        " PRIMARY KEY (server_id, player_name, place))",
        "INSERT INTO access_requests_v9 (server_id, player_name, status, first_at, last_at, attempts)"
        " SELECT server_id, player_name, status, first_at, last_at, attempts FROM access_requests",
        "DROP TABLE access_requests",
        "ALTER TABLE access_requests_v9 RENAME TO access_requests",
        # The server's resource pack (docs/ARCHITECTURE.md 3.10): NULL = none.
        "ALTER TABLE servers ADD COLUMN resource_pack_json TEXT",
    ],
    10: [
        # 0.6.0: the Modrinth modpack the server was made from (ARCHITECTURE 3.11): NULL = none.
        "ALTER TABLE servers ADD COLUMN modpack_json TEXT",
    ],
    11: [
        # 0.7.0: datapacks (ARCHITECTURE 3.12), settings files (3.13), performance graphs (4.17).
        "ALTER TABLE servers ADD COLUMN datapacks_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE servers ADD COLUMN config_files_json TEXT NOT NULL DEFAULT '{}'",
        "CREATE TABLE perf_minutes ("
        " server_id TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,"
        " minute INTEGER NOT NULL,"
        " samples INTEGER NOT NULL,"
        " cpu_sum REAL NOT NULL,"
        " ram_max INTEGER NOT NULL,"
        " tps_sum REAL NOT NULL,"
        " tps_n INTEGER NOT NULL,"
        " tps_min REAL,"
        " mspt_sum REAL NOT NULL,"
        " mspt_n INTEGER NOT NULL,"
        " mspt_max REAL,"
        " players_max INTEGER NOT NULL,"
        " PRIMARY KEY (server_id, minute))",
    ],
    12: [
        # 0.8.0: the hosting PC by priority or the best one (ARCHITECTURE 3.15).
        "ALTER TABLE servers ADD COLUMN host_choice TEXT NOT NULL DEFAULT 'priority'"
        " CHECK (host_choice IN ('priority', 'best'))",
    ],
    13: [
        # 0.8.1: a player accepted from the access requests is shown their personal address when
        # reconnecting through the server's address, from where they asked (ARCHITECTURE 4.12).
        # NULL = not shown (added by hand, or already used).
        "ALTER TABLE player_access ADD COLUMN reveal_until REAL",
    ],
}
SCHEMA_VERSION = max(MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    # FULL: an acknowledged commit (e.g. a lease grant) is on disk before we answer.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this control plane (v{SCHEMA_VERSION})"
        )
    # executescript() would COMMIT implicitly; running statements one by one keeps each step
    # atomic: a failed upgrade leaves the database at the previous version, untouched.
    if current == 0:
        schema = resources.files("mdh_server.database").joinpath("schema.sql").read_text("utf-8")
        with immediate(conn):
            for statement in _split_statements(schema):
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
        current = 1
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with immediate(conn):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialisable write transaction (takes the write lock up front)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (handles trigger bodies)."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not buffer.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete SQL statement at end of schema")
    return statements
