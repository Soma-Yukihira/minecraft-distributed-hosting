"""``server.properties`` and ``eula.txt`` of a server directory."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

#: Always imposed locally, whatever the group configuration says.
FORCED_PROPERTIES: dict[str, str] = {
    "online-mode": "true",            # Microsoft account verification (see OFFLINE_PROPERTIES)
    "enable-rcon": "false",           # no remote console: the API never runs arbitrary commands
    "enable-query": "false",
    "management-server-enabled": "false",
    "enable-jmx-monitoring": "false",
    # Players come through the VPS relay, which connects to 127.0.0.1. Listening only there means
    # nobody on the host's network (or behind a forwarded port) can skip the relay and its
    # personal addresses, and Windows does not ask to open its firewall for Java.
    "server-ip": "127.0.0.1",
}


_KEY = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")


def _escape(value: str) -> str:
    out = []
    for i, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == " " and i == 0:
            out.append("\\ ")
        elif ord(ch) > 126 or ord(ch) < 32:
            out.append(f"\\u{ord(ch):04x}")  # valid whatever charset the server reads with
        else:
            out.append(ch)
    return "".join(out)


def _to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def read_properties(path: Path) -> dict[str, str]:
    """Raw key -> raw value (escapes kept as-is) of an existing file, comments dropped."""
    result: dict[str, str] = {}
    try:
        lines = path.read_text("latin-1").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        key, sep, value = stripped.partition("=")
        if sep:
            result[key.strip()] = value
    return result


#: When a group admin explicitly accepts non-official accounts (TLauncher...): nobody's identity is
#: verified any more, so only the group's list of allowed players may join.
OFFLINE_PROPERTIES: dict[str, str] = {
    "online-mode": "false",
    "white-list": "true",
    "enforce-whitelist": "true",
}


def write_properties(path: Path, group_properties: dict[str, Any], *, port: int,
                     allow_offline_accounts: bool = False) -> dict[str, str]:
    """Merge existing file + group settings + forced local settings, written atomically."""
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    merged = read_properties(path)
    for key, value in group_properties.items():
        if not _KEY.fullmatch(key):  # a "=", ":" or line break would write another setting
            raise ValueError(f"invalid server.properties key {key!r}")
        merged[key] = _escape(_to_text(value))
    merged.update(FORCED_PROPERTIES)
    if allow_offline_accounts:
        merged.update(OFFLINE_PROPERTIES)
    merged["server-port"] = str(port)
    body = "#Managed by Minecraft Distributed Hosting - local changes to managed keys are overwritten\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(merged.items()))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, "latin-1")
    os.replace(tmp, path)
    return merged


def eula_accepted(path: Path) -> bool:
    return read_properties(path).get("eula", "").strip().lower() == "true"


def write_eula(path: Path, accepted_at: str) -> None:
    """Only called after the user explicitly accepted https://aka.ms/MinecraftEULA in the app."""
    datetime.fromisoformat(accepted_at)  # refuse anything that is not a real acceptance date
    path.write_text(
        "#By changing the setting below to TRUE you are indicating your agreement to our EULA "
        "(https://aka.ms/MinecraftEULA).\n"
        f"#Accepted by the user in Minecraft Distributed Hosting on {accepted_at}\n"
        "eula=true\n",
        "latin-1",
    )


def whitelisted_names(server_dir: Path) -> set[str]:
    """Names currently in whitelist.json (written by Minecraft itself)."""
    import json

    try:
        entries = json.loads((server_dir / "whitelist.json").read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return set()
    return {str(e.get("name", "")) for e in entries if isinstance(e, dict) and e.get("name")}


def reset_identities_if_auth_mode_changed(server_dir: Path, allow_offline_accounts: bool) -> bool:
    """Player UUIDs differ between verified and non-verified mode: when the mode changes, drop
    the cached identities so the whitelist is rebuilt with the right ones. Returns True if reset."""
    marker = server_dir / ".mdh-auth-mode"
    mode = "offline" if allow_offline_accounts else "online"
    # Directories created before this marker existed always ran with verified accounts.
    previous = marker.read_text("utf-8").strip() if marker.exists() else "online"
    changed = previous != mode
    if changed:
        for name in ("whitelist.json", "usercache.json"):
            (server_dir / name).unlink(missing_ok=True)
    marker.write_text(mode, "utf-8")
    return changed

def offline_uuid(name: str) -> str:
    """UUID Minecraft gives a player when accounts are not verified (online-mode=false).

    Same as Java's ``UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(UTF_8))``.
    """
    import hashlib
    import uuid

    return str(uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{name}".encode(), usedforsecurity=False).digest(), version=3))


def write_offline_whitelist(server_dir: Path, names: list[str]) -> None:
    """Write whitelist.json ourselves when accounts are not verified.

    Minecraft's ``whitelist add`` looks names up on Mojang's servers even in that mode: for a
    name that also exists as an official account it stores the *official* UUID, while the player
    joins with the offline UUID and is refused. The name cache is seeded too, so console
    commands such as ``op <name>`` resolve to the same identity.
    """
    import json
    from datetime import UTC, timedelta

    entries = [{"uuid": offline_uuid(n), "name": n} for n in names]
    _write_json(server_dir / "whitelist.json", entries)

    cache_path = server_dir / "usercache.json"
    try:
        cache = json.loads(cache_path.read_text("utf-8"))
        if not isinstance(cache, list):
            cache = []
    except (FileNotFoundError, ValueError):
        cache = []
    managed = {n.lower() for n in names}
    expires = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S +0000")
    cache = [e for e in cache if isinstance(e, dict) and str(e.get("name", "")).lower() not in managed]
    cache += [{"name": n, "uuid": offline_uuid(n), "expiresOn": expires} for n in names]
    _write_json(cache_path, cache)


def _write_json(path: Path, data: object) -> None:
    import json

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    os.replace(tmp, path)