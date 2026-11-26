"""A death shows apart from the rates: the history detail, the live line, and files saved before the count."""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import history  # noqa: E402
from mctracker.gui import SessionDetail, death_note  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_death_note():
    assert death_note(1234, 1) == "−1,234 EXP lost (1 death)"
    assert death_note(5000, 2) == "−5,000 EXP lost (2 deaths)"
    assert death_note(5000, 0) == "−5,000 EXP lost"  # saved before the death count


def test_an_old_file_without_a_death_count_loads_and_shows_the_gained_rate(tmp_path, app):
    (tmp_path / "20261120-100000.json").write_text(json.dumps({
        "map": "Old", "started_at": "2026-11-20T10:00:00", "active_seconds": 1200,
        "exp_gained": 60_000, "exp_lost": 90_000, "exp_per_hour": -90_000,  # a net figure from before the fix
    }), encoding="utf-8")
    s = history.load(tmp_path)[0]
    assert s.data.get("deaths") is None and s.exp_per_min == 3000
    d = SessionDetail(None, lambda s: None)
    d.show_session(s)
    text = d.stats.text()
    assert "180.00K EXP/hr" in text  # gained, not the saved net figure
    assert "−90,000 EXP lost" in text and "death" not in text


def test_a_new_file_shows_the_death_count(app):
    d = SessionDetail(None, lambda s: None)
    d.show_session(history.SavedSession(Path("x.json"), {"started_at": "2026-11-26T10:00:00", "active_seconds": 600,
                                                         "exp_gained": 6000, "exp_lost": 1234, "deaths": 1}))
    assert "−1,234 EXP lost (1 death)" in d.stats.text()
