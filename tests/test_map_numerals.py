"""Roman numeral map suffixes resolve exactly (#26): fuzzy matching never turns
"VII" into "VI", and a read that may be cut short doesn't commit to a shorter map."""

from pathlib import Path

import pytest

from mctracker import gamedata as g
from mctracker.kills import Mob

_NAMES = {
    "Victoria Road": ["The Tree Tunnel At the Forest Up North", "Henesys"]
    + [f"Tree Dungeon, Forest Up North {n}" for n in "I II III IV V VI VII VIII IX".split()],
    "Warning Street": [f"The Burnt Land {n}" for n in "I II III IV V".split()],
    "Shallow Passage": ["Primeval Forest I", "Primeval Forest III", "Forgotten Hollow"],
    "Hidden Street": ["Free Market <9>", "Camp 1"],
    # No real street has both IX and X: a made-up one for that pair.
    "Test Street": [f"Ant Tunnel {n}" for n in "VIII IX X".split()],
}
MAPS = [{"id": i, "streetName": s, "name": n}
        for i, (s, n) in enumerate((s, n) for s, ns in _NAMES.items() for n in ns)]


@pytest.fixture(autouse=True)
def fake_maps(monkeypatch):
    monkeypatch.setattr(g, "maps", lambda: MAPS)
    g._map_index.cache_clear()
    yield
    g._map_index.cache_clear()


def name(street, read, clipped=False):
    m = g.find_map(street, read, clipped=clipped)
    return m and m["name"]


FUN = "Tree Dungeon, Forest Up North"


@pytest.mark.parametrize("read, want", [
    ("VI", "VI"), ("VII", "VII"), ("VIII", "VIII"),
    ("II", "II"), ("III", "III"), ("IV", "IV"), ("V", "V"), ("IX", "IX"),
    # I-lookalikes in the numeral are I (the #26 read was "VIl").
    ("VIl", "VII"), ("VI|", "VII"), ("V1I", "VII"), ("Vl", "VI"), ("vii", "VII"), ("ll", "II"),
    # A stray mark after the numeral (#34 review: "VII'").
    ("VII'", "VII"), ("VII.", "VII"), ("VI\"", "VI"),
])
def test_numeral_matches_exactly(read, want):
    assert name("Victoria Road", f"{FUN} {read}") == f"{FUN} {want}"


@pytest.mark.parametrize("read, want", [("IX", "IX"), ("X", "X"), ("VIII", "VIII"), ("lX", "IX")])
def test_ix_vs_x(read, want):
    assert name("Test Street", f"Ant Tunnel {read}") == f"Ant Tunnel {want}"


def test_fuzzy_place_but_exact_numeral():
    # OCR noise in the place still matches; the numeral still has to be exact.
    assert name("Victorla Road", "Tree Dungeon, Forest Up Nort VII") == f"{FUN} VII"
    assert name("Warning Street", "The Burnt Lánd IV") == "The Burnt Land IV"


@pytest.mark.parametrize("read", [FUN, f"{FUN} X", f"{FUN} IIII"])
def test_missing_or_unknown_numeral_is_no_match_on_a_known_place(read):
    m, known = g.match_map("Victoria Road", read)
    assert m is None and known  # show the raw read with no mobs, not map I


def test_garbage_is_not_a_known_place():
    assert g.match_map("Victoria Road", "zzzz qqqq") == (None, None)


def test_numeral_noise_on_a_place_without_numbered_maps():
    # #34 review: "Henesys l" used to fuzzy-match Henesys; the "l" isn't a numeral there.
    assert name("Victoria Road", "Henesys l") == "Henesys"
    assert name("Victoria Road", "Henesys") == "Henesys"


@pytest.mark.parametrize("read, want", [
    # The exact numeral wins when the place has it (#34 review: VI, VII and every
    # I/II with a longer sibling used to resolve to nothing when clipped).
    ("VI", "VI"), ("VII", "VII"), ("VIl", "VII"), ("VIII", "VIII"), ("IX", "IX"), ("I", "I"),
    ("X", None),  # no X here, and nothing starts with it
])
def test_clipped_read_takes_the_exact_numeral(read, want):
    assert name("Victoria Road", f"{FUN} {read}", clipped=True) == (want and f"{FUN} {want}")
    assert g.match_map("Victoria Road", f"{FUN} {read}", clipped=True)[1]


def test_clipped_read_takes_the_only_longer_numeral():
    # Primeval Forest has I and III but no II: a clipped "II" can only be III.
    assert name("Shallow Passage", "Primeval Forest II", clipped=True) == "Primeval Forest III"
    assert name("Shallow Passage", "Primeval Forest II") is None


def test_names_without_numerals_are_unchanged():
    assert name("Hidden Street", "Free Market <9>") == "Free Market <9>"
    assert name("Shallow Passage", "Forgotten Hollow") == "Forgotten Hollow"
    assert name("Hidden Street", "Camp 1") == "Camp 1"
    assert name("Victoria Road", "The Tree Tunnel At the Forest Up North") == \
        "The Tree Tunnel At the Forest Up North"


def _fixture_rows():
    path = Path(__file__).parent / "fixtures" / "minimap_titles.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            frame, street, read, clipped, want = (p.strip() for p in line.split("|"))
            yield pytest.param(street, read, clipped == "1", want or None, id=frame)


@pytest.mark.parametrize("street, read, clipped, want", list(_fixture_rows()))
def test_real_frame_reads(street, read, clipped, want):
    assert name(street, read, clipped) == want


class _Worker:
    """A CaptureWorker with its map_read emissions recorded, title reads 0.4 s
    apart (MAP_EVERY_S), optionally wired to a real Tracker."""

    def __init__(self, monkeypatch, tracker=None):
        from mctracker import capture

        monkeypatch.setattr(g, "map_mobs", lambda map_id: (
            () if map_id == _id("Henesys") else (Mob(map_id, f"mob{map_id}", 10),)))
        self.w = capture.CaptureWorker()
        self.got = []
        self.w.map_read.connect(lambda name, mobs, refine: self.got.append((name, tuple(mobs), refine)))
        if tracker is not None:
            self.w.map_read.connect(tracker.on_map)
        self.t = 0.0
        self.step = capture.MAP_EVERY_S

    def read(self, name, street="Victoria Road", clipped=False, panel_ok=True):
        self.t += self.step
        return self.w._resolve_map(street, name, clipped, self.t, panel_ok)

    def hold(self, name, street="Victoria Road", n=12):  # 4.8 s: past MAP_CONFIRM_S
        for _ in range(n):
            self.read(name, street)

    def names(self):
        return [n for n, _, _ in self.got]


def _id(map_name):
    return next(m["id"] for m in MAPS if m["name"] == map_name)


def _mobs(map_name):
    i = _id(map_name)
    return (Mob(i, f"mob{i}", 10),)


def test_capture_shows_raw_read_with_no_mobs_until_it_resolves(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} X")  # a known place, but it has no X: raw, marked, no mobs
    w.hold(f"{FUN} VII")  # same place, resolved: a refine, once it holds the vote
    assert w.got == [
        (f"Victoria Road : {FUN} X (?)", (), False),
        (f"Victoria Road : {FUN} VII", _mobs(f"{FUN} VII"), True),
    ]


def test_resolved_read_replaces_a_raw_one_with_the_same_text(monkeypatch):
    # #34 review: the raw fallback emitted the same string as the resolved map, so
    # Tracker.on_map ignored the later read's mobs. The raw read is now marked.
    w = _Worker(monkeypatch)
    real, first = g.match_map, [True]

    def raw_once(street, name, clipped=False):
        m, place = real(street, name, clipped)
        if first.pop() if first else False:
            return None, place
        return m, place

    monkeypatch.setattr(g, "match_map", raw_once)
    w.read(f"{FUN} VII", clipped=True)
    w.hold(f"{FUN} VII")
    assert w.got == [
        (f"Victoria Road : {FUN} VII (?)", (), False),
        (f"Victoria Road : {FUN} VII", _mobs(f"{FUN} VII"), True),
    ]


def test_a_single_wrong_refine_read_is_ignored(monkeypatch):
    # #34 re-review: one resolved read used to refine a raw map for the whole visit.
    w = _Worker(monkeypatch)
    w.read(f"{FUN} X")
    w.read(f"{FUN} VII")
    w.hold(f"{FUN} X")
    assert w.names() == [f"Victoria Road : {FUN} X (?)"]


@pytest.mark.parametrize("noise", [
    f"{FUN} VII'",  # stray mark: still VII
    FUN,  # numeral dropped: a raw read of the shown place
    f"{FUN} VI",  # a wrong numeral
    "Tree Dungeon, Forest Up North XI",  # a numeral the place doesn't have: raw
    "zzzz qqqq",  # unreadable
])
def test_isolated_slips_never_win_the_vote(monkeypatch, noise):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    for _ in range(6):  # a slip on every third read: 2 of each 5, never 3
        w.read(noise)
        w.read(f"{FUN} VII")
        w.read(f"{FUN} VII")
    assert w.names() == [f"Victoria Road : {FUN} VII"]  # one map_read: no session split


def test_a_misread_held_on_screen_does_not_split_before_3s(monkeypatch):
    # A window over the end of "VIII" reads "VII", a real map, with the same pixels
    # every read: it leads the vote after 3 reads but must hold MAP_CONFIRM_S.
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.read(f"{FUN} VIII")
    start = w.t
    while w.t - start < capture.MAP_CONFIRM_S:
        assert len(w.got) == 1, w.t - start
        w.read(f"{FUN} VII")
    w.hold(f"{FUN} VIII")  # the window moves away before it confirms...
    assert len(w.got) == 1
    w2 = _Worker(monkeypatch)
    w2.read(f"{FUN} VIII")
    w2.read(f"{FUN} VII")
    w2.read(f"{FUN} VII")
    w2.read(f"{FUN} VIII")  # ...a read of the shown map takes the lead back
    w2.read(f"{FUN} VIII")
    for _ in range(10):
        w2.read(f"{FUN} VII")
        w2.read(f"{FUN} VIII")
    assert w2.names() == [f"Victoria Road : {FUN} VIII"]


def test_a_change_without_loading_lands_after_the_hold(monkeypatch):
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    first = w.t + w.step
    while len(w.got) == 1:
        w.read(f"{FUN} VIII")
    assert w.names()[-1] == f"Victoria Road : {FUN} VIII"
    assert w.got[-1][1] == _mobs(f"{FUN} VIII")
    took = w.t - first
    assert capture.MAP_CONFIRM_S <= took <= capture.MAP_CONFIRM_S + 1.0


def test_a_portal_changes_the_map_fast(monkeypatch):
    # No panel and no EXP read for >= LOADING_S, then a new map: at once.
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    w.w._exp_seen_at = w.t
    w.read("", panel_ok=False)  # portal: dark frame, no panel
    w.read("zz", panel_ok=False)  # fallback crop over the loading frame: matches nothing
    w.read(f"{FUN} VIII")
    assert w.names() == [f"Victoria Road : {FUN} VII", f"Victoria Road : {FUN} VIII"]


def test_a_hidden_minimap_with_exp_reading_is_not_loading(monkeypatch):
    # The minimap toggled off (EXP still read), then back with a misread: no shortcut.
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    for _ in range(4):
        w.read("", panel_ok=False)
        w.w._exp_seen_at = w.t
    w.read(f"{FUN} VIII")
    w.read(f"{FUN} VII")
    assert len(w.got) == 1


def test_a_brief_panel_gap_is_not_loading(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    w.read("", panel_ok=False)  # one read (0.4 s < LOADING_S)
    w.read(f"{FUN} VIII")
    assert len(w.got) == 1


def test_no_loading_shortcut_for_raw_reads_or_other_skins(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    w.read("", panel_ok=False)
    w.read("", panel_ok=False)
    w.read("The Burnt Land XI", street="Warning Street")  # raw: still needs the vote
    assert len(w.got) == 1
    w2 = _Worker(monkeypatch)
    w2.read(f"{FUN} VII", panel_ok=False)  # another skin: fallback still reads the map
    w2.read(f"{FUN} VIII", panel_ok=False)
    assert len(w2.got) == 1


def _tracker(tmp_path, monkeypatch):
    from mctracker.session import Tracker

    tr = Tracker(save_dir=tmp_path)
    ends = []
    real = tr.end_session
    monkeypatch.setattr(tr, "end_session", lambda: (tr.map_name and ends.append(tr.map_name)) or real())
    return tr, ends


def _grind(tr):
    """Start a session on the current map: EXP rising, as in test_history."""
    from mctracker.exp_table import EXP_TABLE, bar_percent

    total = 100_000
    tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=0)
    for i in range(1, 4):
        tr.on_message_lines([("You received EXP (+10)", 0.0)], now=i)
        total += 10
        tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=i)


@pytest.mark.parametrize("before, after, street_after", [
    (f"{FUN} VII", f"{FUN} VIII", "Victoria Road"),  # resolved -> resolved
    (f"{FUN} VII", "The Burnt Land XI", "Warning Street"),  # resolved -> raw, another place
    (f"{FUN} VII", "Henesys", "Victoria Road"),  # -> a non-combat map (no mobs)
])
def test_a_confirmed_map_change_ends_the_session(tmp_path, monkeypatch, before, after, street_after):
    tr, ends = _tracker(tmp_path, monkeypatch)
    w = _Worker(monkeypatch, tr)
    w.read(before)
    _grind(tr)
    assert tr.session is not None and tr.mobs
    w.hold(after, street_after)
    assert tr.session is None  # ended: a new one starts on the next EXP gain
    assert ends == [f"Victoria Road : {before}"]
    assert tr.map_name == w.names()[-1]


def test_raw_to_resolved_of_another_place_ends_the_session(tmp_path, monkeypatch):
    tr, ends = _tracker(tmp_path, monkeypatch)
    w = _Worker(monkeypatch, tr)
    w.read("The Burnt Land XI", street="Warning Street")  # raw, no mobs
    w.hold(f"{FUN} VII")
    assert w.got[-1][2] is False and ends == ["Warning Street : The Burnt Land XI (?)"]
    assert tr.mobs == _mobs(f"{FUN} VII")


def test_a_b_a_ends_a_session_each_time(tmp_path, monkeypatch):
    tr, ends = _tracker(tmp_path, monkeypatch)
    w = _Worker(monkeypatch, tr)
    w.read(f"{FUN} VII")
    w.hold(f"{FUN} VIII")
    w.hold(f"{FUN} VII")
    assert ends == [f"Victoria Road : {FUN} VII", f"Victoria Road : {FUN} VIII"]
    assert tr.map_name == f"Victoria Road : {FUN} VII"


def test_same_place_raw_to_resolved_keeps_the_session(tmp_path, monkeypatch):
    tr, ends = _tracker(tmp_path, monkeypatch)
    w = _Worker(monkeypatch, tr)
    w.read(f"{FUN} X")  # raw
    w.hold(f"{FUN} VII")  # resolved: a refine
    assert ends == [] and tr.mobs == _mobs(f"{FUN} VII")
    # And with a session running under the raw name (on_map directly: a raw map
    # has no mobs, so in the app a session there only starts from a resumed one).
    tr2, ends2 = _tracker(tmp_path / "b", monkeypatch)
    tr2.on_map("Victoria Road : X", _mobs(f"{FUN} VI"))
    _grind(tr2)
    s = tr2.session
    assert s is not None
    tr2.on_map(f"Victoria Road : {FUN} VII", _mobs(f"{FUN} VII"), True)
    assert tr2.session is s and ends2 == []
    assert s.map_name == f"Victoria Road : {FUN} VII" and s.mobs == _mobs(f"{FUN} VII")


@pytest.mark.parametrize("panel, place, exp_age, want", [
    ("ok", "x", 1.0, "ok"),
    ("names_hidden", "x", 1.0, "names_hidden"),
    ("missing", None, 1.0, "missing"),
    ("missing", "x", 1.0, "ok"),  # another skin: the static fallback still matched a map
    ("missing", None, 10.0, "unknown"),  # no EXP bar lately: login, loading, Cash Shop
    ("ok", "x", None, "unknown"),  # no EXP read yet
])
def test_minimap_status_refined(monkeypatch, panel, place, exp_age, want):
    w = _Worker(monkeypatch).w
    w._exp_seen_at = None if exp_age is None else 100.0 - exp_age
    assert w._refine_minimap_status(panel, place, 100.0) == want
