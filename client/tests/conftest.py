"""Shared by the client tests."""

import gc
import sys


def pytest_sessionfinish(session, exitstatus):
    """Destroy the windows and dialogs the tests left open while Qt still runs.

    Left to Python's own shutdown, they were freed in no particular order after the application
    object, and the process crashed once every test had passed (exit code 3 or 1, seen with
    QT_QPA_PLATFORM=offscreen, as on GitHub)."""
    widgets = sys.modules.get("PySide6.QtWidgets")
    app = widgets.QApplication.instance() if widgets is not None else None
    if app is None:
        return
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()
