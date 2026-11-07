"""Dark, calm theme. Status colours follow one convention everywhere."""

COLORS = {
    "bg": "#15171c",
    "panel": "#1d2027",
    "card": "#242832",
    "border": "#323745",
    "text": "#e7e9ee",
    "muted": "#9aa1b2",
    "accent": "#5b9cf6",
    "ok": "#3fb27f",
    "busy": "#d9a441",
    "warn": "#e08a3c",
    "error": "#e35d5d",
    "neutral": "#7d8496",
}

STYLESHEET = f"""
QWidget {{ background: {COLORS['bg']}; color: {COLORS['text']}; font-family: 'Segoe UI'; font-size: 10pt; }}
QDialog, QMainWindow {{ background: {COLORS['bg']}; }}
#Sidebar {{ background: {COLORS['panel']}; border: none; padding-top: 12px; outline: 0; }}
#Sidebar::item {{ padding: 12px 18px; border-radius: 8px; margin: 2px 8px; color: {COLORS['muted']}; }}
#Sidebar::item:selected {{ background: {COLORS['card']}; color: {COLORS['text']}; }}
#Sidebar::item:hover {{ color: {COLORS['text']}; }}
#Header {{ background: {COLORS['panel']}; border-bottom: 1px solid {COLORS['border']}; }}
#Header QLabel {{ background: transparent; color: {COLORS['muted']}; }}
#PageTitle {{ font-size: 16pt; font-weight: 600; background: transparent; }}
#Muted, QLabel[muted="true"] {{ color: {COLORS['muted']}; background: transparent; }}
#Card {{ background: {COLORS['card']}; border: 1px solid {COLORS['border']}; border-radius: 10px; }}
#Card QLabel {{ background: transparent; }}
#CardTitle {{ color: {COLORS['muted']}; font-size: 9pt; }}
#CardValue {{ font-size: 13pt; font-weight: 600; }}
#Banner {{ background: #3a2323; border: 1px solid {COLORS['error']}; border-radius: 10px; }}
#Banner QLabel {{ background: transparent; }}
#Info {{ background: #1f2a3a; border: 1px solid {COLORS['accent']}; border-radius: 10px; }}
#Info QLabel {{ background: transparent; }}
QPushButton {{ background: {COLORS['card']}; border: 1px solid {COLORS['border']}; border-radius: 8px;
              padding: 8px 16px; }}
QPushButton:hover {{ border-color: {COLORS['accent']}; }}
QPushButton:disabled {{ color: {COLORS['neutral']}; border-color: {COLORS['card']}; }}
QPushButton#Primary {{ background: {COLORS['accent']}; border: none; color: #0b1220; font-weight: 600; }}
QPushButton#Primary:disabled {{ background: {COLORS['card']}; color: {COLORS['neutral']}; }}
QPushButton#Danger {{ background: transparent; border: 1px solid {COLORS['error']}; color: {COLORS['error']}; }}
QPushButton#Danger:disabled {{ border-color: {COLORS['card']}; color: {COLORS['neutral']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {COLORS['border']}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {COLORS['neutral']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {COLORS['neutral']}; border-radius: 4px;
                        background: {COLORS['panel']}; }}
QCheckBox::indicator:checked {{ background: {COLORS['accent']}; border-color: {COLORS['accent']}; }}
QCheckBox::indicator:disabled {{ border-color: {COLORS['border']}; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTableWidget, QListWidget {{
    background: {COLORS['panel']}; border: 1px solid {COLORS['border']}; border-radius: 6px; padding: 6px; }}
QPlainTextEdit#Console {{ font-family: 'Cascadia Mono', 'Consolas'; font-size: 9pt; }}
QHeaderView::section {{ background: {COLORS['panel']}; color: {COLORS['muted']}; border: none; padding: 6px; }}
QTableWidget {{ gridline-color: {COLORS['border']}; }}
QProgressBar {{ background: {COLORS['panel']}; border: 1px solid {COLORS['border']}; border-radius: 6px;
               text-align: center; height: 18px; }}
QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 6px; }}
QCheckBox {{ background: transparent; spacing: 8px; }}
QGroupBox {{ border: 1px solid {COLORS['border']}; border-radius: 10px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {COLORS['muted']}; }}
QSlider::groove:horizontal {{ height: 6px; background: {COLORS['panel']}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {COLORS['accent']}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QStatusBar {{ background: {COLORS['panel']}; color: {COLORS['muted']}; }}
QToolTip {{ background: {COLORS['card']}; color: {COLORS['text']}; border: 1px solid {COLORS['border']}; }}
"""


def pill_style(kind: str) -> str:
    color = COLORS.get(kind, COLORS["neutral"])
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    # Qt reads 8-digit hex as #AARRGGBB, so the translucent fill is written as rgba().
    return (f"background: rgba({r}, {g}, {b}, 40); color: {color}; border: 1px solid {color};"
            " border-radius: 10px; padding: 3px 10px; font-weight: 600;")
