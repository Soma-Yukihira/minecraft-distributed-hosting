import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from mdh_server.app import create_app
from mdh_server.database.db import connect, migrate
from mdh_harness import FakeClock, Harness, User, make_settings



@dataclass
class Seed:
    path: Path
    user_id: str
    group_id: str
    server_id: str
    host_ids: list[str]


def new_id() -> str:
    return uuid.uuid4().hex


def seed_database(conn: sqlite3.Connection, path: Path, n_hosts: int = 3) -> Seed:
    user, group, server = new_id(), new_id(), new_id()
    hosts = [new_id() for _ in range(n_hosts)]
    conn.execute("INSERT INTO users VALUES (?, 'oscar', 'x', 0, 0)", (user,))
    conn.execute("INSERT INTO groups VALUES (?, 'amis', ?, 0, 1000000)", (group, user))
    conn.execute(
        "INSERT INTO servers (id, group_id, name, mc_version, loader, memory_mb, created_at)"
        " VALUES (?, ?, 'survie', '1.21.4', 'paper', 6144, 0)",
        (server, group),
    )
    for h in hosts:
        conn.execute(
            "INSERT INTO hosts (id, user_id, display_name, os, cpu_cores, total_ram_mb, created_at)"
            " VALUES (?, ?, 'pc', 'windows', 8, 32768, 0)",
            (h, user),
        )
    conn.execute("INSERT INTO leases (server_id) VALUES (?)", (server,))
    return Seed(path, user, group, server, hosts)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Seed]]:
    path = tmp_path / "mdh.sqlite3"
    conn = connect(path)
    migrate(conn)
    seed = seed_database(conn, path)
    yield conn, seed
    conn.close()


@pytest.fixture
def harness_factory(tmp_path: Path):
    started: list[tuple[TestClient, Harness]] = []

    def factory(**overrides) -> Harness:
        clock = FakeClock()
        app = create_app(make_settings(tmp_path, **overrides), clock=clock,
                         password_hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
        client = TestClient(app).__enter__()
        h = Harness(client, clock)
        started.append((client, h))
        return h

    yield factory
    for client, h in started:
        for c in h.conns:
            c.close()
        client.__exit__(None, None, None)


@pytest.fixture
def h(harness_factory) -> Harness:
    return harness_factory()


@dataclass
class World:
    """Oscar (owner, priority 100 PC) and Léa (member, priority 70 PC) share a server."""

    h: Harness
    oscar: User
    lea: User
    group_id: str
    server_id: str
    pc_oscar: str
    pc_lea: str


@pytest.fixture
def world(h: Harness) -> World:
    oscar = h.register("oscar")
    group_id = h.create_group(oscar)
    lea = h.register("lea", invite=h.invite(oscar, group_id))
    pc_oscar = h.register_host(oscar, "PC Oscar")
    pc_lea = h.register_host(lea, "PC Lea")
    server_id = h.create_server(oscar, group_id)
    h.set_priority(oscar, server_id, pc_oscar, 100)
    h.set_priority(oscar, server_id, pc_lea, 70)
    return World(h, oscar, lea, group_id, server_id, pc_oscar, pc_lea)
