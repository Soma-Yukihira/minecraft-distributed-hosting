"""The control plane is reached by IP with a self-signed certificate, pinned by its SHA-256.

The live tests make a certificate exactly like deployment/vps/install.sh does (OpenSSL, IP in
the subjectAltName) and check that HTTP and both WebSockets accept it with the right pin and
refuse it with any other -- before a single byte (password, token) is sent.
"""

import asyncio
import shutil
import ssl
import subprocess
from http import HTTPStatus
from pathlib import Path

import pytest
import websockets

from mdh_client.networking import tls
from mdh_client.networking.api_client import ApiClient, CertificateMismatch, check_base_url
from mdh_client.networking.relay_tunnel import run_tunnel

OPENSSL = shutil.which("openssl") or next(
    (p for p in (r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe")
     if Path(p).exists()), None)
PIN = bytes(range(32))
PIN_B64 = tls.format_pin(PIN)


# ------------------------------------------------------------------ address parsing

def test_pin_formats():
    hexa = PIN.hex()
    colons = ":".join(hexa[i:i + 2] for i in range(0, 64, 2)).upper()
    assert len(PIN_B64) == 43
    for text in (PIN_B64, hexa, colons, "sha256=" + PIN_B64, f" {PIN_B64} "):
        assert tls.parse_pin(text) == PIN
    for bad in ("", "abc", PIN_B64[:-1], hexa[:-2], "zz" * 32, PIN_B64 + "AA"):
        with pytest.raises(ValueError):
            tls.parse_pin(bad)


def test_addresses():
    assert check_base_url("https://87.106.117.55:8443/") == "https://87.106.117.55:8443"
    assert check_base_url(f"https://87.106.117.55:8443/#{PIN.hex()}") == f"https://87.106.117.55:8443#{PIN_B64}"
    assert check_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ("http://87.106.117.55:8443", f"http://127.0.0.1:8080#{PIN_B64}", "ftp://x", "https://x#nope"):
        with pytest.raises(ValueError):
            check_base_url(bad)
    assert tls.client_context("https://example.org") is None  # system trust store
    assert tls.client_context("http://localhost:8080") is None
    ctx = tls.client_context(f"https://87.106.117.55:8443#{PIN_B64}")
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_api_client_keeps_pin_out_of_urls():
    api = ApiClient(f"https://87.106.117.55:8443#{PIN_B64}")
    assert api.base_url == "https://87.106.117.55:8443"
    assert api.ws_url == "wss://87.106.117.55:8443/api/v1/ws"
    assert api.address.endswith(PIN_B64) and api.ssl_context is not None
    asyncio.run(api.close())


# ------------------------------------------------------------------ live TLS

@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> tuple[Path, Path, str]:
    if OPENSSL is None:
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("cert")
    key, cert = d / "key.pem", d / "cert.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return cert, key, tls.format_pin(tls.fingerprint(der))


def test_shell_pipeline_matches(certificate):
    """install.sh prints the pin with OpenSSL only: it must equal what the client computes."""
    cert, _, pin = certificate
    der = subprocess.run([OPENSSL, "x509", "-in", str(cert), "-outform", "DER"], check=True,
                         capture_output=True).stdout
    digest = subprocess.run([OPENSSL, "dgst", "-sha256", "-binary"], input=der, check=True,
                            capture_output=True).stdout
    b64 = subprocess.run([OPENSSL, "base64", "-A"], input=digest, check=True, capture_output=True).stdout
    assert b64.decode().strip().translate(str.maketrans("+/", "-_")).rstrip("=") == pin


async def _serve(cert: Path, key: Path, body):
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    received: list = []

    async def handler(ws):
        async for message in ws:
            received.append(message)
            await ws.send(message)

    def plain_http(connection, request):
        if request.headers.get("Upgrade") is None:
            return connection.respond(HTTPStatus.OK, "{}\n")
        return None

    server = await websockets.serve(handler, "127.0.0.1", 0, ssl=server_ctx, process_request=plain_http)
    port = server.sockets[0].getsockname()[1]
    try:
        return await body(port, received)
    finally:
        server.close()
        await server.wait_closed()


def test_pinned_connections(certificate):
    cert, key, pin = certificate
    wrong = tls.format_pin(bytes(32))

    async def body(port, received):
        good = ApiClient(f"https://127.0.0.1:{port}#{pin}")
        bad = ApiClient(f"https://127.0.0.1:{port}#{wrong}")
        unpinned = ApiClient(f"https://127.0.0.1:{port}")
        try:
            assert (await good._send("GET", "/ping")).status_code == 200
            with pytest.raises(CertificateMismatch):
                await bad.request("POST", "/auth/login", {"username": "u", "password": "secret-password"})
            with pytest.raises(CertificateMismatch):  # self-signed: the system store refuses it too
                await unpinned.request("GET", "/ping")

            async with websockets.connect(good.ws_url, ssl=good.ssl_context) as ws:
                await ws.send("hello")
                assert await ws.recv() == "hello"
            with pytest.raises(ssl.SSLCertVerificationError):
                async with websockets.connect(bad.ws_url, ssl=bad.ssl_context):
                    pass
            # A relay tunnel to the wrong certificate never sends its one-time token.
            with pytest.raises(ssl.SSLCertVerificationError):
                await run_tunnel(bad.ws_url, "relay-token-must-stay-secret", 9, bad.ssl_context)
            assert received == ["hello"]
        finally:
            for api in (good, bad, unpinned):
                await api.close()

    asyncio.run(_serve(cert, key, body))
