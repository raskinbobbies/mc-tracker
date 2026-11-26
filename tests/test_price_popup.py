"""Price-check popup: header, clipboard, chips, fallback, placement, and the hands-off rules for the site."""

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from mctracker import price_popup  # noqa: E402
from mctracker.price_popup import POPUP_SIZE, PricePopup, blocked_reason, fit_size, place_near  # noqa: E402

SRC = Path(price_popup.__file__)


def popup(**kw):
    opened = []
    p = PricePopup(opened.append, use_web=False, **kw)
    return p, opened


def test_popup_shows_the_name_and_copies_it():
    p, _ = popup()
    p.show_for("Ilbi Throwing-Stars", ["Steely Throwing-Knives", ""], QPoint(100, 100))
    assert p.isVisible()
    assert p.name_label.text() == "Ilbi Throwing-Stars"
    assert QGuiApplication.clipboard().text() == "Ilbi Throwing-Stars"
    assert "Ctrl+V" in p.hint.text()
    assert [c.text() for c in p.chips] == ["Steely Throwing-Knives"]  # blanks dropped
    p.hide()


def test_copy_button_and_chips_put_the_text_on_the_clipboard():
    p, _ = popup()
    p.show_for("Red Potion", ["Orange Potion", "White Potion"])
    QGuiApplication.clipboard().setText("something else")
    p.copy_btn.click()
    assert QGuiApplication.clipboard().text() == "Red Potion"
    p.chips[1].click()
    assert QGuiApplication.clipboard().text() == "White Potion"
    # A new check replaces the chips, never piling them up; the hovered name is not its own chip.
    p.show_for("Elixir", ["Elixir", "Power Elixir"])
    assert [c.text() for c in p.chips] == ["Power Elixir"]
    # One stretch, after the chips, however many checks ran (else the chips drift right).
    for _ in range(3):
        p.show_for("Elixir", ["Power Elixir", "Mana Elixir"])
    row = p.chips_row
    assert row.count() == 3 and row.itemAt(2).spacerItem() is not None
    assert [row.itemAt(i).widget().text() for i in range(2)] == ["Power Elixir", "Mana Elixir"]
    # No other names: the row takes no space.
    p.show_for("Elixir", [])
    assert p.chips_box.isHidden() and row.count() == 1
    p.hide()


def test_fallback_without_webengine_still_offers_the_browser():
    p, opened = popup()
    p.show_for("Brown Work Gloves")
    assert p.view is None and not p.fallback.isHidden()
    p.browser_btn.click()
    assert opened == ["Brown Work Gloves"]
    assert QGuiApplication.clipboard().text() == "Brown Work Gloves"
    assert not p.isVisible()  # not pinned: done with the popup


def test_pin_keeps_it_open_after_open_in_browser():
    p, opened = popup()
    p.show_for("Snail Shell")
    p.pin_btn.setChecked(True)
    p.browser_btn.click()
    assert opened == ["Snail Shell"] and p.isVisible()
    p.hide()


def test_a_block_page_or_403_shows_the_warning():
    assert "403" in blocked_reason(False, 403)
    assert blocked_reason(True, 0)
    assert blocked_reason(False, 200) == "" and blocked_reason(False, 0) == ""
    p, _ = popup()
    p.show_for("Blue Snail Shell")
    p._on_load(False, 403)
    assert not p.warn.isHidden() and "Open in browser" in p.warn.text() and not p._loaded
    p._on_load(False, 200)
    assert p.warn.isHidden() and p._loaded
    p.hide()


def _covers(pos, size, cursor):
    return pos.x() <= cursor.x() < pos.x() + size[0] and pos.y() <= cursor.y() < pos.y() + size[1]


def test_placement_beside_the_cursor_and_on_screen():
    screen = QRect(0, 0, 1920, 1040)  # 1080p less the taskbar
    size = fit_size(POPUP_SIZE, screen)
    assert size == POPUP_SIZE
    # Mid-screen: to the right, centred on the cursor, never over the hovered item.
    mid = QPoint(960, 520)
    pos = place_near(mid, size, screen)
    assert pos == QPoint(978, 520 - size[1] // 2) and not _covers(pos, size, mid)
    # Right edge: flips to the left. Corners: still fully on screen, still off the cursor.
    for cur in (QPoint(1900, 520), QPoint(1900, 1030), QPoint(5, 5), QPoint(1900, 5)):
        pos = place_near(cur, size, screen)
        assert screen.contains(QRect(pos.x(), pos.y(), *size)) and not _covers(pos, size, cur), cur


def test_size_shrinks_to_a_scaled_screen():
    screen = QRect(0, 0, 1097, 594)  # 1080p at 175%, taskbar off
    w, h = fit_size(POPUP_SIZE, screen)
    assert w == POPUP_SIZE[0] and h <= 594 - 16
    pos = place_near(QPoint(548, 300), (w, h), screen)
    assert screen.contains(QRect(pos.x(), pos.y(), w, h))


class FakeView:
    """Stands in for the QWebEngineView: counts loads (no site in tests)."""

    def __init__(self):
        self.loads = 0

    def load(self, _url):
        self.loads += 1


def test_hotkey_presses_never_restart_a_load_or_reset_where_the_user_went():
    p, _ = popup()
    p._use_web, p.view = True, FakeView()
    p.show_for("Red Potion")
    assert p.view.loads == 1
    p.show_for("Blue Potion")  # still loading (a challenge page, say): leave it
    assert p.view.loads == 1
    p._on_load(False, 403)  # blocked: the next press retries
    p.show_for("Blue Potion")
    assert p.view.loads == 2
    p._on_load(False, 200)
    p._loading = True  # the user clicks around the site...
    p._on_load(True, 0)  # ...and a page in there fails
    assert p._loaded and not p.warn.isHidden()
    p.show_for("Elixir")  # the view stays where the user left it
    assert p.view.loads == 2
    p.hide()


def test_source_never_reads_or_scripts_the_site():
    """Their terms forbid bots reading the market: the view is a plain browser for the human."""
    code = re.sub(r'""".*?"""', "", SRC.read_text(encoding="utf-8"), flags=re.S)  # docstrings may name them
    for banned in ("runJavaScript", "toHtml", "toPlainText", "setHttpUserAgent", "QWebChannel",
                   "scripts()", "QWebEngineScript", "UrlRequestInterceptor", "setUrlRequestInterceptor",
                   "findText", "QWebEngineUrlSchemeHandler", "printToPdf", "save(",
                   # where the user is and what the page says: not ours to look at either
                   ".url(", ".title(", "titleChanged", "urlChanged", "linkHovered", "selectedText",
                   "grab(", "setDevToolsPage", "QWebEngineDownloadRequest", "iconChanged", "contextMenu"):
        assert banned not in code, banned
    # Allowed: the load status signal, and only its status/error fields.
    assert "loadingChanged" in code and "req.openIn(page)" in code  # new windows: same view, unread
    uses = re.findall(r"\binfo\.(\w+)\(", code)
    assert uses
    for used in uses:
        assert used in {"status", "errorCode", "errorDomain", "isErrorPage"}, used


def test_setting_round_trips_and_defaults_on(tmp_path):
    from mctracker.items import AppSettings

    s = AppSettings(tmp_path / "app.json")
    assert s.price_popup is True
    s.price_popup = False
    s.save()
    assert AppSettings(tmp_path / "app.json").price_popup is False


def _window(tmp_path):
    from mctracker.gui import MainWindow
    from mctracker.items import AppSettings
    from mctracker.overlay import Overlay
    from mctracker.session import Tracker
    from test_overlay_layout import FlatAssets

    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    assets = FlatAssets(tmp_path)
    return MainWindow(tr, Overlay(tr, assets), assets, "alt+a")


def test_hotkey_opens_the_popup_or_the_old_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(price_popup, "webengine_available", lambda: False)  # no site loads in tests
    w = _window(tmp_path)
    w.show_price([("Mana Elixir", 2000), ("Elixir", 2001)])
    assert w._popup.isVisible() and w._popup.name_label.text() == "Mana Elixir"
    assert [c.text() for c in w._popup.chips] == ["Elixir"]
    w.price_popup_action.setChecked(False)  # Settings: back to the browser flow
    assert not w._popup.isVisible()
    calls = []
    monkeypatch.setattr(w, "price_check", lambda name, others=None: calls.append((name, others)))
    w.show_price([("Mana Elixir", 2000)])
    assert calls == [("Mana Elixir", [])] and not w._popup.isVisible()
    w.close()
