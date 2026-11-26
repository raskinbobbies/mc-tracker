"""Small pop-up toast for price-check feedback, shown over the game (Safe mode: beside it, or not at all).

The toast never takes focus (the game keeps keyboard input) and lets clicks
through. `PriceFeedback` drives the sequence the user sees: what was copied,
"Switching to mesos.watch...", then the result. The tab switch itself takes
about a second (UI Automation), so it runs on a thread-pool thread and the
result comes back by signal, keeping the toast painting meanwhile.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QPoint, QPropertyAnimation, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .theme import CREAM, FONT, INK, LEAF, ORANGE, WOOD

SHOW_S = 2.5  # visible time after the last update, fade included
FADE_MS = 400
SWITCHING_AFTER_MS = 350  # "Copied" gets a beat before "Switching..." replaces it
BUSY_LIMIT_S = 10.0  # never leave a "Switching..." toast up forever
MARGIN = 10  # px between toast and what it is anchored to

DOT_COLORS = {"info": ORANGE, "ok": LEAF, "error": "#c0392b"}

# Settings > Safe mode, for every toast (app.py: ``set_safe_modes``). Off the game, a toast sits beside the game
# window and is never click-through; with the browser-style overlay, no toast window shows at all (a toast is
# a layered tool window): the text goes to ``_notice`` instead.
_off_game = False
_browser = False
_notice: Callable[[str], None] | None = None


def set_safe_modes(off_game: bool, browser: bool, notice: Callable[[str], None] | None = None) -> None:
    global _off_game, _browser, _notice
    _off_game, _browser = off_game, browser
    if notice is not None:
        _notice = notice


class Toast(QWidget):
    """Frameless, always-on-top, non-activating card that fades out by itself."""

    def __init__(self, anchor: QWidget | None = None):
        super().__init__(None, self._wanted_flags())
        self.anchor = anchor
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.card = card = QFrame(self)
        card.setObjectName("toast")
        card.setStyleSheet(
            f"QFrame#toast {{ background: {CREAM}; border: 2px solid {WOOD}; border-radius: 14px; }}"
            f"QLabel {{ background: transparent; font-family: {FONT}; }}"
        )
        self.dot = QLabel("●")
        self.text = QLabel()
        self.text.setStyleSheet(f"color: {INK}; font-size: 14px; font-weight: 700;")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 9, 16, 9)
        row.setSpacing(8)
        row.addWidget(self.dot)
        row.addWidget(self.text)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._hold = QTimer(self, singleShot=True)
        self._hold.timeout.connect(self._fade)
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(FADE_MS)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.finished.connect(self.hide)

    @staticmethod
    def _wanted_flags() -> Qt.WindowType:
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        return flags if _off_game else flags | Qt.WindowType.WindowTransparentForInput

    def _to_notice(self, text: str) -> None:
        self._hold.stop()
        self._fade_anim.stop()
        self.hide()
        if _notice is not None:
            _notice(text)

    def show_message(self, text: str, kind: str = "info", sticky: bool = False) -> None:
        """Show (or replace) the message. `sticky` waits for the next message instead of fading."""
        if _browser:
            self._to_notice(text)
            return
        if self.windowFlags() != (flags := self._wanted_flags()):
            self.setWindowFlags(flags)  # Safe mode changed since the last message
        self._fade_anim.stop()
        self.setWindowOpacity(1.0)
        self.dot.setStyleSheet(f"color: {DOT_COLORS.get(kind, ORANGE)}; font-size: 12px;")
        self.text.setText(text)
        self.card.layout().activate()  # size to the new text before placing
        self.layout().activate()
        self.setFixedSize(self.sizeHint())
        self.layout().activate()
        if not self._place():
            self._to_notice(text)  # off the game, and the game fills every screen
            return
        self.show()  # WA_ShowWithoutActivating: the game keeps focus
        self.raise_()
        hold_s = BUSY_LIMIT_S if sticky else SHOW_S - FADE_MS / 1000
        self._hold.start(int(hold_s * 1000))

    def _fade(self) -> None:
        self._fade_anim.start()

    def _place(self) -> bool:
        """Just below the anchor (the overlay), or above it when there is no room; else top-centre. Off the
        game, the nearest spot beside the game instead; False when there is none."""
        a = self.anchor
        if a is not None and a.isVisible():
            geo = a.frameGeometry()
            screen = a.screen() or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            x = min(max(area.left(), geo.left()), area.right() - self.width())
            y = geo.bottom() + MARGIN
            if y + self.height() > area.bottom():
                y = geo.top() - MARGIN - self.height()
            pos = QPoint(x, max(area.top(), y))
        else:
            area = QGuiApplication.primaryScreen().availableGeometry()
            pos = QPoint(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.12))
        if _off_game:
            from .offgame import free_spot, game_rect, screen_areas

            if (game := game_rect()) is not None:
                pos = free_spot(self.size(), pos, game, screen_areas())
                if pos is None:
                    return False
        self.move(pos)
        return True


class _Signals(QObject):
    done = Signal(int, str)  # request id, result


class _SwitchTask(QRunnable):
    def __init__(self, request_id: int, switch: Callable[[], str], signals: _Signals):
        super().__init__()
        self.request_id, self.switch, self.signals = request_id, switch, signals

    def run(self) -> None:
        try:
            try:
                result = self.switch()
            except Exception as e:  # never lose the toast to a worker error
                result = f"Couldn't switch to mesos.watch: {e}"
            self.signals.done.emit(self.request_id, result)
        except RuntimeError:  # the signal source was deleted: the app is shutting down
            pass


class PriceFeedback(QObject):
    """Runs the mesos.watch switch off the GUI thread and narrates it in a toast."""

    def __init__(self, anchor: QWidget | None = None, switch: Callable[[], str] | None = None, parent=None):
        super().__init__(parent)
        if switch is None:
            from .browser import focus_mesos_watch as switch
        self.toast = Toast(anchor)
        self._switch = switch
        # Pool first: children are deleted in creation order, so the signal source
        # outlives (or at least isn't deleted before) a worker that is still running.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one tab switch at a time
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._request = 0
        self._busy = False
        self._on_done_cb: Callable[[str], None] | None = None

    def start(self, name: str, on_done: Callable[[str], None] | None = None) -> None:
        """The name is already on the clipboard: say so, then switch tabs in the background.

        `on_done(result_text)` runs on the GUI thread when the switch finishes.
        """
        self._on_done_cb = on_done
        self._request += 1
        rid = self._request
        self._busy = True
        self.toast.show_message(f"Copied “{name}”", sticky=True)
        QTimer.singleShot(SWITCHING_AFTER_MS, lambda: self._switching(rid))
        self._pool.start(_SwitchTask(rid, self._switch, self._signals))

    def nothing_found(self) -> None:
        self._busy = False
        self._request += 1  # a late result from an earlier check must not overwrite this
        self.toast.show_message("No item found under the cursor", kind="error")

    def _switching(self, rid: int) -> None:
        if rid == self._request and self._busy:
            self.toast.show_message("Switching to mesos.watch…", sticky=True)

    def _on_done(self, rid: int, result: str) -> None:
        if rid != self._request:
            return
        self._busy = False
        failed = result.lower().startswith(("couldn't", "can't", "failed"))
        self.toast.show_message(result, kind="error" if failed else "ok")
        if self._on_done_cb:
            self._on_done_cb(result)
