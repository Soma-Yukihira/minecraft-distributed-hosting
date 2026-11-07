"""The Minecraft protocol subset read by the relay: strict, bounded, never trusting the client."""

import asyncio
import json

import pytest

from mdh_shared.protocol import minecraft as mc


def handshake_bytes(address: str, next_state: int = mc.LOGIN, protocol: int = 774, port: int = 25565) -> bytes:
    payload = mc.varint(protocol) + mc.string(address) + port.to_bytes(2, "big") + mc.varint(next_state)
    return mc.packet(0x00, payload)


def reader_with(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def read(fn, data: bytes, *args):
    """Run a stream reader function on ``data`` (readers must be created inside the loop)."""
    async def go():
        return await fn(reader_with(data), *args)
    return asyncio.run(go())


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 25565, 2**21, 2**31 - 1, -1])
def test_varint_roundtrip(value) -> None:
    decoded, pos = mc._read_varint(mc.varint(value), 0)
    assert (decoded, pos) == (value, len(mc.varint(value)))


def test_handshake_is_parsed_and_kept_byte_for_byte() -> None:
    raw = handshake_bytes("k7f3q9x2abcd.mc.example.org.", mc.LOGIN)
    got_raw, hs = read(mc.read_handshake, raw + b"rest")
    assert got_raw == raw                                   # replayed as is to the server
    assert (hs.protocol, hs.port, hs.next_state) == (774, 25565, mc.LOGIN)
    assert hs.hostname == "k7f3q9x2abcd.mc.example.org"     # trailing dot removed


def test_forge_marker_and_case_are_normalised() -> None:
    _, hs = read(mc.read_handshake, handshake_bytes("Survie.MC.example.org\0FML3\0", mc.STATUS))
    assert hs.hostname == "survie.mc.example.org" and hs.next_state == mc.STATUS


@pytest.mark.parametrize("data", [
    b"\xfe\x01",                                          # legacy server-list ping
    mc.varint(5000) + b"x" * 5000,                        # oversized "handshake"
    b"\xff\xff\xff\xff\xff\xff",                          # endless varint
    mc.packet(0x01, b"\x00"),                             # not a handshake
    mc.packet(0x00, mc.varint(774) + mc.varint(10_000)),  # address length lies
    handshake_bytes("a" * 300),                           # address too long
    handshake_bytes("ok", next_state=7),                  # unknown next state
    mc.packet(0x00, mc.varint(774) + mc.varint(2) + b"\xff\xfe" + b"\x63\xdd\x02"),  # not UTF-8
])
def test_hostile_handshakes_are_refused(data) -> None:
    with pytest.raises((mc.ProtocolError, asyncio.IncompleteReadError)):
        read(mc.read_handshake, data)


def test_login_start_name() -> None:
    raw, pid, payload = read(mc.read_packet, mc.packet(0x00, mc.string("Krystal") + b"\x00" * 16),
                             mc.MAX_LOGIN_START_BYTES)
    assert mc.parse_login_start(pid, payload) == "Krystal"
    with pytest.raises(mc.ProtocolError):
        mc.parse_login_start(0x00, mc.string("x" * 17))
    with pytest.raises(mc.ProtocolError):
        mc.parse_login_start(0x00, mc.string(""))


def test_relay_answers_are_valid_packets() -> None:
    def decode(data: bytes):
        _, pid, payload = read(mc.read_packet, data, 10_000)
        text, _ = mc._read_string(payload, 0, 32767)
        return pid, json.loads(text)

    pid, doc = decode(mc.status_response("Survie — arrêté", version_name="26.3", protocol=774))
    assert pid == 0 and doc["version"] == {"name": "26.3", "protocol": 774}
    assert doc["description"]["text"] == "Survie — arrêté"
    pid, doc = decode(mc.login_disconnect("Serveur arrêté"))
    assert (pid, doc) == (0, {"text": "Serveur arrêté"})
    assert mc.pong(b"12345678") == mc.packet(0x01, b"12345678")
