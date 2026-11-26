"""Safe mode > Keep overlay off the game: where our windows may sit so they never cover the game's window.

The game's rectangle comes from ``winutil.tracked_game_rect`` (GetWindowRect on the window the capture already
found: nothing new is looked up, no process is touched). Placement is a pure function, so tests can use fake
screens and a fake game.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize

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
