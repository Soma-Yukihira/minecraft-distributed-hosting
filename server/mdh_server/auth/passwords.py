"""Argon2id password hashing, off the event loop and with bounded concurrency.

Each hash uses ~64 MiB of RAM with the default parameters: the semaphore keeps a burst of
logins from eating the VPS memory.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool


class PasswordService:
    def __init__(self, hasher: PasswordHasher | None = None, max_concurrency: int = 2) -> None:
        # argon2-cffi defaults follow RFC 9106's low-memory profile and use the argon2id variant.
        self._hasher = hasher or PasswordHasher()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._dummy_hash = self._hasher.hash("timing-equaliser-password")

    async def hash(self, password: str) -> str:
        async with self._semaphore:
            return await run_in_threadpool(self._hasher.hash, password)

    async def verify(self, encoded: str | None, password: str) -> bool:
        """Constant work whether or not the user exists (``encoded=None``)."""
        async with self._semaphore:
            try:
                return await run_in_threadpool(
                    self._hasher.verify, encoded or self._dummy_hash, password
                ) and encoded is not None
            except (VerificationError, InvalidHashError):
                return False

    def needs_rehash(self, encoded: str) -> bool:
        return self._hasher.check_needs_rehash(encoded)
