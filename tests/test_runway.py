"""Potion runway: estimate from recent quickslot counts, and the once-per-crossing alert (fake clock)."""

from types import SimpleNamespace

import pytest

from mctracker.items import SlotItems
from mctracker.runway import RunwayWatch, alert_text, fmt_minutes, runway_text

RED, BLUE, OTHER = [10.0] * 4, [200.0] * 4, [90.0] * 4


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def slots(**names):
    s = SlotItems(None)
    for i, (name, sig) in enumerate(names.values()):
        s.assign(i, sig, None, name)
    return s


def read(count, sig=RED):
    return SimpleNamespace(count=count, signature=sig)


@pytest.fixture
def watch():
    clock = Clock()
    w = RunwayWatch(clock=clock)
    w.clock_ = clock
    return w


def run(w, s, counts, step=1.0):
    """Feed one count per second for slot 0; returns the last update."""
    out = None
    for c in counts:
        out = w.update([read(c)], s)
        w.clock_.t += step
    return out


def test_estimate_is_count_over_recent_rate(watch):
    s = slots(a=("Red Potion", RED))
    # 2 uses a minute for 3 minutes, 1 read a second.
    counts = [100 - (i // 30) for i in range(181)]
    r = run(watch, s, counts)[0]
    assert r.count == 94
    assert r.per_min == pytest.approx(2.0, rel=0.02)
    assert r.minutes == pytest.approx(47, rel=0.02)
    assert runway_text([r]) == "Red Potion 94 · ~47 min"


def test_no_recent_use_hides_the_estimate(watch):
    s = slots(a=("Red Potion", RED))
    assert run(watch, s, [50] * 30)[0].minutes is None
    run(watch, s, [49])
    watch.clock_.t += 301  # the use falls out of the 5-minute window
    r = run(watch, s, [49])[0]
    assert r.minutes is None and runway_text([r]) == ""


def test_unreadable_or_other_icon_hides_the_estimate(watch):
    s = slots(a=("Red Potion", RED))
    run(watch, s, [50, 49, 48])
    assert watch.update([read(None)], s)[0].minutes is None
    assert watch.update([read(47, OTHER)], s)[0].minutes is None  # the slot shows another item
    assert watch.update([], s)[0].count is None


def test_one_sip_is_not_a_crazy_rate(watch):
    s = slots(a=("Red Potion", RED))
    r = run(watch, s, [50, 49])[0]  # 1 use in 1 s: taken over a minute, not 60/min
    assert r.per_min == pytest.approx(1.0)


def test_refills_and_misreads_are_not_uses(watch):
    s = slots(a=("Red Potion", RED))
    r = run(watch, s, [50, 49, 200, 199, 19, 198])[0]  # refill to 200; 199 -> 19 is a misread
    assert r.per_min == pytest.approx(2.0)  # 50->49 and 200->199 only


def test_item_on_two_slots_uses_the_first_readable(watch):
    s = SlotItems(None)
    s.assign(0, RED, None, "Red Potion")
    s.assign(3, RED, None, "Red Potion")
    out = watch.update([read(None), read(None), read(None), read(40)], s)
    assert [(r.name, r.count) for r in out] == [("Red Potion", 40)]


def test_alert_fires_once_per_crossing_and_rearms_after_refill(watch):
    s = slots(a=("Red Potion", RED))
    counts = [30 - (i // 10) for i in range(120)]  # 6 uses a minute: 30 -> 19
    alerts = []
    for c in counts:
        watch.update([read(c)], s)
        alerts += watch.alerts(5)
        watch.clock_.t += 1
    assert len(alerts) == 1 and alerts[0].minutes < 5
    assert alert_text(alerts[0]).startswith("Red Potion running low: ~")
    # A loot pickup (+1) doesn't re-arm; the alert stays quiet while still low.
    for c in (19, 20, 19, 18):
        watch.update([read(c)], s)
        assert watch.alerts(5) == []
        watch.clock_.t += 1
    # Refill to 25, then use down again: a second alert.
    fired = []
    for c in [25] + [25 - (i // 5) for i in range(60)]:
        watch.update([read(c)], s)
        fired += watch.alerts(5)
        watch.clock_.t += 1
    assert len(fired) == 1


def test_alert_needs_an_estimate(watch):
    s = slots(a=("Red Potion", RED))
    run(watch, s, [3] * 10)  # very few, but none used: no estimate, no alert
    assert watch.alerts(5) == []


def test_unassigned_item_is_forgotten(watch):
    s = slots(a=("Red Potion", RED), b=("Blue Potion", BLUE))
    assert len(watch.update([read(10), read(20, BLUE)], s)) == 2
    s.assign(1, None, None, "")
    assert [r.name for r in watch.update([read(10), read(20, BLUE)], s)] == ["Red Potion"]


def test_fmt_minutes():
    assert fmt_minutes(0.4) == "<1 min"
    assert fmt_minutes(18.4) == "~18 min"
    assert fmt_minutes(150) == "~2.5 h"


# --- settings and the live tab --------------------------------------------------------------------


def test_runway_settings_persist_and_bad_values_fall_back(tmp_path):
    import json

    from mctracker.items import AppSettings

    p = tmp_path / "app.json"
    st = AppSettings(p)
    assert (st.runway_alert, st.runway_alert_min) == (True, 5)
    st.runway_alert, st.runway_alert_min = False, 12
    st.save()
    st = AppSettings(p)
    assert (st.runway_alert, st.runway_alert_min, st.problem) == (False, 12, None)
    p.write_text(json.dumps({"runway_alert": "yes", "runway_alert_min": 0}))
    st = AppSettings(p)
    assert (st.runway_alert, st.runway_alert_min) == (True, 5) and "runway_alert" in st.problem


def test_live_tab_shows_runway_and_toasts_once(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mctracker.gui import MainWindow
    from mctracker.items import AppSettings
    from mctracker.overlay import Overlay
    from mctracker.session import Tracker

    QApplication.instance() or QApplication([])
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    tr.slot_items.assign(0, RED, None, "Red Potion")
    overlay = Overlay(tr, None)
    w = MainWindow(tr, overlay, None, "alt+a")
    clock = Clock()
    w.runway = RunwayWatch(clock=clock)
    toasts = []
    w._runway_toast = SimpleNamespace(show_message=lambda text, kind="info": toasts.append(text))
    for i in range(120):  # 6 uses a minute from 30
        tr.slots = [read(30 - i // 10)]
        w._refresh_runway()
        clock.t += 1
    assert w.runway_label.text().startswith("Runway: Red Potion 19 · ~")
    assert not w.runway_label.isHidden()
    assert len(toasts) == 1 and "Red Potion running low" in toasts[0]
    w.set_game_attached(False)  # stale reads: hidden
    assert w.runway_label.isHidden()
    w.set_game_attached(True)
    w.runway_action.setChecked(False)  # alert off: the estimate stays, no toast after a refill
    assert AppSettings(tmp_path / "app.json").runway_alert is False
    for c in [30] + [30 - i // 5 for i in range(80)]:
        tr.slots = [read(c)]
        w._refresh_runway()
        clock.t += 1
    assert len(toasts) == 1 and w.runway_label.text().startswith("Runway: Red Potion")
    w.close()
    overlay.close()
