"""Certificate pinning for a control plane reached by IP address with a self-signed certificate.

The VPS has no domain name, hence no certificate a public authority would sign. Its address is
shared with the SHA-256 of its certificate appended::

    https://87.106.117.55:8443#q3mF0kV2...   (base64url, 43 characters)

and the client then accepts *that* certificate and nothing else -- stronger than the usual chain
of trust, since no authority can issue a substitute. An address without ``#`` keeps the normal
verification against Windows' trusted authorities (a real domain name, later).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ssl

import httpx

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PIN_BYTES = 32


def parse_pin(text: str) -> bytes:
    """Accept base64url (what the install script prints) or hex, with or without colons
    (what ``openssl x509 -fingerprint -sha256`` prints)."""
    text = text.strip().removeprefix("sha256=").removeprefix("sha256:")
    hexa = text.replace(":", "")
    try:
        if len(hexa) == 2 * PIN_BYTES:
            return binascii.unhexlify(hexa)
        if len(text) == 43:
            return base64.urlsafe_b64decode(text + "=")
    except (binascii.Error, ValueError):
        pass
    raise ValueError("l'empreinte du certificat (après #) est invalide")


def format_pin(pin: bytes) -> str:
    return base64.urlsafe_b64encode(pin).decode().rstrip("=")


def fingerprint(der: bytes) -> bytes:
    return hashlib.sha256(der).digest()


def split_address(address: str) -> tuple[str, bytes | None]:
    """``https://host:port#pin`` -> (``https://host:port``, pin). Raises ValueError."""
    base, sep, pin_text = address.strip().partition("#")
    u = httpx.URL(base)
    if u.scheme == "https":
        pass
    elif u.scheme == "http" and u.host in LOCAL_HOSTS:
        if sep:
            raise ValueError("une empreinte de certificat n'a de sens qu'avec https://")
    else:
        raise ValueError("l'adresse du serveur central doit commencer par https:// "
                         "(http:// n'est accepté que pour ce PC lui-même)")
    return str(u).rstrip("/"), parse_pin(pin_text) if sep else None


def normalize_address(address: str) -> str:
    base, pin = split_address(address)
    return f"{base}#{format_pin(pin)}" if pin else base


class _PinnedSSLObject(ssl.SSLObject):
    """Checks the peer certificate once the handshake completes, before any byte is sent.

    asyncio (websockets) and anyio (httpx) both drive TLS through ``SSLContext.wrap_bio``, which
    instantiates ``sslobject_class``: overriding it covers every connection made with the context.
    """

    _pin: bytes = b""

    def do_handshake(self) -> None:
        super().do_handshake()
        der = self.getpeercert(binary_form=True)
        if der is None or not hmac.compare_digest(fingerprint(der), self._pin):
            raise ssl.SSLCertVerificationError(
                "le certificat du serveur central ne correspond pas à l'empreinte attendue")


def pinned_context(pin: bytes) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # The chain and the name are not checked by OpenSSL (self-signed, IP address): the pin is
    # checked instead, on the exact certificate, in _PinnedSSLObject.do_handshake.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.sslobject_class = type("PinnedSSLObject", (_PinnedSSLObject,), {"_pin": pin})
    return ctx


def client_context(address: str) -> ssl.SSLContext | None:
    """TLS context for an address: pinned, or None (defaults: system trust store, or plain http)."""
    _, pin = split_address(address)
    return pinned_context(pin) if pin else None
