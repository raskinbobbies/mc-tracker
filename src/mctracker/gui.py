"""Main window: live status, price check, and session history."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from .assets import AssetStore
from .overlay import Overlay, Tile, fmt_dur, fmt_eta, fmt_num, potions_used, warmed_up
from .session import Tracker, sessions_dir
from .theme import GAIN, GUI_QSS, INK_MUTED, LOSS, ORANGE, SECONDARY_SPAN, WOOD_DARK

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


def best_index(values: list[int]) -> int | None:
    """Index of the highest minute, or None when there is nothing to mark."""
    if not values or max(values) <= 0:
        return None
    return values.index(max(values))


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


PILL_NEAR_PCT = 2  # within +/-2% (as rounded and shown) reads as "at your average"


def last_minute_pill(minutes: list[int], avg_per_min: float) -> tuple[str, str] | None:
    """(text, tone) comparing the last *finished* minute with the session average.

    ``minutes`` is ``Session.exp_per_minute``, whose last entry is the current partial
    minute, so it is dropped. ``avg_per_min`` should exclude the judged minute (see
    ``average_before_last_minute``). Hidden (None) until two minutes have finished or while
    there is no average to compare against. Tone is "over", "under" or "near".
    """
    finished = minutes[:-1]
    if len(finished) < 2 or avg_per_min <= 0:
        return None
    pct = round((finished[-1] - avg_per_min) / avg_per_min * 100)  # band and text use the same figure
    if abs(pct) <= PILL_NEAR_PCT:
        return "at your average", "near"
    if pct > 0:
        return f"{pct}% over your average", "over"
    return f"{-pct}% under your average", "under"


def average_before_last_minute(net_exp: float, active_s: float, last_minute_exp: float) -> float:
    """EXP/min over the session excluding the finished minute the pill judges (0 if nothing else yet)."""
    rest_s = active_s - 60
    return (net_exp - last_minute_exp) / rest_s * 60 if rest_s > 0 else 0.0


def rate_headline(per_hr: float, active_s: float) -> str:
    """EXP/min headline (rich text); a dash and "warming up" until the session has run a minute."""
    if not warmed_up(active_s):
        return f"— EXP/min<span style='{SECONDARY_SPAN}'>&nbsp;&nbsp;warming up</span>"
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


class EntryGrid(QWidget):
    """Sprites/icons with name and count, wrapping into rows."""

    COLS = 3  # 3 x 96px tiles fit the default detail width without scrolling

    def __init__(self, kind: str, assets: AssetStore):
        super().__init__()
        self.kind, self.assets = kind, assets
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(self.COLS, 1)  # keep tiles packed to the left
        self._tiles: list[Tile] = []
        self._shown: list[tuple] | None = None

    def show_entries(self, entries: list[history.Entry]) -> None:
        shown = [(e.id, e.name, e.count) for e in entries]
        if shown == self._shown:  # nothing changed: leave the layout alone
            return
        self._shown = shown
        for w in self._tiles:
            self.grid.removeWidget(w)  # frees the layout item as well
            w.deleteLater()
        self._tiles = []
        for i, e in enumerate(entries):
            tile = Tile(e.name)
            tile.count.setText(f"{e.name}\n×{e.count:,}")
            tile.count.setWordWrap(True)
            tile.setFixedWidth(96)
            if e.id is not None:
                if self.kind == "kill":
                    self.assets.mob_sprite(e.id, tile.set_image)
                else:
                    self.assets.item_icon(e.id, tile.set_image)
            self.grid.addWidget(tile, i // self.COLS, i % self.COLS, Qt.AlignmentFlag.AlignTop)
            self._tiles.append(tile)
        self.setVisible(bool(entries))


class SessionDetail(QFrame):
    def __init__(self, assets: AssetStore, on_delete):
        super().__init__(objectName="card")
        self._session: history.SavedSession | None = None
        self.title = QLabel(objectName="h2", wordWrap=True)
        self.when = QLabel(objectName="muted", wordWrap=True)
        self.headline = QLabel(objectName="h1")
        self.stats = QLabel(objectName="muted")
        self.stats.setWordWrap(True)
        self.sections = {}
        body = QVBoxLayout()
        body.addWidget(self.title)
        body.addWidget(self.when)
        body.addWidget(self.headline)
        body.addWidget(self.stats)
        for kind, label in (("kill", "PARTY KILLS"), ("loot", "LOOT"), ("used", "USED")):
            lbl = QLabel(label, objectName="section")
            grid = EntryGrid(kind, assets)
            body.addWidget(lbl)
            body.addWidget(grid)
            self.sections[kind] = (lbl, grid)
        body.addStretch(1)

        delete = QPushButton("Delete session", objectName="danger")
        delete.clicked.connect(lambda: self._session and on_delete(self._session))
        self.delete_btn = delete
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(delete)
        body.addLayout(foot)

        inner = QWidget()
        inner.setLayout(body)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addWidget(scroll)
        self.show_session(None)

    def show_session(self, s: history.SavedSession | None) -> None:
        self._session = s
        self.delete_btn.setVisible(s is not None)
        if s is None:
            self.title.setText("No session selected")
            self.when.setText("Sessions are saved when you leave a map, press restart, or close the app.")
            self.headline.setText("")
            self.stats.setText("")
            for lbl, grid in self.sections.values():
                lbl.hide()
                grid.hide()
            return
        self.title.setText(s.map)
        self.when.setText(f"{s.started:%a %d %b %Y, %H:%M} · {fmt_dur(s.seconds)} grinding")
        self.headline.setText(f"{fmt_num(s.exp_per_min)} EXP/min")
        d = s.data
        parts = [
            f"+{fmt_num(s.exp)} EXP",
            f"{fmt_num(d.get('exp_per_hour', s.exp_per_min * 60))} EXP/hr",
            f"{s.kills:,} party kills",
        ]
        if s.levels:
            parts.append(f"{s.levels} level{'s' if s.levels != 1 else ''} gained")
        if d.get("exp_lost"):
            parts.append(f"-{fmt_num(d['exp_lost'])} EXP lost")
        if d.get("mesos"):
            parts.append(f"{fmt_num(d['mesos'])} mesos")
        self.stats.setText(" · ".join(parts))
        for kind, (lbl, grid) in self.sections.items():
            entries = s.entries(kind)
            grid.show_entries(entries)
            lbl.setVisible(bool(entries))
            if kind == "used":
                lbl.setText("USED")


class MainWindow(QMainWindow):
    def __init__(self, tracker: Tracker, overlay: Overlay, assets: AssetStore, hotkey: str):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(1100, 820)
        self.setStyleSheet(GUI_QSS)
        self.tracker, self.overlay, self.assets = tracker, overlay, assets
        self.sessions: list[history.SavedSession] = []

        # --- live bar ---------------------------------------------------------
        self.live_title = QLabel(objectName="h2")
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
        title_row = QHBoxLayout()
        title_row.addWidget(self.live_title, 1)
        for b in (self.pause_btn, restart, pop):
            title_row.addWidget(b)
        self.stat_avg = StatBox("AVG EXP/MIN")
        self.stat_best = StatBox("BEST MINUTE")
        self.stat_eta = StatBox("LEVEL IN")
        self.stat_left = StatBox("EXP TO LEVEL")
        stats = QHBoxLayout()
        for b in (self.stat_avg, self.stat_best, self.stat_eta, self.stat_left):
            stats.addWidget(b)
        self.spark = Sparkline()
        left = QVBoxLayout()
        left.setSpacing(4)
        left.addLayout(title_row)
        left.addWidget(self.live_sub)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.live_rate)
        rate_row.addWidget(self.rate_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        rate_row.addStretch(1)
        left.addLayout(rate_row)
        left.addLayout(stats)
        left.addWidget(QLabel("EXP PER MINUTE \u00b7 THIS MAP", objectName="section"))
        left.addWidget(self.spark, 1)
        self.mob_table = MonsterTable(assets)
        self.unknown_footer = UnknownKillsFooter()
        right = QVBoxLayout()
        right.addWidget(QLabel("MONSTERS ON THIS MAP", objectName="section"))
        right.addWidget(self.mob_table)
        right.addWidget(self.unknown_footer)
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
        split.setSizes([700, 400])

        credit = QLabel(CREDIT, openExternalLinks=True)
        credit.setStyleSheet("color: #2f6a99; font-size: 12px;")

        root = QVBoxLayout()
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)
        root.addLayout(brand_row)
        root.addWidget(live_card)
        root.addLayout(price)
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
        where = tr.map_name or "Unknown map"
        s = tr.session
        if s is None:
            today = tr.today()
            hint = "Paused" if tr.state == "paused" else ("Waiting for EXP" if tr.combat_map else "Not a hunting map")
            self.live_title.setText(f"{where} \u00b7 {hint}")
            self.live_sub.setText(
                f"Today: +{fmt_num(today['exp'])} EXP \u00b7 {today['sessions']} sessions \u00b7 "
                f"{fmt_dur(today['seconds'])} grinding \u00b7 {today['kills']:,} kills"
            )
            self.live_rate.setText("\u2014 EXP/min, so far")
            for b in (self.stat_avg, self.stat_best, self.stat_eta, self.stat_left):
                b.set("\u2014")
            self.stat_eta.set("\u2014", "LEVEL IN")
            self.spark.set_values([])
            self.mob_table.show_mobs(tr.mobs, {}, 0)
            self.rate_pill.hide()
            self.unknown_footer.set_count(0, False)
            return
        status = "Paused" if tr.state == "paused" else ("Idle" if s.is_idle() else "Tracking")
        per_hr = s.rate_per_hour()
        self.live_title.setText(f"{where} · {status} · {tr.party_label}")
        money = (f"{fmt_num(s.net_mesos)} net mesos ({fmt_num(s.net_mesos_per_hour())}/hr) · "
                 f"{potions_used(s):,} potions")
        self.live_sub.setText(
            f"Session {fmt_dur(s.active_s)} · +{fmt_num(s.exp.net)} EXP · {s.total_kills:,} {'party kills' if s.party_seen else 'kills'} · "
            f"{money} · Lv {s.level} {s.exp_percent:.2f}%"
        )
        warm = warmed_up(s.active_s)
        self.live_rate.setText(rate_headline(per_hr, s.active_s))
        if s.exp.warnings and s.exp.warnings[-1] != getattr(self, "_table_warning", None):
            self._table_warning = s.exp.warnings[-1]  # EXP table disagrees with the bar
            self.statusBar().showMessage(self._table_warning)
        avg_per_min = s.session_rate_per_hour() / 60  # same figure as the AVG EXP/MIN box
        minutes = s.exp_per_minute
        last = minutes[-2] if len(minutes) >= 2 else 0
        self.rate_pill.set_minutes(minutes if warm else [], average_before_last_minute(s.exp.net, s.active_s, last))
        self.unknown_footer.set_count(s.unassigned_kills, s.party_seen)
        self.stat_avg.set(fmt_num(avg_per_min) if warm else "—")
        self.stat_best.set(fmt_num(s.best_minute()))
        self.stat_eta.set(fmt_eta(s.seconds_to_level()) if warm else "—", f"LV {s.level + 1} IN")
        left = s.exp.exp_remaining()
        self.stat_left.set(f"{left:,}" if left else "\u2014")
        self.spark.set_values(s.exp_per_minute)
        self.mob_table.show_mobs(s.mobs or tr.mobs, s.kills, s.active_s)

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
        self._feedback().start(name, lambda what: self.statusBar().showMessage(
            f"{what}. '{name}' copied: press Ctrl+V in the search box.{extra}"))

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
