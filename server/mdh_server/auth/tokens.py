"""Opaque session tokens.

* Tokens are 256-bit random strings; only their SHA-256 is stored, so a database leak does not
  leak usable sessions.
* Access tokens live 15 minutes. Refresh tokens live 30 days and are **rotated** on every use.
  Presenting an already-rotated refresh token means it was stolen (or replayed): the whole
  token family is revoked and the user must log in again.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass

from mdh_server.database.db import immediate


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str


class TokenError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def issue(
    conn: sqlite3.Connection,
    user_id: str,
    now: float,
    *,
    access_ttl_s: int,
    refresh_ttl_s: int,
    family_id: str | None = None,
    replaces: str | None = None,
) -> IssuedTokens:
    """Create an access/refresh pair. Must run inside a transaction."""
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    refresh_id = new_id()
    conn.execute(
        "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (refresh_id, user_id, family_id or new_id(), token_hash(refresh), now, now + refresh_ttl_s),
    )
    if replaces is not None:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ?, replaced_by = ? WHERE id = ?",
            (now, refresh_id, replaces),
        )
    conn.execute(
        "INSERT INTO access_tokens (token_hash, user_id, refresh_id, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash(access), user_id, refresh_id, now + access_ttl_s),
    )
    return IssuedTokens(access, refresh, access_ttl_s)


def login(conn: sqlite3.Connection, user_id: str, now: float, *, access_ttl_s: int,
          refresh_ttl_s: int) -> IssuedTokens:
    with immediate(conn):
        return issue(conn, user_id, now, access_ttl_s=access_ttl_s, refresh_ttl_s=refresh_ttl_s)


def authenticate(conn: sqlite3.Connection, access_token: str, now: float) -> AuthenticatedUser | None:
    row = conn.execute(
        "SELECT u.id, u.username FROM access_tokens a JOIN users u ON u.id = a.user_id"
        " WHERE a.token_hash = ? AND a.expires_at > ? AND u.disabled = 0",
        (token_hash(access_token), now),
    ).fetchone()
    return AuthenticatedUser(row["id"], row["username"]) if row else None


def rotate(conn: sqlite3.Connection, refresh_token: str, now: float, *, access_ttl_s: int,
           refresh_ttl_s: int) -> IssuedTokens:
    reused = False
    with immediate(conn):
        row = conn.execute(
            "SELECT r.*, u.disabled FROM refresh_tokens r JOIN users u ON u.id = r.user_id"
            " WHERE r.token_hash = ?",
            (token_hash(refresh_token),),
        ).fetchone()
        if row is None:
            raise TokenError("invalid_token", "unknown refresh token")
        if row["revoked_at"] is not None:
            # Revoke, then let the transaction COMMIT before reporting the error: raising inside
            # the block would roll the revocation back.
            _revoke_family(conn, row["family_id"], now)
            reused = True
        elif row["expires_at"] <= now or row["disabled"]:
            raise TokenError("invalid_token", "refresh token expired")
        else:
            return issue(
                conn, row["user_id"], now, access_ttl_s=access_ttl_s, refresh_ttl_s=refresh_ttl_s,
                family_id=row["family_id"], replaces=row["id"],
            )
    assert reused
    raise TokenError("token_reused", "refresh token reuse detected: all sessions of this login revoked")


def logout(conn: sqlite3.Connection, refresh_token: str, now: float) -> None:
    with immediate(conn):
        row = conn.execute(
            "SELECT family_id FROM refresh_tokens WHERE token_hash = ?", (token_hash(refresh_token),)
        ).fetchone()
        if row is not None:
            _revoke_family(conn, row["family_id"], now)


def revoke_user(conn: sqlite3.Connection, user_id: str, now: float, *, keep_family: str | None = None) -> None:
    """Log the user out everywhere (but in ``keep_family``). Must run inside a transaction."""
    conn.execute(
        "UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?"
        " AND family_id IS NOT ?", (now, user_id, keep_family))
    conn.execute(
        "DELETE FROM access_tokens WHERE user_id = ? AND refresh_id NOT IN"
        " (SELECT id FROM refresh_tokens WHERE family_id IS ?)", (user_id, keep_family))


def family_of(conn: sqlite3.Connection, access_token: str) -> str | None:
    row = conn.execute(
        "SELECT r.family_id FROM access_tokens a JOIN refresh_tokens r ON r.id = a.refresh_id"
        " WHERE a.token_hash = ?", (token_hash(access_token),)).fetchone()
    return row["family_id"] if row else None


def _revoke_family(conn: sqlite3.Connection, family_id: str, now: float) -> None:
    conn.execute(
        "UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE family_id = ?",
        (now, family_id),
    )
    conn.execute(
        "DELETE FROM access_tokens WHERE refresh_id IN"
        " (SELECT id FROM refresh_tokens WHERE family_id = ?)",
        (family_id,),
    )


def purge_expired(conn: sqlite3.Connection, now: float) -> None:
    with immediate(conn):
        conn.execute("DELETE FROM access_tokens WHERE expires_at <= ?", (now,))
        # Keep revoked refresh tokens until they expire: they are needed for reuse detection.
        conn.execute("DELETE FROM refresh_tokens WHERE expires_at <= ?", (now,))
