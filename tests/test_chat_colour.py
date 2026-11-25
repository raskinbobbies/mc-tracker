"""Chat that isn't a system message is never a party event (issue #19).

Text rules reject every kind of player chat; the row colour filter then requires
the party-system salmon. Colours are literal RGB values measured from the game
(2560x1440): no screenshot or crop is committed.
"""

import numpy as np
import pytest

from mctracker.kills import Message, colour_allows_party, parse_message
from mctracker.session import Tracker
from mctracker.vision import reader as reader_mod
from mctracker.vision.reader import CHAT_COLOURS, GameState, TextLine, chat_row_colour

SALMON, WHITE, YELLOW = CHAT_COLOURS["salmon"], CHAT_COLOURS["white"], CHAT_COLOURS["yellow"]
PANEL = (20, 28, 20)  # dim chat panel (RGB)


def row(rgb, h=27, w=300, bg=PANEL, glyph=0.4, blend=0.6, seed=0):
    """A chat row: text-coloured glyph pixels plus anti-aliased edge pixels blended
    with the dark panel, on ``bg``. Returns BGR like the screen capture."""
    rng = np.random.default_rng(seed)
    img = np.empty((h, w, 3), np.float32)
    img[:] = bg
    m = rng.random((h, w))
    img[m < glyph] = rgb
    edge = (m >= glyph) & (m < glyph + 0.15)
    img[edge] = np.array(rgb) * blend + np.array(bg) * (1 - blend)
    return img[:, :, ::-1].astype(np.uint8).copy()


# --- text rules --------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "[03:20] ChimDacBiet() : XD",  # buddy chat
    "[03:20] ChimDacBiet() : You have created a new party.",
    "[03:20] ChimDacBiet() : 'Bob' has joined the party.",
    "[03:11] Aelle>> gdluck to him!",  # whisper
    "[03:11] Aelle>> 'Bob' has joined the party",
    "[03:11] Aelle>> You have created a new party.",
    "Aelle>> The party has been disbanded.",  # timestamp lost
    "[03:11] Aelle<< 'Bob' has left the party.",
    "[03:12] Knny : You have left the party.",  # normal chat
    "[03:12] Knny : 'Bob' has left the party",
    "[03:12] [Party] Knny : You have created a new party.",  # party chat
    "[03:12] [Notice] You have created a new party.",  # notices
    "[03:12] [Notice] 'Bob' has joined the party.",
    "[03:12] (Notice) The party has been disbanded.",
    "[03:13] [Notice] Please beware of scams offering mesos",
])
def test_non_system_chat_is_not_a_party_event(line):
    assert parse_message(line) is None


@pytest.mark.parametrize("line,expected", [
    ("[03:27] You have created a new party.", ("created", "you")),
    ("[03:27] 'Ahbabo' has joined the party.", ("joined", "Ahbabo")),
    ("[03:27] 'Notice' has joined the party.", ("joined", "Notice")),  # a player named Notice
])
def test_system_party_lines_still_parse(line, expected):
    m = parse_message(line)
    assert m is not None and (m.category, m.item) == expected


# --- colour classifier ---------------------------------------------------------------

@pytest.mark.parametrize("name", list(CHAT_COLOURS))
def test_each_chat_colour_is_classified(name):
    assert chat_row_colour(row(CHAT_COLOURS[name])) == name


def test_colour_tolerates_noise_and_bgra():
    rng = np.random.default_rng(3)
    img = row(SALMON)
    noisy = np.clip(img.astype(int) + rng.integers(-8, 9, img.shape), 0, 255).astype(np.uint8)
    assert chat_row_colour(noisy) == "salmon"
    bgra = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    assert chat_row_colour(bgra) == "salmon"


def test_colour_survives_a_bright_map_behind_the_text():
    bg = (200, 190, 120)  # sunny map pixels, near no chat colour
    assert chat_row_colour(row(SALMON, bg=bg, glyph=0.2)) == "salmon"


def test_dimmed_text_keeps_its_hue():
    assert chat_row_colour(row(tuple(int(c * 0.6) for c in SALMON))) == "salmon"  # faded chat


stray = np.full((27, 300, 3), (20, 28, 20), np.uint8)
stray[5:8, 5:8] = SALMON[::-1]  # 9 text pixels: too few to call a colour


@pytest.mark.parametrize("img", [
    np.zeros((0, 0, 3), np.uint8),  # nothing
    np.full((27, 300, 3), 30, np.uint8),  # no text
    stray,  # a few stray pixels
    row((120, 255, 120)),  # no known class
])
def test_unclear_colour_is_none(img):
    assert chat_row_colour(img) is None


def test_colour_gate():
    assert colour_allows_party("salmon") and colour_allows_party(None)
    assert not any(colour_allows_party(c) for c in CHAT_COLOURS if c != "salmon")


@pytest.mark.parametrize("colour", [c for c in CHAT_COLOURS if c != "salmon"])
def test_party_text_in_another_colour_is_rejected(colour):
    assert parse_message("[03:27] You have created a new party.", colour) is None
    assert parse_message("[03:27] 'Ahbabo' has joined the party.", colour) is None


def test_salmon_or_unknown_colour_follows_the_text_rules():
    for colour in ("salmon", None):
        assert parse_message("[03:27] You have created a new party.", colour) == Message("party", 0, "you", "created")
        assert parse_message("[03:27] Bob : You have created a new party.", colour) is None


# --- reader and session ---------------------------------------------------------------

class _ChatReader(reader_mod.Reader):
    def __init__(self, texts):
        self.ocr = None
        self._chat_sig = None
        self._texts = iter(texts)

    def _line(self, img):
        return next(self._texts)


def test_read_chat_attaches_each_rows_colour():
    h, w = 1125, 2000
    frame = np.zeros((h, w, 3), np.uint8)
    x0, y0, x1, y1 = (int(v) for v in (reader_mod.REGIONS["chat"][0] * w, reader_mod.REGIONS["chat"][1] * h,
                                       reader_mod.REGIONS["chat"][2] * w, reader_mod.REGIONS["chat"][3] * h))
    pitch = (y1 - y0) / reader_mod.CHAT_LINES
    colours = ["white", "salmon", "yellow"]
    for i, c in enumerate(colours):
        r = row(CHAT_COLOURS[c], h=round((i + 1) * pitch) - round(i * pitch), w=x1 - x0, seed=i)
        frame[y0 + round(i * pitch):y0 + round((i + 1) * pitch), x0:x1] = r
    state = GameState()
    assert _ChatReader(["a : x", "You have created a new party.", "[Notice] x"]).read_chat(frame, state)
    assert [ln.colour for ln in state.chat] == colours


def _line(text, y, colour):
    return TextLine(text, y, 1.0, 0.0, 300.0, 27.0, colour)


def test_session_ignores_party_text_in_non_salmon_rows(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_chat_lines([_line("[03:27] You have created a new party.", 13, "white")], now=1)
    tr.on_chat_lines([_line("[03:28] 'Ahbabo' has joined the party.", 40, "orange")], now=2)
    assert tr.party_size == 1
    tr.on_chat_lines([_line("[03:29] 'Bob' has joined the party.", 67, "green")], now=3)
    assert tr.party_size == 1


def test_session_counts_salmon_party_rows_and_unknown_colour_falls_back(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_chat_lines([_line("[03:27] 'Ahbabo' has joined the party.", 13, "salmon")], now=1)
    assert tr.party_size == 2
    tr.on_chat_lines([_line("[03:27] 'Ahbabo' has joined the party.", 13, "salmon"),
                      _line("[03:28] 'Bob' has joined the party.", 40, None)], now=2)
    assert tr.party_size == 3  # colour not readable: the text rules decide
