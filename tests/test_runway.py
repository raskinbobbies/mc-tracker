"""Potion runway: estimate from recent quickslot counts, and the once-per-crossing alert (fake clock)."""

from types import SimpleNamespace

import pytest

from mctracker.items import SlotItems
from mctracker.runway import RunwayWatch, alert_text, fmt_minutes, rearm_min, runway_text

RED, BLUE, OTHER = [10.0] * 4, [200.0] * 4, [90.0] * 4


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def slots(*names):
    s = SlotItems(None)
    for i, (name, sig) in enumerate(names):
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


RED_ONLY = (("Red Potion", RED),)


def run(w, s, counts, step=1.0, threshold=None, ready=True):
    """Feed one count per ``step`` seconds for slot 0; returns (last update, alerts fired)."""
    out, fired = None, []
    for c in counts:
        out = w.update([read(c)], s)
        if threshold is not None:
            fired += w.alerts(threshold, ready)
        w.clock_.t += step
    return out, fired


def down(start, every, n):
    """``n`` one-second readings from ``start``, one use every ``every`` seconds."""
    return [start - i // every for i in range(n)]


def test_estimate_is_count_over_recent_rate(watch):
    s = slots(*RED_ONLY)
    r = run(watch, s, down(100, 30, 181))[0][0]  # 2 uses a minute for 3 minutes
    assert r.count == 94
    assert r.per_min == pytest.approx(2.0, rel=0.02)
    assert r.minutes == pytest.approx(47, rel=0.02)
    assert runway_text([r]) == "Red Potion 94 · ~47 min"


def test_no_recent_use_hides_the_estimate(watch):
    s = slots(*RED_ONLY)
    assert run(watch, s, [50] * 30)[0][0].minutes is None
    run(watch, s, [49] + [49] * 185)  # the use falls out of the 3-minute window
    r = run(watch, s, [49])[0][0]
    assert r.minutes is None and runway_text([r]) == ""


def test_unreadable_or_other_icon_hides_the_estimate(watch):
    s = slots(*RED_ONLY)
    run(watch, s, [50, 49, 48])
    assert watch.update([read(None)], s)[0].minutes is None
    assert watch.update([read(47, OTHER)], s)[0].minutes is None  # the slot shows another item
    assert watch.update([], s)[0].count is None


def test_one_sip_is_not_a_crazy_rate(watch):
    s = slots(*RED_ONLY)
    r = run(watch, s, [50, 49])[0][0]  # 1 use in 1 s: taken over a minute, not 60/min
    assert r.per_min == pytest.approx(1.0)


def test_misread_spike_is_ignored_for_count_and_rate(watch):
    s = slots(*RED_ONLY)
    run(watch, s, down(142, 10, 60))  # 6 a minute: 142 -> 137
    r = run(watch, s, [42, 42, 137])[0][0]  # 137 read as 42 twice, then right again
    assert r.count == 137 and r.per_min == pytest.approx(5.0, rel=0.25)
    r = run(watch, s, [42, 42])[0][0]  # while suspect, the last good count shows
    assert r.count == 137


def test_refill_and_moved_stack_count_once_they_hold_never_as_uses(watch):
    s = slots(*RED_ONLY)
    run(watch, s, [50, 49])
    r = run(watch, s, [200] * 4)[0][0]  # a refill holds 3 s: accepted
    assert r.count == 200 and r.per_min == pytest.approx(1.0)
    r = run(watch, s, [199, 19, 198, 197])[0][0]  # 199 -> 19 and back: a misread, not 180 uses
    assert r.count == 197 and r.per_min == pytest.approx(4.0)
    r = run(watch, s, [100] * 5)[0][0]  # half the stack moved away: accepted, not 97 uses
    assert r.count == 100 and r.per_min == pytest.approx(4.0)


def test_gap_restarts_the_rate(watch):
    s = slots(*RED_ONLY)
    run(watch, s, down(100, 10, 60))
    watch.clock_.t += 30  # loading screen: no readings
    r = run(watch, s, [80])[0][0]  # 14 fewer after the gap: not uses
    assert r.count == 80 and r.minutes is None


def test_reset_forgets_the_readings(watch):
    s = slots(*RED_ONLY)
    run(watch, s, down(100, 10, 60))
    watch.reset()
    assert run(watch, s, [94])[0][0].minutes is None


def test_item_on_two_slots_uses_the_first_readable(watch):
    s = SlotItems(None)
    s.assign(0, RED, None, "Red Potion")
    s.assign(3, RED, None, "Red Potion")
    out = watch.update([read(None), read(None), read(None), read(40)], s)
    assert [(r.name, r.count) for r in out] == [("Red Potion", 40)]


def test_alert_fires_once_per_crossing_and_rearms_above_the_margin(watch):
    s = slots(*RED_ONLY)
    _, fired = run(watch, s, down(30, 10, 120), threshold=5)  # 6 a minute: 30 -> 19, under 5 min near the end
    assert len(fired) == 1 and fired[0].minutes < 5
    assert alert_text(fired[0]).startswith("Red Potion running low: ~")
    # Loot pickups and a wobbling rate don't re-arm while the runway stays under the margin.
    _, fired = run(watch, s, [19, 20, 19, 18, 18, 17], threshold=5)
    assert fired == []
    # A refill lifts the runway past the margin; used down again: a second alert.
    _, fired = run(watch, s, [100] * 5 + down(100, 2, 120), threshold=5)
    assert len(fired) == 1


def test_misread_never_toasts_and_never_rearms(watch):
    s = slots(*RED_ONLY)
    _, fired = run(watch, s, down(142, 10, 60), threshold=5)
    assert fired == []
    _, fired = run(watch, s, [42, 42, 137, 42, 42, 137], threshold=5)  # 42 would be ~8 min, 4 would be <1
    assert fired == []
    _, fired = run(watch, s, [4, 4, 137, 137], threshold=5)  # a deep misread: under 3 s, no toast
    assert fired == []


def test_transient_dip_needs_to_hold(watch):
    s = slots(*RED_ONLY)
    run(watch, s, down(40, 2, 40))  # 30 a minute from 40
    rs = watch.update([read(20)], s)
    assert rs[0].minutes < 5
    assert watch.alerts(5) == []  # first look under: not yet
    watch.clock_.t += 3
    watch.update([read(19)], s)
    assert len(watch.alerts(5)) == 1


def test_alert_waits_until_ready_and_fires_once_if_still_short(watch):
    s = slots(*RED_ONLY)
    _, fired = run(watch, s, down(30, 10, 120), threshold=5, ready=False)
    assert fired == []
    _, fired = run(watch, s, [19, 19], threshold=5, ready=True)
    assert len(fired) == 1
    _, fired = run(watch, s, [19, 19], threshold=5, ready=True)
    assert fired == []


def test_alert_needs_an_estimate(watch):
    s = slots(*RED_ONLY)
    _, fired = run(watch, s, [3] * 10, threshold=5)  # very few, but none used: no estimate, no alert
    assert fired == []


def test_unassigned_item_is_forgotten(watch):
    s = slots(("Red Potion", RED), ("Blue Potion", BLUE))
    assert len(watch.update([read(10), read(20, BLUE)], s)) == 2
    s.assign(1, None, None, "")
    assert [r.name for r in watch.update([read(10), read(20, BLUE)], s)] == ["Red Potion"]


def test_fmt_minutes_and_rearm():
    assert fmt_minutes(0.4) == "<1 min"
    assert fmt_minutes(18.4) == "~18 min"
    assert fmt_minutes(150) == "~2.5 h"
    assert rearm_min(5) == 7.5 and rearm_min(2) == 4


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


@pytest.fixture
def live(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mctracker.gui import MainWindow
    from mctracker.items import AppSettings
    from mctracker.overlay import Overlay
    from mctracker.session import Session, Tracker

    QApplication.instance() or QApplication([])
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    tr.slot_items.assign(0, RED, None, "Red Potion")
    tr.session = Session(map_name="Test map")
    overlay = Overlay(tr, None)
    overlay.show()
    w = MainWindow(tr, overlay, None, "alt+a")
    clock = Clock()
    w.runway = RunwayWatch(clock=clock)
    w.front = True
    w.game_in_front = lambda: w.front

    def feed(counts):
        for c in counts:
            tr.slots = [read(c)]
            w._refresh_runway()
            clock.t += 1

    w.feed = feed
    yield tr, w
    if hasattr(w._runway_toast, "close"):
        w._runway_toast.close()
    w.close()
    overlay.close()


def test_live_tab_shows_runway_and_a_real_toast_once(live):
    tr, w = live
    h = w.runway_label.height()
    w.feed(down(30, 10, 120))  # 6 a minute from 30
    assert w.runway_label.text().startswith("Runway: Red Potion 19 · ~")
    assert w.runway_label.height() == h  # space kept: no layout shift
    toast = w._runway_toast  # the real Toast, made on the first alert
    assert toast is not None and toast.isVisible() and "Red Potion running low" in toast.text.text()
    toast.hide()
    w.feed([19] * 10)
    assert not toast.isVisible()  # once per crossing
    w.set_game_attached(False)  # stale reads: empty, and the readings start over
    assert w.runway_label.text() == ""
    w.set_game_attached(True)
    w.feed([19])
    assert w.runway_label.text() == ""


def test_toast_is_gated_and_held_until_the_game_is_in_front(live):
    tr, w = live
    shown = []
    w._runway_toast = SimpleNamespace(show_message=lambda text, kind="info": shown.append(text))
    w.front = False
    w.feed(down(30, 10, 120))
    assert shown == []  # held while another window is in front
    tr.paused = True
    w.front = True
    w.feed([19] * 3)
    assert shown == []  # held while paused
    tr.paused = False
    w.feed([19] * 2)
    assert len(shown) == 1  # fired once the user can see it, still short
    w.feed([19] * 5)
    assert len(shown) == 1


def test_alert_setting_off_holds_the_toast(live, tmp_path):
    from mctracker.items import AppSettings

    tr, w = live
    shown = []
    w._runway_toast = SimpleNamespace(show_message=lambda text, kind="info": shown.append(text))
    w.runway_action.setChecked(False)
    assert AppSettings(tmp_path / "app.json").runway_alert is False
    assert not w.runway_min_action.isEnabled()
    w.feed(down(30, 10, 120))
    assert shown == [] and w.runway_label.text().startswith("Runway: Red Potion")


def test_edit_runway_threshold(live, tmp_path, monkeypatch):
    from mctracker import gui
    from mctracker.items import AppSettings

    tr, w = live
    asked = []
    monkeypatch.setattr(gui.QInputDialog, "getInt",
                        lambda *a: (asked.append(a[3:]), (12, True))[1])
    w.edit_runway_threshold()
    assert asked == [(5, 1, 60)]
    assert tr.settings.runway_alert_min == 12 and AppSettings(tmp_path / "app.json").runway_alert_min == 12
    assert w.runway_min_action.text() == "Runway alert under 12 min…"
    monkeypatch.setattr(gui.QInputDialog, "getInt", lambda *a: (30, False))  # cancelled: unchanged
    w.edit_runway_threshold()
    assert tr.settings.runway_alert_min == 12
