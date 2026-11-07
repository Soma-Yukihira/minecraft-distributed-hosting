"""Install what a server needs on this PC: a Java runtime and a verified server jar."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from mdh_client.config import ClientSettings, Layout
from mdh_client.minecraft.downloads import ChecksumMismatch, DownloadError, download, sha256_file
from mdh_client.minecraft.sources import Artifact, ServerDistribution, SourceResolver
from mdh_client.system.host_os import JavaTarget, java_target
from mdh_shared.models.enums import Loader
from mdh_shared.paths import UnsafePathError, validate_relative_path

MAX_RUNTIME_UNPACKED = 1024**3
MAX_RUNTIME_ENTRIES = 20_000

Status = Callable[[str, float | None], None]


@dataclass(frozen=True)
class PreparedServer:
    java: Path
    jar: Path
    distribution: ServerDistribution


def safe_extract_zip(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest`` rejecting zip-slip, links, bombs and odd names."""
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_RUNTIME_ENTRIES:
            raise DownloadError("archive has too many entries")
        if sum(m.file_size for m in members) > MAX_RUNTIME_UNPACKED:
            raise DownloadError("archive expands to more than 1 GiB")
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        for m in members:
            name = m.filename.rstrip("/")
            if not name:
                continue
            try:
                validate_relative_path(name)
            except UnsafePathError as exc:
                raise DownloadError(f"unsafe path in archive: {m.filename!r}") from exc
            mode = m.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise DownloadError(f"symbolic link in archive: {m.filename!r}")
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise DownloadError(f"archive entry escapes destination: {m.filename!r}")
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)


def safe_extract_tar(archive: Path, dest: Path) -> None:
    """Extract a ``.tar.gz`` (Java for macOS) into ``dest``: same bounds as :func:`safe_extract_zip`.

    Links stay allowed, only inside ``dest`` (a macOS Java has some): Python's "data" filter
    refuses absolute paths, ``..``, links leading out, devices, and drops set-user-ID bits."""
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if len(members) > MAX_RUNTIME_ENTRIES:
            raise DownloadError("archive has too many entries")
        if sum(m.size for m in members if m.isfile()) > MAX_RUNTIME_UNPACKED:
            raise DownloadError("archive expands to more than 1 GiB")
        for m in members:  # same names as in a zip: no absolute path (the filter would strip it)
            name = m.name.rstrip("/")
            if name and name != ".":
                try:
                    validate_relative_path(name)
                except UnsafePathError as exc:
                    raise DownloadError(f"unsafe path in archive: {m.name!r}") from exc
        dest.mkdir(parents=True, exist_ok=True)
        try:
            tf.extractall(dest, filter="data")
        except tarfile.FilterError as exc:
            raise DownloadError(f"unsafe entry in archive: {exc}") from exc


class Installer:
    def __init__(self, layout: Layout, settings: ClientSettings, client: httpx.AsyncClient,
                 resolver: SourceResolver | None = None, java: JavaTarget | None = None) -> None:
        self.layout = layout
        self.settings = settings
        self.client = client
        self.resolver = resolver or SourceResolver(client)
        self.java_target = java or java_target()

    # ------------------------------------------------------------------ Java

    def java_dir(self, major: int) -> Path:
        return self.layout.runtimes / f"temurin-{major}-jre"

    def installed_java(self, major: int) -> Path | None:
        java = self.java_dir(major) / self.java_target.java
        marker = self.java_dir(major) / "mdh-runtime.json"
        return java if java.is_file() and marker.is_file() else None

    async def ensure_java(self, major: int, status: Status | None = None) -> Path:
        existing = self.installed_java(major)
        if existing:
            return existing
        artifact = await self.resolver.java_runtime(major, self.java_target)
        archive = self.layout.downloads / artifact.filename
        _say(status, f"Téléchargement de Java {major}", 0.0)
        sha = await download(self.client, artifact.url, archive, sha256=artifact.sha256,
                             expected_size=artifact.size, progress=_progress(status, f"Java {major}"))
        _say(status, f"Installation de Java {major}", None)
        staging = self.layout.runtimes / f".staging-{major}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            if artifact.filename.endswith(".tar.gz"):
                safe_extract_tar(archive, staging)
            else:
                safe_extract_zip(archive, staging)
            tops = [p for p in staging.iterdir() if p.is_dir()]
            if len(tops) != 1 or not (tops[0] / self.java_target.java).is_file():
                raise DownloadError("unexpected Java archive layout")
            (tops[0] / "mdh-runtime.json").write_text(
                json.dumps({"major": major, "source": artifact.url, "sha256": sha}), "utf-8")
            target = self.java_dir(major)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tops[0], target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            archive.unlink(missing_ok=True)
        java = self.installed_java(major)
        assert java is not None
        return java

    # ------------------------------------------------------------------ server jar

    async def ensure_jar(self, artifact: Artifact, status: Status | None = None) -> Path:
        expected = artifact.sha256
        if artifact.trust == "pin-on-first-use":
            expected = self.settings.pinned_jar_hashes.get(artifact.url)
        if expected:
            cached = self.layout.jar_cache / f"{expected}.jar"
            if cached.is_file() and sha256_file(cached) == expected:
                return cached
        tmp = self.layout.downloads / (artifact.filename + ".download")
        _say(status, f"Téléchargement de {artifact.filename}", 0.0)
        sha = await download(self.client, artifact.url, tmp, sha256=expected, sha1=artifact.sha1,
                             expected_size=artifact.size, progress=_progress(status, artifact.filename))
        if artifact.trust == "pin-on-first-use" and artifact.url not in self.settings.pinned_jar_hashes:
            # No checksum published: remember this one so any later change is detected.
            self.settings.pinned_jar_hashes[artifact.url] = sha
            self.settings.save(self.layout.settings_file)
        final = self.layout.jar_cache / f"{sha}.jar"
        os.replace(tmp, final)
        return final

    async def prepare(self, loader: Loader, mc_version: str, status: Status | None = None) -> PreparedServer:
        _say(status, "Recherche des fichiers officiels", None)
        dist = await self.resolver.resolve(loader, mc_version)
        java = await self.ensure_java(dist.java_major, status)
        try:
            jar = await self.ensure_jar(dist.jar, status)
        except ChecksumMismatch:
            if dist.jar.trust == "pin-on-first-use":
                raise DownloadError(
                    f"le fichier {dist.jar.filename} a changé depuis son premier téléchargement : "
                    "lancement refusé par sécurité") from None
            raise
        return PreparedServer(java=java, jar=jar, distribution=dist)


def _say(status: Status | None, message: str, fraction: float | None) -> None:
    if status:
        status(message, fraction)


def _progress(status: Status | None, label: str):
    if status is None:
        return None

    def report(done: int, total: int | None) -> None:
        status(f"Téléchargement de {label}", (done / total) if total else None)

    return report
