"""Command line: ``python -m mdh_server serve`` / ``create-user`` / ``reset-password``.

``create-user`` bootstraps the first account (registration is invitation-only) and
``reset-password`` gives a new password to someone who forgot theirs. On the VPS, through
``install.sh create-user NAME`` / ``install.sh reset-password NAME``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time

from mdh_server.auth import tokens
from mdh_server.auth.passwords import PasswordService
from mdh_server.config import Settings
from mdh_server.database.db import connect, immediate, migrate
from mdh_shared.models.api import Password, Username
from pydantic import TypeAdapter, ValidationError


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from mdh_server.app import create_app

    host, _, port = args.bind.rpartition(":")
    # One worker only: the lease authority lives in this single process (docs/ARCHITECTURE.md).
    # ws_max_size: no legitimate frame exceeds 64 KiB (control messages, relay data chunks);
    # uvicorn's default (16 MiB) would let a client make it buffer far more before we check.
    uvicorn.run(create_app(Settings.from_env()), host=host or "127.0.0.1", port=int(port),
                workers=1, proxy_headers=False, server_header=False, log_level="info",
                ws_max_size=1024 * 1024)
    return 0


def _ask_password(username: str) -> str | None:
    try:
        TypeAdapter(Username).validate_python(username)
        password = getpass.getpass("Password (10+ characters): ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords differ.", file=sys.stderr)
            return None
        TypeAdapter(Password).validate_python(password)
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return None
    return password


def _reset_password(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    conn = connect(settings.database_path)
    migrate(conn)
    row = conn.execute("SELECT id, username FROM users WHERE lower(username) = lower(?)",
                       (args.username,)).fetchone()
    if row is None:
        print(f"No user named {args.username}.", file=sys.stderr)
        return 1
    password = _ask_password(row["username"])
    if password is None:
        return 1
    password_hash = asyncio.run(PasswordService().hash(password))
    with immediate(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["id"]))
        tokens.revoke_user(conn, row["id"], time.time())
    print(f"New password set for {row['username']}: every PC of this account must log in again.")
    return 0


def _create_user(args: argparse.Namespace) -> int:
    password = _ask_password(args.username)
    if password is None:
        return 1
    username = args.username
    settings = Settings.from_env()
    conn = connect(settings.database_path)
    migrate(conn)
    password_hash = asyncio.run(PasswordService().hash(password))
    try:
        with immediate(conn):
            conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                         (tokens.new_id(), username, password_hash, time.time()))
    except Exception as exc:  # noqa: BLE001
        print(f"Could not create user: {exc}", file=sys.stderr)
        return 1
    print(f"User {username} created.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="mdh_server")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the control plane")
    serve.add_argument("--bind", default=os.environ.get("MDH_API_BIND", "127.0.0.1:8080"))
    serve.set_defaults(func=_serve)
    create = sub.add_parser("create-user", help="create an account without invitation")
    create.add_argument("username")
    create.set_defaults(func=_create_user)
    reset = sub.add_parser("reset-password", help="set a new password for a user who forgot it")
    reset.add_argument("username")
    reset.set_defaults(func=_reset_password)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
