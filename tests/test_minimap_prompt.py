"""The minimap prompt's debounce: shows after a bad status holds, clears at once."""

import pytest

from mctracker.minimap_prompt import HOLD_S, MESSAGES, MinimapPrompt


@pytest.mark.parametrize("bad", ["missing", "names_hidden"])
def test_prompt_waits_for_the_hold_then_shows(bad):
    p = MinimapPrompt()
    assert p.update(bad, 100.0) is None
    assert p.update(bad, 100.0 + HOLD_S - 0.1) is None
    assert p.update(bad, 100.0 + HOLD_S) == MESSAGES[bad]
    assert p.update(bad, 500.0) == MESSAGES[bad]  # stays while the status holds


@pytest.mark.parametrize("clear", ["ok", "unknown"])
def test_prompt_clears_immediately(clear):
    p = MinimapPrompt()
    p.update("missing", 0.0)
    assert p.update("missing", 20.0) is not None
    assert p.update(clear, 20.1) is None


def test_a_blip_of_ok_restarts_the_hold():
    p = MinimapPrompt()
    p.update("missing", 0.0)
    p.update("ok", 6.0)
    assert p.update("missing", 7.0) is None
    assert p.update("missing", 16.0) is None  # only 9 s since it came back
    assert p.update("missing", 17.0) == MESSAGES["missing"]


def test_unknown_shows_nothing_and_resets_the_hold():
    p = MinimapPrompt()
    assert p.update("unknown", 0.0) is None and p.update("unknown", 60.0) is None
    p.update("names_hidden", 61.0)
    p.update("unknown", 65.0)  # loading screen
    assert p.update("names_hidden", 66.0) is None
    assert p.update("names_hidden", 75.0) is None
    assert p.update("names_hidden", 76.0) == MESSAGES["names_hidden"]


def test_switching_between_bad_statuses_restarts_the_hold():
    p = MinimapPrompt()
    p.update("missing", 0.0)
    assert p.update("names_hidden", 9.0) is None
    assert p.update("names_hidden", 18.0) is None
    assert p.update("names_hidden", 19.0) == MESSAGES["names_hidden"]
