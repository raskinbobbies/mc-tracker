"""Main window: live status, price check, and session history."""

from __future__ import annotations

import html
import math
import os
from dataclasses import dataclass
from pathlib import Path

import shiboken6

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import history
from .history import best_index
from .assets import AssetStore
from .rates import PILL_NEAR_PCT, average_before_last_minute, last_minute_pill  # noqa: F401 (re-exported)
from .exp_table import EXP_TABLE
from .overlay import DASH, Overlay, Tile, fmt_dur, fmt_eta, fmt_num, fmt_tile, potion_cost, potions_tip, warmed_up
from .session import Tracker, sessions_dir
from .theme import GAIN, GUI_QSS, INK, INK_MUTED, LOSS, ORANGE, ORANGE_LIGHT, SECONDARY_SPAN, TILE_BORDER, WOOD_DARK

CREDIT = (
    'Game data: <a href="https://meowdb.com/msclassic">NiaDB</a> · '
    'Sprites: <a href="https://maplestory.io">maplestory.io</a> · '
    'Prices: <a href="https://mesos.watch">mesos.watch</a> · '
    "MapleStory © Nexon. Fan project, not affiliated."
)

MOB_COLUMNS = ["", "Monster", "Lv", "EXP", "Killed", "/min"]
SPARK_MINUTES = 120
COLUMNS = ["Date", "Start", "Map", "Time", "EXP", "EXP/min", "Kills", "Lv", "Net mesos", "Potions"]


class NumItem(QTableWidgetItem):
    def __lt__(self, other):
        a = self.data(Qt.ItemDataRole.UserRole)
        b = other.data(Qt.ItemDataRole.UserRole)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


def _cell(text: str, value=None, right: bool = False) -> QTableWidgetItem:
    item = NumItem(text)
    if value is not None:
        item.setData(Qt.ItemDataRole.UserRole, value)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


MINUS = "−"  # U+2212, used for every negative figure in the history table


def _sign(n: float, plus: bool = False) -> str:
    """Sign prefix: U+2212 when negative, "+" when ``plus`` and positive, else nothing."""
    return MINUS if n < 0 else "+" if plus and n > 0 else ""


def _mesos_cell(s: history.SavedSession) -> QTableWidgetItem:
    """Net mesos with the per-minute rate; green when positive, red when negative.

    A dash when the file has no mesos fields; a tooltip when nothing spent was tracked
    (an old session saved before items used were tracked), since the figure is then gross mesos picked up.
    """
    if not s.has_mesos:
        return _cell("—", float("inf"), right=True)  # sorts after every real figure (ascending)
    n = s.net_mesos
    rate = round(s.net_mesos_per_min)
    item = _cell(f"{_sign(n, True)}{abs(n):,} · {_sign(rate)}{fmt_num(abs(rate))}/min", n, right=True)
    if n:
        item.setForeground(QColor(GAIN if n > 0 else LOSS))
    if not s.has_items_used:
        item.setToolTip("Gross mesos picked up — this session was saved before items used were tracked.")
    return item


def _potions_cell(s: history.SavedSession) -> QTableWidgetItem:
    """Potions used as a negative total with the rate; a dash when the session has no items-used data."""
    p = s.potions
    if p is None:  # sorts after every real figure (ascending)
        return _cell("—", float("inf"), right=True)
    return _cell(f"{_sign(-p)}{p:,} · {fmt_num(s.potions_per_min)}/min", -p, right=True)


MAP_MIN_WIDTH = 140


class HistoryTable(QTableWidget):
    """Session table: every column sizes to its contents except Map, which takes the leftover
    width but never less than ``MAP_MIN_WIDTH``; past that the table scrolls sideways."""

    MAP_COL = 2

    def fit_map(self) -> None:
        hdr = self.horizontalHeader()
        others = sum(hdr.sectionSize(c) for c in range(hdr.count()) if c != self.MAP_COL)
        hdr.resizeSection(self.MAP_COL, max(MAP_MIN_WIDTH, self.viewport().width() - others))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_map()


class StatBox(QFrame):
    """Small boxed number with a caption: AVG, BEST MINUTE, ..."""

    def __init__(self, key: str):
        super().__init__(objectName="stat")
        self.key = QLabel(key, objectName="key")
        self.val = QLabel("\u2014", objectName="val")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(0)
        lay.addWidget(self.key)
        lay.addWidget(self.val)

    def set(self, val: str, key: str | None = None) -> None:
        self.val.setText(val)
        if key is not None:
            self.key.setText(key)


class MoneyBox(StatBox):
    """A StatBox whose value has a small per-minute figure after it: "+10,929  499/min"."""

    def __init__(self, key: str):
        super().__init__(key)
        self.sub = QLabel(objectName="muted")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.layout().removeWidget(self.val)
        row.addWidget(self.val)
        row.addWidget(self.sub, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        self.layout().addLayout(row)

    def set_money(self, val: str, rate: str, tone: str, tip: str = "") -> None:
        self.val.setText(val)
        self.sub.setText(rate)
        self.val.setToolTip(tip)
        self.sub.setToolTip(tip)
        self.sub.setVisible(bool(rate))
        set_tone(self.val, tone)


def map_parts(display: str) -> tuple[str, str]:
    """(street, map name) of the tracker's map text, which reads "Street : Map" (a bare name has no street)."""
    street, sep, name = display.partition(" : ")
    return (street, name) if sep else ("", display)


def per_min_text(x: float) -> str:
    """A per-minute figure: "499", "72", "0.8", with a minus when negative."""
    body = f"{abs(x):.1f}" if abs(x) < 10 else f"{round(abs(x)):,}"
    return f"{_sign(x)}{body}"


def money_view(net: int, potions: int, active_s: float,
               unpriced: int = 0) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """((net mesos, per-min, tone), (potions, per-min, tone)) as shown under the items grid.
    ``potions`` is the mesos spent on items used; ``unpriced`` items used have no price (a "+" after the figure).
    Per-minute figures wait for the first minute, like every other rate."""
    mins = active_s / 60
    rates = warmed_up(active_s) and mins > 0
    net_rate = f"{per_min_text(net / mins)}/min" if rates else ""
    pot_rate = f"{per_min_text(-potions / mins)}/min" if rates else ""
    return ((signed(net), net_rate, tone_of(net)),
            (signed(-potions, plus=False) + ("+" if unpriced else ""), pot_rate,
             "loss" if potions > 0 or unpriced else ""))


def item_rows(loot: dict, used: dict, names: dict, limit: int = 18) -> tuple[list[GridRow], int]:
    """Tiles for the items grid: what was picked up (green) and what was used (red, signed), most first.
    Returns (the first ``limit`` rows, how many more there are)."""
    merged = [(n, 0, k) for k, n in loot.items() if n > 0] + [(n, 1, k) for k, n in used.items() if n > 0]
    rows = []
    for n, kind, k in sorted(merged, key=lambda r: (-r[0], r[1], str(r[2]))):
        name = names.get(("item", k), str(k))
        if kind == 0:
            rows.append(GridRow(k, name, fmt_tile(n), "gain", full=f"{n:,} picked up"))
        else:
            rows.append(GridRow(k, name, fmt_tile(-n), "loss", full=f"{n:,} used"))
    return rows[:limit], max(0, len(rows) - limit)


def exp_bar_text(exp: int | None, need: int | None, pct: float | None) -> str:
    """"1,234 / 5,678 · 21.73%" (whatever is known; empty when nothing is)."""
    parts = []
    if exp is not None and need:
        parts.append(f"{exp:,} / {need:,}")
    if pct is not None:
        parts.append(f"{pct:.2f}%")
    return " · ".join(parts)


def eta_text(level: int | None, seconds: float | None, active_s: float) -> tuple[str, str, str]:
    """(caption, time, tail) for the ETA card: "LEVEL 46 IN", "3h 41m", "at your average".
    A dash until the first minute is over; the tail only when there is a time to qualify."""
    key = f"LEVEL {level + 1} IN" if level else "NEXT LEVEL IN"
    if not warmed_up(active_s):
        return key, DASH, "after the first minute"
    eta = fmt_eta(seconds)
    return key, eta, "at your average" if eta != DASH else ""


class ExpBar(QWidget):
    """The player's EXP bar: an orange fill on a cream track, with "cur / need · pct" centred on it."""

    def __init__(self):
        super().__init__()
        self.pct = 0.0
        self.text = ""
        self.setFixedHeight(20)
        self.setMinimumWidth(160)

    def set_state(self, pct: float | None, text: str) -> None:
        pct = max(0.0, min(pct or 0.0, 100.0))
        if (pct, text) != (self.pct, self.text):
            self.pct, self.text = pct, text
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(TILE_BORDER), 1.5))
        p.setBrush(QColor("#f1d7a8"))
        p.drawRoundedRect(r, 9, 9)
        if self.pct > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ORANGE_LIGHT))
            p.setClipRect(QRectF(r.left(), r.top(), max(r.width() * self.pct / 100, 8.0), r.height()))
            p.drawRoundedRect(r, 9, 9)
            p.setClipping(False)
        f = p.font()
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(QColor(INK))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text)


class Sparkline(QWidget):
    """EXP per active minute, painted by hand (no chart dependency)."""

    def __init__(self):
        super().__init__()
        self.values: list[int] = []
        self.setMinimumHeight(52)

    def set_values(self, values: list[int]) -> None:
        values = values[-SPARK_MINUTES:]
        if values != self.values:
            self.values = values
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 6
        if len(self.values) < 2:
            p.setPen(QColor(INK_MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Chart appears after two minutes of EXP")
            return
        top = max(max(self.values), 1)
        n = len(self.values)
        pts = [(pad + i * (w - 2 * pad) / (n - 1), h - pad - v / top * (h - 2 * pad))
               for i, v in enumerate(self.values)]
        fill = QPainterPath()
        fill.moveTo(pts[0][0], h - pad)
        for x, y in pts:
            fill.lineTo(x, y)
        fill.lineTo(pts[-1][0], h - pad)
        fill.closeSubpath()
        p.fillPath(fill, QColor(255, 138, 31, 60))
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawPath(line)
        best = best_index(self.values)
        if best is not None:  # mark the best minute
            bx, by = pts[best]
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(WOOD_DARK))
            p.drawEllipse(int(bx) - 4, int(by) - 4, 8, 8)


class MonsterTable(QTableWidget):
    """One row per monster on the current map: sprite, name, level, EXP, killed, /min."""

    def __init__(self, assets: AssetStore):
        super().__init__(0, len(MOB_COLUMNS))
        self.assets = assets
        self._ids: tuple[int, ...] = ()
        self._icons: dict[int, QIcon] = {}
        self.setHorizontalHeaderLabels(MOB_COLUMNS)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(34)
        self.setIconSize(QSize(28, 28))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        hdr = self.horizontalHeader()
        hdr.setHighlightSections(False)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(MOB_COLUMNS)):
            align = Qt.AlignmentFlag.AlignLeft if c == 1 else Qt.AlignmentFlag.AlignRight
            self.horizontalHeaderItem(c).setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(34 * 5 + 40)

    def show_mobs(self, mobs, kills, active_s: float) -> None:
        from . import gamedata

        ids = tuple(m.id for m in mobs)
        if ids != self._ids:
            self._ids = ids
            self.setRowCount(len(mobs))
            for r, m in enumerate(mobs):
                lvl = gamedata.mob_level(m.id)
                cells = [_cell(""), _cell(m.name), _cell(str(lvl) if lvl else "\u2014", right=True),
                         _cell(f"{m.exp:,}", right=True), _cell("", right=True), _cell("", right=True)]
                for c, item in enumerate(cells):
                    self.setItem(r, c, item)
                if m.id in self._icons:
                    self.item(r, 0).setIcon(self._icons[m.id])
                else:
                    self.assets.mob_sprite(m.id, lambda path, i=m.id: self._set_icon(i, path))
        minutes = active_s / 60 if active_s >= 10 else 0
        for r, m in enumerate(mobs):
            n = kills.get(m.id, 0)
            self.item(r, 4).setText(f"{n:,}")
            self.item(r, 5).setText(f"{n / minutes:.1f}" if minutes else "\u2014")

    def _set_icon(self, mob_id: int, path: str) -> None:
        icon = self._icons[mob_id] = QIcon(QPixmap(path))
        if mob_id in self._ids:
            self.item(self._ids.index(mob_id), 0).setIcon(icon)


def exp_so_far(n: float) -> str:
    """"+1.24K EXP" / "−500 EXP": the first minute's headline (a death can take EXP back)."""
    return f"{_sign(n, True)}{fmt_num(abs(n))} EXP"


def rate_headline(per_hr: float, active_s: float, net_exp: int = 0) -> str:
    """EXP/min headline (rich text). The first minute shows the EXP earned so far instead (no rate until a minute)."""
    if not warmed_up(active_s):
        return f"{exp_so_far(net_exp)}<span style='{SECONDARY_SPAN}'>&nbsp;&nbsp;first minute</span>"
    return (f"{fmt_num(per_hr / 60)} EXP/min, so far"
            f"<span style='{SECONDARY_SPAN}'>&nbsp;&nbsp;{fmt_num(per_hr)} EXP/hr</span>")


def unknown_kills_text(n: int, party: bool) -> str:
    """Footer under the monsters table; empty when every kill is assigned."""
    if n <= 0:
        return ""
    noun = ("unknown shared kill" if party else "unknown kill") + ("" if n == 1 else "s")
    return f"{n:,} {noun}"


class MinutePill(QLabel):
    """Small green/red/grey pill: last finished minute vs the session average."""

    def __init__(self):
        super().__init__(objectName="pill")
        self.hide()

    def set_minutes(self, minutes: list[int], avg_per_min: float) -> None:
        pill = last_minute_pill(minutes, avg_per_min)
        if pill is None:
            self.hide()
            return
        text, tone = pill
        self.setText(text)
        if self.property("tone") != tone:
            self.setProperty("tone", tone)
            self.style().unpolish(self)
            self.style().polish(self)
        self.show()


class UnknownKillsFooter(QLabel):
    """Muted line under the monsters table: kills the screen can't attribute."""

    def __init__(self):
        super().__init__(objectName="muted")
        self.hide()

    def set_count(self, n: int, party: bool) -> None:
        text = unknown_kills_text(n, party)
        self.setText(text)
        self.setVisible(bool(text))


@dataclass
class GridRow:
    """One tile in an ``EntryGrid``: icon, count text, and a tone (gain / loss / "")."""

    id: int | None
    name: str
    text: str
    tone: str = ""
    icon: str = "item"  # "item" | "kill" | "coin" (drawn, for mesos)
    full: str = ""  # the exact figure for the tooltip (the tile shows the short form)


def coin_pixmap(size: int = 32) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(WOOD_DARK), 2))
    p.setBrush(QColor("#ffd24d"))
    p.drawEllipse(3, 3, size - 6, size - 6)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(9, 9, size - 18, size - 18)
    p.end()
    return pm


def placeholder_pixmap(size: int = 32) -> QPixmap:
    """A muted "?" for an item with no sprite (its name is in the tile's tooltip)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(INK_MUTED), 1.5, Qt.PenStyle.DashLine))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(3, 3, size - 6, size - 6), 6, 6)
    font = QFont(p.font())
    font.setBold(True)
    font.setPixelSize(max(8, size // 2))
    p.setFont(font)
    p.drawText(QRectF(0, 0, size, size), int(Qt.AlignmentFlag.AlignCenter), "?")
    p.end()
    return pm


def fmt_full(n: float) -> str:
    """Full commas under 100,000 (12,345), the overlay's short form (1.25M) above."""
    n = int(n)
    return f"{n:,}" if abs(n) < 100_000 else fmt_num(n)


def signed(n: float, plus: bool = True) -> str:
    """"+1,234" / "−1,234" (U+2212); zero has no sign."""
    return f"{_sign(n, plus)}{fmt_full(abs(n))}"


def exact(n: float) -> str:
    """The whole figure with commas and a sign, for tooltips: "+1,052,300" / "−12"."""
    n = int(n)
    return f"{'+' if n > 0 else MINUS if n < 0 else ''}{abs(n):,}"


def tone_of(n: float) -> str:
    return "gain" if n > 0 else "loss" if n < 0 else ""


def set_tone(w: QWidget, tone: str) -> None:
    """Colour a label green/red through the ``tone`` property (see ``DETAIL_QSS``)."""
    if (w.property("tone") or "") != tone:
        w.setProperty("tone", tone)
        w.style().unpolish(w)
        w.style().polish(w)


class FlowLayout(QLayout):
    """Left-to-right rows of equal-size widgets that wrap at the available width (Qt's flow layout example)."""

    def __init__(self, parent: QWidget | None = None, hspace: int = 8, vspace: int = 6):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.hspace, self.vspace = hspace, vspace

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int) -> QLayoutItem | None:
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int) -> QLayoutItem | None:
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect: QRect, apply: bool) -> int:
        """Lay the items out in ``rect``; returns the height used."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_h = area.x(), area.y(), 0
        for it in self._items:
            w, h = it.sizeHint().width(), it.sizeHint().height()
            if x > area.x() and x + w > area.right() + 1:  # wrap
                x, y, row_h = area.x(), y + row_h + self.vspace, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self.hspace
            row_h = max(row_h, h)
        return y + row_h - rect.y() + m.bottom()


class EntryGrid(QWidget):
    """Identical boxed sprite tiles with the count drawn over each sprite (the overlay's tile), flowing into
    as many columns as the width allows. Names live in the tooltip; an item with no sprite gets a "?"."""

    TILE_SCALE = 1.4  # the overlay tile, larger: this window has the room

    def __init__(self, kind: str, assets: AssetStore):
        super().__init__()
        self.kind, self.assets = kind, assets
        self.grid = FlowLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self._tiles: list[QWidget] = []
        self._shown: list[GridRow] | None = None

    def show_entries(self, entries: list[history.Entry]) -> None:
        self.show_rows([GridRow(e.id, e.name, fmt_tile(e.count, "×"), icon=self.kind, full=f"×{e.count:,}")
                        for e in entries])

    def show_rows(self, rows: list[GridRow]) -> None:
        if rows == self._shown:  # nothing changed: leave the layout alone
            return
        self._shown = list(rows)
        for w in self._tiles:
            self.grid.removeWidget(w)  # frees the layout item as well
            w.hide()  # deleteLater is deferred: don't paint over the new tiles meanwhile
            w.deleteLater()
        self._tiles = []
        for r in rows:
            tile = Tile(f"{r.name}: {r.full or r.text}", self.TILE_SCALE, badge=True)
            tile.set_count(r.text, r.tone)
            if r.icon == "coin":
                tile.set_pixmap(coin_pixmap())
            elif r.id is None:
                tile.set_pixmap(placeholder_pixmap())
            else:
                (self.assets.mob_sprite if r.icon == "kill" else self.assets.item_icon)(r.id, tile.set_image)
            self.grid.addWidget(tile)
            self._tiles.append(tile)
        self.grid.invalidate()
        self.updateGeometry()
        self.setVisible(bool(rows))


class Section(QWidget):
    """Small caps title with an optional muted caption on the right, then the content."""

    def __init__(self, title: str, content: QWidget):
        super().__init__()
        # Wrapping lets a long caption give way on a narrow panel instead of widening the whole body.
        self.caption = QLabel(objectName="muted", wordWrap=True,
                              alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.content = content
        head = QHBoxLayout()
        head.addWidget(QLabel(title, objectName="section"))
        head.addStretch(1)
        head.addWidget(self.caption, 0, Qt.AlignmentFlag.AlignBottom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(head)
        lay.addWidget(content)


class ExpChart(QWidget):
    """EXP per active minute as bars on a baseline: the best minute in solid orange, the average as a dotted line.
    The left edge labels the top and zero, the bottom labels the minutes; bars stay slim however wide the chart is."""

    BAR_MAX = 18  # px: a wide chart spreads its bars out instead of fattening them into boxes
    PAD_LEFT, PAD_BOTTOM = 44, 16

    def __init__(self):
        super().__init__()
        self.values: list[int] = []
        self.avg = 0.0
        self.setMinimumHeight(110)

    def set_data(self, values: list[int], avg: float) -> None:
        self.values, self.avg = list(values), avg
        self.update()

    def minute_labels(self, plot_w: float, label_w: float = 34.0) -> list[int]:
        """The minutes (0-based) that get a tick label: evenly spaced, wide enough apart not to touch."""
        n = len(self.values)
        if n == 0:
            return []
        slot = plot_w / n
        step = max(1, math.ceil(label_w / slot)) if slot > 0 else n
        return list(range(0, n, step))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        w, h, pad = self.width(), self.height(), 4
        left, bottom = self.PAD_LEFT, self.PAD_BOTTOM
        plot_w, plot_h = max(w - left - pad, 1), max(h - bottom - pad, 1)
        top = max(max(self.values, default=0), self.avg, 1)
        base_y = pad + plot_h
        small = QFont(p.font())
        small.setPixelSize(10)
        p.setFont(small)
        axis = QColor(INK_MUTED)
        p.setPen(QPen(axis, 1))
        p.drawLine(QPointF(left, base_y), QPointF(left + plot_w, base_y))
        p.setPen(QPen(QColor(axis.red(), axis.green(), axis.blue(), 70), 1))
        p.drawLine(QPointF(left, pad), QPointF(left + plot_w, pad))  # the top gridline
        p.setPen(axis)
        flags = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        p.drawText(QRectF(0, pad - 7, left - 4, 14), flags, fmt_num(top))
        p.drawText(QRectF(0, base_y - 7, left - 4, 14), flags, "0")
        n = max(len(self.values), 1)
        slot = plot_w / n
        bw = max(1.0, min(slot - (2 if slot > 5 else 1), self.BAR_MAX))
        best = best_index(self.values)
        fill = QColor(ORANGE_LIGHT)
        fill.setAlpha(170)
        for i, v in enumerate(self.values):
            bh = max(v, 0) / top * plot_h if v > 0 else 0.0
            p.fillRect(QRectF(left + i * slot + (slot - bw) / 2, base_y - max(bh, 1.0), bw, max(bh, 1.0)),
                       QColor(ORANGE) if i == best else fill)
        for i in self.minute_labels(plot_w):
            x = left + i * slot
            p.drawText(QRectF(x, base_y + 2, slot, bottom - 2), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       str(i + 1))
        if self.avg > 0:
            y = base_y - self.avg / top * plot_h
            p.setPen(QPen(QColor(WOOD_DARK), 1.5, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(left, y), QPointF(left + plot_w, y))


class _Track(QWidget):
    """Level bar: faint up to where the session started, solid for what it gained."""

    def __init__(self):
        super().__init__()
        self.lo = self.hi = 0.0
        self.setFixedHeight(10)

    def set_range(self, lo: float, hi: float) -> None:
        self.lo, self.hi = max(0.0, min(lo, 100.0)), max(0.0, min(hi, 100.0))
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        w, h = self.width(), self.height()
        p.setBrush(QColor("#f1d7a8"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 5, 5)
        lo, hi = self.lo / 100 * w, self.hi / 100 * w
        if lo > 0:
            p.setBrush(QColor(255, 138, 31, 90))
            p.drawRoundedRect(QRectF(0, 0, lo, h), 5, 5)
        if hi > lo:
            p.setBrush(QColor(ORANGE))
            p.drawRoundedRect(QRectF(lo, 0, max(hi - lo, 3.0), h), 5, 5)


def pct_text(x: float) -> str:
    """A percent truncated to one decimal (never rounded up), e.g. 66.85 -> "66.8"."""
    return f"{math.trunc(round(x * 10, 6)) / 10:.1f}"


class LevelBar(QWidget):
    """"Lv 52 · 45.2%  →  Lv 53 · 12.0%   +66.8%" over a bar."""

    def __init__(self):
        super().__init__()
        self.text = QLabel()
        self.gain = QLabel()
        self.track = _Track()
        row = QHBoxLayout()
        row.addWidget(self.text)
        row.addStretch(1)
        row.addWidget(self.gain)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(3)
        lay.addLayout(row)
        lay.addWidget(self.track)

    def set_span(self, span: tuple[int, float, int, float, float]) -> None:
        l0, p0, l1, p1, gained = span
        self.text.setText(f"Lv {l0} · {pct_text(p0)}%  →  Lv {l1} · {pct_text(p1)}%")
        shown = pct_text(abs(gained))
        sign = _sign(gained, True) if float(shown) else ""  # no sign on 0.0%
        self.gain.setText(f"{sign}{shown}%")
        set_tone(self.gain, tone_of(gained) if sign else "")
        # Same level: the gained slice follows the starting point; after a level-up the bar is the new level's fill.
        self.track.set_range(p0 if l0 == l1 else 0.0, p1)


class KilledList(QWidget):
    """One row per mob: sprite, name, kill count; a muted footer for kills no mob could be given."""

    def __init__(self, assets: AssetStore):
        super().__init__()
        self.assets = assets
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(1, 1)
        self.footer = UnknownKillsFooter()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(self.grid)
        lay.addWidget(self.footer)
        self._widgets: list[QWidget] = []
        self._shown: tuple | None = None

    def show_kills(self, entries: list[history.Entry], unknown: int, party: bool) -> None:
        key = (tuple((e.id, e.name, e.count) for e in entries), unknown, party)
        if key == self._shown:
            return
        self._shown = key
        for w in self._widgets:
            self.grid.removeWidget(w)
            w.hide()
            w.deleteLater()
        self._widgets = []
        for r, e in enumerate(entries):
            sprite = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
            sprite.setFixedSize(32, 32)
            if e.id is not None:
                self.assets.mob_sprite(e.id, lambda path, lbl=sprite: self._set_sprite(lbl, path))
            name, count = QLabel(e.name), QLabel(f"{e.count:,}", objectName="count")
            for c, w in enumerate((sprite, name, count)):
                self.grid.addWidget(w, r, c, Qt.AlignmentFlag.AlignVCenter)
                self._widgets.append(w)
        self.footer.set_count(unknown, party)

    @staticmethod
    def _set_sprite(lbl: QLabel, path: str) -> None:
        if shiboken6.isValid(lbl):  # removed before its image arrived
            pm = QPixmap(path)
            if not pm.isNull():
                lbl.setPixmap(pm.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))


class FootStat(QFrame):
    """MESOS / POTIONS / NET: caption, signed value (green or red), per-minute line."""

    def __init__(self, key: str):
        super().__init__(objectName="stat")
        self.key = QLabel(key, objectName="key")
        self.val = QLabel("—", objectName="val")
        self.rate = QLabel(objectName="muted")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(0)
        for w in (self.key, self.val, self.rate):
            lay.addWidget(w)

    def set(self, n: int | None, seconds: int, suffix: str = "", tip: str = "") -> None:
        """Show ``n`` (mesos; negative for a cost) then ``suffix``; hidden when None.

        The per-minute line waits for the warm-up minute, like every other rate.
        """
        self.setVisible(n is not None)
        self.setToolTip(tip)
        if n is None:
            return
        self.val.setText((signed(n) if n else "0") + suffix)
        set_tone(self.val, tone_of(n))
        self.rate.setText(f"{signed(round(n / seconds * 60))}/min" if warmed_up(seconds) else "")


class SessionDetail(QFrame):
    """A saved session, laid out like maplestory.watch's expanded history row.

    A fixed header (title, date, EXP summary, level bar, money boxes) over one scrolling body: EXP/min chart,
    loot grid (mesos tile + items), USED grid, killed list. Whatever the file doesn't have is hidden.
    """

    def __init__(self, assets: AssetStore, on_delete):
        super().__init__(objectName="card")
        self._session: history.SavedSession | None = None
        self.title = QLabel(objectName="h2", wordWrap=True)
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.when = QLabel(objectName="muted", wordWrap=True)
        self.headline = QLabel(objectName="h1")
        self.stats = QLabel(objectName="muted")
        self.stats.setWordWrap(True)
        self.level_bar = LevelBar()
        self.chart = ExpChart()
        self.chart_section = Section("EXP / MINUTE", self.chart)
        self.loot = EntryGrid("loot", assets)
        self.loot_section = Section("LOOT", self.loot)
        self.used = EntryGrid("used", assets)
        self.used_section = Section("USED", self.used)
        self.killed = KilledList(assets)
        self.killed_section = Section("KILLED", self.killed)
        self.foot = {k: FootStat(k) for k in ("MESOS", "POTIONS", "NET")}
        foot_row = QHBoxLayout()
        foot_row.setContentsMargins(0, 8, 0, 0)
        for f in self.foot.values():
            foot_row.addWidget(f)
        foot_row.addStretch(1)
        self.foot_box = QWidget()
        self.foot_box.setLayout(foot_row)

        delete = QPushButton("Delete session", objectName="danger")
        delete.clicked.connect(lambda: self._session and on_delete(self._session))
        self.delete_btn = delete

        # The header never scrolls away; only the body under it does (one vertical scrollbar).
        head = QVBoxLayout()
        for w in (self.title, self.when, self.headline, self.stats, self.level_bar, self.foot_box):
            head.addWidget(w)
        body = QVBoxLayout()
        for w in (self.chart_section, self.loot_section, self.used_section, self.killed_section):
            body.addWidget(w)
        body.addStretch(1)
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(delete)
        body.addLayout(foot)
        inner = QWidget()
        inner.setLayout(body)
        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addLayout(head)
        lay.addWidget(self.scroll, 1)
        self.show_session(None)

    def show_session(self, s: history.SavedSession | None) -> None:
        self._session = s
        self.delete_btn.setVisible(s is not None)
        if s is None:
            self.title.setText("No session selected")
            self.when.setText("Sessions are saved when you leave a map, press restart, or close the app.")
            self.headline.setText("")
            self.stats.setText("")
            for w in (self.level_bar, self.chart_section, self.loot_section, self.used_section, self.killed_section,
                      self.foot_box):
                w.hide()
            return
        street, name = s.street_and_map
        self.title.setText(html.escape(name) + (  # the street under the map, smaller, like the live header
            f"<br><span style='color:{INK_MUTED};font-size:12px;font-weight:400'>{html.escape(street)}</span>"
            if street else ""))
        self.when.setText(f"{s.started:%a %d %b %Y, %H:%M} · {fmt_dur(s.seconds)} played")
        warm = warmed_up(s.seconds)  # no rates until the session has run a minute
        self.headline.setText(f"{fmt_num(s.exp_per_min)} EXP/min" if warm else exp_so_far(s.exp))
        d = s.data
        parts = [f"+{fmt_full(s.exp)} EXP"]
        if warm:
            parts.append(f"{fmt_num(d.get('exp_per_hour', s.exp_per_min * 60))} EXP/hr")
        parts.append(f"{s.kills:,} party kills")
        if d.get("exp_lost"):
            parts.append(f"{MINUS}{fmt_full(d['exp_lost'])} EXP lost")
        self.stats.setText(" · ".join(parts))

        span = s.level_span
        self.level_bar.setVisible(span is not None)
        if span:
            self.level_bar.set_span(span)

        mins, best = s.exp_per_minute, s.best_minute
        self.chart_section.setVisible(best is not None)
        if mins and best:
            self.chart.set_data(mins, s.exp_per_min if warm else 0.0)
            self.chart_section.caption.setText(
                f"avg {fmt_full(s.exp_per_min)} · best {fmt_full(best[1])} (minute {best[0] + 1})" if warm else "")

        loot, unrecognised = s.loot_view()
        rows = [GridRow(None, "Mesos", fmt_tile(s.mesos), "gain", icon="coin", full=exact(s.mesos))] if s.mesos > 0 else []
        rows += [GridRow(e.id, e.name, fmt_tile(e.count), "gain", full=exact(e.count)) for e in loot]
        self.loot.show_rows(rows)
        self.loot_section.setVisible(bool(rows))
        lost = sum(e.count for e in unrecognised)  # lines no item could be put to: counted, never given a tile
        caption = [f"{len(loot)} kind{'' if len(loot) == 1 else 's'}"] if loot else []
        if lost:
            caption.append(f"{lost:,} unrecognised")
        self.loot_section.caption.setText(" · ".join(caption))
        self.loot_section.caption.setToolTip(
            "Not matched to an item: " + ", ".join(e.name for e in unrecognised) if lost else "")

        used = [e for e in s.entries("used") if e.count > 0]
        self.used.show_rows([GridRow(e.id, e.name, fmt_tile(-e.count), "loss", full=exact(-e.count)) for e in used])
        self.used_section.setVisible(bool(used))
        self.used_section.caption.setText(f"{sum(e.count for e in used):,} used" if used else "")

        kills, unknown = s.entries("kill"), s.unassigned_kills
        self.killed.show_kills(kills, unknown, s.party)
        self.killed_section.setVisible(bool(kills) or unknown > 0)

        # POTIONS is what the items used cost in mesos (as in the live window), so NET = MESOS - POTIONS.
        # A file saved before prices were tracked has counts only: no POTIONS (no price to guess) and a gross NET.
        spent, unpriced = s.items_value, s.unpriced_used
        self.foot["MESOS"].set(s.mesos if s.has_mesos else None, s.seconds)
        self.foot["POTIONS"].set(-spent if spent is not None else None, s.seconds, "+" if unpriced else "",
                                 f"{unpriced} used item(s) have no price set" if unpriced else "")
        self.foot["NET"].set(s.net_mesos if s.has_mesos else None, s.seconds, tip="" if spent is not None else
                             "Mesos picked up: this session was saved before items used were priced.")
        self.foot_box.setVisible(any(not f.isHidden() for f in self.foot.values()))


class MainWindow(QMainWindow):
    def __init__(self, tracker: Tracker, overlay: Overlay, assets: AssetStore, hotkey: str):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(1100, 820)
        self.setStyleSheet(GUI_QSS)
        self.tracker, self.overlay, self.assets = tracker, overlay, assets
        self.sessions: list[history.SavedSession] = []

        # --- live bar ---------------------------------------------------------
        self.prompt = QLabel(objectName="prompt")  # "open the minimap": hidden until asked
        self.prompt.hide()
        # Player header: level badge and the EXP bar (the game doesn't show us the name or job).
        self.lv_badge = QLabel(DASH, objectName="lvbadge", alignment=Qt.AlignmentFlag.AlignCenter)
        self.exp_bar = ExpBar()
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.lv_badge)
        header.addWidget(self.exp_bar, 1)
        # Map card: street small, map name large, the channel pill beside it, then the controls.
        self.live_street = QLabel(objectName="street")
        self.live_title = QLabel(objectName="mapname")
        self.channel_pill = QLabel(objectName="pill")  # "Ch. 26": hidden until the channel is read
        self.channel_pill.hide()
        self.live_sub = QLabel(objectName="muted")
        self.live_rate = QLabel(objectName="h1")
        self.rate_pill = MinutePill()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._toggle_pause)
        restart = QPushButton("New session", objectName="primary")
        restart.setToolTip("Save the current session and start a new one")
        restart.clicked.connect(self._restart)
        pop = QPushButton("Overlay")
        pop.clicked.connect(self._show_overlay)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self.live_title)
        name_row.addWidget(self.channel_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        name_row.addStretch(1)
        place = QVBoxLayout()
        place.setSpacing(0)
        place.addWidget(self.live_street)
        place.addLayout(name_row)
        title_row = QHBoxLayout()
        title_row.addLayout(place, 1)
        for b in (self.pause_btn, restart, pop):
            title_row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        map_card = QFrame(objectName="stat")
        map_card.setLayout(title_row)
        self.stat_avg = StatBox("AVG EXP/MIN")
        self.stat_best = StatBox("BEST MINUTE")
        # ETA card: "LEVEL 46 IN  3h 41m  at your average"
        self.eta_key = QLabel(objectName="key")
        self.eta_val = QLabel(DASH, objectName="val")
        self.eta_tail = QLabel(objectName="muted")
        eta_row = QHBoxLayout()
        eta_row.setSpacing(6)
        eta_row.addWidget(self.eta_val)
        eta_row.addWidget(self.eta_tail, 0, Qt.AlignmentFlag.AlignBottom)
        eta_row.addStretch(1)
        eta_col = QVBoxLayout()
        eta_col.setContentsMargins(10, 4, 10, 4)
        eta_col.setSpacing(0)
        eta_col.addWidget(self.eta_key)
        eta_col.addLayout(eta_row)
        self.eta_card = QFrame(objectName="stat")
        self.eta_card.setLayout(eta_col)
        stats = QHBoxLayout()
        stats.addWidget(self.stat_avg)
        stats.addWidget(self.stat_best)
        stats.addWidget(self.eta_card, 2)
        self.spark = Sparkline()
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self.prompt)
        left.addLayout(header)
        left.addWidget(map_card)
        left.addWidget(self.live_sub)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.live_rate)
        rate_row.addWidget(self.rate_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        rate_row.addStretch(1)
        left.addLayout(rate_row)
        left.addLayout(stats)
        left.addWidget(QLabel("EXP PER MINUTE · THIS MAP", objectName="section"))
        left.addWidget(self.spark, 1)
        self.mob_table = MonsterTable(assets)
        self.unknown_footer = UnknownKillsFooter()
        self.kills_caption = QLabel(objectName="muted")  # "No kills yet" until the first one
        mob_head = QHBoxLayout()
        mob_head.addWidget(QLabel("MONSTERS ON THIS MAP", objectName="section"))
        mob_head.addStretch(1)
        mob_head.addWidget(self.kills_caption, 0, Qt.AlignmentFlag.AlignBottom)
        # Items this session: boxed tiles (red for items used), then net mesos and potions.
        self.items_grid = EntryGrid("item", assets)
        self.items_empty = QLabel("Nothing picked up yet", objectName="muted")
        self.items_more = QLabel(objectName="muted")
        self.net_box = MoneyBox("NET MESOS")
        self.potion_box = MoneyBox("POTIONS")
        money = QHBoxLayout()
        money.addWidget(self.net_box)
        money.addWidget(self.potion_box)
        items_head = QHBoxLayout()
        items_head.addWidget(QLabel("ITEMS THIS SESSION", objectName="section"))
        items_head.addStretch(1)
        items_head.addWidget(self.items_more, 0, Qt.AlignmentFlag.AlignBottom)
        right = QVBoxLayout()
        right.addLayout(mob_head)
        right.addWidget(self.mob_table)
        right.addWidget(self.unknown_footer)
        right.addLayout(items_head)
        right.addWidget(self.items_grid)
        right.addWidget(self.items_empty)
        right.addLayout(money)
        right.addStretch(1)
        live = QHBoxLayout()
        live.setSpacing(16)
        live.addLayout(left, 3)
        live.addLayout(right, 2)
        live_card = QFrame(objectName="card")
        live_card.setLayout(live)
        brand = QLabel("🍁 mc-tracker", objectName="brand")
        tagline = QLabel("your little grinding buddy", objectName="brand")
        tagline.setStyleSheet("font-size: 12px; font-weight: 600; color: #eaf6ff;")
        brand_row = QHBoxLayout()
        brand_row.addWidget(brand)
        brand_row.addWidget(tagline)
        brand_row.addStretch(1)

        # --- price check ------------------------------------------------------
        self.query = QLineEdit(placeholderText=f"Item name, or hover an item in game and press {hotkey.upper()}")
        go = QPushButton("Price check", objectName="primary")
        self.query.returnPressed.connect(self._search)
        go.clicked.connect(self._search)
        price = QHBoxLayout()
        price.addWidget(self.query, 1)
        price.addWidget(go)
        # Messages (price check progress, warnings) show here, under the search bar, not in a status bar.
        self.notice = QLabel(objectName="notice", wordWrap=True)
        self.notice.hide()
        self._notice_timer = QTimer(self, singleShot=True)
        self._sticky = ""  # last persistent notice; a timed one shows over it, then gives it back
        self._notice_timer.timeout.connect(self._end_timed_notice)

        # --- history ----------------------------------------------------------
        self.day_filter = QComboBox()
        self.day_filter.currentIndexChanged.connect(self._fill_table)
        self.day_summary = QLabel(objectName="muted")
        open_dir = QPushButton("Open folder")
        open_dir.clicked.connect(lambda: os.startfile(self._dir()))  # noqa: S606 - local folder
        hist_bar = QHBoxLayout()
        hist_bar.addWidget(QLabel("Sessions", objectName="h2"))
        hist_bar.addWidget(self.day_filter)
        hist_bar.addWidget(self.day_summary, 1)
        hist_bar.addWidget(open_dir)

        self.table = HistoryTable(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.verticalHeader().setDefaultSectionSize(34)
        hdr = self.table.horizontalHeader()
        hdr.setSortIndicatorShown(False)
        hdr.setHighlightSections(False)
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(HistoryTable.MAP_COL, QHeaderView.ResizeMode.Interactive)
        hdr.setMinimumSectionSize(40)
        for c in range(3, len(COLUMNS)):  # numbers: right-align header to match cells
            self.table.horizontalHeaderItem(c).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.itemSelectionChanged.connect(self._on_select)

        self.detail = SessionDetail(assets, self._delete)
        table_card = QFrame(objectName="card")
        tc = QVBoxLayout(table_card)
        tc.setContentsMargins(8, 6, 8, 8)
        tc.addWidget(self.table)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(table_card)
        split.addWidget(self.detail)
        split.setHandleWidth(16)  # visible gap between the sessions table and the detail panel
        split.setSizes([700, 400])

        credit = QLabel(CREDIT, openExternalLinks=True)
        credit.setStyleSheet("color: #2f6a99; font-size: 12px;")

        root = QVBoxLayout()
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)
        root.addLayout(brand_row)
        root.addLayout(price)
        root.addWidget(self.notice)
        root.addWidget(live_card)
        root.addLayout(hist_bar)
        root.addWidget(split, 1)
        root.addWidget(credit)
        w = QWidget(objectName="root")
        w.setLayout(root)
        self.setCentralWidget(w)

        tracker.on_saved.append(lambda _p: QTimer.singleShot(0, self.reload))
        self.timer = QTimer(self, interval=1000, timeout=self._refresh_live)
        self.timer.start()
        self.reload()
        self._refresh_live()

    # --- data -------------------------------------------------------------------

    def _dir(self) -> Path:
        return self.tracker.save_dir or sessions_dir()

    def reload(self) -> None:
        self.sessions = history.load(self._dir())
        days = history.by_day(self.sessions)
        current = self.day_filter.currentData()
        self.day_filter.blockSignals(True)
        self.day_filter.clear()
        self.day_filter.addItem("All days", None)
        for d in days:
            self.day_filter.addItem(d.day, d.day)
        idx = self.day_filter.findData(current)
        self.day_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.day_filter.blockSignals(False)
        self._fill_table()

    def _visible_sessions(self) -> list[history.SavedSession]:
        day = self.day_filter.currentData()
        return [s for s in self.sessions if day is None or s.day == day]

    def _fill_table(self) -> None:
        rows = self._visible_sessions()
        totals = history.DayTotals("", len(rows), sum(s.seconds for s in rows), sum(s.exp for s in rows),
                                   sum(s.kills for s in rows), sum(s.levels for s in rows))
        self.day_summary.setText(
            f"{totals.sessions} session{'s' if totals.sessions != 1 else ''} · {fmt_dur(totals.seconds)} · "
            f"+{fmt_num(totals.exp)} EXP · {fmt_num(totals.exp_per_min)} EXP/min avg · {totals.kills:,} kills"
            if rows else "No saved sessions yet"
        )
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            stamp = s.started.timestamp()
            cells = [
                _cell(s.started.strftime("%a %d %b"), stamp),
                _cell(s.started.strftime("%H:%M"), stamp),
                _cell(s.map.split(" : ")[-1]),
                _cell(fmt_dur(s.seconds), s.seconds, right=True),
                _cell(f"+{fmt_num(s.exp)}", s.exp, right=True),
                _cell(fmt_num(s.exp_per_min), s.exp_per_min, right=True),
                _cell(f"{s.kills:,}", s.kills, right=True),
                _cell(f"+{s.levels}" if s.levels else "", s.levels, right=True),
                _mesos_cell(s),
                _potions_cell(s),
            ]
            cells[0].setData(Qt.ItemDataRole.UserRole + 1, str(s.path))
            cells[2].setToolTip(s.map)
            for c, item in enumerate(cells):
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.fit_map()
        QTimer.singleShot(0, self.table.fit_map)  # once the other columns have sized to their contents
        if rows:
            self.table.selectRow(0)
        else:
            self.detail.show_session(None)

    def _on_select(self) -> None:
        sel = self.table.selectedItems()
        if not sel:
            self.detail.show_session(None)
            return
        path = self.table.item(sel[0].row(), 0).data(Qt.ItemDataRole.UserRole + 1)
        self.detail.show_session(next((s for s in self.sessions if str(s.path) == path), None))

    def _delete(self, s: history.SavedSession) -> None:
        ok = QMessageBox.question(
            self, "Delete session",
            f"Delete the {s.map} session from {s.started:%d %b %H:%M}? This can't be undone.",
        )
        if ok == QMessageBox.StandardButton.Yes:
            try:
                s.path.unlink()
            except OSError as e:
                QMessageBox.warning(self, "Delete session", f"Couldn't delete: {e}")
            self.tracker._load_today()
            self.reload()

    # --- live -------------------------------------------------------------------

    def _refresh_live(self) -> None:
        tr = self.tracker
        tr.tick()
        self.pause_btn.setText("Resume" if tr.state == "paused" else "Pause")
        street, name = map_parts(tr.map_name or "Unknown map")
        self.live_street.setText(street)
        self.live_street.setVisible(bool(street))
        self.live_title.setText(name)
        self.channel_pill.setText(f"Ch. {tr.channel}" if tr.channel else "")
        self.channel_pill.setVisible(bool(tr.channel))
        s = tr.session
        if s is None:
            today = tr.today()
            hint = "Paused" if tr.state == "paused" else ("Waiting for EXP" if tr.combat_map else "Not a hunting map")
            self.live_sub.setText(
                f"{hint} · Today: +{fmt_num(today['exp'])} EXP · {today['sessions']} sessions · "
                f"{fmt_dur(today['seconds'])} played · {today['kills']:,} kills"
            )
            self.live_rate.setText(rate_headline(0, 0, 0))  # the first minute's wording: nothing earned yet
            level, exp, pct = tr._status or (None, None, None)
            self._set_header(level, exp, EXP_TABLE.get(level) if level else None, pct)
            for b in (self.stat_avg, self.stat_best):
                b.set(DASH)
            self._set_eta(level, None, 0)
            self.spark.set_values([])
            self.mob_table.show_mobs(tr.mobs, {}, 0)
            self.rate_pill.hide()
            self.unknown_footer.set_count(0, False)
            self.kills_caption.setText("No kills yet")
            self._show_items(None)
            return
        status = "Paused" if tr.state == "paused" else ("Idle" if s.is_idle() else "Tracking")
        per_hr = s.rate_per_hour()
        self.live_sub.setText(
            f"{status} · {tr.party_label} · Session {fmt_dur(s.active_s)} · +{fmt_num(s.exp.net)} EXP · "
            f"{s.total_kills:,} {'party kills' if s.party_seen else 'kills'}"
        )
        warm = warmed_up(s.active_s)
        self.live_rate.setText(rate_headline(per_hr, s.active_s, s.exp.net))
        if s.exp.warnings and s.exp.warnings[-1] != getattr(self, "_table_warning", None):
            self._table_warning = s.exp.warnings[-1]  # EXP table disagrees with the bar
            self.show_notice(self._table_warning)
        avg_per_min = s.session_rate_per_hour() / 60  # same figure as the AVG EXP/MIN box
        minutes = s.exp_per_minute
        last = minutes[-2] if len(minutes) >= 2 else 0
        self.rate_pill.set_minutes(minutes if warm else [], average_before_last_minute(s.exp.net, s.active_s, last))
        self.unknown_footer.set_count(s.unassigned_kills, s.party_seen)
        self.kills_caption.setText("" if s.total_kills else "No kills yet")
        self.stat_avg.set(fmt_num(avg_per_min) if warm else DASH)
        self.stat_best.set(fmt_num(s.best_minute()))
        cur = s.exp.last
        self._set_header(s.level, cur.exp if cur is not None else None, s.exp.need(s.level) if s.level else None,
                         s.exp_percent)
        self._set_eta(s.level, s.seconds_to_level() if warm else None, s.active_s)
        self.spark.set_values(s.exp_per_minute)
        self.mob_table.show_mobs(s.mobs or tr.mobs, s.kills, s.active_s)
        self._show_items(s)

    def _set_header(self, level: int | None, exp: int | None, need: int | None, pct: float | None) -> None:
        """Level badge and EXP bar (name and job aren't on screen, so there's no name here)."""
        self.lv_badge.setText(f"Lv {level}" if level else DASH)
        self.exp_bar.set_state(pct, exp_bar_text(exp, need, pct))

    def _set_eta(self, level: int | None, seconds: float | None, active_s: float) -> None:
        key, eta, tail = eta_text(level, seconds, active_s)
        self.eta_key.setText(key)
        self.eta_val.setText(eta)
        self.eta_tail.setText(tail)

    def _show_items(self, s) -> None:
        """The items grid, its empty state, and net mesos / potions beneath it."""
        rows, more = item_rows(s.loot, s.used, s.names) if s is not None else ([], 0)
        self.items_grid.show_rows(rows)
        self.items_empty.setVisible(not rows)
        self.items_more.setText(f"+{more} more" if more else "")
        spent, unpriced = potion_cost(s) if s is not None else (0, 0)
        net, pot = money_view(s.mesos - spent if s is not None else 0, spent, s.active_s if s is not None else 0, unpriced)
        self.net_box.set_money(*net)
        self.potion_box.set_money(*pot, potions_tip(spent, unpriced))

    def set_minimap_prompt(self, text: str | None) -> None:
        """Show ``text`` at the top of the live card (None hides it)."""
        if text:
            self.prompt.setText(text)
        self.prompt.setVisible(bool(text))

    def _toggle_pause(self) -> None:
        self.tracker.toggle_pause()
        self._refresh_live()

    def _restart(self) -> None:
        self.tracker.restart()
        self._refresh_live()

    def _show_overlay(self) -> None:
        self.overlay.show()
        self.overlay.raise_()

    # --- price check ------------------------------------------------------------

    def _feedback(self):
        if getattr(self, "_price_feedback", None) is None:
            from .toast import PriceFeedback

            self._price_feedback = PriceFeedback(self.overlay, parent=self)
        return self._price_feedback

    def price_check(self, name: str, others: list[str] | None = None) -> None:
        """Copy the item name and bring up mesos.watch (off the GUI thread; a toast shows progress)."""
        self.query.setText(name)
        QGuiApplication.clipboard().setText(name)
        extra = f"  (also seen: {', '.join(others)})" if others else ""
        self._feedback().start(name, lambda what: self.show_notice(
            f"{what}. '{name}' copied: press Ctrl+V in the search box.{extra}", timeout_s=20))

    def show_notice(self, msg: str, timeout_s: float = 0) -> None:
        """Show ``msg`` under the search bar (empty hides it). With ``timeout_s`` it is temporary:
        when it ends, the last persistent notice (a warning, say) comes back."""
        self._notice_timer.stop()
        if not timeout_s:
            self._sticky = msg
        self.notice.setText(msg)
        self.notice.setVisible(bool(msg))
        if msg and timeout_s:
            self._notice_timer.start(int(timeout_s * 1000))

    def _end_timed_notice(self) -> None:
        self.show_notice(self._sticky)

    def _search(self) -> None:
        name = self.query.text().strip()
        if name:
            self.price_check(name)

    def show_price(self, items) -> None:
        """Hotkey result: (name, item_id) candidates, hovered item first."""
        if items and items[0][0]:  # "" = the hovered panel's title couldn't be read
            self.price_check(items[0][0], [n for n, _ in items[1:] if n])
        else:
            self._feedback().nothing_found()

    def closeEvent(self, e):
        self.tracker.end_session()  # save what we have
        self.overlay.close()
        super().closeEvent(e)
