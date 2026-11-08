"""IP bans on every type of server (docs/ARCHITECTURE.md 4.22).

Players come through the VPS relay: only Paper reads their real address (PROXY protocol). Fabric
and vanilla see every player as 127.0.0.1, so on their own:

* ``ban-ip <name>`` would ban 127.0.0.1, that is everybody;
* a ban of a real address in ``banned-ips.json`` would never match anyone.

The hosting PC knows each player's address from the relay (``relay_open``). It therefore:

* refuses itself, in the tunnel, a connection from an address listed in ``banned-ips.json``
  (any server type: the list is Minecraft's own, kept with the world);
* turns ``ban-ip <name>`` into ``ban-ip <address>`` (then kicks the player) where Minecraft cannot
  see the address, and refuses to ban the loopback address, which would shut everyone out;
* undoes a ban with ``deban-ip <address or name>`` (Minecraft's ``pardon-ip``, which still works);
* removes a loopback entry from the list before a start (left by an older version).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path

from mdh_client.minecraft.process import CommandRejected

FILE = "banned-ips.json"
BANNED_MESSAGE = "Tu es banni de ce serveur."
#: Also "minecraft:ban-ip", the same command under its full name.
_BAN_IP = re.compile(r"^/?(?:minecraft:)?ban-ip\s+(\S+)(.*)$", re.IGNORECASE)
#: ban-ip run by another command ("execute ... run ban-ip <name>"): cannot be translated.
_NESTED_BAN_IP = re.compile(r"\brun\s+/?(?:minecraft:)?ban-ip\s+(\S+)", re.IGNORECASE)
_DEBAN_IP = re.compile(r"^/?deban-ip\s+(\S+)\s*$", re.IGNORECASE)


def normalize(address: str) -> str | None:
    """The canonical form of an address ("::ffff:1.2.3.4" is 1.2.3.4), None if it is not one."""
    try:
        ip = ipaddress.ip_address(address.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _loopback(address: str | None) -> bool:
    return address is not None and ipaddress.ip_address(address).is_loopback


class BanList:
    """``banned-ips.json`` of one server folder, read again whenever Minecraft rewrites it."""

    def __init__(self, server_dir: Path) -> None:
        self.path = server_dir / FILE
        self._stamp: tuple[int, int] | None = None
        self._ips: frozenset[str] = frozenset()

    def addresses(self) -> frozenset[str]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._ips = None, frozenset()
            return self._ips
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._stamp, self._ips = stamp, frozenset(_read(self.path))
        return self._ips

    def bans(self, address: str | None) -> bool:
        ip = normalize(address) if address else None
        return ip is not None and ip in self.addresses()


def _read(path: Path) -> list[str]:
    try:
        entries = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    found = []
    for e in entries if isinstance(entries, list) else []:
        ip = normalize(str(e.get("ip", ""))) if isinstance(e, dict) else None
        if ip is not None:
            found.append(ip)
    return found


def drop_loopback(server_dir: Path) -> int:
    """Before a start: remove 127.0.0.1 and ::1 (a ban of everybody behind the relay)."""
    path = server_dir / FILE
    try:
        entries = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(entries, list):
        return 0
    kept = [e for e in entries if not (isinstance(e, dict) and _loopback(normalize(str(e.get("ip", "")))))]
    if len(kept) == len(entries):
        return 0
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=2), "utf-8")
    os.replace(tmp, path)
    return len(entries) - len(kept)


def ban_commands(command: str, player_ips: dict[str, str]) -> list[str]:
    """The console commands that carry out ``command`` (unchanged unless it is ``ban-ip``).

    ``player_ips``: lower-case name -> address of the players seen through the relay.

    A name is always turned into the address the relay saw, Paper included: when its tunnel had
    no address to give (an older VPS), Paper too sees 127.0.0.1, and would ban everybody.

    ``deban-ip <adresse ou pseudo>`` is the French name of Minecraft's ``pardon-ip``."""
    m = _DEBAN_IP.match(command.strip())
    if m:
        target = m.group(1)
        ip = normalize(target) or player_ips.get(target.lower())
        if ip is None:
            raise CommandRejected(f"« {target} » n'est pas une adresse IP, ni un joueur vu depuis le démarrage du "
                                  "serveur sur ce PC")
        return [f"pardon-ip {ip}"]
    m = _BAN_IP.match(command.strip())
    if not m:
        nested = _NESTED_BAN_IP.search(command)
        target_ip = normalize(nested.group(1)) if nested else None
        if nested and (target_ip is None or _loopback(target_ip)):
            raise CommandRejected("ban-ip d'un pseudo dans une autre commande bannirait 127.0.0.1, donc tout le "
                                  "monde : tape « ban-ip <pseudo> » seul")
        return [command]
    target, rest = m.group(1), m.group(2)
    ip = normalize(target)
    if ip is not None:
        if _loopback(ip):
            raise CommandRejected("bannir 127.0.0.1 bannirait tout le monde : tous les joueurs passent par le relais")
        return [f"ban-ip {ip}{rest}"]
    ip = player_ips.get(target.lower())
    if ip is None:
        raise CommandRejected(f"adresse de {target} inconnue : il doit s'être connecté depuis le démarrage "
                              "du serveur sur ce PC")
    return [f"ban-ip {ip}{rest}", f"kick {target} {BANNED_MESSAGE}"]
