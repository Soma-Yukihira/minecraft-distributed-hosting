"""Understand the Minecraft console output (vanilla, Paper, Fabric).

Patterns are anchored right after the log prefix, so a player typing "Oscar joined the game" in
the chat (logged as ``<Eve> Oscar joined the game``) cannot fake events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|§[0-9a-fk-or]")
# "[12:34:56] [Server thread/INFO]: msg" (vanilla/Fabric) or "[12:34:56 INFO]: msg" (Paper)
_PREFIX = re.compile(r"^\[[^\]]*\](?: \[[^\]]*\])?: ?")
_NAME = r"([A-Za-z0-9_]{3,16})"

_DONE = re.compile(r"^Done \(([\d.,]+)s\)! For help, type")
_JOINED = re.compile(rf"^{_NAME} joined the game$")
_LEFT = re.compile(rf"^{_NAME} left the game$")
_LIST = re.compile(r"^There are (\d+) of a max of (\d+) players online:?\s*(.*)$")
_SAVED = re.compile(r"^Saved the game$")
_TPS = re.compile(r"^TPS from last 1m, 5m, 15m: \*?([\d.]+)")
_AVG_TICK = re.compile(r"^Average time per tick: ([\d.]+) ?ms")
_PAPER_MSPT_VALUES = re.compile(r"^.?\s*([\d.]+)/([\d.]+)/([\d.]+)")
#: Player chat: "<Steve> hello", "[Not Secure] <Steve> hello" (unsigned chat, offline accounts).
_CHAT = re.compile(rf"^(?:\[Not Secure\] )?<{_NAME}> (.+)$")
_SYSTEM_CHAT = "System chat: "
_EULA = re.compile(r"^You need to agree to the EULA")
_BIND = re.compile(r"^\*+ ?FAILED TO BIND TO PORT")
#: Chunky's progress (its English messages; checked against Chunky 1.5.3).
_PREGEN_UPDATE = re.compile(r"^(?:\[Chunky\] )?Task running for (\S+?)\. Processed: [\d,. ]+ chunks "
                            r"\(([\d.,]+)%\), ETA: (\d+):(\d+):(\d+)")
_PREGEN_DONE = re.compile(r"^(?:\[Chunky\] )?Task finished for (\S+?)\. Processed")
#: spark's answers (checked against spark 1.10.187); its prefix ("[⚡] ") may come out garbled.
_SPARK_URL = re.compile(r"^(?:\[[^\]]{1,8}\] )?(https://spark\.lucko\.me/[A-Za-z0-9]{5,30})\s*$")
_SPARK_FAILED = re.compile(r"^(?:\[[^\]]{1,8}\] )?(?:Profiler operation failed unexpectedly|"
                           r"An error occurred whilst uploading the results)")
_PREGEN_STOP = re.compile(r"^(?:\[Chunky\] )?Task (cancelled|stopped|paused) for (\S+?)\.$")


@dataclass(frozen=True)
class LogEvent:
    kind: str           # done | joined | left | list | saved | tps | mspt | eula | bind_failed | chat | pregen...
    value: object = None


def clean(line: str) -> str:
    return _ANSI.sub("", line).rstrip("\r\n")


class LogParser:
    def __init__(self) -> None:
        self._expect_mspt = False

    def parse(self, raw_line: str) -> LogEvent | None:
        line = clean(raw_line)
        message = _PREFIX.sub("", line, count=1)
        # Minecraft 26.x logs command feedback as "System chat: <message>". Only the server can
        # produce that prefix (players' chat is logged as "<Name> ..."), so it is safe to strip.
        message = message.removeprefix(_SYSTEM_CHAT)
        if _EULA.match(message):
            return LogEvent("eula")
        if _BIND.match(message):
            return LogEvent("bind_failed")
        if self._expect_mspt:
            self._expect_mspt = False
            m = _PAPER_MSPT_VALUES.match(message.strip())
            if m:
                return LogEvent("mspt", float(m.group(1)))
        if message.startswith("Server tick times"):
            self._expect_mspt = True  # Paper prints the values on the next line
            return None
        if m := _DONE.match(message):
            return LogEvent("done", float(m.group(1).replace(",", ".")))
        if m := _JOINED.match(message):
            return LogEvent("joined", m.group(1))
        if m := _LEFT.match(message):
            return LogEvent("left", m.group(1))
        if m := _LIST.match(message):
            names = [n.strip() for n in m.group(3).split(",") if n.strip()]
            return LogEvent("list", [n for n in names if re.fullmatch(_NAME, n)])
        if _SAVED.match(message):
            return LogEvent("saved")
        if m := _TPS.match(message):
            return LogEvent("tps", float(m.group(1)))
        if m := _AVG_TICK.match(message):
            return LogEvent("mspt", float(m.group(1)))
        if m := _CHAT.match(message):
            return LogEvent("chat", (m.group(1), m.group(2)[:256]))
        if m := _PREGEN_UPDATE.match(message):
            percent = m.group(2) if "." in m.group(2) else m.group(2).replace(",", ".")
            try:
                value = min(100.0, float(percent.replace(",", "")))
            except ValueError:
                return None
            eta = int(m.group(3)) * 3600 + int(m.group(4)) * 60 + int(m.group(5))
            return LogEvent("pregen", (m.group(1), value, eta))
        if m := _PREGEN_DONE.match(message):
            return LogEvent("pregen_done", m.group(1))
        if m := _PREGEN_STOP.match(message):
            return LogEvent("pregen_stopped", (m.group(2), m.group(1)))  # (world, how)
        if m := _SPARK_URL.match(message):
            return LogEvent("spark_url", m.group(1))
        if _SPARK_FAILED.match(message):
            return LogEvent("spark_failed")
        return None
