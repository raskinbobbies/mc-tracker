"""Frameless, always-on-top, draggable session overlay to place over the game."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import shiboken6
from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QMovie, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .assets import AssetStore
from .history import total_used
from .items import OVERLAY_SCALE_MAX, OVERLAY_SCALE_MIN
from .rates import average_before_last_minute, delta_tone, last_minute_delta
from .session import Session, Tracker
from .theme import GOLD, INK_MUTED, WOOD, overlay_qss, unit_span

TILE = QSize(32, 32)
SCALE_STEP = 0.1  # Ctrl+wheel step
WHEEL_NOTCH = 120  # angleDelta units per wheel step; smaller deltas (touchpads) add up
APPLY_MS = 40  # during a grip drag, re-lay out at most this often
SAVE_MS = 500  # write the scale to disk this long after the last change
ICON_SLOTS = 5  # tiles per row at 100% (an overflow tile counts as one)
OPACITY_STEPS = (100, 85, 70, 55, 40)
MINUS = "−"  # U+2212, same minus as the history table
DASH = "—"
ETA_CAP_S = 99 * 3600  # a level further away than this reads as no estimate


def fmt_num(n: float) -> str:
    """Full commas under 1,000, then K/M/B with two decimals, truncated (never rounded up):
    999,999 is "999.99K" and 1,999,999 is "1.99M", so a figure is never shown above its value."""
    n = int(n)
    for unit, div in (("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if abs(n) >= div:
            hundredths = abs(n) * 100 // div  # integer math: no float rounding
            return f"{'-' if n < 0 else ''}{hundredths // 100}.{hundredths % 100:02d}{unit}"
    return f"{n:,}"


def fmt_short(n: float) -> str:
    """Overlay figure: full commas under 100,000 ("10,929"), then K/M/B ("125.3K", "1.25M")."""
    n = round(n)
    if abs(n) < 100_000:
        return f"{n:,}".replace("-", MINUS)
    v = abs(n) / 1e3
    for unit in "KMB":
        dec = 1 if unit == "K" else 2
        if round(v, dec) < 1000 or unit == "B":
            return f"{MINUS if n < 0 else ''}{v:.{dec}f}{unit}"
        v /= 1e3
    return ""  # unreachable


def fmt_signed(n: float, plus: bool = False) -> str:
    """``fmt_short`` with an explicit sign: U+2212 when negative, "+" when ``plus`` and positive."""
    n = round(n)
    return ("+" if plus and n > 0 else "") + fmt_short(n)


WARMUP_S = 60  # no EXP/min, EXP/hr or level ETA until the session has run this long


def warmed_up(active_s: float) -> bool:
    """True once a session has run long enough for its rate figures to mean anything."""
    return active_s >= WARMUP_S


def fmt_dur(s: float | None) -> str:
    if s is None:
        return DASH
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def fmt_eta(s: float | None) -> str:
    """Time-to-level: "<1m", "37m", "2h 14m", or a dash with no rate yet or over 99 hours away."""
    if s is None or s > ETA_CAP_S:
        return DASH
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
    return f"{left:,} to level" if left else DASH


def potions_used(s: Session) -> int:
    """Items used this session: id'd and name-only alike (same rule as the history column)."""
    return total_used(s.used.values(), s.used_other.values())


@dataclass(frozen=True)
class Entry:
    """One icon in a row. ``key`` is ("mob", id), ("loot", id), ("used", id) or ("mesos",)."""

    key: tuple
    text: str  # the count badge
    tone: str = ""  # "loss" paints the badge red
    tip: str = ""


def plan_entries(entries: list[Entry], slots: int) -> tuple[list[Entry], int]:
    """The entries that fit in ``slots`` and how many are left out (the last slot becomes the "+N" tile)."""
    if len(entries) <= slots:
        return entries, 0
    keep = max(0, slots - 1)
    return entries[:keep], len(entries) - keep


def item_entries(mesos: int, loot: Counter, used: Counter, names: dict, slots: int = ICON_SLOTS) -> tuple[list[Entry], int]:
    """The one icon row: mesos first, then loot ("14") and used items (red "−3") by count."""
    rows = []
    if mesos > 0:
        rows.append(Entry(("mesos",), fmt_short(mesos), tip=f"{mesos:,} mesos picked up"))
    merged = [(n, 0, "loot", k) for k, n in loot.items() if n > 0] + [(n, 1, "used", k) for k, n in used.items() if n > 0]
    for n, _, kind, k in sorted(merged, key=lambda r: (-r[0], r[1], str(r[3]))):
        name = names.get(("item", k), str(k))
        if kind == "loot":
            rows.append(Entry(("loot", k), fmt_short(n), tip=f"{name}: {n:,} picked up"))
        else:
            rows.append(Entry(("used", k), fmt_signed(-n), "loss", f"{name}: {n:,} used"))
    return plan_entries(rows, slots)


def mob_entries(kills: Counter, names: dict, unassigned: int, slots: int = ICON_SLOTS) -> tuple[list[Entry], int, str]:
    """Monster row: a tile per mob with its kill count, plus a "+N unknown" note for unassigned kills."""
    rows = [Entry(("mob", k), f"×{fmt_short(n)}", tip=f"{names.get(('mob', k), str(k))}: {n:,} kills")
            for k, n in kills.most_common() if n > 0]
    note = f"+{unassigned:,} unknown" if unassigned > 0 else ""
    shown, more = plan_entries(rows, slots - 1 if note else slots)
    return shown, more, note


@dataclass(frozen=True)
class View:
    """Every figure the overlay shows for a running session, already formatted."""

    rate: str  # EXP/min, a dash while warming up
    warming: bool
    delta: tuple[str, str] | None  # (text, tone "over"/"under"/"near") vs the session average; None while warming up
    avg: str
    eta_label: str
    eta: str
    net: str
    net_rate: str
    net_tone: str  # "gain" / "loss" / ""
    potions: str
    potions_rate: str
    potions_tone: str
    extra: str  # the small line under the headline
    tip: str


def session_view(s: Session, today_exp: int | None = None) -> View:
    warm = warmed_up(s.active_s)
    per_hr = s.rate_per_hour()
    avg = s.session_rate_per_hour() / 60
    minutes = s.exp_per_minute
    last = minutes[-2] if len(minutes) >= 2 else 0
    pct = last_minute_delta(minutes if warm else [], average_before_last_minute(s.exp.net, s.active_s, last))
    delta = None if not warm else ("—", "near")  # a dash until two minutes have finished
    if pct is not None:
        tone = delta_tone(pct)
        delta = ("≈ AVG" if tone == "near" else f"▲{pct}%" if pct > 0 else f"▼{-pct}%", tone)
    net, potions = s.net_mesos, potions_used(s)
    mins = s.active_s / 60
    net_rate = net / mins if warm and mins > 0 else None
    pot_rate = potions / mins if warm and mins > 0 else None
    exp_line = f"+{fmt_short(s.exp.net)} EXP"
    tip = [f"Lv {s.level} · {s.exp_percent:.2f}% · {exp_to_level(s)}",
           f"{fmt_short(per_hr)} EXP/hr" if warm else "EXP/hr after the first minute",
           f"{fmt_short(s.exp.net)} EXP this session"]
    if today_exp is not None:
        tip.append(f"{fmt_short(today_exp)} EXP today")
    return View(
        rate=fmt_short(per_hr / 60) if warm else DASH,
        warming=not warm,
        delta=delta,
        avg=fmt_short(avg) if warm and avg > 0 else DASH,
        eta_label=f"LV {s.level + 1} IN",
        eta=fmt_eta(s.seconds_to_level()) if warm else DASH,
        net=fmt_signed(net, plus=True),
        net_rate=DASH if net_rate is None else fmt_signed(net_rate, plus=True) + "/min",
        net_tone="gain" if net > 0 else "loss" if net < 0 else "",
        potions=fmt_signed(-potions),
        potions_rate=DASH if pot_rate is None else fmt_signed(-pot_rate) + "/min",
        potions_tone="loss" if potions > 0 else "",
        extra=f"{fmt_short(per_hr)}/hr · {exp_line}" if warm else exp_line,
        tip="\n".join(tip),
    )


def set_tone(label: QLabel, tone: str) -> None:
    """Set the label's ``tone`` property (the stylesheet colours "gain"/"loss"/"warn") and repaint if it changed."""
    if (label.property("tone") or "") != tone:
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)


_coin: dict[int, QPixmap] = {}


def coin_pixmap(size: int = 28) -> QPixmap:
    """A drawn gold coin for the mesos tile (no game art)."""
    if size not in _coin:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#a8692c"), max(1.0, size / 12)))
        p.setBrush(QColor("#ffc94d"))
        m = size / 12
        p.drawEllipse(round(m), round(m), round(size - 2 * m), round(size - 2 * m))
        p.setPen(QPen(QColor(GOLD), max(1.0, size / 14)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        q = size * 0.3
        p.drawEllipse(round(q), round(q), round(size - 2 * q), round(size - 2 * q))
        p.end()
        _coin[size] = pm
    return _coin[size]


class Tile(QWidget):
    """Sprite or icon with a count underneath, or (``badge``) a boxed icon with the count over its corner."""

    def __init__(self, tooltip: str, scale: float = 1.0, badge: bool = False):
        super().__init__()
        self.scale = scale
        self.badge = badge
        self._src: QPixmap | None = None  # the image as loaded, so a rescale never re-reads the file
        self.img = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.count = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        if badge:
            self.setObjectName("badge")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            self.count.setParent(self)  # floats over the icon's bottom-right corner
            self.count.setObjectName("cnt")
            lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignCenter)
        else:
            lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(self.count, 0, Qt.AlignmentFlag.AlignHCenter)
        self.setToolTip(tooltip)
        self._movie: QMovie | None = None
        self.set_scale(scale)

    def set_scale(self, scale: float) -> None:
        """Resize the icon box (and the image in it) to ``scale`` times the base size."""
        self.scale = scale
        self.img.setFixedSize(self._scaled(TILE))
        if self.badge:
            self.setFixedHeight(round((TILE.height() + 6) * scale))
            self.place_badge()
        else:
            # Fixed width keeps the count right under its icon (no stretching).
            self.setFixedWidth(round((TILE.width() + 12) * scale))
        self._render()

    def set_count(self, text: str, tone: str = "") -> None:
        self.count.setText(text)
        if self.badge:
            set_tone(self.count, tone)
            self.place_badge()

    def place_badge(self) -> None:
        """Badge tiles: widen to fit the count if needed and pin it to the bottom-right corner."""
        if not self.badge:
            return
        self.count.ensurePolished()
        self.count.adjustSize()
        w = max(round((TILE.width() + 6) * self.scale), self.count.width() + 2)
        if self.width() != w:
            self.setFixedWidth(w)
        self.count.move(w - self.count.width() - 1, self.height() - self.count.height() - 1)
        self.count.raise_()

    def _scaled(self, size: QSize) -> QSize:
        return QSize(max(1, round(size.width() * self.scale)), max(1, round(size.height() * self.scale)))

    def set_pixmap(self, pm: QPixmap) -> None:
        if not shiboken6.isValid(self):
            return
        self._src, self._movie = pm, None
        self._render()

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
    """A left-aligned row of tiles. ``update_counts`` shows a Counter ("×N" under each icon);
    ``set_entries`` shows ``Entry`` badge tiles with an optional "+N" overflow tile and a muted note."""

    def __init__(self, kind: str, assets: AssetStore, empty: str = "", scale: float = 1.0, badge: bool = False):
        super().__init__()
        self.kind, self.assets, self.scale, self.badge = kind, assets, scale, badge
        self.empty = QLabel(empty, objectName="muted", parent=self)
        self.empty.hide()
        self.more = QLabel(objectName="more", alignment=Qt.AlignmentFlag.AlignCenter, parent=self)  # dashed "+N" tile
        self.more.hide()
        self.note = QLabel(objectName="chip", parent=self)  # e.g. "+12 unknown"
        self.note.hide()
        self.names: dict = {}
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(round(6 * scale))
        self.row.addStretch(1)  # one persistent stretch, always last
        self.tiles: dict = {}
        self._layout_key: tuple | None = None  # what is laid out: tile keys in order plus which extras show
        self._resize_more()

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self.row.setSpacing(round((4 if self.badge else 6) * scale))
        for tile in self.tiles.values():
            tile.set_scale(scale)
        self._resize_more()

    def _resize_more(self) -> None:
        self.more.setFixedSize(round((TILE.width() + 6) * self.scale), round((TILE.height() + 6) * self.scale))

    def place_badges(self) -> None:
        for tile in self.tiles.values():
            tile.place_badge()

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

    def set_entries(self, entries: list[Entry], overflow: int = 0, note: str = "") -> None:
        key = (tuple(e.key for e in entries), overflow > 0, bool(note))
        if key != self._layout_key:
            self._relayout_entries(entries, overflow > 0, bool(note))
            self._layout_key = key
        for e in entries:
            tile = self.tiles[e.key]
            tile.set_count(e.text, e.tone)
            tile.setToolTip(e.tip)
        self.more.setText(f"+{overflow}")
        self.note.setText(note)
        self.setVisible(bool(entries) or overflow > 0 or bool(note))

    def _clear_extras(self) -> None:
        for w in (self.empty, self.more, self.note):
            self.row.removeWidget(w)
            w.hide()

    def _relayout_entries(self, entries: list[Entry], more: bool, note: bool) -> None:
        keys = {e.key for e in entries}
        for key in list(self.tiles):
            if key not in keys:
                tile = self.tiles.pop(key)
                self.row.removeWidget(tile)
                tile.deleteLater()
        for w in self.tiles.values():
            self.row.removeWidget(w)
        self._clear_extras()
        for i, e in enumerate(entries):
            tile = self.tiles.get(e.key)
            if tile is None:
                tile = self.tiles[e.key] = Tile(e.tip, self.scale, badge=True)
                if e.key[0] == "mesos":
                    tile.set_pixmap(coin_pixmap())
                elif e.key[0] == "mob":
                    self.assets.mob_sprite(e.key[1], tile.set_image)
                else:
                    self.assets.item_icon(e.key[1], tile.set_image)
            self.row.insertWidget(i, tile)
        n = len(entries)
        for w, on in ((self.more, more), (self.note, note)):
            if on:
                self.row.insertWidget(n, w, 0, Qt.AlignmentFlag.AlignVCenter)
                w.show()
                n += 1

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


class ElidedLabel(QLabel):
    """One-line label that cuts its text with "…" instead of widening the window.

    ``keep_tail`` (a regex): the part of the text it matches at the end is never cut
    ("Primeval Fo… I": the numeral tells maps of one name apart).
    """

    def __init__(self, keep_tail: str = "", **kw):
        super().__init__(**kw)
        self._full = ""
        self._tail = re.compile(keep_tail + "$") if keep_tail else None
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(1)

    def set_full(self, text: str, tip: str = "") -> None:
        self._full = text
        self.setToolTip(tip or text)
        self._elide()

    def setText(self, text: str) -> None:  # the plain setter keeps the full text too
        self.set_full(text)

    def full_width(self) -> int:
        """Pixels the whole text needs."""
        self.ensurePolished()
        return self.fontMetrics().horizontalAdvance(self._full) + 2 * self.margin() + 2

    def _elided(self, width: int) -> str:
        fm = self.fontMetrics()
        text = self._full
        if self._tail and fm.horizontalAdvance(text) > width:
            m = self._tail.search(text)
            if m and m.start() and fm.horizontalAdvance(m.group()) * 2 < width:
                head = fm.elidedText(text[: m.start()], Qt.TextElideMode.ElideRight,
                                     width - fm.horizontalAdvance(m.group()))
                return head + m.group()
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, width)

    def _elide(self) -> None:
        self.ensurePolished()
        super().setText(self._elided(max(0, self.width())))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()


class ScaleGrip(QWidget):
    """Corner grip: drag to scale the whole overlay, double-click to reset to 100%."""

    SIZE = 14

    def __init__(self, overlay: "Overlay"):
        super().__init__()
        self.overlay = overlay
        self._start: tuple[QPoint, float, QSize] | None = None
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Drag to resize · Ctrl+wheel · double-click to reset")
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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # only for the rounded corners: the frame is opaque
        self.tracker = tracker
        self._drag: QPoint | None = None
        self.scale = min(OVERLAY_SCALE_MAX, max(OVERLAY_SCALE_MIN, tracker.settings.overlay_scale))
        self._applied = self.scale  # the scale the widgets are laid out at (lags self.scale mid-drag)
        self._wheel = 0  # unspent Ctrl+wheel delta
        self._apply_timer = QTimer(self, singleShot=True, interval=APPLY_MS, timeout=self._apply_pending)
        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_MS, timeout=self.save_scale)
        self._refit = QTimer(self, singleShot=True, interval=0, timeout=self._fit_size)

        frame = self.frame = QWidget(objectName="overlay")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        # Title bar: map name, session clock, pause / reset / hide.
        self.map = ElidedLabel(keep_tail=r"(?:\s[IVXivxl|1!]{1,5})?(?:\s\(\?\))?", objectName="head")
        self.channel = QLabel(objectName="chip")  # "Ch. 26": hidden until the channel is read
        self.channel.hide()
        self.clock = ElidedLabel(objectName="clock")
        self.pause_btn = QPushButton("⏸︎", toolTip="Pause tracking")
        self.pause_btn.clicked.connect(self._toggle_pause)
        restart = QPushButton("⟲", toolTip="Save this session and start a new one")
        restart.clicked.connect(self._restart)
        close = QPushButton("×", toolTip="Hide overlay")
        close.clicked.connect(self.hide)
        top = self.top = QHBoxLayout()
        top.addWidget(self.map, 1)
        top.addWidget(self.channel)
        top.addWidget(self.clock)
        top.addWidget(self.pause_btn)
        top.addWidget(restart)
        top.addWidget(close)
        self._title_buttons = (restart, close)
        self._clock_hint = False  # the clock's text is a status hint ("Not a hunting map"), not a time

        # Headline: big EXP/min | vs-average badge, then a small line with the status chips.
        self.rate = QLabel(objectName="big")
        self.sep = QFrame(objectName="rule")
        self.delta = QLabel(objectName="delta")
        head = self.head = QHBoxLayout()
        mid = Qt.AlignmentFlag.AlignVCenter
        head.addWidget(self.rate, 0, mid)
        head.addWidget(self.sep, 0, mid)
        head.addWidget(self.delta, 0, mid)
        head.addStretch(1)
        self.party_chip = QLabel(objectName="chip")
        self.status_chip = QLabel(objectName="chip")
        self.sub = QLabel(objectName="muted")
        meta = self.meta = QHBoxLayout()
        for w in (self.party_chip, self.status_chip, self.sub):
            meta.addWidget(w)
        meta.addStretch(1)

        self.items = TileRow("item", assets, scale=self.scale, badge=True)
        self.kills = TileRow("mob", assets, scale=self.scale, badge=True)
        self.rows = (self.items, self.kills)
        self.left_w = QWidget()
        left = self.left = QVBoxLayout(self.left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left.addLayout(head)
        left.addLayout(meta)
        left.addWidget(self.items)
        left.addWidget(self.kills)
        left.addStretch(1)

        # Boxed stat panel: AVG / LV n IN, then net mesos and potions with their per-minute rates.
        self.panel = QFrame(objectName="panel")
        grid = self.grid = QGridLayout(self.panel)
        grid.setVerticalSpacing(0)
        self.avg_key, self.avg_val = QLabel("AVG", objectName="key"), QLabel(objectName="val")
        self.eta_key, self.eta_val = QLabel(objectName="key"), QLabel(objectName="val")
        self.net_key, self.net_val, self.net_sub = QLabel("NET MESOS", objectName="key"), QLabel(objectName="val"), QLabel(objectName="sub")
        self.pot_key, self.pot_val, self.pot_sub = QLabel("POTIONS", objectName="key"), QLabel(objectName="val"), QLabel(objectName="sub")
        self.rule = QFrame(objectName="rule")
        right = Qt.AlignmentFlag.AlignRight
        self.values = (self.avg_val, self.eta_val, self.net_val, self.net_sub, self.pot_val, self.pot_sub)
        for r, (k, v) in enumerate(((self.avg_key, self.avg_val), (self.eta_key, self.eta_val))):
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1, right)
        grid.addWidget(self.rule, 2, 0, 1, 2)
        for r, (k, v, sub) in enumerate(((self.net_key, self.net_val, self.net_sub), (self.pot_key, self.pot_val, self.pot_sub))):
            grid.addWidget(k, 3 + 2 * r, 0)
            grid.addWidget(v, 3 + 2 * r, 1, right)
            grid.addWidget(sub, 4 + 2 * r, 1, right)

        self.grip = ScaleGrip(self)  # under the panel, so it never covers content
        side = self.side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.panel)
        side.addStretch(1)
        side.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignRight)

        body = self.body = QHBoxLayout()
        body.addWidget(self.left_w, 1)
        body.addLayout(side)

        self._prompt: str | None = None  # "open the minimap": replaces the title while set
        lay = self.lay = QVBoxLayout(frame)
        lay.addLayout(top)
        lay.addLayout(body)
        self._apply_scale()

        self.timer = QTimer(self, interval=1000, timeout=self.refresh)
        self.timer.start()
        self.refresh()

    def set_minimap_prompt(self, text: str | None) -> None:
        """Show ``text`` in place of the map name, channel and clock (None restores them).

        It takes over the title line rather than adding one, so the overlay never changes height.
        """
        text = text or None
        if text == self._prompt:
            return
        self._prompt = text
        self._show_title()

    def _show_title(self) -> None:
        tr = self.tracker
        _street, _, name = (tr.map_name or "Unknown map").rpartition(" : ")
        if self._prompt:
            self.map.set_full(self._prompt)
        else:
            self.map.set_full(name, tr.map_name or name)
        set_tone(self.map, "warn" if self._prompt else "")
        self.clock.setVisible(not self._prompt)
        ch = None if self._prompt else tr.channel  # the prompt has the title line to itself
        self.channel.setText(f"Ch. {ch}" if ch else "")
        self.channel.setVisible(bool(ch))
        self._fit_title()  # after the chip, so the room it leaves the name counts it

    def _fit_title(self) -> None:
        """Give the map name the room it needs; the clock or status hint gets what is left.

        The hint ("Not a hunting map") is far wider than the clock and used to squeeze the
        name down to "Pr…". Now the name goes first: the hint shows only if it fits beside
        the whole name, else it moves into the title's tooltip. The clock keeps a quarter of the
        row. The row's width never changes.
        """
        self.clock.setMinimumWidth(1)
        self.clock.setMaximumWidth(16777215)
        if self._prompt:
            return
        top = self.top
        others = [w for w in (self.channel, self.pause_btn, *self._title_buttons) if w.isVisibleTo(self)]
        room = (top.geometry().width() - top.spacing() * (len(others) + 2)
                - sum(w.sizeHint().width() for w in others))
        if room <= 0:
            return
        need, left = self.clock.full_width(), room - self.map.full_width()
        name = self.tracker.map_name or ""
        if self._clock_hint:
            fits = need <= left
            self.clock.setVisible(fits)
            self.map.setToolTip(name if fits else f"{name}\n{self.clock._full}")
            if fits:
                self.clock.setFixedWidth(need)
        else:
            self.clock.setFixedWidth(max(1, min(need, max(left, room // 4))))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_title()

    def _toggle_pause(self) -> None:
        self.tracker.toggle_pause()
        self.refresh()

    def _restart(self) -> None:
        self.tracker.restart()
        self.refresh()

    def _rate_html(self, figure: str, unit: str, size: float = 14) -> str:
        return f"{figure}<span style='{unit_span(size, self._applied)}'>{unit}</span>"

    # --- scale: fonts, margins, tiles, panel and grip all follow one factor -------------

    def _apply_scale(self) -> None:
        s = self._applied

        def n(v: float) -> int:
            return round(v * s)

        self.frame.setStyleSheet(overlay_qss(s))
        self.lay.setContentsMargins(n(12), n(8), n(12), n(8))
        self.lay.setSpacing(n(4))
        self.top.setSpacing(n(4))
        self.body.setSpacing(n(8))
        self.left.setSpacing(n(3))
        self.head.setSpacing(n(8))
        self.meta.setSpacing(n(5))
        self.grid.setContentsMargins(n(8), n(5), n(8), n(5))
        self.grid.setHorizontalSpacing(n(10))
        self.left_w.setMinimumWidth(n(196))
        self.panel.setMinimumWidth(n(132))
        self.sep.setFixedSize(max(1, n(1)), n(26))
        self.rule.setFixedHeight(max(1, n(1)))
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
        for row in self.rows:
            row.place_badges()
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

    def set_opacity(self, pct: int) -> None:
        self.setWindowOpacity(min(100, max(30, pct)) / 100)

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
        current = round(self.windowOpacity() * 100)
        sub = menu.addMenu("Opacity")
        for pct in OPACITY_STEPS:
            act = sub.addAction(f"{pct}%", lambda pct=pct: self.set_opacity(pct))
            act.setCheckable(True)
            act.setChecked(abs(pct - current) < 3)
        menu.addAction("Hide overlay", self.hide)
        menu.exec(e.globalPos())

    def _show_waiting(self, hint: str) -> None:
        """No session yet (town / not fighting): today's totals, nothing else."""
        tr = self.tracker
        today = tr.today()
        n = today["sessions"]
        avg = today["exp"] / today["seconds"] * 60 if today["seconds"] else 0
        self.clock.setText(hint)
        self._clock_hint = True
        self._fit_title()
        self.rate.setText(self._rate_html(f"+{fmt_short(today['exp'])}", "EXP today"))
        self.delta.setText("")
        self.sep.hide()
        self.delta.hide()
        self.party_chip.hide()
        self.status_chip.hide()
        self.sub.setText(f"{n} session{'s' if n != 1 else ''} · {fmt_dur(today['seconds'])} · {fmt_short(avg)}/min")
        self.sub.setToolTip(f"{today['kills']:,} party kills today")
        self.panel.hide()
        for row in self.rows:
            row.hide()
        self._fit_size()

    def refresh(self) -> None:
        tr = self.tracker
        tr.tick()
        state = tr.state
        self.pause_btn.setText("▶︎" if state == "paused" else "⏸︎")
        self.pause_btn.setToolTip("Resume tracking" if state == "paused" else "Pause tracking")
        s = tr.session
        self._show_title()

        if s is None:
            self._show_waiting("Paused" if state == "paused" else "Waiting for EXP" if tr.combat_map else "Not a hunting map")
            return

        v = session_view(s, tr.today()["exp"])
        self.clock.setText(fmt_dur(s.active_s))
        self._clock_hint = False
        self._fit_title()
        self.rate.setText(self._rate_html(v.rate, "/min"))
        self.sep.setVisible(v.delta is not None)
        self.delta.setVisible(v.warming or v.delta is not None)
        if v.delta is not None:
            text, tone = v.delta
            self.delta.setText(f"{text}<br><span style='{unit_span(8, self._applied)}'>VS AVG</span>")
            set_tone(self.delta, {"over": "gain", "under": "loss"}.get(tone, ""))
        else:
            self.delta.setText(f"<span style='{unit_span(11, self._applied)}'>warming up</span>" if v.warming else "")
            set_tone(self.delta, "")
        self.party_chip.setText(tr.party_label)
        self.party_chip.show()
        status = "Paused" if state == "paused" else "Idle" if s.is_idle() else ""
        self.status_chip.setText(status)
        self.status_chip.setVisible(bool(status))
        set_tone(self.status_chip, "warn")
        self.sub.setText(v.extra)
        self.sub.setToolTip(v.tip)

        self.avg_val.setText(self._rate_html(v.avg, "/min", 10) if v.avg != DASH else DASH)
        self.eta_key.setText(v.eta_label)
        self.eta_val.setText(v.eta)
        self.net_val.setText(v.net)
        self.net_sub.setText(v.net_rate)
        self.pot_val.setText(v.potions)
        self.pot_sub.setText(v.potions_rate)
        for lbl, tone in ((self.net_val, v.net_tone), (self.net_sub, v.net_tone), (self.pot_val, v.potions_tone), (self.pot_sub, v.potions_tone)):
            set_tone(lbl, tone)
        self.panel.show()

        entries, more = item_entries(s.mesos, s.loot, s.used, s.names)
        self.items.set_entries(entries, more)
        entries, more, note = mob_entries(s.kills, s.names, s.unassigned_kills)
        self.kills.set_entries(entries, more, note)
        self._fit_size()
        self._refit.start()  # labels' size hints settle once the event loop has run: fit again then

    # Drag anywhere to move
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None
