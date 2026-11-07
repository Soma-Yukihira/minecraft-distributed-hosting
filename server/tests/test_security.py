"""Hardening found by the Phase 7 security review (docs/SECURITY.md)."""

import json

import pytest
from starlette.websockets import WebSocketDisconnect


def test_oversized_bodies_are_refused_before_being_read(h) -> None:
    user = h.register("oscar")
    big = b'{"name": "' + b"x" * (17 * 1024 * 1024) + b'"}'
    r = h.client.post("/api/v1/groups", content=big, headers={**user.headers, "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    def chunks():                       # no Content-Length: counted while streaming
        yield b'{"name": "'
        for _ in range(17):
            yield b"x" * (1024 * 1024)
        yield b'"}'
    r = h.client.post("/api/v1/groups", content=chunks(), headers={**user.headers, "Content-Type": "application/json"})
    assert r.status_code in (400, 413)   # reading stops at the cap (FastAPI reports it as a bad body)
    assert h.post(user, "/groups", {"name": "normal"}).status_code == 201   # normal traffic unaffected


def test_relay_data_socket_for_an_unknown_connection_is_refused_at_once(h) -> None:
    with pytest.raises(WebSocketDisconnect):
        with h.client.websocket_connect("/api/v1/relay/data/" + "0" * 32) as ws:
            ws.send_text(json.dumps({"relay_token": "x" * 32}))
            ws.receive_text()


def test_websocket_opening_flood_is_throttled(h) -> None:
    refused = 0
    for _ in range(40):
        try:
            with h.client.websocket_connect("/api/v1/ws") as ws:
                ws.send_text(json.dumps({"type": "hello", "access_token": "x" * 40, "client_version": "t"}))
                ws.receive_text()
        except WebSocketDisconnect as exc:
            refused += exc.code == 1008
    assert refused == 40          # bad token anyway; the point is that the server stayed responsive
    assert h.client.get("/api/v1/health").status_code == 200
    assert not h.ctx.limiter.hit("ws:testclient", per_minute=60, burst=20)   # bucket exhausted
