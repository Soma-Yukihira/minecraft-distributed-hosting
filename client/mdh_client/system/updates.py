"""Automatic updates of the application (docs/ARCHITECTURE.md 4.8).

The control plane only distributes installers. Each one is signed on the developer's PC with an
Ed25519 key that never leaves it (deployment/windows/sign_release.py), and this module runs an
installer only if its signature matches one of :data:`TRUSTED_KEYS`: whoever takes over the VPS
still cannot push a program to the group's PCs. A release must also be newer than the running
version, so an old (signed) installer cannot be replayed to bring back a fixed bug.

Installing, once the application has quit:
* Windows: Inno Setup in silent mode. The installer waits for the application to be gone, replaces
  the program and starts it again (deployment/windows/installer.iss).
* macOS: a short shell script (:data:`MAC_INSTALL_SCRIPT`) waits for the application to be gone,
  unpacks the zipped application next to the old one with ``ditto``, swaps them (the old one is
  put back if anything fails) and opens the new one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from mdh_client.system.host_os import release_platform
from mdh_shared import __version__, versions
from mdh_shared.models.api import ClientRelease, release_file_name, release_message

if TYPE_CHECKING:
    from mdh_client.networking.api_client import ApiClient

log = logging.getLogger("mdh.updates")

#: Public halves of the developer's update keys (raw Ed25519, base64). Several may be listed
#: while moving to a new key; a release signed by any of them is accepted.
TRUSTED_KEYS: tuple[str, ...] = (
    "YOilJ86js9f+3TlBsEt5c7zvMgY9DZM5y78qNEEUUgU=",  # created 2026-11-01
)

#: Since 0.12.0 the control plane announces a new release the moment it is published
#: (``ClientReleaseNotice``); the regular checks only catch what that message missed (an older
#: control plane, the application offline at that moment).
FIRST_CHECK_AFTER_S = 20.0
CHECK_EVERY_S = 3600.0
RETRY_AFTER_S = 600.0
#: After an announcement, each application waits a random moment up to this long, so that the
#: whole group does not download 40 MB from the VPS in the same second.
ANNOUNCE_SPREAD_S = 30.0

Relaunch = Literal["show", "tray"]


class UpdateError(Exception):
    pass


def is_newer(candidate: str, current: str) -> bool:
    return versions.older_than(current, candidate)


def verify_signature(release: ClientRelease, keys: tuple[str, ...] = TRUSTED_KEYS) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    message = release_message(release.version, release.sha256, release.size, release.platform)
    signature = base64.b64decode(release.signature, validate=True)
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(signature, message)
            return
        except InvalidSignature:
            continue
    raise UpdateError(f"version {release.version}: signature non reconnue, mise à jour ignorée")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ReadyUpdate:
    """A newer release, downloaded and verified, waiting for a good moment to be installed."""

    version: str
    notes: str | None
    installer: Path
    sha256: str
    size: int


class Updater:
    def __init__(self, folder: Path, api: Callable[[], ApiClient], *, current: str = __version__,
                 keys: tuple[str, ...] | None = None, platform: str | None = None,
                 on_ready: Callable[[ReadyUpdate], None] = lambda _: None) -> None:
        self.folder = folder
        self._api = api  # the agent replaces its client when the address changes
        self.current = current
        self.platform = platform or release_platform()
        self.keys = TRUSTED_KEYS if keys is None else keys
        self._on_ready = on_ready
        self.ready: ReadyUpdate | None = None
        #: One check at a time (the button, an announcement and the hourly check may meet).
        self._lock = asyncio.Lock()

    async def check(self) -> ReadyUpdate | None:
        """Ask the control plane, download and verify a newer release if there is one."""
        async with self._lock:
            return await self._check()

    async def _check(self) -> ReadyUpdate | None:
        raw = await self._api().client_release(self.platform)
        if raw is None:
            return self._set_ready(None)
        try:
            release = ClientRelease.model_validate(raw)
        except ValidationError as exc:
            raise UpdateError(f"description de mise à jour invalide : {exc.error_count()} erreur(s)") from exc
        if (release.platform or "windows") != self.platform:
            raise UpdateError(f"version {release.version}: publiée pour {release.platform or 'windows'}, "
                              "pas pour ce système")
        if not is_newer(release.version, self.current):
            return self._set_ready(None)
        if self.ready and self.ready.version == release.version and self.ready.sha256 == release.sha256:
            return self.ready
        # Before downloading anything: an unsigned release is not worth 40 MB.
        verify_signature(release, self.keys)
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / release_file_name(release.version, self.platform)
        if not (target.exists() and target.stat().st_size == release.size
                and await asyncio.to_thread(file_sha256, target) == release.sha256):
            part = target.with_name(target.name + ".part")
            log.info("downloading version %s (%d bytes)", release.version, release.size)
            size, sha256 = await self._api().download_client_installer(part, release.size, self.platform)
            if size != release.size or sha256 != release.sha256:
                part.unlink(missing_ok=True)
                raise UpdateError(f"version {release.version}: fichier téléchargé différent de la signature")
            os.replace(part, target)
        return self._set_ready(ReadyUpdate(release.version, release.notes, target,
                                           release.sha256, release.size))

    def _set_ready(self, ready: ReadyUpdate | None) -> ReadyUpdate | None:
        changed = ready != self.ready
        self.ready = ready
        self._clean(keep=ready.installer if ready else None)
        if ready and changed:
            log.info("version %s ready to install", ready.version)
            self._on_ready(ready)
        return ready

    def _clean(self, keep: Path | None) -> None:
        """Installers of older or withdrawn releases, interrupted downloads."""
        if not self.folder.is_dir():
            return
        for path in self.folder.iterdir():
            if path != keep and path.name.startswith("MinecraftDistributedHosting-"):
                try:
                    path.unlink()
                except OSError:
                    pass  # still open by a finishing installer: next time

    async def run(self) -> None:
        """Check now and then for the life of the session. Never raises."""
        from mdh_client.networking.api_client import ApiError

        await asyncio.sleep(FIRST_CHECK_AFTER_S)
        while True:
            delay = CHECK_EVERY_S
            try:
                await self.check()
            except (ApiError, UpdateError, OSError) as exc:
                log.warning("update check failed: %s", exc)
                delay = RETRY_AFTER_S
            await asyncio.sleep(delay)

    def verify_ready(self) -> ReadyUpdate:
        """Right before running it: the installer on disk is still the one that was verified."""
        ready = self.ready
        if ready is None:
            raise UpdateError("aucune mise à jour prête")
        if not ready.installer.exists() or file_sha256(ready.installer) != ready.sha256:
            self.ready = None
            raise UpdateError("l'installateur téléchargé a changé : il sera téléchargé à nouveau")
        return ready

    @staticmethod
    def launch(ready: ReadyUpdate, *, relaunch: Relaunch, log_file: Path) -> None:
        """Start the installer, detached: it waits for this process to exit before installing."""
        if sys.platform == "darwin":
            _launch_mac(ready, relaunch=relaunch, log_file=log_file)
            return
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(  # noqa: S603 - a signed installer, verified just before
            [str(ready.installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL",
             f"/RELAUNCH={relaunch}", f"/LOG={log_file}"],
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


#: Run by /bin/sh, detached: $1 application pid, $2 verified zip, $3 the .app to replace,
#: $4 "tray" to start in the menu bar only. Nothing is downloaded or trusted here: the zip was
#: checked against the signed SHA-256 right before.
MAC_INSTALL_SCRIPT = """set -u
pid="$1"; zip="$2"; app="$3"; relaunch="$4"
start() {
    if [ "$relaunch" = tray ]; then /usr/bin/open "$app" --args --minimized; else /usr/bin/open "$app"; fi
}
# Whatever goes wrong, the application that was there starts again.
fail() { echo "update not installed: $1"; start; exit 1; }
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
work="$(mktemp -d "${TMPDIR:-/tmp}/mdh-update.XXXXXX")" || fail "no temporary folder"
trap 'rm -rf "$work"' EXIT
/usr/bin/ditto -x -k "$zip" "$work" || fail "cannot unpack"
new="$work/$(basename "$app")"
[ -x "$new/Contents/MacOS/MinecraftDistributedHosting" ] || fail "unexpected archive"
old="$app.previous"
rm -rf "$old"
mv "$app" "$old" || fail "cannot move the application (rights on its folder?)"
if ! mv "$new" "$app"; then mv "$old" "$app"; fail "cannot put the new version in place"; fi
rm -rf "$old"
/usr/bin/xattr -dr com.apple.quarantine "$app" 2>/dev/null
echo "update installed"
start
"""


def mac_bundle(executable: str | None = None) -> Path:
    """The running application: .../Minecraft Distributed Hosting.app/Contents/MacOS/<program>."""
    return Path(executable or sys.executable).resolve().parents[2]


def _launch_mac(ready: ReadyUpdate, *, relaunch: Relaunch, log_file: Path) -> None:
    bundle = mac_bundle()
    if bundle.suffix != ".app":
        raise UpdateError("application non installée : mise à jour impossible depuis cet emplacement")
    script = ready.installer.with_name("install-update.sh")
    script.write_text(MAC_INSTALL_SCRIPT, "utf-8")
    with log_file.open("ab") as log_out:
        subprocess.Popen(  # noqa: S603 - fixed script, a verified archive
            ["/bin/sh", str(script), str(os.getpid()), str(ready.installer), str(bundle), relaunch],
            start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, stdout=log_out, stderr=log_out)

