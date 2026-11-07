"""The server's resource pack (docs/ARCHITECTURE.md 3.10).

Minecraft itself downloads the pack when a player joins, from the address written in
``server.properties``, and checks it against its SHA-1. The pack never goes through the VPS
nor the hosting PC. It comes either from Modrinth (address and SHA-1 given by Modrinth's API)
or from a direct HTTPS link: the admin's PC then downloads it once, to compute its SHA-1 and
check that it is really a resource pack, and stores only that in the server's configuration.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from mdh_client.minecraft.downloads import USER_AGENT
from mdh_client.minecraft.modrinth import ModrinthError
from mdh_shared.models.api import MAX_PACK_BYTES, ResourcePack


class PackError(ModrinthError):
    """Shown to the user as it is (French)."""


async def _https_only(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise PackError("Le lien doit commencer par https:// (Minecraft refuse les autres).")


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Any HTTPS host: the file is only hashed and opened as a zip, never run."""
    return httpx.AsyncClient(follow_redirects=True, max_redirects=5, timeout=httpx.Timeout(30.0, read=60.0),
                             headers={"User-Agent": USER_AGENT}, event_hooks={"request": [_https_only]},
                             transport=transport)


def _name_from(url: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name).removesuffix(".zip").strip()
    name = "".join(ch for ch in name if ch.isprintable())[:100].strip()
    return name or "Pack de ressources"


async def from_url(client: httpx.AsyncClient, url: str, required: bool) -> ResourcePack:
    """Download the pack once to describe it. Raises PackError with a readable reason."""
    url = url.strip()
    if not url.startswith("https://") or len(url) > 1000 or any(ch.isspace() for ch in url):
        raise PackError("Le lien doit commencer par https:// et ne contenir aucun espace.")
    sha1 = hashlib.sha1(usedforsecurity=False)  # what Minecraft checks the pack with
    size = 0
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise PackError(f"Le lien ne donne pas le fichier (HTTP {response.status_code}).")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PACK_BYTES:
                            raise PackError("Le pack dépasse 250 Mo : Minecraft le refuserait.")
                        sha1.update(chunk)
                        out.write(chunk)
            except httpx.HTTPError as exc:
                raise PackError(f"Le lien ne répond pas ({exc.__class__.__name__}).") from exc
        if not size:
            raise PackError("Le lien donne un fichier vide.")
        try:
            with zipfile.ZipFile(tmp) as archive:
                if "pack.mcmeta" not in archive.namelist():
                    raise PackError("Ce fichier n'est pas un pack de ressources (pas de pack.mcmeta à la racine du "
                                    "zip).")
        except zipfile.BadZipFile as exc:
            raise PackError("Ce lien ne donne pas un fichier .zip. Pour Dropbox ou Google Drive, utilise un lien "
                            "de téléchargement direct.") from exc
    finally:
        os.unlink(tmp)
    return ResourcePack(name=_name_from(url), url=url, sha1=sha1.hexdigest(), size=size, required=required)


def properties(pack: dict | None) -> dict[str, str]:
    """server.properties keys for the pack (cleared when there is none)."""
    if not pack:
        return {"resource-pack": "", "resource-pack-sha1": "", "require-resource-pack": "false",
                "resource-pack-prompt": ""}
    prompt = ("Ce serveur utilise un pack de ressources : il est nécessaire pour jouer." if pack.get("required")
              else "Ce serveur propose un pack de ressources.")
    return {"resource-pack": pack["url"], "resource-pack-sha1": pack["sha1"],
            "require-resource-pack": "true" if pack.get("required") else "false",
            "resource-pack-prompt": '{"text":"' + prompt + '"}'}
