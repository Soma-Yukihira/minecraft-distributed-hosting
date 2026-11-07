"""``Idempotency-Key`` support for state-changing requests.

A client that retries a request (timeout, flaky Wi-Fi) with the same key gets the original
response instead of executing the action twice. Reusing a key for a different request is
refused. Keys are scoped per user and kept 24 hours.
"""

from __future__ import annotations

import hashlib
import json
import re

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mdh_server.auth.tokens import authenticate

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
RETENTION_S = 24 * 3600
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _send_json(send: Send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _MUTATING:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        key = headers.get("idempotency-key")
        if key is None:
            await self.app(scope, receive, send)
            return
        if not KEY_PATTERN.fullmatch(key):
            await _send_json(send, 400, {"error": {"code": "invalid_idempotency_key",
                                                   "message": "8-64 characters [A-Za-z0-9_-]"}})
            return
        ctx = scope["app"].state.ctx
        scheme, _, token = headers.get("authorization", "").partition(" ")
        user = authenticate(ctx.conn, token.strip(), ctx.now()) if scheme.lower() == "bearer" and token else None
        if user is None:
            await self.app(scope, receive, send)  # the route itself answers 401
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        request_hash = hashlib.sha256(
            b"\n".join([scope["method"].encode(), scope["path"].encode(), scope.get("query_string", b""), body])
        ).hexdigest()

        row = ctx.conn.execute(
            "SELECT request_hash, response_json FROM idempotency_keys WHERE user_id = ? AND key = ?",
            (user.user_id, key)).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                await _send_json(send, 422, {"error": {"code": "idempotency_key_reused",
                                                       "message": "this key was used for another request"}})
                return
            stored = json.loads(row["response_json"])
            payload = stored["body"].encode("utf-8")
            headers_out = [(b"content-length", str(len(payload)).encode()), (b"idempotent-replay", b"true")]
            if stored.get("content_type"):
                headers_out.append((b"content-type", stored["content_type"].encode()))
            await send({"type": "http.response.start", "status": stored["status"], "headers": headers_out})
            await send({"type": "http.response.body", "body": payload})
            return

        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = 500
        content_type = ""
        chunks: list[bytes] = []

        async def capture_send(message: Message) -> None:
            nonlocal status, content_type
            if message["type"] == "http.response.start":
                status = message["status"]
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, replay_receive, capture_send)
        if status < 500:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys (key, user_id, request_hash, response_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, user.user_id, request_hash,
                 json.dumps({"status": status, "content_type": content_type,
                             "body": b"".join(chunks).decode("utf-8", "replace")}),
                 ctx.now()))


def purge(conn, now: float) -> None:
    conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (now - RETENTION_S,))
