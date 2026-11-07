"""Run the agent's asyncio loop in a background thread, next to the Qt event loop.

The GUI never blocks on the network: it submits coroutines with :meth:`AgentRuntime.submit` and
receives results/events through Qt signals.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any


class AgentRuntime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()  # Proactor loop on Windows: subprocesses supported
        self._thread = threading.Thread(target=self._run, name="mdh-agent", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        return self.submit(coro).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
