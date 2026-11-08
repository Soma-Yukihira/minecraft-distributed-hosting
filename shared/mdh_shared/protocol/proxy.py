"""PROXY protocol, version 2 (HAProxy): the player's real address, in front of the connection.

Players reach the hosting PC through the VPS relay, so Minecraft sees every connection come from
127.0.0.1, and its IP bans are useless. With ``proxies.proxy-protocol`` on, Paper reads this
header first and takes the address it carries (docs/ARCHITECTURE.md 4.22).

The header is written by the hosting PC itself, on its connection to 127.0.0.1: Minecraft only
listens there, so nothing from the network can write one. Without a known address (an older VPS),
the LOCAL command tells Paper to keep the connection's own address.
"""

from __future__ import annotations

import ipaddress
import struct

SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
_PROXY, _LOCAL = 0x21, 0x20          # version 2 + command
_TCP4, _TCP6, _UNSPEC = 0x11, 0x21, 0x00


def header(peer_ip: str | None, peer_port: int | None, local_port: int) -> bytes:
    """The header for a player connection from ``peer_ip:peer_port`` to 127.0.0.1:``local_port``."""
    if peer_ip is None or peer_port is None:
        return SIGNATURE + struct.pack(">BBH", _LOCAL, _UNSPEC, 0)
    address = ipaddress.ip_address(peer_ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # "::ffff:1.2.3.4" is an IPv4 player
    if isinstance(address, ipaddress.IPv4Address):
        body = address.packed + ipaddress.IPv4Address("127.0.0.1").packed
        family = _TCP4
    else:
        body = address.packed + ipaddress.IPv6Address("::1").packed
        family = _TCP6
    body += struct.pack(">HH", peer_port, local_port)
    return SIGNATURE + struct.pack(">BBH", _PROXY, family, len(body)) + body


def parse(data: bytes) -> tuple[tuple[str, int] | None, int]:
    """(source address or None for LOCAL, header length). Raises ValueError if not a header."""
    if not data.startswith(SIGNATURE) or len(data) < 16:
        raise ValueError("not a PROXY protocol v2 header")
    command, family, length = struct.unpack(">BBH", data[12:16])
    if len(data) < 16 + length or command not in (_PROXY, _LOCAL):
        raise ValueError("truncated or unknown PROXY header")
    body = data[16:16 + length]
    if command == _LOCAL:
        return None, 16 + length
    if family == _TCP4:
        ip, port = str(ipaddress.IPv4Address(body[:4])), struct.unpack(">H", body[8:10])[0]
    elif family == _TCP6:
        ip, port = str(ipaddress.IPv6Address(body[:16])), struct.unpack(">H", body[32:34])[0]
    else:
        raise ValueError("unsupported PROXY address family")
    return (ip, port), 16 + length
