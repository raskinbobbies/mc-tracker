"""Roman numeral map suffixes resolve exactly (#26): fuzzy matching never turns
"VII" into "VI", and a read that may be cut short doesn't commit to a shorter map."""

from pathlib import Path

import pytest

from mctracker import gamedata as g

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
    """A CaptureWorker with its map_read emissions recorded, reads 3 s apart."""

    def __init__(self, monkeypatch):
        from mctracker import capture

        monkeypatch.setattr(g, "map_mobs", lambda map_id: (f"mob{map_id}",))
        self.w = capture.CaptureWorker()
        self.got = []
        self.w.map_read.connect(lambda name, mobs: self.got.append((name, tuple(mobs))))
        self.t = 0.0

    def read(self, name, street="Victoria Road", clipped=False):
        self.t += 3.0
        return self.w._resolve_map(street, name, clipped, self.t)

    def names(self):
        return [n for n, _ in self.got]


def _id(map_name):
    return next(m["id"] for m in MAPS if m["name"] == map_name)


def test_capture_shows_raw_read_with_no_mobs_until_it_resolves(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} X")  # a known place, but it has no X: raw, marked, no mobs
    w.read(f"{FUN} VII")  # same place, resolved: replaces the raw read at once
    assert w.got == [
        (f"Victoria Road : {FUN} X (?)", ()),
        (f"Victoria Road : {FUN} VII", (f"mob{_id(FUN + ' VII')}",)),
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
    w.read(f"{FUN} VII")
    assert w.got == [
        (f"Victoria Road : {FUN} VII (?)", ()),
        (f"Victoria Road : {FUN} VII", (f"mob{_id(FUN + ' VII')}",)),
    ]


@pytest.mark.parametrize("noise", [
    f"{FUN} VII'",  # stray mark: still VII
    FUN,  # numeral dropped: a raw read of the shown place
    f"{FUN} VI",  # one wrong numeral: not confirmed
    "zzzz qqqq",  # unreadable
])
def test_one_noisy_frame_does_not_change_the_map(monkeypatch, noise):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    w.read(noise)
    w.read(f"{FUN} VII")
    w.read(noise)
    w.read(f"{FUN} VII")
    assert w.names() == [f"Victoria Road : {FUN} VII"]  # one map_read: no session split


def test_a_real_map_change_lands_after_it_holds(monkeypatch):
    w = _Worker(monkeypatch)
    w.read(f"{FUN} VII")
    w.read(f"{FUN} VIII")
    assert len(w.got) == 1  # not yet
    w.read(f"{FUN} VIII")  # held 3 s (> MAP_CONFIRM_S)
    assert w.names() == [f"Victoria Road : {FUN} VII", f"Victoria Road : {FUN} VIII"]
    assert w.got[1][1] == (f"mob{_id(FUN + ' VIII')}",)


def test_reads_closer_than_the_hold_need_three(monkeypatch):
    from mctracker import capture

    w = _Worker(monkeypatch)
    w.w._resolve_map("Victoria Road", f"{FUN} VII", False, 0.0)
    for i, t in enumerate((10.0, 10.2, 10.4)):
        w.w._resolve_map("Victoria Road", f"{FUN} VIII", False, t)
        assert len(w.got) == (2 if i == 2 else 1)
    assert capture.MAP_CONFIRM_READS == 3


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
