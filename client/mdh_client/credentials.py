"""Refresh token at rest.

* Windows: encrypted with DPAPI (bound to the Windows user account). Another Windows account on
  the same PC, or a copy of the file on another PC, cannot decrypt it.
* macOS: a file only its owner may read (mode 0600), in the user's Library, like SSH keys. Not the
  Keychain: the application is not signed by Apple, so macOS would ask again for access after
  every update. FileVault encrypts it with the rest of the disk.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

_ENTROPY = b"mdh-refresh-token-v1"


def _call(fn_name: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    fn = getattr(crypt32, fn_name)
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    src, _keep1 = _blob(data)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, CRYPTPROTECT_UI_FORBIDDEN,
              ctypes.byref(out)):
        raise OSError(f"{fn_name} failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes, platform: str | None = None) -> bytes:
    if (platform or sys.platform) == "darwin":
        return data  # protected by the file's permissions (see the module's docstring)
    return _call("CryptProtectData", data)


def unprotect(data: bytes, platform: str | None = None) -> bytes:
    if (platform or sys.platform) == "darwin":
        return data
    return _call("CryptUnprotectData", data)


def store_refresh_token(path: Path, token: str, platform: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    # Created owner-only from the start: never readable by others, not even for a moment.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(protect(token.encode("utf-8"), platform))
    os.replace(tmp, path)


def load_refresh_token(path: Path, platform: str | None = None) -> str | None:
    try:
        return unprotect(path.read_bytes(), platform).decode("utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None


def forget_refresh_token(path: Path) -> None:
    path.unlink(missing_ok=True)
