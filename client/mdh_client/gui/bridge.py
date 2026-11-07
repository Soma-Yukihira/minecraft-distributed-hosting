"""Glue between the Qt thread and the agent's asyncio thread."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal

from mdh_client.application.runtime import AgentRuntime


class Bridge(QObject):
    #: (event name, payload) coming from the agent thread; delivered in the GUI thread.
    agent_event = Signal(str, dict)
    #: (offer info, concurrent future to resolve with True/False)
    offer_question = Signal(dict, object)
    _finished = Signal(object, object, object)

    def __init__(self, runtime: AgentRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self._finished.connect(self._dispatch)

    # Called from the agent thread.
    def emit_from_agent(self, event: str, /, **payload: Any) -> None:
        self.agent_event.emit(event, payload)

    async def ask_offer(self, info: dict[str, Any]) -> bool:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.offer_question.emit(info, fut)
        return await asyncio.wrap_future(fut)

    def run(self, coro: Coroutine[Any, Any, Any], on_ok: Callable[[Any], None] | None = None,
            on_error: Callable[[BaseException], None] | None = None) -> None:
        """Run ``coro`` on the agent loop; callbacks are invoked in the GUI thread."""
        future = self.runtime.submit(coro)

        def done(f: concurrent.futures.Future) -> None:
            error = f.exception()
            self._finished.emit((on_ok, on_error), None if error else f.result(), error)

        future.add_done_callback(done)

    def _dispatch(self, callbacks: tuple, result: Any, error: BaseException | None) -> None:
        on_ok, on_error = callbacks
        if error is not None:
            if on_error:
                on_error(error)
            else:
                self.agent_event.emit("error", {"message": describe_error(error)})
        elif on_ok:
            on_ok(result)


def describe_error(error: BaseException) -> str:
    from mdh_client.gui.texts import error_text

    return error_text(error)
