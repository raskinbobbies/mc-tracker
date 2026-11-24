"""Small pop-up toast for price-check feedback, shown over the game.

The toast never takes focus (the game keeps keyboard input) and lets clicks
through. `PriceFeedback` drives the sequence the user sees: what was copied,
"Switching to mesos.watch...", then the result. The tab switch itself takes
about a second (UI Automation), so it runs on a thread-pool thread and the
result comes back by signal, keeping the toast painting meanwhile.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QPropertyAnimation, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .theme import CREAM, FONT, INK, LEAF, ORANGE, WOOD

SHOW_S = 2.5  # visible time after the last update, fade included
FADE_MS = 400
SWITCHING_AFTER_MS = 350  # "Copied" gets a beat before "Switching..." replaces it
BUSY_LIMIT_S = 10.0  # never leave a "Switching..." toast up forever
MARGIN = 10  # px between toast and what it is anchored to

DOT_COLORS = {"info": ORANGE, "ok": LEAF, "error": "#c0392b"}


class Toast(QWidget):
    """Frameless, always-on-top, non-activating card that fades out by itself."""

    def __init__(self, anchor: QWidget | None = None):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,
        )
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

    def show_message(self, text: str, kind: str = "info", sticky: bool = False) -> None:
        """Show (or replace) the message. `sticky` waits for the next message instead of fading."""
        self._fade_anim.stop()
        self.setWindowOpacity(1.0)
        self.dot.setStyleSheet(f"color: {DOT_COLORS.get(kind, ORANGE)}; font-size: 12px;")
        self.text.setText(text)
        self.card.layout().activate()  # size to the new text before placing
        self.layout().activate()
        self.setFixedSize(self.sizeHint())
        self.layout().activate()
        self._place()
        self.show()  # WA_ShowWithoutActivating: the game keeps focus
        self.raise_()
        hold_s = BUSY_LIMIT_S if sticky else SHOW_S - FADE_MS / 1000
        self._hold.start(int(hold_s * 1000))

    def _fade(self) -> None:
        self._fade_anim.start()

    def _place(self) -> None:
        """Just below the anchor (the overlay), or above it when there is no room; else top-centre."""
        a = self.anchor
        if a is not None and a.isVisible():
            geo = a.frameGeometry()
            screen = a.screen() or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            x = min(max(area.left(), geo.left()), area.right() - self.width())
            y = geo.bottom() + MARGIN
            if y + self.height() > area.bottom():
                y = geo.top() - MARGIN - self.height()
            self.move(x, max(area.top(), y))
            return
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.12))


class _Signals(QObject):
    done = Signal(int, str)  # request id, result


class _SwitchTask(QRunnable):
    def __init__(self, request_id: int, switch: Callable[[], str], signals: _Signals):
        super().__init__()
        self.request_id, self.switch, self.signals = request_id, switch, signals

    def run(self) -> None:
        try:
            result = self.switch()
        except Exception as e:  # never lose the toast to a worker error
            result = f"Couldn't switch to mesos.watch: {e}"
        self.signals.done.emit(self.request_id, result)


class PriceFeedback(QObject):
    """Runs the mesos.watch switch off the GUI thread and narrates it in a toast."""

    def __init__(self, anchor: QWidget | None = None, switch: Callable[[], str] | None = None, parent=None):
        super().__init__(parent)
        if switch is None:
            from .browser import focus_mesos_watch as switch
        self.toast = Toast(anchor)
        self._switch = switch
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._request = 0
        self._busy = False
        self._on_done_cb: Callable[[str], None] | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one tab switch at a time

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
