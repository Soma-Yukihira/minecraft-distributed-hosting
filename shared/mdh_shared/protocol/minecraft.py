"""The few bits of the Minecraft Java protocol the relay must understand.

The relay only reads what a player sends *before* the server takes over: the handshake (which
address was typed, status or login) and, for a login, the player name. Both are unencrypted and
uncompressed by design of the protocol. Everything after that is forwarded byte for byte.

It also builds the two answers it may give itself when no PC hosts the server: a server-list
status (MOTD) and a login refusal with a readable reason.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass

STATUS = 1
LOGIN = 2
TRANSFER = 3  # 1.20.5+: a login coming from another server

MAX_HANDSHAKE_BYTES = 1024    # address <= 255 chars: anything bigger is not a real client
MAX_LOGIN_START_BYTES = 512   # name <= 16 chars + UUID (+ old signature data is gone since 1.19.3)
MAX_ADDRESS_CHARS = 255


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Handshake:
    protocol: int
    address: str
    port: int
    next_state: int

    @property
    def hostname(self) -> str:
        """Typed address, normalised: Forge appends "\\0FML…", some clients a trailing dot."""
        return self.address.split("\0", 1)[0].rstrip(".").lower()


# --------------------------------------------------------------------------- encoding


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def string(value: str) -> bytes:
    data = value.encode("utf-8")
    return varint(len(data)) + data


def packet(packet_id: int, payload: bytes) -> bytes:
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def status_response(text: str, *, version_name: str, protocol: int, max_players: int = 0) -> bytes:
    """Server-list answer given by the relay itself (no PC hosts the server right now)."""
    doc = {"version": {"name": version_name, "protocol": protocol},
           "players": {"max": max_players, "online": 0},
           "description": {"text": text}}
    return packet(0x00, string(json.dumps(doc, ensure_ascii=False)))


def pong(payload: bytes) -> bytes:
    return packet(0x01, payload)


def login_disconnect(text: str) -> bytes:
    """Refuse a login with a message shown on the player's screen."""
    return packet(0x00, string(json.dumps({"text": text}, ensure_ascii=False)))


#: Login state, 1.13 and later (protocol 393): server -> client "Login Plugin Request", and the
#: client's "Login Plugin Response". Proxies (Velocity, BungeeCord) use the same exchange.
LOGIN_PLUGIN_REQUEST = 0x04
LOGIN_PLUGIN_RESPONSE = 0x02
MIN_PLUGIN_PROTOCOL = 393
WAIT_CHANNEL = "mdh:wait"


def login_plugin_request(message_id: int, channel: str = WAIT_CHANNEL) -> bytes:
    """A query on a channel no client knows: it answers "not understood" at once. Sent every few
    seconds, it keeps a player waiting on "Connexion…" while the server changes PC (ARCHITECTURE 4.18)."""
    return packet(LOGIN_PLUGIN_REQUEST, varint(message_id) + string(channel))


def parse_plugin_response(packet_id: int, payload: bytes) -> int:
    """The message id the client answers."""
    if packet_id != LOGIN_PLUGIN_RESPONSE:
        raise ProtocolError("expected a Login Plugin Response")
    message_id, _ = _read_varint(payload, 0)
    return message_id


def login_handshake(hs: Handshake) -> bytes:
    """The same handshake, as a plain login: a player sent by /transfer reaches a server that does
    not need ``accepts-transfers``."""
    return packet(0x00, varint(hs.protocol) + string(hs.address) + struct.pack(">H", hs.port) + varint(LOGIN))


# --------------------------------------------------------------------------- decoding


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            raise ProtocolError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return (value - (1 << 32) if value & (1 << 31) else value), pos
    raise ProtocolError("varint too long")


def _read_string(data: bytes, pos: int, max_chars: int) -> tuple[str, int]:
    length, pos = _read_varint(data, pos)
    if length < 0 or length > max_chars * 4 or pos + length > len(data):
        raise ProtocolError("bad string length")
    try:
        value = data[pos:pos + length].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("string is not UTF-8") from exc
    if len(value) > max_chars:
        raise ProtocolError("string too long")
    return value, pos + length


async def read_packet(reader: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, int, bytes]:
    """Read one uncompressed packet. Returns (raw bytes as received, packet id, payload)."""
    header = b""
    length = 0
    for shift in range(0, 35, 7):
        byte = await reader.readexactly(1)
        header += byte
        length |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            break
    else:
        raise ProtocolError("packet length varint too long")
    if length <= 0 or length > max_bytes:
        raise ProtocolError(f"packet of {length} bytes refused")
    body = await reader.readexactly(length)
    packet_id, pos = _read_varint(body, 0)
    return header + body, packet_id, body[pos:]


def parse_handshake(packet_id: int, payload: bytes) -> Handshake:
    if packet_id != 0x00:
        raise ProtocolError("first packet is not a handshake")
    protocol, pos = _read_varint(payload, 0)
    address, pos = _read_string(payload, pos, MAX_ADDRESS_CHARS)
    if pos + 2 > len(payload):
        raise ProtocolError("truncated handshake")
    (port,) = struct.unpack(">H", payload[pos:pos + 2])
    next_state, pos = _read_varint(payload, pos + 2)
    if next_state not in (STATUS, LOGIN, TRANSFER):
        raise ProtocolError(f"unknown next state {next_state}")
    return Handshake(protocol, address, port, next_state)


def parse_login_start(packet_id: int, payload: bytes) -> str:
    """The player name (the UUID that follows is the client's claim and is not used here)."""
    if packet_id != 0x00:
        raise ProtocolError("expected Login Start")
    name, _ = _read_string(payload, 0, 16)
    if not name:
        raise ProtocolError("empty player name")
    return name


async def read_handshake(reader: asyncio.StreamReader) -> tuple[bytes, Handshake]:
    """First packet of a connection. Returns (raw bytes to replay to the server, parsed handshake)."""
    first = await reader.readexactly(1)
    if first == b"\xfe":
        raise ProtocolError("legacy (pre-1.7) server list ping")
    raw = bytearray(first)
    length = first[0] & 0x7F
    shift = 7
    while raw[-1] & 0x80:
        if shift >= 35:
            raise ProtocolError("packet length varint too long")
        raw += await reader.readexactly(1)
        length |= (raw[-1] & 0x7F) << shift
        shift += 7
    if length <= 0 or length > MAX_HANDSHAKE_BYTES:
        raise ProtocolError(f"handshake of {length} bytes refused")
    body = await reader.readexactly(length)
    packet_id, pos = _read_varint(body, 0)
    return bytes(raw) + body, parse_handshake(packet_id, body[pos:])