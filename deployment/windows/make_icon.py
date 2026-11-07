"""Render the application icon (the same drawing as the window icon) into a Windows .ico, or a
1024-pixel .png that deployment/macos/build.sh turns into the macOS .icns."""

from __future__ import annotations

import sys

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication, QImage, QImageWriter, QPainter

from mdh_client.gui.main_window import app_icon


def main(target: str) -> int:
    app = QGuiApplication(["make-icon", "-platform", "offscreen"])  # noqa: F841 - needed by QPainter
    png = target.lower().endswith(".png")
    size = 1024 if png else 256
    source = app_icon().pixmap(QSize(64, 64)).toImage()
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(image.rect(), source)
    painter.end()
    writer = QImageWriter(target, b"png" if png else b"ico")
    if not writer.write(image):
        print(f"cannot write {target}: {writer.errorString()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
