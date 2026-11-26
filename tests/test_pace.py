"""Pace vs your record here: saved-session stats per map, the comparison, and its place in both windows."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mctracker.pace import (
    MIN_LIVE_S,
    MIN_SAVED_S,
    RAW_MAP_MARK,
    MapRecord,
    best_sessions,
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


def test_saved_rate_is_exp_gained_not_net_of_deaths():
    data = {"map": MAP, "active_seconds": 1800, "exp_gained": 60_000, "exp_lost": 10_000}
    assert saved_rate(data) == pytest.approx(120_000)
    # An older file saved a net exp_per_hour a death made negative: the gained EXP still counts.
    old = {**data, "exp_lost": 90_000, "exp_per_hour": -60_000}
    assert saved_rate(old) == pytest.approx(120_000)


def test_the_star_and_the_record_rank_by_the_same_figure():
    from mctracker.history import SavedSession
    from mctracker.pace import best_sessions, records

    def saved(gained, lost, per_hour, day):
        return SavedSession(Path(f"{day}.json"), {"map": MAP, "started_at": f"2026-11-{day}T10:00:00",
                                                 "active_seconds": 1800, "exp_gained": gained,
                                                 "exp_lost": lost, "exp_per_hour": per_hour})

    died = saved(90_000, 80_000, 20_000, 20)  # best gained, but a net figure saved before the fix
    clean = saved(60_000, 0, 120_000, 21)
    assert best_sessions([died, clean])[MAP] is died
    assert records([died, clean])[MAP].best == pytest.approx(180_000)  # the pill's best is the starred one


def test_saved_rate_skips_junk():
    assert saved_rate({"active_seconds": "x", "exp_per_hour": 5}) is None
    assert saved_rate({"active_seconds": 900, "exp_per_hour": "fast"}) is None
    assert saved_rate({}) is None
    for bad in (float("inf"), float("nan"), "inf"):  # hand-edited files: never a NaN percentage
        assert saved_rate(fake(bad).data) is None
        assert records([fake(bad), fake(1000)])[MAP].best == 1000


def test_raw_reads_never_pool_and_numerals_stay_apart():
    raw = "Warning Street : Burnt Land" + RAW_MAP_MARK
    recs = records([fake(1000, map_=raw), fake(2000, map_=raw), fake(3000, map_="Ant Tunnel : Ant Tunnel I"),
                    fake(4000, map_="Ant Tunnel : Ant Tunnel II")])
    assert raw not in recs and recs.get(raw) is None  # so a live raw-read map shows no pace
    assert recs["Ant Tunnel : Ant Tunnel I"].best == 3000 and recs["Ant Tunnel : Ant Tunnel II"].best == 4000


def test_raw_map_mark_matches_capture():
    capture = pytest.importorskip("mctracker.capture")
    assert capture.RAW_MAP_MARK == RAW_MAP_MARK


def session(rate, per_min, day, map_=MAP):
    s = fake(rate, map_=map_)
    s.exp_per_min, s.started = per_min, day
    return s


def test_best_sessions_by_the_shown_exp_per_min_newest_on_ties():
    old, new, slow = session(9e9, 500, "2026-11-01"), session(1, 500, "2026-11-20"), session(5e9, 400, "2026-11-25")
    assert best_sessions([old, new, slow]) == {MAP: new}  # the column's figure, not the saved EXP/hour
    assert best_sessions([session(1000, 900, "2026-11-01")]) == {}  # one session: nothing to beat
    assert best_sessions([session(-5, 999, "2026-11-01"), session(1000, 1, "2026-11-02")]) == {}  # a death's


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
    assert fmt_rate(999_949) == "999.9K/h"
    assert fmt_rate(999_950) == "1.00M/h"  # not "1000.0K/h"
    assert fmt_rate(99_999.4) == "99,999/h"
    assert fmt_rate(99_999.6) == "100.0K/h"  # not "100,000/h"


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

    from PySide6.QtCore import Qt

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
    assert " vs " not in w.overlay.sub.full_text()

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
    assert w.overlay.sub.full_text().endswith(" · ▲25% vs best")
    assert "▲ 25% vs your best here" in w.overlay.sub.toolTip()

    # The history table stars each map's best (only where there's more than one session to beat).
    here = s.map_name.split(" : ")[-1]
    rows = [(w.table.item(r, 5).text(), w.table.item(r, 5).data(Qt.ItemDataRole.UserRole))
            for r in range(w.table.rowCount()) if w.table.item(r, 2).text() == here]
    assert len(rows) == 2  # the other map's single session is never starred (nothing to beat)
    assert sum(w.table.item(r, 5).text().startswith("★") for r in range(w.table.rowCount())) == 1
    starred = [v for t, v in rows if t.startswith("★")]
    assert starred == [max(v for _t, v in rows if v)]  # one star, on the highest EXP/min the column shows

    # A long pill elides instead of forcing the left pane wider.
    w.pace_pill.set_full("▲ 25% vs your best here " + "x" * 400)
    assert w.pace_pill.minimumSizeHint().width() < 200

    # A raw-read live map has no record, so no pace.
    s.map_name = s.map_name + RAW_MAP_MARK
    w._refresh_live()
    assert not w.pace_pill.isVisibleTo(w)
    app.processEvents()  # the table's deferred fit runs while the window still exists
