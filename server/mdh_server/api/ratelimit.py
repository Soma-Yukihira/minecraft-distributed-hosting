"""In-memory token-bucket rate limiting (one process: no Redis needed)."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from fastapi import Request, WebSocket

from mdh_server.api.errors import ApiError


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}

    def hit(self, key: str, *, per_minute: float, burst: int) -> bool:
        """Consume one token. Returns False when the caller must be throttled."""
        now = self._clock()
        tokens, last = self._buckets.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + (now - last) * per_minute / 60.0)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        if key not in self._buckets and len(self._buckets) >= self._max_keys:
            self._evict(now)
        self._buckets[key] = (tokens, now)
        return allowed

    def check(self, key: str, *, per_minute: float, burst: int) -> None:
        if not self.hit(key, per_minute=per_minute, burst=burst):
            # The real wait until the next token, so the application can say how long.
            tokens, _ = self._buckets[key]
            wait = math.ceil((1.0 - tokens) * 60.0 / per_minute)
            raise ApiError(429, "rate_limited", "too many requests, slow down",
                           headers={"Retry-After": str(max(1, wait))},
                           details={"retry_after_s": max(1, wait)})

    def _evict(self, now: float) -> None:
        # Buckets idle for 10 minutes are full again: forgetting them changes nothing.
        stale = [k for k, (_, last) in self._buckets.items() if now - last > 600]
        for k in stale or list(self._buckets)[: len(self._buckets) // 2]:
            del self._buckets[k]


def client_ip(request: Request, trusted_proxies: frozenset[str]) -> str:
    return _ip(request.client.host if request.client else "unknown", request.headers, trusted_proxies)


def ws_client_ip(websocket: WebSocket, trusted_proxies: frozenset[str]) -> str:
    return _ip(websocket.client.host if websocket.client else "unknown", websocket.headers, trusted_proxies)


def _ip(peer: str, headers, trusted_proxies: frozenset[str]) -> str:
    if peer in trusted_proxies:
        forwarded = headers.get("x-real-ip")
        if forwarded:
            return forwarded.strip()[:64]
    return peer
