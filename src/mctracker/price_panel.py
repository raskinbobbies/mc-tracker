"""Embedded mesos.watch panel with automated item search.

mesos.watch forbids framing and has no per-item URLs, so we load it as a
normal top-level page in a QWebEngineView (the user browses it as themselves)
and type the item name into its search box. We never call its API directly.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QKeyEvent
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

MESOS_WATCH_URL = "https://mesos.watch/"
SEARCH_SELECTOR = 'input[placeholder="Search or ask the market"]'

_FILL_JS = """
(function (selector, text) {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(el, text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
})(%s, %s)
"""


class PricePanel(QWebEngineView):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Persistent profile keeps the user's mesos.watch login and Cloudflare clearance.
        from .paths import sub

        self._profile = QWebEngineProfile("mc-tracker", self)
        self._profile.setPersistentStoragePath(str(sub("web")))
        self._profile.setCachePath(str(sub("web-cache")))
        self.setPage(QWebEnginePage(self._profile, self))
        self._loaded = False
        self._pending: str | None = None
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl(MESOS_WATCH_URL))

    def search(self, item_name: str) -> None:
        self._pending = item_name
        if self._loaded:
            self._try_fill(attempts=20)

    def _on_loaded(self, ok: bool) -> None:
        self._loaded = ok
        if ok and self._pending:
            self._try_fill(attempts=20)

    def _try_fill(self, attempts: int) -> None:
        name = self._pending
        if not name:
            return
        js = _FILL_JS % (json.dumps(SEARCH_SELECTOR), json.dumps(name))

        def done(found):
            if found:
                self._pending = None
                self._press_enter()
            elif attempts > 0:  # SPA may still be rendering
                QTimer.singleShot(250, lambda: self._try_fill(attempts - 1))

        self.page().runJavaScript(js, 0, done)

    def _press_enter(self) -> None:
        # Real key events, so the site sees a trusted Enter press.
        target = self.focusProxy() or self
        self.setFocus()
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            ev = QKeyEvent(kind, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r")
            from PySide6.QtWidgets import QApplication

            QApplication.sendEvent(target, ev)
