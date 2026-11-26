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
    "Tunnel Street": ["Deep Tunnel I", "Deep Tunnel II"],  # a name that ends in an I-lookalike letter
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


def test_a_fuzzy_change_without_loading_lands_after_the_hold(monkeypatch):
    # "VIIl" is VIII only through the I-lookalike repair: not an exact read, so it votes.
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    first = w.t + w.step
    while len(w.got) == 1:
        w.read(f"{FUN} VIIl")
    assert w.names()[-1] == f"Victoria Road : {FUN} VIII"
    assert w.got[-1][1] == _mobs(f"{FUN} VIII")
    took = w.t - first
    # 3 reads to lead the vote, then the hold
    lead_at = (capture.MAP_CONFIRM_READS - 1) * w.step
    assert lead_at + capture.MAP_CONFIRM_S <= took <= lead_at + capture.MAP_CONFIRM_S + 1.0


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


def _channel_world(monkeypatch, tmp_path):
    """A worker whose channel reads feed a real Tracker that already knows channel 26."""
    from mctracker.items import AppSettings
    from mctracker.session import Tracker

    w = _Worker(monkeypatch)
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    w.w.channel_read.connect(tr.on_channel)
    w.w.map_read.connect(tr.on_map)
    w.read(f"{FUN} VII")
    tr.on_channel(26)
    return w, tr


def test_a_portal_to_another_map_keeps_the_channel(monkeypatch, tmp_path):
    w, tr = _channel_world(monkeypatch, tmp_path)
    w.w._exp_seen_at = w.t
    w.read("", panel_ok=False)
    w.read("zz", panel_ok=False)
    w.read(f"{FUN} VIII")  # a loading transition, then a different map
    assert w.names()[-1] == f"Victoria Road : {FUN} VIII" and tr.channel == 26


def test_the_same_map_after_a_loading_gap_clears_the_channel(monkeypatch, tmp_path):
    w, tr = _channel_world(monkeypatch, tmp_path)
    w.w._exp_seen_at = w.t
    w.read("", panel_ok=False)
    w.read("zz", panel_ok=False)
    w.read(f"{FUN} VII")  # a channel change reloads the same map
    assert len(w.got) == 1 and tr.channel is None


def test_a_collapsed_minimap_without_loading_keeps_the_channel(monkeypatch, tmp_path):
    w, tr = _channel_world(monkeypatch, tmp_path)
    for _ in range(4):
        w.read("", panel_ok=False)
        w.w._exp_seen_at = w.t  # EXP still reads: not a loading screen
    w.read(f"{FUN} VII")
    assert tr.channel == 26


def test_a_loading_gap_that_ends_on_an_unreadable_map_clears_the_channel_after_a_few_seconds(monkeypatch, tmp_path):
    from mctracker import capture

    w, tr = _channel_world(monkeypatch, tmp_path)
    w.w._exp_seen_at = w.t
    for _ in range(3):
        w.read("", panel_ok=False)  # loading screen: no panel, EXP not read
    for _ in range(int(capture.UNRESOLVED_S / w.step) - 2):
        w.read("zz")  # the panel is back, the map matches nothing
    assert tr.channel == 26  # not yet
    for _ in range(4):
        w.read("zz")
    assert tr.channel is None


def test_an_unreadable_map_after_a_collapsed_minimap_keeps_the_channel(monkeypatch, tmp_path):
    w, tr = _channel_world(monkeypatch, tmp_path)
    for _ in range(5):
        w.read("", panel_ok=False)
        w.w._exp_seen_at = w.t  # EXP keeps reading: no loading screen
    for _ in range(30):
        w.read("zz")
    assert tr.channel == 26


# --- the numeral glued to the name (owner's live Primeval Forest I, 2026-11-25) --------------------

@pytest.mark.parametrize("read, want", [
    # What the recognizer gave for the real title "Primeval Forest I" at 2000x1125,
    # 1920x1080 and 2560x1440 / 1280x720: the thin "I" glued on as T, 1 or i.
    ("Prime vai ForestT", "Primeval Forest I"),
    ("Prime vai Forest1", "Primeval Forest I"),
    ("Prime vai Foresti", "Primeval Forest I"),
    ("Primeval ForestI", "Primeval Forest I"),
    ("Primeval Forestl", "Primeval Forest I"),
    ("Primeval Forest|", "Primeval Forest I"),
    ("Primeval Forest T", "Primeval Forest I"),  # spaced, read as T
    ("Primeval Forest I", "Primeval Forest I"),
    ("Primeval ForestIII", "Primeval Forest III"),  # III stays III, I stays I
    ("Primeval Forest III", "Primeval Forest III"),
])
def test_glued_or_lookalike_numeral_resolves_to_its_map(read, want):
    assert name("Shallow Passage", read) == want
    assert name("Shallow Passage", read, clipped=True) == want


@pytest.mark.parametrize("read", [
    "Primeval Forest", "Primeval Forestll", "Primeval Forest II",
    "Primeval Foresl", "Primeval Foresi", "Prime vai Foresl",  # a noisy "t": not "Fores" + I
])
def test_a_numeral_the_place_lacks_or_a_dropped_one_is_never_another_map(read):
    # Primeval Forest has I and III here: II is not III, and no numeral is not I.
    assert name("Shallow Passage", read) is None
    assert g.match_map("Shallow Passage", read)[1]  # the place is still known: raw read, no mobs


def test_a_word_ending_in_a_numeral_letter_is_not_split():
    # "Deep Tunnel" ends in "l", but "Deep Tunne" + I would be a worse match than the name as read.
    assert name("Tunnel Street", "Deep Tunnel") is None
    assert name("Tunnel Street", "Deep Tunnel I") == "Deep Tunnel I"
    assert name("Tunnel Street", "Deep TunnelI") == "Deep Tunnel I"


def test_raw_reads_of_a_raw_map_do_not_outvote_its_resolved_reads(monkeypatch):
    # A numeral the recognizer reads one time in three used to leave the map raw ("(?)",
    # no mobs, "Not a hunting map") for good: the raw reads all voted for the shown raw map.
    w = _Worker(monkeypatch)
    w.read(FUN)
    for _ in range(12):
        w.read(f"{FUN} VII")
        w.read(FUN)
        w.read(FUN)
    assert w.got == [
        (f"Victoria Road : {FUN} (?)", (), False),
        (f"Victoria Road : {FUN} VII", _mobs(f"{FUN} VII"), True),
    ]


def test_a_resolved_map_still_ignores_isolated_raw_reads(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    for _ in range(12):
        w.read(FUN)
        w.read(FUN)
        w.read(f"{FUN} VII")
    assert w.names() == [f"Victoria Road : {FUN} VII"]


def test_stale_votes_expire_and_do_not_shorten_a_later_hold(monkeypatch):
    # Raw reads of a raw map return early: a lead from long ago must not keep its old clock
    # and let one more misread confirm at once, without the MAP_CONFIRM_S hold.
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.read(FUN)  # raw place shown
    for _ in range(3):
        w.read(f"{FUN} Vl")  # a misread leads: the hold starts
    for _ in range(40):  # then a long run of raw reads (16 s: past MAP_VOTE_TTL_S)
        w.read(FUN)
    w.read(f"{FUN} Vl")  # one more, long after
    assert w.names() == [f"Victoria Road : {FUN} (?)"]  # not confirmed: the old lead is gone
    w.hold(f"{FUN} Vl")
    assert w.names()[-1] == f"Victoria Road : {FUN} VI"  # a real change still lands


def test_a_lead_whose_votes_aged_out_loses_its_clock(monkeypatch):
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.read(FUN)
    for _ in range(3):
        w.read(f"{FUN} Vl")  # leads: the hold starts
    assert w.w._map_lead is not None
    w.t += capture.MAP_VOTE_TTL_S + 1
    w.read(FUN)  # a raw read of the raw shown place, long after
    assert w.w._map_lead is None


# --- Forgotten Entrance III shown as I (owner report 2026-11-26) -------------------------------

_FE = "Forgotten Entrance"
FE_MAPS = [{"id": 900 + i, "streetName": "Shallow Passage", "name": f"{_FE} {n}"}
           for i, n in enumerate(["I", "II", "III"])]


@pytest.fixture
def fe_maps(monkeypatch):
    monkeypatch.setattr(g, "maps", lambda: MAPS + FE_MAPS)
    g._map_index.cache_clear()


@pytest.mark.parametrize("read, want", [
    # clean reads
    (f"{_FE} I", "I"), (f"{_FE} II", "II"), (f"{_FE} III", "III"),
    # I-lookalikes
    (f"{_FE} lII", "III"), (f"{_FE} Ill", "III"), (f"{_FE} lll", "III"), (f"{_FE} Il", "II"),
    (f"{_FE} II1", "III"), (f"{_FE} 111", "III"), (f"{_FE} III'", "III"),
    # the numeral glued to the last word, in every length
    ("Forgotten EntranceIII", "III"), ("Forgotten EntranceII", "II"),
    ("Forgotten EntranceI", "I"), ("Forgotten EntranceIl", "II"), ("Forgotten Entrancelll", "III"),
    # the strokes split apart by spaces: these used to read as I or II
    (f"{_FE} I II", "III"), (f"{_FE} I I I", "III"),
    (f"{_FE} I I", "II"), ("Forgotten EntranceI II", "III"), ("Forgotten EntranceI I", "II"),
])
def test_forgotten_entrance_numerals(fe_maps, read, want):
    assert name("Shallow Passage", read) == f"{_FE} {want}"


@pytest.mark.parametrize("read", [
    _FE,  # no numeral at all
    f"{_FE} IV", f"{_FE} IIII", f"{_FE} V",  # numerals the place doesn't have
    # a fragment longer than one stroke before the numeral: II or III, so neither
    f"{_FE} II I", f"{_FE} Il l", f"{_FE} II Il", f"{_FE} III I",
])
def test_forgotten_entrance_never_invents_a_numeral(fe_maps, read):
    assert name("Shallow Passage", read) is None
    assert g.match_map("Shallow Passage", read)[1] is not None  # the place still matched: raw read


def test_a_read_is_not_given_strokes_it_does_not_have(fe_maps):
    assert g.split_numeral(f"{_FE} I") == (_FE, "I")
    assert g.split_numeral(f"{_FE} I II") == (_FE, "III")
    assert g.split_numeral(f"{_FE} II I") == (f"{_FE} II I", None)  # not promoted to III, not cut to I
    assert g.split_numeral("Free Market <9>") == ("Free Market <9>", None)


@pytest.mark.parametrize("read, exact", [
    (f"{_FE} III", True), (f"{_FE} I", True), (f" {_FE}  II ", True),
    (f"{_FE} Ill", False), (f"{_FE} I II", False), ("Forgotten EntranceIII", False),
    (f"{_FE[:-1]}", False), (f"Forgotton Entrance III", False), (f"{_FE} IIII", False),
])
def test_exact_read(fe_maps, read, exact):
    m = g.find_map("Shallow Passage", read)
    assert bool(m and g.exact_read("Shallow Passage", read, m)) is exact


def _fe_worker(monkeypatch):
    w = _Worker(monkeypatch)
    w.step = 0.4
    return w


def _switch_time(w, read, street="Shallow Passage"):
    """Seconds from the first read of ``read`` to the map_read it causes."""
    n, first = len(w.got), w.t + w.step
    for _ in range(40):
        w.read(read, street)
        if len(w.got) > n:
            return w.t - first
    return None


def test_exact_read_of_another_map_switches_fast(fe_maps, monkeypatch):
    from mctracker import capture

    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    took = _switch_time(w, f"{_FE} III")
    assert w.names()[-1] == f"Shallow Passage : {_FE} III"
    assert took == pytest.approx(capture.MAP_FAST_SPAN_S)
    assert took <= 1.0  # was MAP_CONFIRM_S + 3 reads: ~4.4 s


def test_a_fuzzy_read_of_another_map_still_waits_for_the_vote(fe_maps, monkeypatch):
    from mctracker import capture

    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    took = _switch_time(w, f"{_FE} Ill")  # the lookalike repair: not exact
    assert w.names()[-1] == f"Shallow Passage : {_FE} III"
    assert took >= capture.MAP_CONFIRM_S


def test_split_strokes_get_off_a_stale_map(fe_maps, monkeypatch):
    # The shown map is the old I; the title is III, read as spaced strokes. Before the numeral
    # merge these read as I / II and the shown map never left I.
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    for read in (f"{_FE} I II", f"{_FE} I I I", f"{_FE} III", f"{_FE} I II", f"{_FE} I I I", f"{_FE} III"):
        w.read(read, "Shallow Passage")
    for _ in range(8):
        w.read(f"{_FE} I II", "Shallow Passage")
    assert w.names()[-1] == f"Shallow Passage : {_FE} III"


def test_one_exact_read_alone_never_switches(fe_maps, monkeypatch):
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} III", "Shallow Passage")
    for _ in range(10):  # a single exact II every third read: never two in a row
        w.read(f"{_FE} II", "Shallow Passage")
        w.read(f"{_FE} III", "Shallow Passage")
        w.read(f"{_FE} III", "Shallow Passage")
    assert w.names() == [f"Shallow Passage : {_FE} III"]


def test_a_truncated_numeral_is_never_a_fast_switch(fe_maps, monkeypatch):
    # A window over the end of "III" reads an exact "II" / "I", a prefix of the shown numeral:
    # that waits for the vote and the hold like any other change.
    from mctracker import capture

    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} III", "Shallow Passage")
    start = w.t
    while w.t - start < capture.MAP_CONFIRM_S:
        w.read(f"{_FE} II", "Shallow Passage")
        assert len(w.got) == 1


def test_a_raw_read_with_the_wrong_numeral_does_not_vote_for_the_shown_map(fe_maps, monkeypatch):
    # Shown: the stale I. Raw reads of the place whose numeral the place lacks used to count as
    # "no change" for I and could outvote the real III reads (2 exact III per 5 reads never lead).
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    for _ in range(6):
        w.read(f"{_FE} Ill", "Shallow Passage")
        w.read(f"{_FE} IV", "Shallow Passage")
        w.read(f"{_FE} IV", "Shallow Passage")
        w.read(f"{_FE} Ill", "Shallow Passage")
    assert w.names()[-1] == f"Shallow Passage : {_FE} III"


def _count(w):
    return len(w.got)


def test_two_back_to_back_exact_reads_do_not_flip_the_map(fe_maps, monkeypatch):
    # A frozen frame read twice 0.4 s apart is one observation: a flip ends the session.
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    assert _count(w) == 1
    w.read(f"{_FE} I", "Shallow Passage")  # and the stuck frame ends: back to the shown map
    assert w.names() == [f"Shallow Passage : {_FE} I"]


def test_exact_reads_further_apart_than_the_window_do_not_chain(fe_maps, monkeypatch):
    from mctracker import capture

    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.t += capture.MAP_FAST_WINDOW_S + 0.5  # nothing readable in between
    w.read(f"{_FE} III", "Shallow Passage")
    w.t += capture.MAP_FAST_WINDOW_S + 0.5
    w.read(f"{_FE} III", "Shallow Passage")
    assert _count(w) == 1  # three exact reads over 4 s, never a run of them


def test_a_read_of_the_shown_map_restarts_the_fast_run(fe_maps, monkeypatch):
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} I", "Shallow Passage")  # the run was 0.4 s: this resets it
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    assert _count(w) == 1  # without the reset this run would be 4 reads long and have flipped already
    w.read(f"{_FE} III", "Shallow Passage")
    assert w.names()[-1] == f"Shallow Passage : {_FE} III"  # the new run spans MAP_FAST_SPAN_S: now it does


def test_a_fuzzy_read_restarts_the_fast_run(fe_maps, monkeypatch):
    w = _fe_worker(monkeypatch)
    w.read(f"{_FE} I", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    w.read(f"{_FE} Ill", "Shallow Passage")  # fuzzy: resolves to III but is no exact read
    w.read(f"{_FE} III", "Shallow Passage")
    assert _count(w) == 1


def test_a_fast_switch_leaves_no_stale_votes_behind(fe_maps, monkeypatch):
    w = _fe_worker(monkeypatch)
    for _ in range(3):
        w.read(f"{_FE} I", "Shallow Passage")  # votes for the old map
    for _ in range(3):
        w.read(f"{_FE} III", "Shallow Passage")
    assert w.names() == [f"Shallow Passage : {_FE} I", f"Shallow Passage : {_FE} III"]
    w.read(f"{_FE} I", "Shallow Passage")  # one stray read of the old map, then the new one again
    w.read(f"{_FE} I", "Shallow Passage")
    w.read(f"{_FE} III", "Shallow Passage")
    assert w.names()[-1] == f"Shallow Passage : {_FE} III" and _count(w) == 2
    assert w.w._map_lead is None
