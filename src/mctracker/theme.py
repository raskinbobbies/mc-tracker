"""Original bright theme: sky-blue backdrop, cream parchment cards with warm
wood-brown edges, orange "candy" buttons. Inspired by the cheerful palette of
classic side-scroller MMO title screens; no game art is used."""

from __future__ import annotations

from pathlib import Path

SKY_TOP = "#7cc8f7"
SKY_BOTTOM = "#d8f0ff"
CREAM = "#fff8e8"
CREAM_ALT = "#fff0d2"
WOOD = "#d9a15b"
WOOD_DARK = "#a8692c"
INK = "#5b3a17"  # main text: dark cocoa
INK_MUTED = "#9b7650"
ORANGE = "#ff8a1f"
ORANGE_LIGHT = "#ffb54d"
ORANGE_DARK = "#d9661a"
LEAF = "#5bb83a"
SELECT = "#ffd98a"
GOLD = "#ff7a00"

CHEVRON = (Path(__file__).parent / "res" / "chevron.svg").as_posix()

FONT = "'Segoe UI Rounded', 'Nunito', 'Segoe UI', sans-serif"

GUI_QSS = f"""
* {{ font-family: {FONT}; font-size: 13px; color: {INK}; }}
QMainWindow, QWidget#root {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {SKY_TOP}, stop:1 {SKY_BOTTOM});
}}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {INK_MUTED}; }}
QLabel#h1 {{ font-size: 24px; font-weight: 800; color: {GOLD}; }}
QLabel#h2 {{ font-size: 15px; font-weight: 700; }}
QLabel#brand {{ font-size: 20px; font-weight: 800; color: white; }}
QLabel#section {{ color: {WOOD_DARK}; font-size: 11px; font-weight: 700; letter-spacing: 1px; margin-top: 8px; }}
QFrame#stat {{ background: white; border: 2px solid {WOOD}; border-radius: 12px; }}
QFrame#stat QLabel#key {{ color: {WOOD_DARK}; font-size: 10px; font-weight: 800; letter-spacing: 1px; }}
QFrame#stat QLabel#val {{ font-size: 16px; font-weight: 800; }}
QFrame#card {{
    background: {CREAM}; border: 2px solid {WOOD}; border-radius: 16px;
}}
QLineEdit, QComboBox {{
    background: white; border: 2px solid {WOOD}; border-radius: 12px; padding: 6px 10px;
    selection-background-color: {SELECT}; selection-color: {INK};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ORANGE}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url("{CHEVRON}"); width: 12px; height: 8px; margin-right: 10px; }}
QComboBox QAbstractItemView {{ background: {CREAM}; border: 2px solid {WOOD}; selection-background-color: {SELECT}; }}
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #e3f3ff);
    border: 2px solid #8cc4ea; border-radius: 14px; padding: 6px 14px; font-weight: 700; color: #2f6a99;
}}
QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #cfeaff); }}
QPushButton:pressed {{ background: #bfe0fb; }}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {ORANGE_LIGHT}, stop:1 {ORANGE});
    border: 2px solid {ORANGE_DARK}; color: white;
}}
QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffc56e, stop:1 #ff9a3a); }}
QPushButton#danger {{ background: white; border: 2px solid #e58a8a; color: #c0392b; }}
QTableWidget {{
    background: {CREAM}; alternate-background-color: #fffbf2; gridline-color: transparent;
    border: none; selection-background-color: {SELECT}; selection-color: {INK};
}}
QTableWidget {{ outline: 0; }}
QTableWidget::item {{ border: none; padding: 0 6px; }}
QTableWidget::item:selected {{ background: {SELECT}; color: {INK}; border: none; }}
QHeaderView {{ background: {CREAM}; border: none; border-bottom: 2px solid #f1d7a8; }}
QHeaderView::section {{
    background: {CREAM}; color: {WOOD_DARK}; border: none;
    padding: 8px 6px; font-weight: 800; font-size: 12px;
}}
QHeaderView::up-arrow, QHeaderView::down-arrow {{ image: none; width: 0; height: 0; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: {WOOD}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 4px; }}
QScrollBar::handle:horizontal {{ background: {WOOD}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QTableCornerButton::section {{ background: {CREAM}; border: none; }}
QSplitter::handle {{ background: transparent; width: 10px; }}
QStatusBar {{ background: {CREAM}; border-top: 2px solid {WOOD}; color: {INK}; }}
QToolTip {{ background: {CREAM}; color: {INK}; border: 2px solid {WOOD}; border-radius: 6px; padding: 4px; }}
"""

def overlay_qss(scale: float = 1.0) -> str:
    """The overlay stylesheet with every size (fonts, borders, radii, padding) times ``scale``."""

    def px(v: float, lo: int = 1) -> str:
        return f"{max(lo, round(v * scale))}px"

    def font(v: float) -> str:
        return px(v, 6)  # keep text legible at the smallest scale

    return f"""
#overlay {{ background: rgba(255, 248, 232, 232); border: {px(2)} solid {WOOD}; border-radius: {px(14)}; }}
QLabel {{ color: {INK}; font-family: {FONT}; font-size: {font(12)}; background: transparent; }}
QLabel#big {{ font-size: {font(22)}; font-weight: 800; color: {GOLD}; }}
QLabel#muted {{ color: {INK_MUTED}; font-size: {font(11)}; }}
QLabel#head {{ font-weight: 700; }}
QLabel#section {{ color: {WOOD_DARK}; font-size: {font(10)}; font-weight: 700; letter-spacing: {px(1)}; margin-top: {px(6, 0)}; }}
QFrame#panel {{ background: white; border: {px(2)} solid {WOOD}; border-radius: {px(10)}; }}
QFrame#panel QLabel#key {{ color: {WOOD_DARK}; font-size: {font(10)}; font-weight: 700; letter-spacing: {px(1)}; }}
QFrame#panel QLabel#val {{ font-weight: 700; }}
QPushButton {{ color: {WOOD_DARK}; background: transparent; border: none; font-size: {font(14)}; font-weight: 700; padding: 0 {px(4, 0)}; }}
QPushButton:hover {{ color: {ORANGE}; }}
QSlider::groove:horizontal {{ height: {px(4)}; background: #f1d7a8; border-radius: {px(2)}; }}
QSlider::handle:horizontal {{ background: {ORANGE}; border: {px(2)} solid white; width: {px(12)}; margin: -{px(5)} 0; border-radius: {px(7)}; }}
"""


# Overlay sits on top of the game: same palette, slightly translucent.
OVERLAY_QSS = overlay_qss(1.0)


def secondary_span(scale: float = 1.0) -> str:
    """Inline style for the smaller second figure in a headline (e.g. EXP/hr)."""
    return f"color:{INK_MUTED};font-size:{max(6, round(14 * scale))}px;font-weight:600"


SECONDARY_SPAN = secondary_span(1.0)

# Positive / negative figures in the history table (dark enough to read on cream).
GAIN = "#2e8b3a"
LOSS = "#c0392b"

# Last-minute pill next to the EXP/min headline (tone: over / under / near).
PILL_OVER_BG = "#e3f4d9"
PILL_UNDER_BG = "#fbe1dc"
PILL_NEAR_BG = "#ece6dc"

PILL_QSS = f"""
QLabel#pill {{ font-size: 12px; font-weight: 700; padding: 2px 10px; border-radius: 10px; color: {INK_MUTED}; background: {PILL_NEAR_BG}; }}
QLabel#pill[tone="over"] {{ color: {GAIN}; background: {PILL_OVER_BG}; }}
QLabel#pill[tone="under"] {{ color: {LOSS}; background: {PILL_UNDER_BG}; }}
QLabel#pill[tone="near"] {{ color: {INK_MUTED}; background: {PILL_NEAR_BG}; }}
"""
GUI_QSS += PILL_QSS

# Session detail (history): signed figures and kill counts.
DETAIL_QSS = f"""
QLabel[tone="gain"] {{ color: {GAIN}; font-weight: 700; }}
QLabel[tone="loss"] {{ color: {LOSS}; font-weight: 700; }}
QFrame#stat QLabel#val[tone="gain"] {{ color: {GAIN}; }}
QFrame#stat QLabel#val[tone="loss"] {{ color: {LOSS}; }}
QLabel#count {{ font-weight: 700; }}
"""
GUI_QSS += DETAIL_QSS
