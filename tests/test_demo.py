"""Demo feed: produces identified monster kills."""

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.demo import MOBS, DemoFeed  # noqa: E402
from mctracker.session import Tracker  # noqa: E402


def test_demo_identifies_monsters(tmp_path):
    _app = QApplication.instance() or QApplication([])  # widget tests share it
    random.seed(1)
    tr = Tracker(save_dir=tmp_path)
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(60):
        feed.tick()
    s = tr.session
    assert s is not None
    assert s.killer.party_identifiable()
    kills = s.kills
    assert set(kills) <= {m.id for m in MOBS}
    assert len(kills) == len(MOBS)  # every monster shows up as a tile
    assert sum(kills.values()) >= s.total_kills * 0.6
    assert s.exp.levels_gained >= 1 and s.exp.rejected_total == 0
