import sqlite3
import uuid

import pytest

from mdh_server.database.db import SCHEMA_VERSION, connect, migrate


def new_id() -> str:
    return uuid.uuid4().hex


def insert_version(conn: sqlite3.Connection, server_id: str, pinned: int = 0) -> str:
    vid = new_id()
    conn.execute(
        "INSERT INTO world_versions (id, server_id, epoch, kind, manifest_sha256, total_size,"
        " created_at, pinned) VALUES (?, ?, 1, 'final', ?, 10, 0, ?)",
        (vid, server_id, "0" * 64, pinned),
    )
    return vid


def test_migrate_is_idempotent(db) -> None:
    conn, _ = db
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_v1_database_is_upgraded_in_place_without_data_loss(tmp_path) -> None:
    from importlib import resources

    from mdh_server.database.db import _split_statements

    path = tmp_path / "old.sqlite3"
    conn = connect(path)
    for statement in _split_statements(
            resources.files("mdh_server.database").joinpath("schema.sql").read_text("utf-8")):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    user, group, server = new_id(), new_id(), new_id()
    conn.execute("INSERT INTO users VALUES (?, 'oscar', 'x', 0, 0)", (user,))
    conn.execute("INSERT INTO groups VALUES (?, 'dev', ?, 0, 1000)", (group, user))
    conn.execute("INSERT INTO servers (id, group_id, name, mc_version, loader, memory_mb, created_at)"
                 " VALUES (?, ?, 'Survie', '26.3', 'fabric', 3072, 0)", (server, group))
    conn.close()

    conn = connect(path)
    migrate(conn)
    row = conn.execute("SELECT name, memory_mb, allow_offline_accounts, allowed_players_json FROM servers").fetchone()
    assert tuple(row) == ("Survie", 3072, 0, "[]")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_refuses_newer_schema(tmp_path) -> None:
    conn = connect(tmp_path / "x.sqlite3")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        migrate(conn)


def test_durability_pragmas(db) -> None:
    conn, _ = db
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_head_and_pinned_versions_cannot_be_deleted(db) -> None:
    conn, seed = db
    head = insert_version(conn, seed.server_id)
    pinned = insert_version(conn, seed.server_id, pinned=1)
    old = insert_version(conn, seed.server_id)
    conn.execute("UPDATE servers SET head_version_id = ? WHERE id = ?", (head, seed.server_id))

    for vid in (head, pinned):
        with pytest.raises(sqlite3.IntegrityError, match="cannot delete"):
            conn.execute("DELETE FROM world_versions WHERE id = ?", (vid,))
    conn.execute("DELETE FROM world_versions WHERE id = ?", (old,))


def test_versions_are_immutable(db) -> None:
    conn, seed = db
    vid = insert_version(conn, seed.server_id)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE world_versions SET manifest_sha256 = 'x' WHERE id = ?", (vid,))
    conn.execute("UPDATE world_versions SET pinned = 1, label = 'avant le dragon' WHERE id = ?", (vid,))


def test_lease_holder_and_expiry_go_together(db) -> None:
    conn, seed = db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE leases SET holder_host_id = ?", (seed.host_ids[0],))


def test_usernames_unique_case_insensitive(db) -> None:
    conn, _ = db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users VALUES (?, 'OSCAR', 'x', 0, 0)", (new_id(),))
