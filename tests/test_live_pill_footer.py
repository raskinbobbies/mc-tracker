"""Live card: last-minute pill and the unknown-kills footer."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.gui import (  # noqa: E402
    MinutePill,
    UnknownKillsFooter,
    last_minute_pill,
    unknown_kills_text,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# minutes always end with the current partial minute, which the pill ignores
def test_pill_hidden_before_two_finished_minutes():
    assert last_minute_pill([], 100) is None
    assert last_minute_pill([500], 100) is None  # only a partial minute
    assert last_minute_pill([500, 40], 100) is None  # one finished + partial
    assert last_minute_pill([100, 100, 5], 0) is None  # no average to compare


def test_pill_over_under_near():
    assert last_minute_pill([100, 112, 7], 100) == ("12% over your average", "over")
    assert last_minute_pill([100, 66, 7], 100) == ("34% under your average", "under")
    assert last_minute_pill([100, 101, 7], 100) == ("at your average", "near")
    assert last_minute_pill([100, 98, 7], 100) == ("at your average", "near")  # -2% edge
    assert last_minute_pill([100, 103, 7], 100)[1] == "over"


def test_pill_ignores_partial_minute():
    assert last_minute_pill([100, 100, 9999], 100)[1] == "near"


def test_pill_widget_text_tone_and_visibility(app):
    w = MinutePill()
    w.set_minutes([100, 112, 7], 100)
    assert not w.isHidden() and w.text() == "12% over your average" and w.property("tone") == "over"
    w.set_minutes([100, 66, 7], 100)
    assert w.text() == "34% under your average" and w.property("tone") == "under"
    w.set_minutes([100, 100, 7], 100)
    assert w.text() == "at your average" and w.property("tone") == "near"
    w.set_minutes([100], 100)
    assert w.isHidden()


def test_footer_wording():
    assert unknown_kills_text(0, False) == ""
    assert unknown_kills_text(0, True) == ""
    assert unknown_kills_text(7, False) == "7 unknown kills"
    assert unknown_kills_text(7, True) == "7 unknown shared kills"
    assert unknown_kills_text(1, True) == "1 unknown shared kill"
    assert unknown_kills_text(1200, False) == "1,200 unknown kills"


def test_footer_widget_shown_or_hidden(app):
    f = UnknownKillsFooter()
    assert f.isHidden()
    f.set_count(7, True)
    assert not f.isHidden() and f.text() == "7 unknown shared kills"
    f.set_count(3, False)
    assert f.text() == "3 unknown kills"
    f.set_count(0, False)
    assert f.isHidden()


def test_warmup_threshold_is_one_minute():
    from mctracker.overlay import WARMUP_S, warmed_up

    assert WARMUP_S == 60
    assert not warmed_up(0) and not warmed_up(59) and not warmed_up(59.9)
    assert warmed_up(60) and warmed_up(61)


def test_rate_headline_at_59s_and_61s():
    from mctracker.gui import rate_headline

    cold = rate_headline(120_000, 59)
    assert cold.startswith("\u2014 EXP/min") and "warming up" in cold
    assert "120" not in cold and "EXP/hr" not in cold  # no rate figures leak
    warm = rate_headline(120_000, 61)
    assert warm.startswith("2.00K EXP/min") and "120.00K EXP/hr" in warm
    assert "warming up" not in warm and "EXP/hr" in warm and "\u2014" not in warm
