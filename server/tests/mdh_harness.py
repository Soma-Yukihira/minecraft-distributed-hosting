"""Test harness: real app, controlled clock, simulated host PCs over HTTP + WebSocket."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from fastapi.testclient import TestClient

from mdh_server.config import Settings
from mdh_shared import __version__

PASSWORD = "correct horse battery"
DEFAULT_AVAILABILITY = {"hosting_allowed": True, "free_ram_mb": 20000, "free_disk_mb": 200_000,
                        "relay_ok": True}

# --------------------------------------------------------------------------- API harness


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class Closed(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"websocket closed with code {code}")
        self.code = code


@dataclass
class User:
    user_id: str
    username: str
    access_token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def _receive_with_timeout(stream, timeout: float):
    with anyio.fail_after(timeout):
        return await stream.receive()


class WsConn:
    """A test WebSocket session with timeouts (a missing message fails instead of hanging)."""

    def __init__(self, harness: "Harness", user: User, host_id: str | None) -> None:
        self.h = harness
        self.user = user
        self.host_id = host_id
        self.seq = 0
        self.ws = harness.client.websocket_connect("/api/v1/ws").__enter__()
        self.closed_code: int | None = None
        self.backlog: list[dict] = []

    def send(self, payload: dict) -> None:
        self.ws.send_text(json.dumps(payload))

    def recv(self, timeout: float = 2.0) -> dict:
        if self.backlog:
            return self.backlog.pop(0)
        message = self.ws.portal.call(_receive_with_timeout, self.ws._send_rx, timeout)
        if message["type"] == "websocket.close":
            self.closed_code = message.get("code", 1000)
            raise Closed(self.closed_code)
        return json.loads(message["text"])

    def expect(self, type_: str, timeout: float = 2.0) -> dict:
        skipped = []
        try:
            while True:
                msg = self.recv(timeout)
                if msg["type"] == type_:
                    return msg
                skipped.append(msg)
        finally:
            self.backlog = skipped + self.backlog

    def expect_none(self, type_: str, timeout: float = 0.3) -> None:
        try:
            msg = self.expect(type_, timeout)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected {type_}: {msg}")

    def expect_closed(self, timeout: float = 2.0) -> int:
        try:
            while True:
                self.recv(timeout)
        except Closed as exc:
            return exc.code

    def close(self) -> None:
        try:
            self.ws.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - already closed by the server
            pass


class HostConn(WsConn):
    def __init__(self, harness: "Harness", user: User, host_id: str,
                 availability: dict[str, Any] | None) -> None:
        super().__init__(harness, user, host_id)
        self.send({"type": "hello", "access_token": user.access_token, "host_id": host_id,
                   "client_version": __version__})
        self.expect("welcome")
        self.leases: dict[str, int] = {}
        if availability is not None:
            self.heartbeat(availability=availability)

    def heartbeat(self, leases: dict[str, int] | None = None, availability: dict | None = None,
                  metrics: dict | None = None) -> dict:
        self.seq += 1
        payload: dict[str, Any] = {
            "type": "heartbeat", "seq": self.seq,
            "leases": [{"server_id": s, "epoch": e}
                       for s, e in (self.leases if leases is None else leases).items()],
        }
        if availability is not None:
            payload["availability"] = availability
        if metrics is not None:
            payload["metrics"] = metrics
        self.send(payload)
        return self.expect("heartbeat_ack")

    def acquire(self, server_id: str, offer_id: str):
        r = self.h.client.post(f"/api/v1/servers/{server_id}/lease/acquire", headers=self.user.headers,
                               json={"host_id": self.host_id, "offer_id": offer_id})
        if r.status_code == 200:
            self.leases[server_id] = r.json()["epoch"]
        return r

    def accept_next_offer(self) -> int:
        offer = self.expect("hosting_offer")
        r = self.acquire(offer["server_id"], offer["offer_id"])
        assert r.status_code == 200, r.text
        return r.json()["epoch"]

    def report(self, server_id: str, state: str):
        return self.h.client.post(f"/api/v1/servers/{server_id}/host-state", headers=self.user.headers,
                                  json={"host_id": self.host_id, "epoch": self.leases[server_id],
                                        "state": state})

    def release(self, server_id: str):
        epoch = self.leases.pop(server_id)
        return self.h.client.post(f"/api/v1/servers/{server_id}/lease/release", headers=self.user.headers,
                                  json={"host_id": self.host_id, "epoch": epoch})


@dataclass
class Harness:
    client: TestClient
    clock: FakeClock
    conns: list[WsConn] = field(default_factory=list)

    @property
    def ctx(self):
        return self.client.app.state.ctx

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds

    def tick(self) -> None:
        self.client.portal.call(self.ctx.coordinator.tick)

    def register(self, username: str, invite: str | None = None) -> User:
        body = {"username": username, "password": PASSWORD}
        if invite:
            body["invite_code"] = invite
        r = self.client.post("/api/v1/auth/register", json=body)
        assert r.status_code == 201, r.text
        return self.login(username)

    def login(self, username: str) -> User:
        r = self.client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        me = self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
        return User(me.json()["user_id"], username, r.json()["access_token"], r.json()["refresh_token"])

    def post(self, user: User, path: str, json_body: dict | None = None, **kw):
        return self.client.post(f"/api/v1{path}", headers=user.headers, json=json_body, **kw)

    def get(self, user: User, path: str):
        return self.client.get(f"/api/v1{path}", headers=user.headers)

    def create_group(self, user: User, name: str = "Les copains") -> str:
        r = self.post(user, "/groups", {"name": name})
        assert r.status_code == 201, r.text
        return r.json()["group_id"]

    def invite(self, user: User, group_id: str, max_uses: int = 1) -> str:
        r = self.post(user, f"/groups/{group_id}/invites", {"max_uses": max_uses})
        assert r.status_code == 201, r.text
        return r.json()["code"]

    def register_host(self, user: User, name: str = "PC") -> str:
        r = self.post(user, "/hosts", {"display_name": name, "os": "Windows 11", "cpu_cores": 16,
                                       "total_ram_mb": 32768})
        assert r.status_code == 201, r.text
        return r.json()["host_id"]

    def create_server(self, user: User, group_id: str, **kw) -> str:
        body = {"name": "Survie", "mc_version": "1.21.4", "loader": "paper", "memory_mb": 6144, **kw}
        r = self.post(user, f"/groups/{group_id}/servers", body)
        assert r.status_code == 201, r.text
        return r.json()["server_id"]

    def set_priority(self, user: User, server_id: str, host_id: str, priority: int) -> None:
        r = self.client.put(f"/api/v1/servers/{server_id}/hosts/{host_id}/priority",
                            headers=user.headers, json={"priority": priority})
        assert r.status_code == 204, r.text

    def status(self, user: User, server_id: str) -> dict:
        r = self.get(user, f"/servers/{server_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def event_types(self, user: User, server_id: str) -> list[str]:
        return [e["type"] for e in reversed(self.get(user, f"/servers/{server_id}/events").json())]

    def connect_host(self, user: User, host_id: str,
                     availability: dict | None = DEFAULT_AVAILABILITY) -> HostConn:
        conn = HostConn(self, user, host_id, availability)
        self.conns.append(conn)
        return conn

    def connect_viewer(self, user: User) -> WsConn:
        conn = WsConn(self, user, None)
        conn.send({"type": "hello", "access_token": user.access_token, "client_version": __version__})
        conn.expect("welcome")
        self.conns.append(conn)
        return conn


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(database_path=str(tmp_path / "db" / "mdh.sqlite3"), storage_dir=str(tmp_path / "storage"),
                background_tasks=False, relay_enabled=False,
                registration_mode="open",
                # Tests run hours of fake time in a few real seconds: no flood limit by default.
                ws_messages_per_minute=1e9, ws_messages_burst=10**9)
    base.update(overrides)
    return Settings(**base)
