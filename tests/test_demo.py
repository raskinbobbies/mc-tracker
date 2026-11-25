"""Demo feed: produces identified monster kills."""

import os
import random
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.demo import MOBS, DemoFeed  # noqa: E402
from mctracker.session import Tracker  # noqa: E402


def test_demo_identifies_monsters(tmp_path, monkeypatch):
    _app = QApplication.instance() or QApplication([])  # widget tests share it
    random.seed(1)
    clock = [time.monotonic()]  # the feed ticks once a second: party kills settle seconds later
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tr = Tracker(save_dir=tmp_path)
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(60):
        feed.tick()
        clock[0] += 1.0
    s = tr.session
    assert s is not None
    kills = s.kills
    assert set(kills) <= {m.id for m in MOBS}
    assert kills[MOBS[-1].id] > 0  # party shares count for the top mob only (#27)
    assert sum(kills.values()) <= s.total_kills
    assert s.exp.levels_gained >= 1 and s.exp.rejected_total == 0
