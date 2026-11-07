"""Sign an installer for the automatic updates (docs/DEPLOYMENT.md, "Mettre à jour
l'application"): the Windows installer, or the zipped macOS application built on a Mac
(deployment/macos/build.sh), signed here because the key never leaves this PC.

    python deployment/windows/sign_release.py keygen
    python deployment/windows/sign_release.py sign dist/installer/MinecraftDistributedHosting-Setup-X.Y.Z.exe [--notes "..."]
    python deployment/windows/sign_release.py sign MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip [--notes "..."]

``keygen`` creates the developer's update key, once, outside the repository
(``%USERPROFILE%\\.mdh-release\\update-signing-key.pem``), and prints its public half for
``TRUSTED_KEYS`` in client/mdh_client/system/updates.py. Keep a copy of the key file somewhere
safe (USB stick): without it, new versions can no longer be installed automatically.

``sign`` writes ``<installer>.json`` next to the installer: version, size, SHA-256 and signature.
Both files are then published on the VPS with ``install.sh publish-client``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Runs from the project without installing it (build.ps1 sets PYTHONPATH, a plain call does not).
_ROOT = Path(__file__).resolve().parents[2]
for _folder in ("shared", "client"):
    if str(_ROOT / _folder) not in sys.path:
        sys.path.insert(0, str(_ROOT / _folder))

from mdh_client.system import updates  # noqa: E402
from mdh_shared.models.api import ClientRelease, release_message  # noqa: E402

DEFAULT_KEY = Path.home() / ".mdh-release" / "update-signing-key.pem"


def public_key(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def keygen(path: Path) -> int:
    if path.exists():
        print(f"La clé existe déjà : {path} (jamais remplacée).", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    print(f"Clé de signature créée : {path}")
    print("Sauvegarde ce fichier en lieu sûr ; ne le mets jamais dans le dépôt Git.")
    print(f"Clé publique (pour TRUSTED_KEYS) : {public_key(private)}")
    return 0


def identify(name: str) -> tuple[str, str | None] | None:
    """(version, platform) from the file name; platform None for Windows (as before 0.9.0)."""
    if match := re.fullmatch(r"MinecraftDistributedHosting-Setup-(\d+\.\d+\.\d+)\.exe", name):
        return match.group(1), None
    if match := re.fullmatch(r"MinecraftDistributedHosting-(\d+\.\d+\.\d+)-(macos-arm64)\.zip", name):
        return match.group(1), match.group(2)
    return None


def sign(installer: Path, key_path: Path, notes: str | None) -> int:
    found = identify(installer.name)
    if found is None:
        print(f"Nom d'installateur inattendu : {installer.name}", file=sys.stderr)
        return 1
    version, platform = found
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        print(f"{key_path} n'est pas une clé Ed25519", file=sys.stderr)
        return 1
    if public_key(private) not in updates.TRUSTED_KEYS:
        # The application would refuse this signature: better to find out now.
        print("Cette clé n'est pas dans TRUSTED_KEYS (client/mdh_client/system/updates.py).", file=sys.stderr)
        return 1
    data = installer.read_bytes()
    sha256, size = hashlib.sha256(data).hexdigest(), len(data)
    signature = base64.b64encode(private.sign(release_message(version, sha256, size, platform))).decode()
    release = ClientRelease(version=version, platform=platform, size=size, sha256=sha256, signature=signature,
                            notes=notes or None)
    updates.verify_signature(release)
    out = installer.with_name(installer.name + ".json")
    out.write_text(json.dumps(release.model_dump(exclude_none=True), indent=2) + "\n", "utf-8")
    print(f"Signature : {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    s = sub.add_parser("sign")
    s.add_argument("installer", type=Path)
    s.add_argument("--notes", default="")
    args = parser.parse_args()
    if args.command == "keygen":
        return keygen(args.key)
    return sign(args.installer, args.key, args.notes.strip())


if __name__ == "__main__":
    sys.exit(main())
