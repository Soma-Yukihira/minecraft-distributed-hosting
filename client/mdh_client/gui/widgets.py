"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from mdh_client.gui.theme import pill_style


class Card(QFrame):
    def __init__(self, title: str, value: str = "—", hint: str = "") -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        # Plain text: values often come from other users (server, PC and player names).
        self.value = QLabel(value)
        self.value.setObjectName("CardValue")
        self.value.setWordWrap(True)
        self.value.setTextFormat(Qt.TextFormat.PlainText)
        self.hint = QLabel(hint)
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.setVisible(bool(hint))
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.hint)

    def set(self, value: str, hint: str | None = None) -> None:
        self.value.setText(value)
        if hint is not None:
            self.hint.setText(hint)
            self.hint.setVisible(bool(hint))


class Pill(QLabel):
    def __init__(self, text: str = "", kind: str = "neutral") -> None:
        super().__init__()
        self.set(text, kind)

    def set(self, text: str, kind: str) -> None:
        self.setText(text)
        self.setStyleSheet(pill_style(kind))


class Banner(QFrame):
    """Highlighted message with optional action widgets on the right."""

    def __init__(self, kind: str = "Banner") -> None:
        super().__init__()
        self.setObjectName(kind)
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(16, 12, 16, 12)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.layout_.addWidget(self.text, 1)

    def add_action(self, widget: QWidget) -> None:
        self.layout_.addWidget(widget)


def page_title(text: str, subtitle: str = "") -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 8)
    title = QLabel(text)
    title.setObjectName("PageTitle")
    layout.addWidget(title)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return box


def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{n} o"


class LineChart(QFrame):
    """A small time chart drawn by hand: the average as a line, the worst moment as a faint one
    (docs/ARCHITECTURE.md 4.17). Gaps (server stopped) are left blank."""

    def __init__(self, title: str, unit: str, color: str, fixed_max: float | None = None,
                 decimals: int = 1) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumHeight(150)
        self.title, self.unit, self.color, self.fixed_max, self.decimals = title, unit, color, fixed_max, decimals
        self.points: list[tuple[float, float | None, float | None]] = []
        self.span: tuple[float, float] = (0.0, 1.0)
        self.step = 60.0

    def set_series(self, points: list[tuple[float, float | None, float | None]], span: tuple[float, float],
                   step: float) -> None:
        """``points``: (time, average, worst) — worst may be None."""
        self.points, self.span, self.step = points, span, step
        self.update()

    def _fmt(self, value: float) -> str:
        return f"{value:.{self.decimals}f}".replace(".", ",") + (f" {self.unit}" if self.unit else "")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt name
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

        from mdh_client.gui.theme import COLORS

        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        values = [v for _, v, _ in self.points if v is not None]
        last = next((v for _, v, _ in reversed(self.points) if v is not None), None)
        p.setPen(QColor(COLORS["muted"]))
        p.drawText(QRectF(14, 8, w - 28, 18), Qt.AlignmentFlag.AlignLeft, self.title)
        p.setPen(QColor(COLORS["text"]))
        p.drawText(QRectF(14, 8, w - 28, 18), Qt.AlignmentFlag.AlignRight,
                   self._fmt(last) if last is not None else "—")
        plot = QRectF(14, 32, w - 28, h - 56)
        p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.drawLine(plot.bottomLeft(), plot.bottomRight())
        if not values:
            p.setPen(QColor(COLORS["muted"]))
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Pas encore de mesure sur cette période")
            return
        worst = [x for _, _, x in self.points if x is not None]
        top = self.fixed_max or max(values + worst) * 1.15 or 1.0
        t0, t1 = self.span
        x_of = lambda t: plot.left() + (t - t0) / max(1.0, t1 - t0) * plot.width()  # noqa: E731
        y_of = lambda v: plot.bottom() - min(v, top) / top * plot.height()  # noqa: E731

        def path(index: int) -> QPainterPath:
            line = QPainterPath()
            previous = None
            for point in self.points:
                t, v = point[0], point[index]
                if v is None:
                    previous = None
                    continue
                if previous is None or t - previous > 2.5 * self.step:
                    line.moveTo(QPointF(x_of(t), y_of(v)))
                else:
                    line.lineTo(QPointF(x_of(t), y_of(v)))
                previous = t
            return line

        faint = QColor(self.color)
        faint.setAlpha(90)
        p.setPen(QPen(faint, 1))
        p.drawPath(path(2))
        p.setPen(QPen(QColor(self.color), 2))
        p.drawPath(path(1))
        p.setPen(QColor(COLORS["muted"]))
        p.drawText(QRectF(plot.left(), plot.top() - 2, 80, 14), Qt.AlignmentFlag.AlignLeft, self._fmt(top))
        from datetime import datetime
        fmt = "%H:%M" if t1 - t0 <= 24 * 3600 else "%d/%m"
        for t, align in ((t0, Qt.AlignmentFlag.AlignLeft), (t1, Qt.AlignmentFlag.AlignRight)):
            p.drawText(QRectF(plot.left(), plot.bottom() + 4, plot.width(), 16), align,
                       datetime.fromtimestamp(t).strftime(fmt))
