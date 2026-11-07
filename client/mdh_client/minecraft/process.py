"""Run one Minecraft server process: start, console, graceful stop, crash detection."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from mdh_client.minecraft.log_parser import LogEvent, LogParser, clean
from mdh_client.system import watchdog

if TYPE_CHECKING:
    from mdh_client.minecraft.job import JobObject

log = logging.getLogger("mdh.minecraft")

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
#: macOS "below normal": the nice value given to Minecraft.
BELOW_NORMAL_NICE = 10


class ProcessState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


class CommandRejected(ValueError):
    pass


def validate_command(command: str) -> str:
    command = command.strip().removeprefix("/")
    if not command or len(command) > 256:
        raise CommandRejected("empty or too long command")
    if any(ord(c) < 32 for c in command):
        raise CommandRejected("control characters are not allowed in console commands")
    return command


class MinecraftProcess:
    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        on_line: Callable[[str], None] | None = None,
        on_event: Callable[[LogEvent], None] | None = None,
        on_state: Callable[[ProcessState], None] | None = None,
        below_normal_priority: bool = False,
        cpu_limit_percent: int | None = None,
        log_capacity: int = 5000,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self._on_line = on_line
        self._on_event = on_event
        self._on_state = on_state
        self._below_normal = below_normal_priority
        self._cpu_limit = cpu_limit_percent
        self.lines: collections.deque[str] = collections.deque(maxlen=log_capacity)
        self.state = ProcessState.STOPPED
        self.players: set[str] = set()
        self.tps: float | None = None
        self.mspt: float | None = None
        self.exit_code: int | None = None
        self.eula_required = False
        self.bind_failed = False
        self.started_at: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._job: JobObject | None = None
        self._watchdog: subprocess.Popen | None = None
        self._parser = LogParser()
        self._stop_requested = False
        self._tps_reported = False
        self._ready = asyncio.Event()
        self._saved = asyncio.Event()
        self._exited = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _set_state(self, state: ProcessState) -> None:
        if state != self.state:
            self.state = state
            log.info("minecraft process %s", state.value)
            if self._on_state:
                self._on_state(state)

    async def start(self) -> None:
        if self.alive:
            raise RuntimeError("server already running")
        self._stop_requested = False
        self._ready.clear()
        self._exited.clear()
        self.players.clear()
        self.eula_required = self.bind_failed = False
        flags = 0
        posix = sys.platform != "win32"
        if not posix:
            from mdh_client.minecraft.job import CREATE_SUSPENDED, JobObject

            flags = (CREATE_NO_WINDOW | CREATE_SUSPENDED
                     | (BELOW_NORMAL_PRIORITY_CLASS if self._below_normal else 0))
            self._job = JobObject(cpu_limit_percent=self._cpu_limit)
        self._set_state(ProcessState.STARTING)
        # macOS: its own process group, so that stopping it reaches whatever Java starts too.
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=flags, start_new_session=posix)
        if posix:
            try:
                if self._below_normal:
                    os.setpriority(os.PRIO_PROCESS, self._proc.pid, BELOW_NORMAL_NICE)
                # No job object on macOS: a watchdog stops Minecraft if the application dies.
                self._watchdog = watchdog.spawn(self._proc.pid)
                if self._cpu_limit:
                    log.info("CPU cap not available on this system: only the priority applies")
            except OSError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                await self._proc.wait()
                self._set_state(ProcessState.CRASHED)
                raise
        if self._job is not None:
            try:
                self._job.assign_and_resume(self._proc.pid)
            except OSError:
                # Without the job, Java could outlive the agent: refuse to run.
                self._proc.kill()
                await self._proc.wait()
                self._set_state(ProcessState.CRASHED)
                raise
        self.started_at = time.monotonic()
        self._tasks = [asyncio.create_task(self._read_output()), asyncio.create_task(self._watch_exit())]

    async def _read_output(self) -> None:
        assert self._proc and self._proc.stdout
        while raw := await self._proc.stdout.readline():
            line = clean(raw.decode("utf-8", errors="replace"))
            self.lines.append(line)
            if self._on_line:
                self._on_line(line)
            event = self._parser.parse(line)
            if event is not None:
                self._handle(event)

    def _handle(self, event: LogEvent) -> None:
        if event.kind == "done":
            self._ready.set()
            self._set_state(ProcessState.RUNNING)
        elif event.kind == "joined":
            self.players.add(str(event.value))
        elif event.kind == "left":
            self.players.discard(str(event.value))
        elif event.kind == "list":
            self.players = set(event.value)  # type: ignore[arg-type]
        elif event.kind == "saved":
            self._saved.set()
        elif event.kind == "tps":
            self.tps = float(event.value)  # type: ignore[arg-type]
            self._tps_reported = True
        elif event.kind == "mspt":
            self.mspt = float(event.value)  # type: ignore[arg-type]
            if not self._tps_reported:  # vanilla/Fabric only report MSPT: derive TPS from it
                self.tps = round(min(20.0, 1000.0 / self.mspt), 1) if self.mspt else 20.0
        elif event.kind == "eula":
            self.eula_required = True
        elif event.kind == "bind_failed":
            self.bind_failed = True
        if self._on_event:
            self._on_event(event)

    async def _watch_exit(self) -> None:
        assert self._proc
        self.exit_code = await self._proc.wait()
        for t in self._tasks:
            if t is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(t, 2)
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._watchdog is not None:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                await asyncio.to_thread(self._watchdog.wait, 5)  # it ends by itself once Minecraft has
            self._watchdog = None
        self.players.clear()
        self._exited.set()
        self._set_state(ProcessState.STOPPED if self._stop_requested else ProcessState.CRASHED)

    async def wait_ready(self, timeout: float) -> bool:
        """True once the server printed "Done", False on timeout or exit."""
        ready = asyncio.create_task(self._ready.wait())
        exited = asyncio.create_task(self._exited.wait())
        done, pending = await asyncio.wait({ready, exited}, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        return ready in done and self._ready.is_set()

    async def send(self, command: str) -> None:
        command = validate_command(command)
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("server is not running")
        self._proc.stdin.write((command + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def announce(self, message: str) -> None:
        await self.send("say " + message.replace("\n", " ")[:200])

    async def save_all_flush(self, timeout: float = 60) -> bool:
        self._saved.clear()
        await self.send("save-all flush")
        try:
            await asyncio.wait_for(self._saved.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def stop(self, timeout: float = 90) -> int | None:
        """Graceful stop (saves the world); kills only if the server does not exit in time."""
        if not self.alive:
            return self.exit_code
        self._stop_requested = True
        self._set_state(ProcessState.STOPPING)
        was_ready = self._ready.is_set()
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            await self.send("stop")
        except (RuntimeError, OSError):
            pass
        if not was_ready:
            # A server still starting may drop console input (seen with Fabric 26.3): ask again
            # as soon as it is up instead of waiting for the kill.
            if await self.wait_ready(timeout) and self.alive:
                with contextlib.suppress(RuntimeError, OSError):
                    await self.send("stop")
        try:
            await asyncio.wait_for(self._exited.wait(), max(0.0, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            log.warning("server did not stop within %ss, killing it", timeout)
            self.kill()
            await self._exited.wait()
        return self.exit_code

    def kill(self) -> None:
        self._stop_requested = True
        if self._job is not None:
            self._job.terminate()
        elif self.alive and self._proc is not None and sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)  # the whole group, children included
        elif self.alive and self._proc is not None:
            self._proc.kill()

    async def wait_exit(self) -> int | None:
        await self._exited.wait()
        return self.exit_code
