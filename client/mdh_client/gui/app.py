"""Application entry point."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, QTranslator
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from mdh_client.application.agent import HostAgent
from mdh_client.application.runtime import AgentRuntime
from mdh_client.config import ClientSettings, Layout, app_home
from mdh_client.gui.bridge import Bridge
from mdh_client.gui.dialogs import LoginDialog
from mdh_client.gui.main_window import MainWindow, app_icon
from mdh_client.gui.theme import STYLESHEET
from mdh_client.system import autostart


def setup_logging(layout: Layout) -> None:
    handler = logging.handlers.RotatingFileHandler(layout.logs / "client.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


#: Named mutex the installer waits on (Inno Setup ``AppMutex``): an update or an uninstall never
#: kills a running app, which could stop Minecraft without its final backup.
RUNNING_MUTEX = "MinecraftDistributedHostingRunning"


def announce_running() -> object | None:
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.kernel32.CreateMutexW(None, False, RUNNING_MUTEX)  # released at exit


def main() -> int:
    layout = Layout(app_home())
    layout.ensure()
    setup_logging(layout)
    app = QApplication(sys.argv)
    app.setApplicationName("Minecraft Distributed Hosting")
    app.setWindowIcon(app_icon())
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)  # the tray icon keeps the agent alive
    # Qt's own buttons and messages ("Cancel", "Save"...) in French, like the rest of the app.
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.French), "qtbase", "_",
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)

    # One instance per Windows user: two agents would fight over the same PC identity.
    _running_mutex = announce_running()  # kept open until exit: the installer waits for it
    lock = QLockFile(str(layout.home / "instance.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "Déjà ouvert", "L'application est déjà lancée (icône près de l'horloge).")
        return 0

    settings = ClientSettings.load(layout.settings_file)
    if settings.start_with_windows and autostart.available():
        with contextlib.suppress(OSError):
            autostart.set_enabled(True)  # keeps the path right after an update or a reinstall
    runtime = AgentRuntime()
    bridge = Bridge(runtime)

    async def build() -> HostAgent:
        return HostAgent(layout, settings, emit=bridge.emit_from_agent, ask_offer=bridge.ask_offer)

    agent: HostAgent = runtime.call(build())
    try:
        user = runtime.call(agent.restore_session(), timeout=30)
    except Exception:  # noqa: BLE001 - offline at startup: just ask to log in
        user = None
    if user is None and LoginDialog(bridge, agent).exec() != QDialog.DialogCode.Accepted:
        runtime.call(agent.close(), timeout=30)
        runtime.stop()
        return 0

    window = MainWindow(bridge, agent)
    if autostart.MINIMIZED_FLAG not in sys.argv[1:]:
        window.show()  # started with Windows: stay in the notification area
    code = app.exec()
    runtime.stop()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
