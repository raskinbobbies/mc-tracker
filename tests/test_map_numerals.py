"""Roman numeral map suffixes resolve exactly (#26): fuzzy matching never turns
"VII" into "VI", and a read that may be cut short doesn't commit to a shorter map."""

from pathlib import Path

import pytest

from mctracker import gamedata as g

_NAMES = {
    "Victoria Road": ["The Tree Tunnel At the Forest Up North"]
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
    assert g.match_map("Victoria Road", "zzzz qqqq") == (None, False)


@pytest.mark.parametrize("read, want", [
    ("VI", None),  # VI, VII or VIII
    ("VII", None),  # VII or VIII
    ("VIII", "VIII"),  # nothing longer
    ("IX", "IX"),
    ("I", None),  # I, II, III, IV or IX
])
def test_clipped_read_never_commits_to_a_shorter_map(read, want):
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


def test_capture_shows_raw_read_with_no_mobs_when_numeral_is_ambiguous(monkeypatch):
    from mctracker.capture import CaptureWorker

    monkeypatch.setattr(g, "map_mobs", lambda map_id: ("mob",))
    w = CaptureWorker()
    got = []
    w.map_read.connect(lambda name, mobs: got.append((name, tuple(mobs))))
    w._resolve_map("Victoria Road", f"{FUN} VIl")
    w._resolve_map("Victoria Road", f"{FUN} VI", True)  # clipped: VI, VII or VIII
    w._resolve_map("Victoria Road", "zzzz qqqq")  # unreadable: keep the last map
    assert got == [
        (f"Victoria Road : {FUN} VII", ("mob",)),
        (f"Victoria Road : {FUN} VI", ()),
    ]
