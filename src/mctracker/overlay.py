"""Frameless, always-on-top, draggable session overlay to place over the game."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QMovie, QPixmap
import shiboken6
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .assets import AssetStore
from .session import Session, Tracker

from .theme import OVERLAY_QSS as STYLE, SECONDARY_SPAN

TILE = QSize(32, 32)


def fmt_num(n: float) -> str:
    n = int(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{n:,}"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def fmt_eta(s: float | None) -> str:
    """Time-to-level: "<1m", "37m", "2h 14m", or "\u2014" with no rate yet."""
    if s is None:
        return "\u2014"
    m = int(s // 60)
    if m < 1:
        return "<1m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def level_eta(s: Session) -> str:
    """"Lv 46 in 2h 14m": the next level and the time left at the current rate."""
    return f"Lv {s.level + 1} in {fmt_eta(s.seconds_to_level())}"


def exp_to_level(s: Session) -> str:
    left = s.exp.exp_remaining()
    return f"{left:,} to level" if left else "\u2014"


def net_mesos(s: Session) -> int:
    """Mesos picked up minus spent when the session tracks it, else picked up."""
    v = getattr(s, "net_mesos", None)
    return int(v() if callable(v) else v) if v is not None else s.mesos


def potions_used(s: Session) -> int:
    return sum(s.used.values())


class Tile(QWidget):
    """Sprite or icon with a count underneath."""

    def __init__(self, tooltip: str):
        super().__init__()
        self.img = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.img.setFixedSize(TILE)
        self.count = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.count, 0, Qt.AlignmentFlag.AlignHCenter)
        # Fixed width keeps the count right under its icon (no stretching).
        self.setFixedWidth(TILE.width() + 12)
        self.setToolTip(tooltip)
        self._movie: QMovie | None = None

    def set_image(self, path: str) -> None:
        if not shiboken6.isValid(self):  # tile was removed before its image arrived
            return
        if path.endswith(".gif"):
            self._movie = QMovie(path)
            self._movie.setScaledSize(self._fit(QPixmap(path).size()))
            self.img.setMovie(self._movie)
            self._movie.start()
        else:
            pm = QPixmap(path)
            self.img.setPixmap(pm.scaled(self._fit(pm.size()), Qt.AspectRatioMode.KeepAspectRatio))

    @staticmethod
    def _fit(size: QSize) -> QSize:
        if size.isEmpty():
            return TILE
        return size.scaled(TILE, Qt.AspectRatioMode.KeepAspectRatio) if (
            size.width() > TILE.width() or size.height() > TILE.height()
        ) else size


class TileRow(QWidget):
    def __init__(self, kind: str, assets: AssetStore, empty: str = ""):
        super().__init__()
        self.kind, self.assets = kind, assets
        self.empty = QLabel(empty, objectName="muted", parent=self)
        self.empty.hide()
        self.names: dict = {}
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.row.addStretch(1)  # one persistent stretch, always last
        self.tiles: dict[int, Tile] = {}
        self._layout_key: tuple | None = None  # (keys in order, empty label shown) as laid out

    def update_counts(self, counts, names: dict) -> None:
        self.names = names
        top = counts.most_common(8)
        order = tuple(key for key, _ in top)
        key = (order, not order and bool(self.empty.text()))
        if key != self._layout_key:  # only touch the layout when the tiles change
            self._relayout(order, names)
            self._layout_key = key
        for key, n in top:
            self.tiles[key].count.setText(f"×{fmt_num(n)}")
        self.setVisible(bool(counts) or bool(self.empty.text()))

    def _relayout(self, order: tuple[int, ...], names: dict) -> None:
        for key in list(self.tiles):  # drop tiles that fell out of the top list
            if key not in order:
                tile = self.tiles.pop(key)
                self.row.removeWidget(tile)
                tile.deleteLater()
        for w in [*self.tiles.values(), self.empty]:
            self.row.removeWidget(w)  # also frees the layout item
        for i, key in enumerate(order):
            tile = self.tiles.get(key)
            if tile is None:
                name = names.get(("mob" if self.kind == "mob" else "item", key), str(key))
                tile = self.tiles[key] = Tile(name)
                if self.kind == "mob":
                    self.assets.mob_sprite(key, tile.set_image)
                else:
                    self.assets.item_icon(key, tile.set_image)
            self.row.insertWidget(i, tile)  # left-aligned, in count order, before the stretch
        if not order and self.empty.text():
            self.row.insertWidget(0, self.empty)
            self.empty.show()
        else:
            self.empty.hide()


class Overlay(QWidget):
    def __init__(self, tracker: Tracker, assets: AssetStore):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.tracker = tracker
        self._drag: QPoint | None = None

        frame = QWidget(objectName="overlay")
        frame.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        self.map = QLabel(objectName="head")
        self.pause_btn = QPushButton("\u23f8\ufe0e", toolTip="Pause tracking")
        self.pause_btn.clicked.connect(self._toggle_pause)
        restart = QPushButton("\u27f2", toolTip="Save this session and start a new one")
        restart.clicked.connect(self._restart)
        close = QPushButton("\u00d7", toolTip="Hide overlay")
        close.clicked.connect(self.hide)
        self.opacity = QSlider(Qt.Orientation.Horizontal, minimum=30, maximum=100, value=100, toolTip="Opacity")
        self.opacity.setFixedWidth(60)
        self.opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100))
        top = QHBoxLayout()
        top.addWidget(self.map, 1)
        top.addWidget(self.opacity)
        top.addWidget(self.pause_btn)
        top.addWidget(restart)
        top.addWidget(close)

        self.rate = QLabel(objectName="big")
        self.sub = QLabel(objectName="muted")
        self.stats = QLabel(objectName="muted")
        left = QVBoxLayout()
        left.setSpacing(0)
        left.addWidget(self.rate)
        left.addWidget(self.sub)
        left.addWidget(self.stats)
        left.addStretch(1)

        # Boxed side panel: AVG / LV n IN / MESOS / POTIONS
        self.panel = QFrame(objectName="panel")
        grid = QGridLayout(self.panel)
        grid.setContentsMargins(8, 4, 8, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(0)
        self.panel_keys: list[QLabel] = []
        self.panel_vals: list[QLabel] = []
        for r in range(4):
            k, v = QLabel(objectName="key"), QLabel(objectName="val", alignment=Qt.AlignmentFlag.AlignRight)
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1)
            self.panel_keys.append(k)
            self.panel_vals.append(v)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addWidget(self.panel, 0, Qt.AlignmentFlag.AlignTop)

        self.kills_lbl = QLabel("PARTY KILLS", objectName="section")
        self.kills = TileRow("mob", assets, "No kills yet")
        self.loot_lbl = QLabel("LOOT", objectName="section")
        self.loot = TileRow("item", assets, "Nothing picked up yet")
        self.used_lbl = QLabel("USED", objectName="section")
        self.used = TileRow("item", assets)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(2)
        lay.addLayout(top)
        lay.addLayout(body)
        for w in (self.kills_lbl, self.kills, self.loot_lbl, self.loot, self.used_lbl, self.used):
            lay.addWidget(w)

        self.timer = QTimer(self, interval=1000, timeout=self.refresh)
        self.timer.start()
        self.refresh()

    def _toggle_pause(self) -> None:
        self.tracker.toggle_pause()
        self.refresh()

    def _restart(self) -> None:
        self.tracker.restart()
        self.refresh()

    @staticmethod
    def _headline(main: str, secondary: str) -> str:
        return f"{main}<span style='{SECONDARY_SPAN}'>&nbsp;&nbsp;{secondary}</span>"

    def _set_panel(self, rows: list[tuple[str, str]]) -> None:
        for (k, v), kl, vl in zip(rows, self.panel_keys, self.panel_vals):
            kl.setText(k)
            vl.setText(v)

    def refresh(self) -> None:
        tr = self.tracker
        tr.tick()
        state = tr.state
        self.pause_btn.setText("\u25b6\ufe0e" if state == "paused" else "\u23f8\ufe0e")
        self.pause_btn.setToolTip("Resume tracking" if state == "paused" else "Pause tracking")
        where = tr.map_name or "Unknown map"
        s = tr.session

        if s is None:
            # Waiting (town / not fighting yet): show today's totals.
            today = tr.today()
            hint = ("Paused" if state == "paused"
                    else "Waiting for EXP" if tr.combat_map else "Not a hunting map")
            self.map.setText(f"{where} \u00b7 {hint}")
            self.rate.setText(self._headline(f"+{fmt_num(today['exp'])} EXP", "today"))
            avg = today["exp"] / today["seconds"] * 60 if today["seconds"] else 0
            n = today["sessions"]
            self.sub.setText(
                f"{n} session{'s' if n != 1 else ''} \u00b7 "
                f"{fmt_dur(today['seconds'])} grinding \u00b7 {fmt_num(avg)} EXP/min avg"
            )
            self.stats.setText(f"{today['kills']:,} party kills today")
            self.panel.hide()
            for lbl, row in ((self.kills_lbl, self.kills), (self.loot_lbl, self.loot), (self.used_lbl, self.used)):
                lbl.hide()
                row.hide()
            self.adjustSize()
            return

        status = "Paused" if state == "paused" else "Idle" if s.is_idle() else None
        self.map.setText(f"{where} \u00b7 {fmt_dur(s.active_s)}" + (f" \u00b7 {status}" if status else ""))
        per_hr = s.rate_per_hour()
        self.rate.setText(self._headline(f"{fmt_num(per_hr / 60)} EXP/min", f"{fmt_num(per_hr)} EXP/hr"))
        self.sub.setText(f"Lv {s.level} \u00b7 {s.exp_percent:.2f}% \u00b7 {exp_to_level(s)}")
        self.stats.setText(f"+{fmt_num(s.exp.net)} EXP this session")
        self._set_panel([
            ("AVG", f"{fmt_num(s.session_rate_per_hour() / 60)}/min"),
            (f"LV {s.level + 1} IN", fmt_eta(s.seconds_to_level())),
            ("MESOS", fmt_num(net_mesos(s))),
            ("POTIONS", f"{potions_used(s):,}"),
        ])
        self.panel.show()
        missed = s.missed_kills
        kills = f"{s.total_kills:,}" + (f" (+~{missed:,} unread)" if missed else "")
        self.kills_lbl.setText(f"PARTY KILLS \u00b7 {kills}")
        self.kills_lbl.show()
        self.kills.empty.setText("No kills yet" if not s.total_kills else "Monsters not identified")
        self.kills.update_counts(s.kills, s.names)
        self.loot_lbl.show()
        self.loot.update_counts(s.loot, s.names)
        self.used.update_counts(s.used, s.names)
        self.used_lbl.setVisible(bool(s.used))
        self.adjustSize()

    # Drag anywhere to move
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None
