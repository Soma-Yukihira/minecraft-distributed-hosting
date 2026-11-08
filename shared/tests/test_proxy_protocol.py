"""PROXY protocol v2 header written by the hosting PC in front of each player connection."""

import pytest

from mdh_shared.protocol import proxy


def test_ipv4_player() -> None:
    data = proxy.header("203.0.113.7", 51234, 25565)
    assert data[:12] == proxy.SIGNATURE and data[12:14] == b"\x21\x11" and len(data) == 16 + 12
    assert proxy.parse(data + b"rest") == (("203.0.113.7", 51234), 28)


def test_ipv6_and_mapped_ipv4_players() -> None:
    data = proxy.header("2001:db8::42", 40000, 25565)
    assert data[13] == 0x21 and proxy.parse(data) == (("2001:db8::42", 40000), 16 + 36)
    assert proxy.parse(proxy.header("::ffff:198.51.100.9", 1, 25565))[0] == ("198.51.100.9", 1)


def test_unknown_address_keeps_the_connection_own() -> None:
    """An older VPS sends no address: LOCAL, which Paper reads as "keep 127.0.0.1"."""
    data = proxy.header(None, None, 25565)
    assert data == proxy.SIGNATURE + b"\x20\x00\x00\x00"
    assert proxy.parse(data) == (None, 16)


def test_not_a_header() -> None:
    with pytest.raises(ValueError):
        proxy.parse(b"\x10\x00handshake...")
    with pytest.raises(ValueError):
        proxy.header("not an address", 1, 25565)
