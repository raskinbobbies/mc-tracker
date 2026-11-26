"""Safe mode window helpers. Keep overlay off the game: where our windows may sit so they never cover the game's
window, and refusing title-bar drags onto it. Browser-style: the window bits of a browser's picture-in-picture.

The game's rectangle comes from ``winutil.tracked_game_rect`` (GetWindowRect on the window the capture already
found: nothing new is looked up, no process is touched). Placement is a pure function, so tests can use fake
screens and a fake game.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt

MARGIN = 6  # px between the game and a window snapped beside it


def to_logical(native: tuple[int, int, int, int], screens: list[tuple[QRect, float]]) -> QRect:
    """A physical-pixel rectangle (GetWindowRect) in Qt's logical coordinates. ``screens``: (geometry,
    devicePixelRatio) per screen; Qt keeps each screen's top-left in physical pixels and scales its size."""
    left, top, right, bottom = native
    cx, cy = (left + right) // 2, (top + bottom) // 2
    for geo, dpr in screens:
        o = geo.topLeft()
        if o.x() <= cx < o.x() + geo.width() * dpr and o.y() <= cy < o.y() + geo.height() * dpr:
            def p(x: int, y: int) -> QPoint:
                return QPoint(o.x() + round((x - o.x()) / dpr), o.y() + round((y - o.y()) / dpr))
            return QRect(p(left, top), p(right, bottom) - QPoint(1, 1))
    return QRect(QPoint(left, top), QPoint(right - 1, bottom - 1))


def free_spot(size: QSize, pos: QPoint, game: QRect, areas: list[QRect], margin: int = MARGIN) -> QPoint | None:
    """Where a window of ``size`` now at ``pos`` should go so it doesn't overlap ``game``: where it is when that
    is fine, else the nearest of the spots beside each of the game's edges, or on another screen (``areas``:
    each screen's available geometry). None when there is no room anywhere (the game fills every screen)."""
    if not QRect(pos, size).intersects(game) and any(a.contains(QRect(pos, size)) for a in areas):
        return pos
    tries = ((game.left() - margin - size.width(), pos.y()), (game.right() + 1 + margin, pos.y()),
             (pos.x(), game.top() - margin - size.height()), (pos.x(), game.bottom() + 1 + margin),
             (pos.x(), pos.y()))  # the last one, clamped into another screen, is "move it over there"
    spots = []
    for a in areas:
        if size.width() > a.width() or size.height() > a.height():
            continue
        for x, y in tries:
            p = QPoint(min(max(x, a.left()), a.right() + 1 - size.width()),
                       min(max(y, a.top()), a.bottom() + 1 - size.height()))
            if not QRect(p, size).intersects(game):
                spots.append(p)
    return min(spots, key=lambda p: (p - pos).manhattanLength(), default=None)


def game_rect() -> QRect | None:
    """The game window's rectangle in Qt coordinates, or None when no game window is tracked."""
    from PySide6.QtGui import QGuiApplication

    from .winutil import tracked_game_rect

    native = tracked_game_rect()
    if native is None:
        return None
    return to_logical(native, [(s.geometry(), s.devicePixelRatio()) for s in QGuiApplication.screens()])


def screen_areas() -> list[QRect]:
    from PySide6.QtGui import QGuiApplication

    return [s.availableGeometry() for s in QGuiApplication.screens()]


def overlaps_game(rect: QRect) -> bool:
    game = game_rect()
    return game is not None and rect.intersects(game)


# --- browser-style windows ------------------------------------------------------

# A title bar with only a close button, on top: a browser's picture-in-picture window (measured on Chrome:
# WS_CAPTION | WS_SYSMENU | WS_THICKFRAME, WS_EX_TOPMOST; not layered, not a tool window).
BROWSER_FLAGS = (Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.CustomizeWindowHint
                 | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowSystemMenuHint
                 | Qt.WindowType.WindowCloseButtonHint)
GWL_STYLE, WS_POPUP = -16, 0x80000000
SWP_FRAME_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED


def match_browser_style(w) -> None:
    """Qt gives every top-level window WS_POPUP; a browser's picture-in-picture window is WS_CAPTION |
    WS_SYSMENU | WS_THICKFRAME without it. Clear it on our own window (SetWindowLong on our hwnd only)."""
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "windows":
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = int(w.winId())
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    if style & WS_POPUP:
        user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_POPUP)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FRAME_ONLY)


# --- title-bar drags ------------------------------------------------------------

WM_MOVING, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE = 0x0216, 0x0231, 0x0232
Rect = tuple[int, int, int, int]  # left, top, right, bottom (physical pixels, as Windows gives them)


def refuse_move(proposed: Rect, game: Rect | None, last_good: Rect | None) -> Rect | None:
    """A title-bar drag would put the window at ``proposed``: the rectangle to keep it at instead (the last one
    that didn't overlap the game), or None to let it go there."""
    if game is None or last_good is None:
        return None
    left, top, right, bottom = proposed
    if left < game[2] and game[0] < right and top < game[3] and game[1] < bottom:
        return last_good
    return None


class NativeMoveGuard:
    """Off the game, refuse native (title-bar) drags onto it: WM_MOVING's rectangle is put back to the last one
    that was clear. ``moving`` is True during the drag (the placement timer leaves the window alone)."""

    def __init__(self, on_done=None):
        self.moving = False
        self.on_done = on_done  # called when a title-bar drag ends
        self._last: Rect | None = None

    def handle(self, event_type, message) -> bool:
        """For ``nativeEvent``: True when the message was handled (return ``(True, 1)``)."""
        if bytes(event_type) != b"windows_generic_MSG":
            return False
        import ctypes
        from ctypes import wintypes

        from .winutil import tracked_game_rect

        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_ENTERSIZEMOVE:
            r = wintypes.RECT()  # where the drag starts: clear of the game (placement saw to that)
            ok = ctypes.windll.user32.GetWindowRect(msg.hWnd, ctypes.byref(r))
            self.moving, self._last = True, ((r.left, r.top, r.right, r.bottom) if ok else None)
        elif msg.message == WM_EXITSIZEMOVE:
            self.moving = False
            if self.on_done is not None:
                self.on_done()
        elif msg.message == WM_MOVING and msg.lParam:
            r = wintypes.RECT.from_address(msg.lParam)
            proposed = (r.left, r.top, r.right, r.bottom)
            keep = refuse_move(proposed, tracked_game_rect(), self._last)
            if keep is None:
                self._last = proposed
                return False
            r.left, r.top, r.right, r.bottom = keep
            return True
        return False
