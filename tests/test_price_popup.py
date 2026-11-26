"""Price-check popup: header, clipboard, chips, fallback, placement, and the hands-off rules for the site."""

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from mctracker import price_popup  # noqa: E402
from mctracker.price_popup import PricePopup, blocked_reason, place_near  # noqa: E402

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


def test_placement_stays_on_screen():
    screen = QRect(0, 0, 1920, 1080)
    assert place_near(QPoint(100, 100), (560, 680), screen) == QPoint(118, 118)
    # Near the bottom-right corner it flips to the cursor's left and above.
    pos = place_near(QPoint(1900, 1060), (560, 680), screen)
    assert pos.x() + 560 <= 1920 and pos.y() + 680 <= 1080 and pos.x() >= 0 and pos.y() >= 0
    # Smaller screen than the popup: pinned to the top-left rather than off-screen.
    assert place_near(QPoint(50, 50), (560, 680), QRect(0, 0, 400, 300)) == QPoint(0, 0)


def test_source_never_reads_or_scripts_the_site():
    """Their terms forbid bots reading the market: the view is a plain browser for the human."""
    code = re.sub(r'""".*?"""', "", SRC.read_text(encoding="utf-8"), flags=re.S)  # docstrings may name them
    for banned in ("runJavaScript", "toHtml", "toPlainText", "setHttpUserAgent", "QWebChannel",
                   "scripts()", "QWebEngineScript", "UrlRequestInterceptor", "setUrlRequestInterceptor",
                   "findText", "QWebEngineUrlSchemeHandler", "printToPdf", "save("):
        assert banned not in code, banned


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
