"""What differs between Windows and macOS (Apple chips): one place for the platform choices.

Every function takes the platform as an argument (``sys.platform`` by default) so that the macOS
choices are tested on Windows too.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

#: Platforms the application is published for (``ClientRelease.platform``).
WINDOWS = "windows"
MACOS = "macos-arm64"


def is_mac(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def release_platform(platform: str | None = None) -> str:
    """Which published release this copy of the application installs."""
    return MACOS if is_mac(platform) else WINDOWS


@dataclass(frozen=True)
class JavaTarget:
    """Eclipse Temurin build for this computer (api.adoptium.net)."""

    os: str
    architecture: str
    package_suffix: str
    #: The ``java`` program, inside the archive's single top folder.
    java: PurePosixPath


def java_target(platform: str | None = None) -> JavaTarget:
    if is_mac(platform):
        return JavaTarget("mac", "aarch64", ".tar.gz", PurePosixPath("Contents/Home/bin/java"))
    return JavaTarget("windows", "x64", ".zip", PurePosixPath("bin/java.exe"))
