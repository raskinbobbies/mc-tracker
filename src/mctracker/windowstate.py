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

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer

from .items import clean_window_entry

SAVE_MS = 500  # wait this long after the last move/resize before writing to disk
ARM_MS = 1000  # moves and resizes this soon after the first show are the system placing the window
MIN_VISIBLE_W, MIN_VISIBLE_H = 100, 50  # how much of the title area has to be on a screen
MAXIMIZED_INSET = 40  # where a normal-size window lands on the screen it was maximized on
DEFAULT_SIZE = (800, 600)


class ScreenInfo(NamedTuple):
    name: str
    area: QRect  # the usable area (without the taskbar): used to check a window can be reached
    full: QRect | None = None  # the whole screen: its origin is stable, the taskbar doesn't move it

    @property
    def origin(self) -> QPoint:
        return (self.full or self.area).topLeft()


def title_visible(rect: QRect, screens: Sequence[ScreenInfo], whole: bool = False) -> bool:
    """Whether enough of the window is on one screen to grab it: 100x50 px of its top strip, or, with
    ``whole`` (the overlay, which can be dragged by any part), of the window anywhere. A window
    smaller than that only needs to be fully visible in that direction."""
    band = rect if whole else QRect(rect.x(), rect.y(), rect.width(), min(rect.height(), MIN_VISIBLE_H))
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
        d.update(screen=scr.name, sx=scr.origin.x(), sy=scr.origin.y())
    if maximized:
        d["maximized"] = True
        if scr is not None:
            d["max_screen"] = scr.name
    return d


def resolve(saved: dict | None, screens: Sequence[ScreenInfo], size: tuple[int, int] | None = None,
            whole: bool = False) -> QRect | None:
    """Where to put a window, or None to leave it at its default place.

    ``size`` overrides the saved size (the overlay sizes itself); otherwise the saved size is used,
    shrunk to fit the screen it lands on. A window with no saved size comes back as a 1x1 rect, so only
    its top-left corner should be used. A window saved as maximized goes to the screen it was maximized
    on, if that is still connected, even when its normal-size place isn't visible any more.
    """
    d = clean_window_entry(saved)
    if d is None or not screens:
        return None
    x, y = d["x"], d["y"]
    scr = next((s for s in screens if s.name == d.get("screen")), None)
    if scr is not None:  # the same screen, perhaps at another spot in the desktop: follow it
        x += scr.origin.x() - d["sx"]
        y += scr.origin.y() - d["sy"]
    w, h = size if size else (d.get("w", 1), d.get("h", 1))
    rect = QRect(x, y, w, h)
    target = next((s for s in screens if s.name == d.get("max_screen")), None) if d.get("maximized") else None
    if target is not None and not (title_visible(rect, [target]) or target.area.contains(rect.center())):
        rect = QRect(target.area.x() + MAXIMIZED_INSET, target.area.y() + MAXIMIZED_INSET,
                     *(size or (d.get("w", DEFAULT_SIZE[0]), d.get("h", DEFAULT_SIZE[1]))))
    elif target is None and not title_visible(rect, screens, whole):
        return None
    if size is None:
        host = next((s for s in screens if not rect.intersected(s.area).isEmpty()), screens[0])
        rect.setSize(rect.size().boundedTo(host.area.size()))
    return rect


def current_screens() -> list[ScreenInfo]:
    from PySide6.QtGui import QGuiApplication

    return [ScreenInfo(s.name(), s.availableGeometry(), s.geometry()) for s in QGuiApplication.screens()]


class WindowMemory(QObject):
    """Restores one window's saved place and saves it again when it moves or resizes.

    Installed as an event filter, so the window class itself needs no changes. ``keep_size`` is
    False for the overlay, which sizes itself (its scale is saved separately) and can be grabbed
    anywhere. Nothing is written until the user has moved or resized the window: a window that opens
    at its default place (the saved spot was off-screen) doesn't overwrite the stored one.
    """

    def __init__(self, widget, settings, key: str, *, keep_size: bool = True, screens=current_screens):
        super().__init__(widget)
        self.widget, self.settings, self.key = widget, settings, key
        self.keep_size, self.screens = keep_size, screens
        self.armed = False  # set shortly after the first show: later moves and resizes are the user's
        self.touched = False
        self._timer = QTimer(self, singleShot=True, interval=SAVE_MS, timeout=self.save)
        self._arm = QTimer(self, singleShot=True, interval=ARM_MS, timeout=self._set_armed)
        widget.installEventFilter(self)

    def _set_armed(self) -> None:
        self.armed = True

    def restore(self) -> None:
        """Call before the first ``show()``."""
        saved = self.settings.windows.get(self.key)
        w = self.widget
        maximized = bool(self.keep_size and isinstance(saved, dict) and saved.get("maximized") is True)
        size = None if self.keep_size else (w.width(), w.height())
        rect = resolve(saved, self.screens(), size, not self.keep_size)
        if rect is not None:
            if self.keep_size:
                w.resize(rect.size())
            w.move(rect.topLeft())
        if maximized:  # even with no place to restore to: it opens maximized on the default screen
            w.setWindowState(w.windowState() | Qt.WindowState.WindowMaximized)

    def eventFilter(self, obj, e) -> bool:
        if obj is getattr(self, "widget", None):  # gone while the window is being torn down
            t = e.type()
            if t == QEvent.Type.Show and not self.armed and not self._arm.isActive():
                self._arm.start()
            elif t in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.WindowStateChange):
                if self.armed and self.widget.isVisible() and not self.widget.isMinimized():
                    self.touched = True
                    self._timer.start()  # restarts: one write after the dragging stops
        return False

    def save(self) -> None:
        """Write the window's place now (also called on quit). Off-screen places are never saved."""
        self._timer.stop()
        w = self.widget
        if not self.touched or w.isMinimized():  # Windows parks minimized windows at -32000,-32000
            return
        maximized = bool(self.keep_size and w.isMaximized())
        screens = self.screens()
        scr = w.screen()
        name = scr.name() if scr else None
        previous = clean_window_entry(self.settings.windows.get(self.key))
        have_previous = bool(maximized and previous and "w" in previous)
        entry = None
        if have_previous and previous.get("screen") == name:
            # keep the normal-size place stored earlier: a maximized frame's border offsets would shift it
            entry = {**previous, "maximized": True, "max_screen": name}
        else:
            geo = w.normalGeometry() if maximized else w.geometry()
            # the frame (title bar, borders) sits outside geometry(): restore moves the frame, so store its corner
            off = w.geometry().topLeft() - w.frameGeometry().topLeft()
            rect = QRect(geo.topLeft() - off, geo.size())
            user_pos = getattr(w, "user_frame_pos", lambda: None)()
            if user_pos is not None:  # Safe mode moved it off the game: keep the user's own place
                rect.moveTopLeft(user_pos)
            if title_visible(rect, screens, not self.keep_size):
                entry = describe(rect, screens, name, maximized)
            elif have_previous:
                entry = {**previous, "maximized": True, "max_screen": name}
        if entry is None:
            return
        if entry.get("max_screen") is None:
            entry.pop("max_screen", None)
        if not self.keep_size:
            entry.pop("w", None)
            entry.pop("h", None)
        if self.settings.windows.get(self.key) == entry:
            return
        self.settings.windows[self.key] = entry
        try:
            self.settings.save()
        except OSError:
            pass  # remembered for this run only
