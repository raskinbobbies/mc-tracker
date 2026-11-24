"""Main window: live status, price check, and session history."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
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
from .overlay import Overlay, Tile, fmt_dur, fmt_num
from .session import Tracker, sessions_dir
from .theme import GUI_QSS

CREDIT = (
    'Game data: <a href="https://meowdb.com/msclassic">NiaDB</a> · '
    'Sprites: <a href="https://maplestory.io">maplestory.io</a> · '
    'Prices: <a href="https://mesos.watch">mesos.watch</a> · '
    "MapleStory © Nexon. Fan project, not affiliated."
)

COLUMNS = ["Date", "Start", "Map", "Time", "EXP", "EXP/min", "Kills", "Lv"]


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


class EntryGrid(QWidget):
    """Sprites/icons with name and count, wrapping into rows."""

    COLS = 4

    def __init__(self, kind: str, assets: AssetStore):
        super().__init__()
        self.kind, self.assets = kind, assets
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(self.COLS, 1)  # keep tiles packed to the left

    def show_entries(self, entries: list[history.Entry]) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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


class MainWindow(QMainWindow):
    def __init__(self, tracker: Tracker, overlay: Overlay, assets: AssetStore, hotkey: str):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(1100, 720)
        self.setStyleSheet(GUI_QSS)
        self.tracker, self.overlay, self.assets = tracker, overlay, assets
        self.sessions: list[history.SavedSession] = []

        # --- live bar ---------------------------------------------------------
        self.live_title = QLabel(objectName="h2")
        self.live_sub = QLabel(objectName="muted")
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._toggle_pause)
        restart = QPushButton("New session", objectName="primary")
        restart.setToolTip("Save the current session and start a new one")
        restart.clicked.connect(self._restart)
        pop = QPushButton("Overlay")
        pop.clicked.connect(self._show_overlay)
        live_text = QVBoxLayout()
        live_text.addWidget(self.live_title)
        live_text.addWidget(self.live_sub)
        live = QHBoxLayout()
        live.addLayout(live_text, 1)
        for b in (self.pause_btn, restart, pop):
            live.addWidget(b)
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

        self.table = QTableWidget(0, len(COLUMNS))
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
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setMinimumSectionSize(40)
        for c in range(3, len(COLUMNS)):  # numbers: right-align header to match cells
            self.table.horizontalHeaderItem(c).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
            ]
            cells[0].setData(Qt.ItemDataRole.UserRole + 1, str(s.path))
            cells[2].setToolTip(s.map)
            for c, item in enumerate(cells):
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
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
            self.live_title.setText(f"{where} · {hint}")
            self.live_sub.setText(
                f"Today: +{fmt_num(today['exp'])} EXP · {today['sessions']} sessions · "
                f"{fmt_dur(today['seconds'])} grinding · {today['kills']:,} kills"
            )
            return
        status = "Paused" if tr.state == "paused" else ("Idle" if s.is_idle() else "Tracking")
        per_hr = s.rate_per_hour()
        self.live_title.setText(f"{where} · {status} · {fmt_num(per_hr / 60)} EXP/min")
        self.live_sub.setText(
            f"Session {fmt_dur(s.active_s)} · +{fmt_num(s.exp.net)} EXP · {s.total_kills:,} kills · "
            f"Lv {s.level} {s.exp_percent:.2f}% · next level in {fmt_dur(s.seconds_to_level())}"
        )

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

    def price_check(self, name: str, others: list[str] | None = None) -> None:
        """Copy the item name and bring up mesos.watch in the user's browser."""
        from .browser import focus_mesos_watch

        self.query.setText(name)
        QGuiApplication.clipboard().setText(name)
        what = focus_mesos_watch()
        extra = f"  (also seen: {', '.join(others)})" if others else ""
        self.statusBar().showMessage(f"{what}. '{name}' copied: press Ctrl+V in the search box.{extra}")

    def _search(self) -> None:
        name = self.query.text().strip()
        if name:
            self.price_check(name)

    def show_price(self, items) -> None:
        """Hotkey result: (name, item_id) candidates, hovered item first."""
        if items:
            self.price_check(items[0][0], [n for n, _ in items[1:]])

    def closeEvent(self, e):
        self.tracker.end_session()  # save what we have
        self.overlay.close()
        super().closeEvent(e)
