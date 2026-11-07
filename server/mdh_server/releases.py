"""Installers published on the VPS for the automatic updates (docs/API.md section 8 ter).

``install.sh publish-client`` copies the installer and its signed description (``release.json``)
into the updates folder: the Windows one at its root (as before 0.9.0), the macOS one in
``macos-arm64/``. The control plane never signs nor trusts anything itself: it checks that the
file is the one described, then serves both to logged-in users. The application verifies the
developer's signature before installing anything.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from mdh_shared.models.api import CLIENT_PLATFORMS, ClientRelease, release_file_name

log = logging.getLogger("mdh.releases")

MANIFEST = "release.json"


def installer_name(version: str, platform: str = "windows") -> str:
    return release_file_name(version, platform)


def shelves(updates_dir: Path) -> dict[str, ReleaseShelf]:
    """One shelf per system: Windows at the root of the folder, the others in a subfolder."""
    return {p: ReleaseShelf(updates_dir if p == "windows" else updates_dir / p, p) for p in CLIENT_PLATFORMS}


class ReleaseWatcher:
    """Notices a newly published release (0.12.0), so that the applications hear of it at once.

    Polled by the control plane's sweeper: :meth:`ReleaseShelf.current` only stats two files when
    nothing changed. What was already published when the control plane started is not announced
    (the applications check when they start).
    """

    def __init__(self, shelves: dict[str, ReleaseShelf]) -> None:
        self.shelves = shelves
        self._seen: dict[str, tuple[str, str] | None] = {p: self._key(s) for p, s in shelves.items()}

    @staticmethod
    def _key(shelf: ReleaseShelf) -> tuple[str, str] | None:
        found = shelf.current()
        return (found[0].version, found[0].sha256) if found else None

    def poll(self) -> list[tuple[str, ClientRelease]]:
        """Releases published (or replaced) since the last call. Blocking: run in a thread."""
        new = []
        for platform, shelf in self.shelves.items():
            found = shelf.current()
            key = (found[0].version, found[0].sha256) if found else None
            if key != self._seen[platform]:
                self._seen[platform] = key
                if found:
                    new.append((platform, found[0]))
        return new


class ReleaseShelf:
    """The published release, re-read whenever its files change on disk."""

    def __init__(self, folder: Path, platform: str = "windows") -> None:
        self.folder = folder
        self.platform = platform
        self._lock = threading.Lock()
        self._cached: tuple[tuple, ClientRelease, Path] | None = None

    def current(self) -> tuple[ClientRelease, Path] | None:
        """The release and its installer, or None (nothing published, or files inconsistent).

        Hashes the installer the first time: call it from a worker thread.
        """
        manifest = self.folder / MANIFEST
        try:
            mstat = manifest.stat()
            release = ClientRelease.model_validate_json(manifest.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            log.error("unreadable %s: %s", manifest, exc)
            return None
        if (release.platform or "windows") != self.platform:
            log.error("%s describes a %s release, not %s: not distributed", manifest, release.platform, self.platform)
            return None
        installer = self.folder / installer_name(release.version, self.platform)
        try:
            istat = installer.stat()
        except OSError:
            log.error("%s describes %s, which is missing", manifest, installer.name)
            return None
        key = (mstat.st_mtime_ns, mstat.st_size, istat.st_mtime_ns, istat.st_size)
        with self._lock:
            if self._cached and self._cached[0] == key:
                return self._cached[1], self._cached[2]
            digest = hashlib.sha256()
            with installer.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    digest.update(chunk)
            if istat.st_size != release.size or digest.hexdigest() != release.sha256:
                log.error("%s does not match %s: not distributed", installer.name, MANIFEST)
                return None
            release = release.model_copy(
                update={"published_at": datetime.fromtimestamp(mstat.st_mtime, UTC)})
            self._cached = (key, release, installer)
            log.info("client release %s for %s available (%d bytes)", release.version, self.platform, release.size)
            return release, installer
