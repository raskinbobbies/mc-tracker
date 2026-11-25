"""Remember where the main window and the overlay were, and put them back on the next start.

Positions live in ``AppSettings.windows`` (the app's own settings file), in Qt's logical
(device-independent) pixels, so they are the same whatever the display scaling is. Each entry also
records the screen it was on and that screen's origin: if the monitors were rearranged but that screen
is still connected, the window follows it; if the saved spot isn't on any connected screen any more,
nothing is restored and the window opens where it normally would.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer

SAVE_MS = 500  # wait this long after the last move/resize before writing to disk
MIN_VISIBLE_W, MIN_VISIBLE_H = 100, 50  # how much of the title area has to be on a screen


class ScreenInfo(NamedTuple):
    name: str
    area: QRect  # the usable area (without the taskbar), in logical pixels


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def clean_saved(d) -> dict | None:
    """A saved entry with only the usable fields, or None when it can't be used."""
    if not isinstance(d, dict) or not (_int(d.get("x")) and _int(d.get("y"))):
        return None
    out: dict = {"x": d["x"], "y": d["y"]}
    if _int(d.get("w")) and _int(d.get("h")) and d["w"] > 0 and d["h"] > 0:
        out["w"], out["h"] = d["w"], d["h"]
    if isinstance(d.get("screen"), str) and _int(d.get("sx")) and _int(d.get("sy")):
        out["screen"], out["sx"], out["sy"] = d["screen"], d["sx"], d["sy"]
    if d.get("maximized") is True:
        out["maximized"] = True
    return out


def title_visible(rect: QRect, screens: Sequence[ScreenInfo]) -> bool:
    """Whether enough of the window's title strip (its top edge) is on one screen to grab it."""
    band = QRect(rect.x(), rect.y(), rect.width(), min(rect.height(), MIN_VISIBLE_H))
    need_w, need_h = min(MIN_VISIBLE_W, band.width()), min(MIN_VISIBLE_H, band.height())
    for s in screens:
        hit = band.intersected(s.area)
        if hit.width() >= need_w and hit.height() >= need_h:
            return True
    return False


def describe(rect: QRect, screens: Sequence[ScreenInfo], screen_name: str | None, maximized: bool = False) -> dict:
    """The entry to store for a window at ``rect`` (its normal, not maximized, geometry)."""
    d: dict = {"x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height()}
    scr = next((s for s in screens if s.name == screen_name), None)
    if scr is not None:
        d.update(screen=scr.name, sx=scr.area.x(), sy=scr.area.y())
    if maximized:
        d["maximized"] = True
    return d


def resolve(saved: dict | None, screens: Sequence[ScreenInfo], size: tuple[int, int] | None = None) -> QRect | None:
    """Where to put a window, or None to leave it at its default place.

    ``size`` overrides the saved size (the overlay sizes itself); otherwise the saved size is used,
    shrunk to fit the screen it lands on. A window with no saved size comes back as a 1x1 rect, so only
    its top-left corner should be used.
    """
    d = clean_saved(saved)
    if d is None or not screens:
        return None
    x, y = d["x"], d["y"]
    scr = next((s for s in screens if s.name == d.get("screen")), None)
    if scr is not None:  # the same screen, perhaps at another spot in the desktop: follow it
        x += scr.area.x() - d["sx"]
        y += scr.area.y() - d["sy"]
    w, h = size if size else (d.get("w", 1), d.get("h", 1))
    rect = QRect(x, y, w, h)
    if not title_visible(rect, screens):
        return None
    if size is None:
        host = next((s for s in screens if rect.intersected(s.area).width() * rect.intersected(s.area).height() > 0), screens[0])
        rect.setSize(rect.size().boundedTo(host.area.size()))
    return rect


def current_screens() -> list[ScreenInfo]:
    from PySide6.QtGui import QGuiApplication

    return [ScreenInfo(s.name(), s.availableGeometry()) for s in QGuiApplication.screens()]


class WindowMemory(QObject):
    """Restores one window's saved place and saves it again when it moves or resizes.

    Installed as an event filter, so the window class itself needs no changes. ``keep_size`` is
    False for the overlay, which sizes itself (its scale is saved separately).
    """

    def __init__(self, widget, settings, key: str, *, keep_size: bool = True, screens=current_screens):
        super().__init__(widget)
        self.widget, self.settings, self.key = widget, settings, key
        self.keep_size, self.screens = keep_size, screens
        self._timer = QTimer(self, singleShot=True, interval=SAVE_MS, timeout=self.save)
        widget.installEventFilter(self)

    def restore(self) -> None:
        """Call before the first ``show()``."""
        saved = self.settings.windows.get(self.key)
        rect = resolve(saved, self.screens(), None if self.keep_size else (self.widget.width(), self.widget.height()))
        if rect is None:
            return
        w = self.widget
        if self.keep_size:
            w.resize(rect.size())
        w.move(rect.topLeft())
        if self.keep_size and saved.get("maximized"):
            w.setWindowState(w.windowState() | Qt.WindowState.WindowMaximized)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.widget and e.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.WindowStateChange):
            if self.widget.isVisible() and not self.widget.isMinimized():
                self._timer.start()  # restarts: one write after the dragging stops
        return False

    def save(self) -> None:
        """Write the window's place now (also called on quit). Off-screen places are never saved."""
        self._timer.stop()
        w = self.widget
        if w.isMinimized():  # Windows parks minimized windows at -32000,-32000
            return
        maximized = bool(self.keep_size and w.isMaximized())
        screens = self.screens()
        previous = self.settings.windows.get(self.key)
        if maximized and previous and "w" in previous:
            # keep the normal-size place stored earlier: a maximized frame's border offsets would shift it
            entry = {**previous, "maximized": True}
        else:
            geo = w.normalGeometry() if maximized else w.geometry()
            # the frame (title bar, borders) sits outside geometry(): restore moves the frame, so store its corner
            off = w.geometry().topLeft() - w.frameGeometry().topLeft()
            rect = QRect(geo.topLeft() - off, geo.size())
            if not title_visible(rect, screens):
                return
            scr = w.screen()
            entry = describe(rect, screens, scr.name() if scr else None, maximized)
        if not self.keep_size:
            entry.pop("w"), entry.pop("h")
        if self.settings.windows.get(self.key) == entry:
            return
        self.settings.windows[self.key] = entry
        try:
            self.settings.save()
        except OSError:
            pass  # remembered for this run only
