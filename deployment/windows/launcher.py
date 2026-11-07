"""Entry point of the packaged application, Windows and macOS (PyInstaller).

``--self-test <report.json>`` checks the frozen build without opening a window: every module
imports, Qt's French translations, TLS certificates and the native calls are present. Used by
build.ps1 and deployment/macos/build.sh right after packaging.

``--mdh-watchdog <app pid> <minecraft pid>`` (macOS): the small process that stops Minecraft if
the application dies (mdh_client/system/watchdog.py). No window, no Qt.
"""

from __future__ import annotations

import json
import sys


def self_test(report_path: str) -> int:
    results: dict[str, object] = {}
    try:
        import ssl

        import httpx  # noqa: F401
        import psutil  # noqa: F401
        import websockets  # noqa: F401
        import zstandard  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        from PySide6.QtWidgets import QApplication

        from mdh_client.application.agent import HostAgent  # noqa: F401
        from mdh_client.credentials import load_refresh_token  # noqa: F401
        from mdh_client.gui.main_window import MainWindow  # noqa: F401
        from mdh_client.system import autostart, watchdog  # noqa: F401

        if sys.platform == "win32":
            from mdh_client.minecraft.job import JobObject  # noqa: F401

        app = QApplication.instance() or QApplication(["self-test", "-platform", "offscreen"])
        translator = QTranslator(app)
        results["qt_french"] = translator.load(QLocale(QLocale.Language.French), "qtbase", "_",
                                               QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        results["tls_ca_certs"] = len(ssl.create_default_context().get_ca_certs()) > 0 or bool(
            __import__("certifi").where())
        results["autostart_available"] = autostart.available()
        key = Ed25519PrivateKey.generate()  # the native part checking update signatures
        key.public_key().verify(key.sign(b"self-test"), b"self-test")
        results["update_signatures"] = True
        results["frozen"] = bool(getattr(sys, "frozen", False))
        results["ok"] = all(results[k] for k in ("qt_french", "tls_ca_certs", "autostart_available", "frozen",
                                                   "update_signatures"))
    except Exception as exc:  # noqa: BLE001 - reported to the build script
        results["ok"] = False
        results["error"] = repr(exc)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh)
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--mdh-watchdog":
        from mdh_client.system import watchdog

        sys.exit(watchdog.main(sys.argv[2:]))
    from mdh_client.gui.app import main

    sys.exit(main())
