"""Registry of live WebSocket sessions and fan-out of messages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field

from fastapi import WebSocket

from mdh_shared.protocol.messages import encode

log = logging.getLogger("mdh.realtime")

#: Longest wait for one message to leave: a healthy client takes milliseconds.
SEND_TIMEOUT_S = 5.0


@dataclass(eq=False)
class Session:
    session_id: str
    user_id: str
    host_id: str | None
    websocket: WebSocket
    closed: bool = field(default=False)

    async def send(self, message) -> bool:
        if self.closed:
            return False
        try:
            # A client that stops reading fills its buffers, then send_text waits forever: it
            # would hold up whoever sends (the sweeper too, so every lease of the VPS).
            await asyncio.wait_for(self.websocket.send_text(encode(message)), SEND_TIMEOUT_S)
            return True
        except Exception:  # noqa: BLE001 - a dead socket must never break the caller
            log.info("send failed on session %s, closing it", self.session_id)
            self.closed = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.websocket.close(code=1011), 1.0)
            return False


class Hub:
    def __init__(self) -> None:
        self._hosts: dict[str, Session] = {}
        self._users: dict[str, set[Session]] = {}

    def register(self, session: Session) -> Session | None:
        """Add a session. Returns the previous session of the same host, which must be closed.

        A host reconnecting after a network blip replaces its half-open old session.
        """
        self._users.setdefault(session.user_id, set()).add(session)
        previous = None
        if session.host_id is not None:
            previous = self._hosts.get(session.host_id)
            self._hosts[session.host_id] = session
        return previous

    def unregister(self, session: Session) -> bool:
        """Remove a session. Returns True if it was the current session of its host."""
        self._users.get(session.user_id, set()).discard(session)
        if not self._users.get(session.user_id):
            self._users.pop(session.user_id, None)
        if session.host_id is not None and self._hosts.get(session.host_id) is session:
            del self._hosts[session.host_id]
            return True
        return False

    def host_session(self, host_id: str) -> Session | None:
        return self._hosts.get(host_id)

    def is_online(self, host_id: str) -> bool:
        s = self._hosts.get(host_id)
        return s is not None and not s.closed

    async def send_to_host(self, host_id: str, message) -> bool:
        session = self._hosts.get(host_id)
        return await session.send(message) if session else False

    async def send_to_user(self, user_id: str, message) -> None:
        await _send_all(list(self._users.get(user_id, ())), message)

    async def broadcast(self, message) -> int:
        """To every connected application (whoever is logged in); returns how many got it."""
        return await _send_all([s for sessions in self._users.values() for s in sessions], message)

    async def group_changed(self, conn: sqlite3.Connection, group_id: str, *also: str) -> None:
        """Members, roles or servers changed: every member's application reads the group again,
        and so do ``also`` (a member who just left, and is no longer in the list)."""
        from mdh_shared.protocol.messages import GroupUpdate

        message = GroupUpdate(group_id=group_id)
        await self.send_to_group(conn, group_id, message)
        for user_id in also:
            await self.send_to_user(user_id, message)

    async def send_to_group(self, conn: sqlite3.Connection, group_id: str, message) -> None:
        members = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM memberships WHERE group_id = ?", (group_id,))]
        await _send_all([s for user_id in members for s in self._users.get(user_id, ())], message)


async def _send_all(sessions: list[Session], message) -> int:
    """In parallel: one slow application delays nobody else (each send has its own timeout)."""
    if not sessions:
        return 0
    return sum(await asyncio.gather(*(s.send(message) for s in sessions)))
