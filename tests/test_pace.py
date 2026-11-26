"""Pace vs your record here: saved-session stats per map, the comparison, and its place in both windows."""

import json
import os
from types import SimpleNamespace

import pytest

from mctracker.pace import (
    MIN_LIVE_S,
    MIN_SAVED_S,
    MapRecord,
    compare,
    fmt_rate,
    pace_short,
    pace_text,
    records,
    saved_rate,
)

MAP = "Warning Street : The Burnt Land V"


def fake(rate, seconds=MIN_SAVED_S, map_=MAP, **extra):
    data = {"map": map_, "active_seconds": seconds, "exp_per_hour": rate, **extra}
    return SimpleNamespace(data=data)


# --- saved sessions ---------------------------------------------------------------------------

def test_saved_rate_needs_ten_minutes_and_a_positive_rate():
    assert saved_rate(fake(1000).data) == 1000
    assert saved_rate(fake(1000, seconds=MIN_SAVED_S - 1).data) is None
    assert saved_rate(fake(0).data) is None
    assert saved_rate(fake(-5000).data) is None  # deaths can make the saved figure negative


def test_saved_rate_falls_back_to_net_exp_for_old_files():
    data = {"map": MAP, "active_seconds": 1800, "exp_gained": 60_000, "exp_lost": 10_000}
    assert saved_rate(data) == pytest.approx(100_000)


def test_saved_rate_skips_junk():
    assert saved_rate({"active_seconds": "x", "exp_per_hour": 5}) is None
    assert saved_rate({"active_seconds": 900, "exp_per_hour": "fast"}) is None
    assert saved_rate({}) is None


def test_records_best_and_median_per_map():
    recs = records([fake(1_000_000), fake(1_400_000), fake(1_200_000), fake(900_000, map_="Other : Map"),
                    fake(5_000_000, seconds=60), fake(-1)])  # short and negative sessions don't count
    assert recs[MAP] == MapRecord(best=1_400_000, median=1_200_000, sessions=3)
    assert recs["Other : Map"].sessions == 1
    assert records([fake(-1), fake(10, seconds=5)]) == {}  # nothing that counts: no entry (hidden)
    assert records([SimpleNamespace(data={"active_seconds": 900, "exp_per_hour": 9})]) == {}  # no map


# --- the comparison ---------------------------------------------------------------------------

REC = MapRecord(best=1_420_000, median=1_200_000, sessions=4)


def test_nothing_until_the_live_session_has_run_long_enough():
    assert compare(REC, 2_000_000, MIN_LIVE_S - 1) is None
    assert compare(REC, 2_000_000, MIN_LIVE_S) is not None
    assert compare(None, 2_000_000, 3600) is None  # no history for the map
    assert compare(REC, 0, 3600) is None
    assert compare(REC, -50_000, 3600) is None  # right after a death


def test_ahead_of_best_is_green_vs_best():
    p = compare(REC, 1_590_400, 600)
    assert (p.ref, p.pct, p.tone) == ("best", 12, "over")
    assert pace_text(p) == "▲ 12% vs your best here (1.42M/h)"
    assert pace_short(p) == "▲12% vs best"


def test_between_median_and_best_is_grey_vs_best():
    p = compare(REC, 1_300_000, 600)
    assert (p.ref, p.tone) == ("best", "near")
    assert pace_text(p).startswith("▼ 8% vs your best here")


def test_below_median_is_red_vs_median():
    p = compare(REC, 1_104_000, 600)
    assert (p.ref, p.pct, p.tone) == ("median", -8, "under")
    assert pace_text(p) == "▼ 8% vs your median here (1.20M/h)"


def test_matching_best_reads_as_about_equal():
    p = compare(REC, 1_430_000, 600)
    assert p.tone == "near" and pace_text(p) == "≈ your best here (1.42M/h)" and pace_short(p) == "≈ best"


def test_one_saved_session_is_always_the_best():
    one = MapRecord(best=1_000_000, median=1_000_000, sessions=1)
    p = compare(one, 800_000, 600)
    assert (p.ref, p.pct, p.tone) == ("best", -20, "under")


def test_fmt_rate():
    assert fmt_rate(1_420_000) == "1.42M/h"
    assert fmt_rate(125_300) == "125.3K/h"
    assert fmt_rate(8_400) == "8,400/h"


# --- both windows -----------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def save(directory, name, map_, rate, seconds=1200):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({"map": map_, "started_at": f"2026-11-2{len(name) % 5}T10:00:00",
                                              "active_seconds": seconds, "exp_per_hour": rate,
                                              "exp_gained": round(rate * seconds / 3600)}), encoding="utf-8")


def test_live_tab_and_overlay_show_pace_only_with_history(app, tmp_path, monkeypatch):
    import random
    import time

    from mctracker.demo import DemoFeed
    from mctracker.gui import MainWindow
    from mctracker.items import AppSettings
    from mctracker.overlay import Overlay
    from mctracker.session import Tracker

    class NoAssets:
        def mob_sprite(self, _id, _cb):
            pass

        item_icon = mob_sprite

    random.seed(2)
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    tr.on_map(MAP, ())
    w = MainWindow(tr, Overlay(tr, NoAssets()), NoAssets(), "alt+a")
    w.show()
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(MIN_LIVE_S + 30):
        feed.tick()
        clock[0] += 1.0
    s = tr.session
    assert s is not None and s.session_rate_per_hour() > 0
    w._refresh_live()
    w.overlay.refresh()
    assert not w.pace_pill.isVisibleTo(w)  # no saved sessions on this map
    assert " vs " not in w.overlay.sub._full

    live = s.session_rate_per_hour()
    d = tmp_path / "sessions"
    save(d, "a.json", s.map_name, live / 2)
    save(d, "bb.json", s.map_name, live / 1.25)
    save(d, "ccc.json", "Elsewhere : Map", live * 10)  # another map never counts
    w.reload()
    w._refresh_live()
    assert w.pace_pill.isVisibleTo(w)
    assert w.pace_pill.text().startswith("▲ 25% vs your best here")
    assert w.pace_pill.property("tone") == "over"
    w.overlay.refresh()
    assert w.overlay.sub._full.endswith(" · ▲25% vs best")
    assert "▲ 25% vs your best here" in w.overlay.sub.toolTip()

    # The history table stars each map's best (only where there's more than one session to beat).
    stars = [w.table.item(r, 5).text() for r in range(w.table.rowCount()) if w.table.item(r, 5).text().startswith("★")]
    assert len(stars) == 1
