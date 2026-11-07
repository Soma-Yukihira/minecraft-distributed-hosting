"""Verified downloads from an allowlist of official hosts.

* HTTPS only, every hop (redirects included) must target an allowlisted host.
* Size bounded while streaming; checksum verified before the file gets its final name.
* A file that fails verification is deleted: it can never be executed by accident.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from mdh_shared import __version__

ALLOWED_HOSTS = frozenset({
    # Mojang
    "piston-meta.mojang.com", "piston-data.mojang.com", "launchermeta.mojang.com",
    "launcher.mojang.com",
    # PaperMC (Fill v3)
    "fill.papermc.io", "fill-data.papermc.io",
    # Fabric
    "meta.fabricmc.net", "maven.fabricmc.net",
    # Eclipse Adoptium (Temurin) and the GitHub release storage it redirects to
    "api.adoptium.net", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    # Modrinth: its API (search, versions) and its CDN (mods and plugins, SHA-512 checked)
    "api.modrinth.com", "cdn.modrinth.com",
})
MAX_JSON_BYTES = 8 * 1024 * 1024
USER_AGENT = f"MinecraftDistributedHosting/{__version__} (private group hosting)"

Progress = Callable[[int, int | None], None]


class DownloadError(Exception):
    pass


class ChecksumMismatch(DownloadError):
    pass


class UntrustedHost(DownloadError):
    pass


def check_url(url: httpx.URL | str) -> None:
    u = httpx.URL(str(url))
    if u.scheme != "https":
        raise UntrustedHost(f"refusing non-HTTPS URL {u}")
    if u.host not in ALLOWED_HOSTS:
        raise UntrustedHost(f"refusing download from untrusted host {u.host}")


async def _check_request(request: httpx.Request) -> None:
    check_url(request.url)


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(30.0, read=60.0),
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_check_request]},
        transport=transport,
    )


async def fetch_json(client: httpx.AsyncClient, url: str, *, sha1: str | None = None) -> Any:
    check_url(url)
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise DownloadError(f"{url}: HTTP {response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_JSON_BYTES:
                raise DownloadError(f"{url}: response too large")
    if sha1 is not None and hashlib.sha1(body, usedforsecurity=False).hexdigest() != sha1.lower():
        raise ChecksumMismatch(f"{url}: SHA-1 mismatch")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise DownloadError(f"{url}: invalid JSON") from exc


async def download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    sha256: str | None = None,
    sha1: str | None = None,
    sha512: str | None = None,
    expected_size: int | None = None,
    max_bytes: int = 512 * 1024 * 1024,
    progress: Progress | None = None,
) -> str:
    """Download ``url`` to ``dest`` and return its SHA-256. Raises on any mismatch."""
    check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h256 = hashlib.sha256()
    h1 = hashlib.sha1(usedforsecurity=False)
    h512 = hashlib.sha512()
    received = 0
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise DownloadError(f"{url}: HTTP {response.status_code}")
            total = expected_size or (int(response.headers["content-length"])
                                      if "content-length" in response.headers else None)
            with part.open("wb") as fh:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    received += len(chunk)
                    if received > max_bytes or (expected_size is not None and received > expected_size):
                        raise DownloadError(f"{url}: larger than expected")
                    h256.update(chunk)
                    h1.update(chunk)
                    h512.update(chunk)
                    fh.write(chunk)
                    if progress:
                        progress(received, total)
                fh.flush()
                os.fsync(fh.fileno())
        if expected_size is not None and received != expected_size:
            raise DownloadError(f"{url}: got {received} bytes, expected {expected_size}")
        if sha256 is not None and h256.hexdigest() != sha256.lower():
            raise ChecksumMismatch(f"{url}: SHA-256 mismatch")
        if sha1 is not None and h1.hexdigest() != sha1.lower():
            raise ChecksumMismatch(f"{url}: SHA-1 mismatch")
        if sha512 is not None and h512.hexdigest() != sha512.lower():
            raise ChecksumMismatch(f"{url}: SHA-512 mismatch")
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return h256.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()
