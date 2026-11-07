"""Process-wide state of the control plane.

There is exactly one control-plane process and one SQLite connection. All database access
happens on the event-loop thread (handlers are ``async def`` and never await in the middle of a
transaction), so transactions are naturally serialised and the lease authority is unambiguous.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mdh_server.alerts import RunMarker
from mdh_server.api.ratelimit import RateLimiter
from mdh_server.auth.passwords import PasswordService
from mdh_server.config import Settings
from mdh_server.releases import ReleaseShelf, ReleaseWatcher
from mdh_server.storage.store import BlobStore

if TYPE_CHECKING:
    from mdh_server.coordination.service import Coordinator
    from mdh_server.realtime import Hub
    from mdh_server.relay.service import Relay


@dataclass
class AppContext:
    settings: Settings
    conn: sqlite3.Connection
    passwords: PasswordService
    #: Wall clock (UTC seconds). Injected so tests can move time.
    clock: Callable[[], float] = time.time
    limiter: RateLimiter = field(default_factory=RateLimiter)
    started_at: float = 0.0
    hub: Hub = field(init=False)
    coordinator: Coordinator = field(init=False)
    store: BlobStore = field(init=False)
    #: One per system the application is published for ("windows", "macos-arm64").
    releases: dict[str, ReleaseShelf] = field(init=False)
    release_watcher: ReleaseWatcher = field(init=False)
    run_marker: RunMarker = field(init=False)
    relay: Relay = field(init=False)

    def now(self) -> float:
        return self.clock()
