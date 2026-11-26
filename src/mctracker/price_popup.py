"""The price-check popup: the hovered item's name, ready to paste, over maplestory.watch.

The hotkey reads the item name off the screen, puts it on the clipboard and opens this popup next to the
cursor. The user clicks the site's search box and presses Ctrl+V.

maplestory.watch's terms forbid scripts, bots and AI agents from reading its market index, so the embedded
view is a plain browser for the human only:

- never read the page (no DOM, text, HTML or network responses) and never inject or run JavaScript;
- never type into or auto-fill the search box: the user pastes;
- the default Chromium user agent, nothing spoofed.

The only thing the code looks at is the load *status* (succeeded, failed, HTTP error code), the same
thing a browser's own error page uses, so a block page or 403 can offer "Open in browser" instead.

No QtWebEngine in the build: the popup still shows the name, the Copy button and "Open in browser".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import CREAM, CREAM_ALT, FONT, INK, INK_MUTED, LOSS, ORANGE, ORANGE_LIGHT, WOOD, WOOD_DARK

WATCH_URL = "https://maplestory.watch/"
PROFILE_NAME = "maplestory-watch"
POPUP_SIZE = (560, 680)
CURSOR_GAP = 18  # px between the cursor and the popup's side
SCREEN_MARGIN = 8  # px kept free at the screen edges
MAX_CHIPS = 6
POPUP_FLAGS = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint

# Settings > Safe mode (app.py: ``set_safe_modes``). Off the game, the popup opens beside the game window (or, with
# no room anywhere, the price check goes to the user's browser); browser-style, it gets a browser
# picture-in-picture window's bits (see ``offgame``) instead of a frameless tool window.
_off_game = False
_browser = False


def set_safe_modes(off_game: bool, browser: bool) -> None:
    global _off_game, _browser
    _off_game, _browser = off_game, browser

POPUP_QSS = f"""
QFrame#pricePopup {{ background: {CREAM}; border: 2px solid {WOOD}; border-radius: 10px; }}
QLabel {{ color: {INK}; font-family: {FONT}; }}
QLabel#ppName {{ font-size: 15pt; font-weight: 700; }}
QLabel#ppHint {{ color: {INK_MUTED}; font-size: 9pt; }}
QLabel#ppWarn {{ color: {LOSS}; font-size: 9pt; font-weight: 600; }}
QPushButton {{ color: {INK}; background: {CREAM_ALT}; border: 1px solid {WOOD}; border-radius: 6px;
    padding: 3px 10px; font-family: {FONT}; font-weight: 600; }}
QPushButton:hover {{ background: {ORANGE_LIGHT}; }}
QPushButton:checked {{ background: {ORANGE}; color: white; border-color: {WOOD_DARK}; }}
QPushButton#ppChip {{ padding: 1px 8px; font-weight: 500; font-size: 9pt; border-radius: 9px; }}
"""


def webengine_available() -> bool:
    """True when this build has QtWebEngine (the dev venv does; an old .exe may not)."""
    try:
        import PySide6.QtWebEngineCore  # noqa: F401
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


def fit_size(size: tuple[int, int], screen: QRect) -> tuple[int, int]:
    """``size`` shrunk to fit ``screen`` with a margin (a 1080p screen at 175% is only ~617 px tall)."""
    return (min(size[0], screen.width() - 2 * SCREEN_MARGIN), min(size[1], screen.height() - 2 * SCREEN_MARGIN))


def place_near(cursor: QPoint, size: tuple[int, int], screen: QRect) -> QPoint:
    """Top-left for a popup of ``size`` beside ``cursor``: to its right, else its left, vertically centred
    on it, and always inside ``screen`` (the available geometry). Beside, not below: a tall popup fits
    above or below a mid-screen cursor on no common screen, and would then cover the hovered item."""
    w, h = size
    x = cursor.x() + CURSOR_GAP
    if x + w - 1 > screen.right():
        x = cursor.x() - CURSOR_GAP - w
    y = cursor.y() - h // 2
    x = max(screen.left(), min(x, screen.right() - w + 1))
    y = max(screen.top(), min(y, screen.bottom() - h + 1))
    return QPoint(x, y)


def blocked_reason(failed: bool, http_code: int) -> str:
    """Why the site didn't show (for the warning line), or "" when it loaded fine."""
    if http_code >= 400:
        return f"maplestory.watch refused the embedded view (HTTP {http_code})."
    if failed:
        return "maplestory.watch didn't load here."
    return ""


class _WebProfile:
    """One persistent browser profile for the app's life: cookies, cache and site settings live under
    the data dir and survive restarts. Owned by the QApplication so it outlives every page."""

    _profile = None
    root: Path | None = None  # set_profile_dir: --demo keeps its own; default <data dir>/web-profile

    @classmethod
    def get(cls):
        if cls._profile is None:
            from PySide6.QtWebEngineCore import QWebEngineProfile

            from .paths import sub

            p = QWebEngineProfile(PROFILE_NAME, QCoreApplication.instance())
            root = cls.root or sub("web-profile")
            p.setPersistentStoragePath(str(root / "storage"))
            p.setCachePath(str(root / "cache"))
            p.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
            cls._profile = p  # user agent left at the Chromium default on purpose
        return cls._profile


def set_profile_dir(path: Path) -> None:
    """Keep the browser profile in ``path`` instead (before the first popup), e.g. a demo's temp folder."""
    _WebProfile.root = path


class PricePopup(QFrame):
    """Frameless tool window: name + Copy, candidate chips, then maplestory.watch (or a fallback).

    ``open_in_browser(name)`` is the old flow (browser.py, off the GUI thread); the popup only calls it.
    ``use_web=False`` forces the fallback (and is what a build without QtWebEngine gets).
    """

    def __init__(self, open_in_browser: Callable[[str], None], parent: QWidget | None = None,
                 use_web: bool | None = None, url: str = WATCH_URL):
        super().__init__(parent, POPUP_FLAGS)
        self.setObjectName("pricePopup")
        self.setStyleSheet(POPUP_QSS)
        self.resize(*POPUP_SIZE)
        self._open_in_browser = open_in_browser
        self._url = url
        self._use_web = webengine_available() if use_web is None else use_web
        self.name = ""
        self.view = None  # the QWebEngineView, built on the first show and kept between popups
        self._loaded = False  # a load succeeded once: later popups leave the view (and where the user went) alone
        self._loading = False  # a load is under way: another hotkey press must not restart it
        self._activated = False

        # --- header: name, Copy, Open in browser, Pin, close
        self.name_label = QLabel(objectName="ppName")
        self.name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the name again (it is copied already)")
        self.copy_btn.clicked.connect(lambda: self.copy(self.name))
        self.browser_btn = QPushButton("Open in browser")
        self.browser_btn.setToolTip("The name is copied: bring up the price site in your own browser")
        self.browser_btn.clicked.connect(self._browser)
        self.pin_btn = QPushButton("Pin")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip("Keep this open when you click elsewhere")
        close_btn = QPushButton("✕")
        close_btn.setToolTip("Close (Esc)")
        close_btn.clicked.connect(self.hide)
        top = QHBoxLayout()
        top.addWidget(self.name_label, 1)
        for b in (self.copy_btn, self.browser_btn, self.pin_btn, close_btn):
            top.addWidget(b)

        self.hint = QLabel(objectName="ppHint")
        self.warn = QLabel(objectName="ppWarn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        self.chips_box = QWidget()
        self.chips_row = QHBoxLayout(self.chips_box)
        self.chips_row.setContentsMargins(0, 0, 0, 0)
        self.chips_row.setSpacing(4)
        self.chips_row.addStretch(1)  # the only stretch: chips go in before it
        self.chips_box.hide()  # no chips, no row
        self.chips: list[QPushButton] = []

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.fallback = QLabel(
            "The built-in browser isn't available in this build.\n"
            "Use “Open in browser”, click the site's search box and press Ctrl+V.",
            objectName="ppHint", alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.body.addWidget(self.fallback, 1)
        self.fallback.setVisible(not self._use_web)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(top)
        lay.addWidget(self.hint)
        lay.addWidget(self.chips_box)
        lay.addWidget(self.warn)
        lay.addLayout(self.body, 1)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.hide, context=Qt.ShortcutContext.WindowShortcut)

    # --- showing ------------------------------------------------------------------

    def show_for(self, name: str, others: list[str] | None = None, cursor: QPoint | None = None) -> None:
        """Copy ``name``, fill the header and chips, and show next to ``cursor`` (on the hotkey only)."""
        self.name = name
        self.name_label.setText(name)
        self.name_label.setToolTip(name)
        self.copy(name)
        self._set_chips([o for o in (others or []) if o and o != name][:MAX_CHIPS])
        from .offgame import BROWSER_FLAGS, match_browser_style

        if self.windowFlags() != (flags := BROWSER_FLAGS if _browser else POPUP_FLAGS):
            self.setWindowFlags(flags)  # Safe mode changed since the last popup
        if cursor is not None:
            screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.resize(*fit_size(POPUP_SIZE, area))
                self.move(place_near(cursor, (self.width(), self.height()), area))
        if not self._off_the_game():
            self.hide()
            self._open_in_browser(name)  # the game fills every screen: no room beside it
            return
        if self._use_web:
            self._ensure_view()
        self._activated = False
        self.show()
        if _browser:
            match_browser_style(self)
        self._off_the_game()  # again with the real frame: a title bar makes it taller
        self.raise_()
        self.activateWindow()

    def _off_the_game(self) -> bool:
        """Safe mode, off the game: move beside the game window. False when there is no room anywhere."""
        if not _off_game:
            return True
        from .offgame import free_spot, game_rect, screen_areas

        if (game := game_rect()) is None:
            return True
        geo = self.frameGeometry()
        spot = free_spot(geo.size(), geo.topLeft(), game, screen_areas())
        if spot is None:
            return False
        self.move(spot)
        return True

    def copy(self, text: str) -> None:
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.hint.setText(f"Copied “{text}”: click the site's search box and press Ctrl+V.")

    def _set_chips(self, names: list[str]) -> None:
        for c in self.chips:
            self.chips_row.removeWidget(c)
            c.deleteLater()
        self.chips = []
        for n in names:
            c = QPushButton(n, objectName="ppChip")
            c.setToolTip(f"Also seen: copy “{n}”")
            c.clicked.connect(lambda _=False, n=n: self.copy(n))
            self.chips_row.insertWidget(len(self.chips), c)
            self.chips.append(c)
        self.chips_box.setVisible(bool(self.chips))

    def _browser(self) -> None:
        self.copy(self.name)
        self._open_in_browser(self.name)
        if not self.pin_btn.isChecked():
            self.hide()

    # --- the embedded site ----------------------------------------------------------

    def _ensure_view(self) -> None:
        """Build the view once; (re)load only when it never loaded fine, so popups don't reload the site."""
        if self.view is None:
            try:
                from PySide6.QtWebEngineCore import QWebEnginePage
                from PySide6.QtWebEngineWidgets import QWebEngineView
            except ImportError:
                self._use_web = False
                self.fallback.show()
                return
            class _QuietPage(QWebEnginePage):
                def javaScriptConsoleMessage(self, *_):
                    pass  # Qt would copy the site's console into our log: we keep nothing from the page

            self.view = QWebEngineView(self)
            page = _QuietPage(_WebProfile.get(), self.view)
            self.view.setPage(page)
            page.loadingChanged.connect(self._on_loading)
            # A link that wants a new tab or window opens here, in the same view (unread: openIn only).
            page.newWindowRequested.connect(lambda req, page=page: req.openIn(page))
            page.renderProcessTerminated.connect(lambda *_: self._on_load(True, 0))
            self.body.addWidget(self.view, 1)
        if not self._loaded and not self._loading:
            self.warn.hide()
            self._loading = True
            self.view.load(QUrl(self._url))

    def _on_loading(self, info) -> None:
        """Load status only (never the page itself): spot a block page or an HTTP error."""
        from PySide6.QtWebEngineCore import QWebEngineLoadingInfo as LI

        status = info.status()
        if status == LI.LoadStatus.LoadStartedStatus:
            self._loading = True
            return
        code = info.errorCode() if info.errorDomain() == LI.ErrorDomain.HttpStatusCodeDomain else 0
        failed = status == LI.LoadStatus.LoadFailedStatus or info.isErrorPage()
        self._on_load(failed, code)

    def _on_load(self, failed: bool, http_code: int) -> None:
        reason = blocked_reason(failed, http_code)
        self._loading = False
        self._loaded = self._loaded or not reason  # a later failed click inside the site doesn't undo it
        self.warn.setText(f"{reason} Use “Open in browser”: the name is on your clipboard." if reason else "")
        self.warn.setVisible(bool(reason))

    # --- closing: Esc, the ✕, or a click elsewhere unless pinned ---------------------------

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._activated = True
            elif self._activated and not self.pin_btn.isChecked():
                # Only after it was active once: if Windows kept the game in front, the popup stays
                # until Esc or ✕ rather than vanishing before the user sees it.
                self.hide()
