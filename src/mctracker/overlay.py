"""Frameless, always-on-top, draggable session overlay to place over the game."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QMovie, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .assets import AssetStore
from .session import Tracker

from .theme import OVERLAY_QSS as STYLE, SECONDARY_SPAN

TILE = QSize(36, 36)


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
        lay.addWidget(self.img)
        lay.addWidget(self.count)
        self.setToolTip(tooltip)
        self._movie: QMovie | None = None

    def set_image(self, path: str) -> None:
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
    def __init__(self, kind: str, assets: AssetStore):
        super().__init__()
        self.kind, self.assets = kind, assets
        self.names: dict = {}
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.tiles: dict[int, Tile] = {}

    def update_counts(self, counts, names: dict) -> None:
        self.names = names
        shown = dict(counts.most_common(8))
        for key in list(self.tiles):  # drop tiles from a previous session
            if key not in shown:
                self.tiles.pop(key).deleteLater()
        for i, (key, n) in enumerate(counts.most_common(8)):
            tile = self.tiles.get(key)
            if tile is None:
                name = self.names.get(("mob" if self.kind == "mob" else "item", key), str(key))
                tile = self.tiles[key] = Tile(name)
                if self.kind == "mob":
                    self.assets.mob_sprite(key, tile.set_image)
                else:
                    self.assets.item_icon(key, tile.set_image)
            self.grid.addWidget(tile, 0, i)
            tile.count.setText(f"×{fmt_num(n)}")
        self.setVisible(bool(counts))


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

        self.map = QLabel(objectName="muted")
        self.pause_btn = QPushButton("\u23f8", toolTip="Pause tracking")
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
        self.stats = QLabel()

        self.kills_lbl = QLabel("PARTY KILLS", objectName="section")
        self.kills = TileRow("mob", assets)
        self.loot_lbl = QLabel("LOOT", objectName="section")
        self.loot = TileRow("item", assets)
        self.used_lbl = QLabel("USED", objectName="section")
        self.used = TileRow("item", assets)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(2)
        lay.addLayout(top)
        lay.addWidget(self.rate)
        lay.addWidget(self.sub)
        lay.addWidget(self.stats)
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

    def refresh(self) -> None:
        tr = self.tracker
        tr.tick()
        state = tr.state
        self.pause_btn.setText("\u25b6" if state == "paused" else "\u23f8")
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
            for lbl, row in ((self.kills_lbl, self.kills), (self.loot_lbl, self.loot), (self.used_lbl, self.used)):
                lbl.hide()
                row.hide()
            self.adjustSize()
            return

        status = "Paused" if state == "paused" else "Idle" if s.is_idle() else "Tracking"
        self.map.setText(f"{where} \u00b7 {status}")
        per_hr = s.rate_per_hour()
        self.rate.setText(self._headline(f"{fmt_num(per_hr / 60)} EXP/min", f"{fmt_num(per_hr)} EXP/hr"))
        self.sub.setText(
            f"Lv {s.level} \u00b7 {s.exp_percent:.2f}% \u00b7 next level in {fmt_dur(s.seconds_to_level())}"
        )
        self.stats.setText(
            f"Session {fmt_dur(s.active_s)} \u00b7 +{fmt_num(s.exp.net)} EXP \u00b7 {fmt_num(s.mesos)} mesos"
        )
        missed = s.missed_kills
        kills = f"{s.total_kills:,}" + (f" (+~{missed:,} unread)" if missed else "")
        self.kills_lbl.setText(f"PARTY KILLS \u00b7 {kills}")
        self.kills_lbl.setVisible(s.total_kills > 0)
        self.kills.update_counts(s.kills, s.names)
        for lbl, row, counts in ((self.loot_lbl, self.loot, s.loot), (self.used_lbl, self.used, s.used)):
            row.update_counts(counts, s.names)
            lbl.setVisible(bool(counts))
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
