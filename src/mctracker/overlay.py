"""Frameless, always-on-top, draggable session overlay to place over the game."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QMovie, QPainter, QPen, QPixmap
import shiboken6
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .assets import AssetStore
from .items import OVERLAY_SCALE_MAX, OVERLAY_SCALE_MIN
from .session import Session, Tracker
from .theme import INK_MUTED, WOOD, overlay_qss, secondary_span

TILE = QSize(32, 32)
SCALE_STEP = 0.1  # Ctrl+wheel step
WHEEL_NOTCH = 120  # angleDelta units per wheel step; smaller deltas (touchpads) add up
APPLY_MS = 40  # during a grip drag, re-lay out at most this often
SAVE_MS = 500  # write the scale to disk this long after the last change


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


def potions_used(s: Session) -> int:
    return sum(s.used.values())


class Tile(QWidget):
    """Sprite or icon with a count underneath."""

    def __init__(self, tooltip: str, scale: float = 1.0):
        super().__init__()
        self.scale = scale
        self._src: QPixmap | None = None  # the image as loaded, so a rescale never re-reads the file
        self.img = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.count = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.count, 0, Qt.AlignmentFlag.AlignHCenter)
        self.setToolTip(tooltip)
        self._movie: QMovie | None = None
        self.set_scale(scale)

    def set_scale(self, scale: float) -> None:
        """Resize the icon box (and the image in it) to ``scale`` times the base size."""
        self.scale = scale
        self.img.setFixedSize(self._scaled(TILE))
        # Fixed width keeps the count right under its icon (no stretching).
        self.setFixedWidth(round((TILE.width() + 12) * scale))
        self._render()

    def _scaled(self, size: QSize) -> QSize:
        return QSize(max(1, round(size.width() * self.scale)), max(1, round(size.height() * self.scale)))

    def set_image(self, path: str) -> None:
        if not shiboken6.isValid(self):  # tile was removed before its image arrived
            return
        self._src = QPixmap(path)  # for a GIF: its first frame, only used for the size
        if path.endswith(".gif"):
            self._movie = QMovie(path)
            self.img.setMovie(self._movie)
            self._render()
            self._movie.start()
        else:
            self._movie = None
            self._render()

    def _render(self) -> None:
        """Show the loaded image at the current scale (a GIF keeps playing, only its size changes)."""
        if self._src is None:
            return
        target = self._scaled(self._fit(self._src.size()))
        if self._movie is not None:
            self._movie.setScaledSize(target)
        else:
            mode = (Qt.TransformationMode.SmoothTransformation if self.scale < 1
                    else Qt.TransformationMode.FastTransformation)
            self.img.setPixmap(self._src.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, mode))

    @staticmethod
    def _fit(size: QSize) -> QSize:
        """The image's size at 100%: native, but shrunk to fit the base tile."""
        if size.isEmpty():
            return TILE
        return size.scaled(TILE, Qt.AspectRatioMode.KeepAspectRatio) if (
            size.width() > TILE.width() or size.height() > TILE.height()
        ) else size


class TileRow(QWidget):
    def __init__(self, kind: str, assets: AssetStore, empty: str = "", scale: float = 1.0):
        super().__init__()
        self.kind, self.assets, self.scale = kind, assets, scale
        self.empty = QLabel(empty, objectName="muted", parent=self)
        self.empty.hide()
        self.names: dict = {}
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(round(6 * scale))
        self.row.addStretch(1)  # one persistent stretch, always last
        self.tiles: dict[int, Tile] = {}
        self._layout_key: tuple | None = None  # (keys in order, empty label shown) as laid out

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self.row.setSpacing(round(6 * scale))
        for tile in self.tiles.values():
            tile.set_scale(scale)

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
                tile = self.tiles[key] = Tile(name, self.scale)
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


class ScaleGrip(QWidget):
    """Corner grip: drag to scale the whole overlay, double-click to reset to 100%."""

    SIZE = 14

    def __init__(self, overlay: "Overlay"):
        super().__init__()
        self.overlay = overlay
        self._start: tuple[QPoint, float, QSize] | None = None
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Drag to resize \u00b7 Ctrl+wheel \u00b7 double-click to reset")
        self.set_scale(overlay.scale)

    def set_scale(self, scale: float) -> None:
        n = max(8, round(self.SIZE * scale))
        self.setFixedSize(n, n)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(INK_MUTED if self._start else WOOD), max(1.0, 1.5 * self.overlay.scale)))
        n = self.width()
        for k in (0.3, 0.6, 0.9):  # three diagonal ticks in the bottom-right corner, like a size grip
            d = round(n * k)
            p.drawLine(n - 2 - d, n - 2, n - 2, n - 2 - d)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = (e.globalPosition().toPoint(), self.overlay.scale, self.overlay.size())
            self.update()

    def mouseMoveEvent(self, e) -> None:
        if self._start is None:
            return
        origin, scale, size = self._start
        d = e.globalPosition().toPoint() - origin
        # Dragging out by x% of the window's width/height grows the scale by x% (the average of both).
        grow = (d.x() / max(1, size.width()) + d.y() / max(1, size.height())) / 2
        self.overlay.set_scale(scale * (1 + grow), save=False, defer=True)

    def mouseReleaseEvent(self, e) -> None:
        if self._start is not None:
            self._start = None
            self.overlay.flush_scale()
            self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        self._start = None
        self.overlay.set_scale(1.0)


class Overlay(QWidget):
    def __init__(self, tracker: Tracker, assets: AssetStore):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.tracker = tracker
        self._drag: QPoint | None = None
        self.scale = min(OVERLAY_SCALE_MAX, max(OVERLAY_SCALE_MIN, tracker.settings.overlay_scale))
        self._applied = self.scale  # the scale the widgets are laid out at (lags self.scale mid-drag)
        self._wheel = 0  # unspent Ctrl+wheel delta
        self._apply_timer = QTimer(self, singleShot=True, interval=APPLY_MS, timeout=self._apply_pending)
        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_MS, timeout=self.save_scale)

        frame = self.frame = QWidget(objectName="overlay")
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
        self.opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100))
        self.opacity.installEventFilter(self)  # Ctrl+wheel over the slider scales, not dims
        top = self.top = QHBoxLayout()
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
        grid = self.grid = QGridLayout(self.panel)
        grid.setVerticalSpacing(0)
        self.panel_keys: list[QLabel] = []
        self.panel_vals: list[QLabel] = []
        for r in range(4):
            k, v = QLabel(objectName="key"), QLabel(objectName="val", alignment=Qt.AlignmentFlag.AlignRight)
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1)
            self.panel_keys.append(k)
            self.panel_vals.append(v)

        body = self.body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addWidget(self.panel, 0, Qt.AlignmentFlag.AlignTop)

        self.kills_lbl = QLabel("PARTY KILLS", objectName="section")
        self.kills = TileRow("mob", assets, "No kills yet", self.scale)
        self.loot_lbl = QLabel("LOOT", objectName="section")
        self.loot = TileRow("item", assets, "Nothing picked up yet", self.scale)
        self.used_lbl = QLabel("USED", objectName="section")
        self.used = TileRow("item", assets, scale=self.scale)

        lay = self.lay = QVBoxLayout(frame)
        lay.addLayout(top)
        lay.addLayout(body)
        for w in (self.kills_lbl, self.kills, self.loot_lbl, self.loot, self.used_lbl, self.used):
            lay.addWidget(w)
        self.grip = ScaleGrip(self)  # bottom-right, in its own row so it never covers content
        lay.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignRight)
        self.rows = (self.kills, self.loot, self.used)
        self._apply_scale()

        self.timer = QTimer(self, interval=1000, timeout=self.refresh)
        self.timer.start()
        self.refresh()

    def _toggle_pause(self) -> None:
        self.tracker.toggle_pause()
        self.refresh()

    def _restart(self) -> None:
        self.tracker.restart()
        self.refresh()

    def _headline(self, main: str, secondary: str) -> str:
        return f"{main}<span style='{secondary_span(self._applied)}'>&nbsp;&nbsp;{secondary}</span>"

    # --- scale: fonts, margins, tiles, panel and grip all follow one factor -------------

    def _apply_scale(self) -> None:
        s = self._applied

        def n(v: float) -> int:
            return round(v * s)

        self.frame.setStyleSheet(overlay_qss(s))
        self.lay.setContentsMargins(n(12), n(8), n(12), n(4))
        self.lay.setSpacing(n(2))
        self.top.setSpacing(n(6))
        self.body.setSpacing(n(6))
        self.grid.setContentsMargins(n(8), n(4), n(8), n(4))
        self.grid.setHorizontalSpacing(n(10))
        self.opacity.setFixedWidth(n(60))
        for row in self.rows:
            row.set_scale(s)
        self.grip.set_scale(s)

    def set_scale(self, scale: float, save: bool = True, defer: bool = False) -> None:
        """Scale the whole overlay (clamped to 60%-200%).

        ``save`` writes it to the settings now. ``defer`` (grip drag) lays out at most once per
        APPLY_MS instead of on every call; ``flush_scale`` finishes it.
        """
        self.scale = round(min(OVERLAY_SCALE_MAX, max(OVERLAY_SCALE_MIN, scale)), 2)
        if defer:
            if not self._apply_timer.isActive():
                self._apply_timer.start()
        else:
            self._apply_pending()
        if save:
            self.save_scale()

    def _apply_pending(self) -> None:
        self._apply_timer.stop()
        if self._applied != self.scale:
            self._applied = self.scale
            self._apply_scale()
            self.refresh()  # rebuilds the rich text at the new size, then resizes
            self._fit_size()  # the stylesheet's new font sizes only reach the size hints after refresh()

    def flush_scale(self) -> None:
        """Apply any pending drag change and save the scale now."""
        self._apply_pending()
        self.save_scale()

    def _fit_size(self) -> None:
        """Size the window to its content. Like adjustSize(), but not capped at 2/3 of the
        screen, which would clip a large overlay."""
        for w in self.findChildren(QWidget):
            w.ensurePolished()  # sizeHints must reflect a just-changed stylesheet
        self.layout().invalidate()
        self.layout().activate()
        self.resize(self.sizeHint())

    def save_scale(self) -> None:
        self._save_timer.stop()
        settings = self.tracker.settings
        if settings.overlay_scale != self.scale:
            settings.overlay_scale = self.scale
            try:
                settings.save()
            except OSError:
                pass  # applies for this run, just not remembered

    def eventFilter(self, obj, e) -> bool:
        if obj is self.opacity and e.type() == QEvent.Type.Wheel and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.wheelEvent(e)
            return True
        return super().eventFilter(obj, e)

    def wheelEvent(self, e) -> None:
        delta = e.angleDelta().y()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier and delta:
            if self._wheel * delta < 0:  # turned the other way: drop what was left over
                self._wheel = 0
            self._wheel += delta
            steps = int(self._wheel / WHEEL_NOTCH)  # one 10% step per full notch, however it arrives
            if steps:
                self._wheel -= steps * WHEEL_NOTCH
                self.set_scale(round(self.scale / SCALE_STEP) * SCALE_STEP + steps * SCALE_STEP, save=False)
                self._save_timer.start()  # one disk write after the wheel stops
            e.accept()
        else:
            e.ignore()

    def hideEvent(self, e) -> None:
        if self._save_timer.isActive():
            self.save_scale()
        super().hideEvent(e)

    def closeEvent(self, e) -> None:
        self.flush_scale()
        super().closeEvent(e)

    def contextMenuEvent(self, e) -> None:
        menu = QMenu(self)
        reset = menu.addAction("Reset size (100%)", lambda: self.set_scale(1.0))
        reset.setEnabled(self.scale != 1.0)
        menu.exec(e.globalPos())

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
            self._fit_size()
            return

        status = "Paused" if state == "paused" else "Idle" if s.is_idle() else None
        self.map.setText(f"{where} \u00b7 {fmt_dur(s.active_s)}"
                         + (f" \u00b7 {status}" if status else ""))
        per_hr = s.rate_per_hour()
        self.rate.setText(self._headline(f"{fmt_num(per_hr / 60)} EXP/min", f"{fmt_num(per_hr)} EXP/hr"))
        self.sub.setText(f"Lv {s.level} · {s.exp_percent:.2f}% · {exp_to_level(s)}")
        self.stats.setText(f"+{fmt_num(s.exp.net)} EXP this session")
        if s.items_used:  # experimental: every spending figure is an estimate
            # Items used are the tiles under "USED (estimate)" below.
            money = [("MESOS", fmt_num(s.mesos)), ("NET (EST.)", f"~{fmt_num(s.net_mesos)}")]
        else:
            money = [("MESOS", fmt_num(s.mesos)), ("KILLS", f"{s.total_kills:,}")]
        self._set_panel([
            ("AVG", f"{fmt_num(s.session_rate_per_hour() / 60)}/min"),
            (f"LV {s.level + 1} IN", fmt_eta(s.seconds_to_level())),
            *money,
        ])
        self.panel.show()
        left = s.unassigned_kills
        kills = f"{s.total_kills:,}" + (f" ({left:,} unassigned)" if left else "")
        title = "PARTY KILLS" if s.party_seen else "KILLS"
        self.kills_lbl.setText(f"{title} \u00b7 {kills}")
        self.kills_lbl.show()
        self.kills.empty.setText("No kills yet" if not s.total_kills else "Monsters not identified")
        self.kills.update_counts(s.kills, s.names)
        self.loot_lbl.show()
        self.loot.update_counts(s.loot, s.names)
        self.used.update_counts(s.used, s.names)
        self.used_lbl.setVisible(bool(s.used))
        self.used_lbl.setText("USED (estimate)")
        self._fit_size()

    # Drag anywhere to move
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None
