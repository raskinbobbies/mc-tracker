"""Read game state from a frame of the MapleStory Classic window.

Regions are fractions of the client area, calibrated on 2560x1440 fullscreen
with UI Size = Ideal Ratio. Other resolutions/aspect ratios are unverified.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from ..kills import parse_message

# (x0, y0, x1, y1) as fractions of width/height
REGIONS = {
    "level": (0.190, 0.956, 0.250, 0.996),
    "status_exp": (0.530, 0.951, 0.630, 0.978),
    # Minimap title: map icon, then street (top line) and map name. The panel's
    # width depends on the map (0.118-0.197 of the frame measured; 0.197 is
    # "Tree Dungeon, Forest Up North VII"), so ``minimap_span`` finds its blue
    # title fill and these x bounds are only the fallback: right of the icon
    # (which ends at 0.039) and out to the widest panel seen.
    "minimap_street": (0.041, 0.043, 0.200, 0.066),
    "minimap_name": (0.041, 0.066, 0.200, 0.089),
    "messages": (0.780, 0.530, 1.000, 0.800),
    # Party HP widget: one row per member ("* name  [HP bar]"), stacking down,
    # room for a full party of 6. It's a movable window: this is only where it
    # starts (top right); ``find_party_widget`` finds it anywhere.
    "party": (0.840, 0.235, 0.998, 0.430),
    # Chat box, bottom left: party join/leave/create messages show only here.
    # Only its newest 3 lines (of 5), left 60% (a party line is short), each
    # read at its fixed row without text detection: 150-230 ms a read instead
    # of ~1 s for the whole box with detection. Checked on 2000x1125 (16:9)
    # screenshots of the default chat box position: lines 25 px apart.
    "chat": (0.190, 0.839, 0.451, 0.906),
}
CHAT_LINES = 3  # rows in REGIONS["chat"]
# Messages fast read: only the newest rows, each at its fixed row without text
# detection. New messages come in at the bottom, at a fixed slot, one row
# apart. As fractions of the frame height (checked on 2000x1125 screenshots:
# newest row centred 284 px into the region, rows 22 px apart); the full
# detection read (``read_messages``) re-measures both when it can.
MSG_ROWS = 6
MSG_BOTTOM = 0.7824  # centre of the newest row
MSG_PITCH = 0.01964
MSG_TEXT = 200  # message text is bright; the area behind it is the game scene
PARTY_SEARCH_EVERY_S = 9.0  # widget lost: search the whole frame at most this often
PARTY_SEARCH_SLOW_S = 30.0  # ... and this often once it has missed a few times (playing solo)
PARTY_SEARCH_MISSES = 3
UPSCALE = 2.0


@dataclass
class TextLine:
    text: str
    y: float  # center, in region pixels (pre-upscale)
    score: float
    x0: float = 0.0
    x1: float = 0.0
    height: float = 0.0
    colour: str | None = None  # text colour class (chat: ``chat_row_colour``, messages:
    #                            ``msg_row_colour``); None = unknown


# Chat text colours (RGB) measured from 2560x1440 screenshots; the game draws each
# kind of line in one flat colour, so the classes are far apart.
CHAT_COLOURS = {
    "salmon": (255, 175, 175),  # system messages, incl. party events
    "white": (255, 255, 255),  # normal chat
    "orange": (255, 153, 0),  # buddy chat
    "purple": (255, 153, 204),  # party chat (pink-purple)
    "green": (0, 255, 0),  # whispers
    "yellow": (255, 255, 0),  # notices
}
CHAT_CORE_MIN = 200  # only bright core pixels classify; dimmer ones are blended edges
CHAT_CHROMA_MIN = 40  # max-min of a coloured pixel; below it the pixel is grey/white
CHAT_WHITE_MIN = 220  # lowest channel of a white pixel (grey scenery, frames: not white)
CHAT_COLOUR_TOL = 30.0  # max distance (RGB, brightness-normalised) to a class
CHAT_COLOUR_MARGIN = 8.0  # a pixel must be this much nearer its class than the next one
CHAT_COLOUR_MIN_PX = 25  # voting pixels needed to call a colour
CHAT_WIN_SHARE = 0.6  # the winner needs this share of the votes ...
CHAT_WIN_RATIO = 2.0  # ... and this multiple of the runner-up's
CHAT_PROTECT_SHARE = 0.25  # a protected class with this share of the votes is never ruled out


def classify_row_colour(row: np.ndarray, palette: dict[str, tuple[int, int, int]],
                        protect: str | None = None) -> str | None:
    """Text colour class of one row of text (BGR/BGRA pixels), or None if unclear.

    Palette-driven so other strips can reuse it. Only bright core pixels
    (``CHAT_CORE_MIN``) vote, so anti-aliased edges, dim map scenery and the frame
    don't. A saturated pixel (scaled to full brightness) votes for the nearest
    non-grey palette class within ``CHAT_COLOUR_TOL`` and clearly nearer it than the next
    class (``CHAT_COLOUR_MARGIN``: a pixel between salmon and purple abstains); a near-white pixel votes
    for the grey/white class. The winner needs ``CHAT_WIN_SHARE`` of the votes and
    ``CHAT_WIN_RATIO`` times the runner-up, else None: close classes (salmon /
    pink-purple) are left to the caller's fallback. The ``protect`` class is never
    ruled out: if it got ``CHAT_PROTECT_SHARE`` of the votes but didn't win, None."""
    if row.size == 0:
        return None
    px = row[:, :, 2::-1].reshape(-1, 3).astype(np.float32)  # BGR(A) -> RGB
    px = px[px.max(axis=1) >= CHAT_CORE_MIN]
    if len(px) < CHAT_COLOUR_MIN_PX:
        return None
    names = list(palette)
    ref = np.array([palette[n] for n in names], dtype=np.float32)
    chromatic = (ref.max(axis=1) - ref.min(axis=1)) >= CHAT_CHROMA_MIN
    votes = np.zeros(len(names), dtype=np.int64)
    sat = (px.max(axis=1) - px.min(axis=1)) >= CHAT_CHROMA_MIN
    if sat.any() and chromatic.any():
        c = px[sat]
        c = c * (255.0 / c.max(axis=1, keepdims=True))
        d = np.linalg.norm(c[:, None, :] - ref[None], axis=2)
        d[:, ~chromatic] = np.inf
        two = np.sort(d, axis=1)[:, :2]
        near = (two[:, 0] <= CHAT_COLOUR_TOL) & (two[:, 1] - two[:, 0] >= CHAT_COLOUR_MARGIN)
        votes += np.bincount(d.argmin(axis=1)[near], minlength=len(names))
    white = ~sat & (px.min(axis=1) >= CHAT_WHITE_MIN)
    if white.any() and (~chromatic).any():
        votes[int(np.flatnonzero(~chromatic)[0])] += int(white.sum())
    total = int(votes.sum())
    if total < CHAT_COLOUR_MIN_PX:
        return None
    order = np.argsort(votes)[::-1]
    win, runner = int(votes[order[0]]), int(votes[order[1]]) if len(order) > 1 else 0
    if win < CHAT_WIN_SHARE * total or win < CHAT_WIN_RATIO * runner:
        return None
    if protect in palette and names[order[0]] != protect and votes[names.index(protect)] >= CHAT_PROTECT_SHARE * total:
        return None
    return names[order[0]]


# Message strip (#30): "You received EXP" is white for your own kill, yellow for a party
# share. Sampled from the party recording (2026-11-25, 1080p60): white reads ~(250,248,244),
# yellow ~(238,246,100) (video chroma pulls a game yellow's blue up), both dimming as they
# fade. Pixels are scored by shape, not by distance to a reference, so a fade keeps its class:
# blue / max (white ~1, yellow under 0.5) and |red - green| / max (both ~0; the tree trunk
# and other scenery behind the strip are far off).
MSG_COLOUR_MIN_V = 160  # the row's brightest text (99.5th percentile): dimmer = faded, unknown
MSG_COLOUR_CORE = 0.85  # pixels this close to that brightest are the text's core
MSG_COLOUR_MIN_PX = 20  # core pixels needed
MSG_WHITE_B = 0.8  # blue / max at or above it: white
MSG_YELLOW_B = 0.6  # blue / max at or below it: yellow
MSG_RG_MAX = 0.1  # |red - green| / max above it: neither (orange scenery, salmon...)
MSG_COLOUR_WIN = 0.7  # the winner needs this share of the core pixels ...
MSG_COLOUR_OTHER = 0.1  # ... and the other class at most this share


def msg_row_colour(row: np.ndarray) -> str | None:
    """"white" or "yellow" for one message-strip row (BGR/BGRA pixels), or None if unclear
    (faded, scenery behind it, too few pixels, mixed). None counts as neither (#30)."""
    if row.size == 0:
        return None
    px = row[:, :, 2::-1].reshape(-1, 3).astype(np.float32)
    mx = px.max(axis=1)
    top = float(np.percentile(mx, 99.5))
    if top < MSG_COLOUR_MIN_V:
        return None
    core = px[mx >= MSG_COLOUR_CORE * top]
    if len(core) < MSG_COLOUR_MIN_PX:
        return None
    m = core.max(axis=1)
    b = core[:, 2] / m
    flat = np.abs(core[:, 0] - core[:, 1]) / m <= MSG_RG_MAX
    n = len(core)
    w = int((flat & (b >= MSG_WHITE_B)).sum())
    y = int((flat & (b <= MSG_YELLOW_B)).sum())
    if w >= MSG_COLOUR_WIN * n and y <= MSG_COLOUR_OTHER * n:
        return "white"
    if y >= MSG_COLOUR_WIN * n and w <= MSG_COLOUR_OTHER * n:
        return "yellow"
    return None


def chat_row_colour(row: np.ndarray) -> str | None:
    """Colour class of one chat row (``CHAT_COLOURS``), or None if unclear."""
    return classify_row_colour(row, CHAT_COLOURS, protect="salmon")


@dataclass
class GameState:
    level: int | None = None
    exp: int | None = None
    exp_percent: float | None = None
    street: str | None = None
    map_name: str | None = None
    map_clipped: bool = False  # the map name's ink runs into the crop's right edge
    map_repaired: bool = False  # the numeral was set from bars counted in the pixels: never an exact read
    # From the panel alone: "ok"; "missing": no minimap title panel at the top
    # left (moved, closed, another skin, or not the game at all); "names_hidden":
    # the panel is there but its name line reads blank. CaptureWorker refines it
    # ("unknown" off the playfield, "ok" if the fallback read still matched a map).
    minimap_status: str | None = None
    messages: list[TextLine] = field(default_factory=list)
    chat: list[TextLine] = field(default_factory=list)
    party: int = 0  # party members (HP-bar rows); 0 = no party widget


_STATUS_RE = re.compile(r"([\d][\d,.\s]*)\s*\[\s*([\d.,]+)\s*%")


def parse_status_exp(text: str) -> tuple[int, float] | None:
    """'EXP.66,700[14.24%]' -> (66700, 14.24). OCR may read ',' as '.'."""
    t = text.replace("EXP", "", 1)
    m = _STATUS_RE.search(t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    pct = m.group(2).replace(",", ".")
    try:
        return int(digits), float(pct)
    except ValueError:
        return None


def parse_level(texts: list[str]) -> int | None:
    for t in texts:
        d = re.sub(r"\D", "", t)
        if d and 1 <= int(d) <= 300:
            return int(d)
    return None


def crop(frame: np.ndarray, region: str) -> np.ndarray:
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = REGIONS[region]
    return frame[int(y0 * h): int(y1 * h), int(x0 * w): int(x1 * w)]


# The minimap's title panel is a flat blue (RGB 153,187,204) in every map; the
# icon, text and frame are not. Searched over the title rows, right of the frame,
# out past the widest panel seen (0.197) so a wider one isn't cut short.
_MINIMAP_FILL_BGR = np.array([204, 187, 153], dtype=np.int16)
_MINIMAP_SEARCH = (0.030, 0.300, 0.047, 0.086)  # x0, x1, y0, y1 (fractions)
_FILL_TOL = 30  # RGB-sum distance that still counts as fill


def _fill_mask(img: np.ndarray) -> np.ndarray:
    return np.abs(img[:, :, :3].astype(np.int16) - _MINIMAP_FILL_BGR).sum(axis=2) <= _FILL_TOL


def minimap_span(frame: np.ndarray) -> tuple[float, float] | None:
    """(x0, x1) as fractions of the width of the minimap title text area: from
    the panel fill right of the map icon to the panel's right border. ``None`` if
    no panel is found (other skin or resolution): use the static regions.

    The band covers both title lines and the margins around them, so every column
    of text keeps some fill (>= 25% on every frame measured). The panel's right
    border is a vertical frame line with none: the first such column ends the
    panel, even if a fill-coloured sky continues past it (#18), and the crop runs
    to it rather than to the last fill column, so a final glyph close to the
    border is never cut off (#26).
    """
    h, w = frame.shape[:2]
    sx0, sx1, sy0, sy1 = _MINIMAP_SEARCH
    a, b = int(sx0 * w), int(sx1 * w)
    band = frame[int(sy0 * h): int(sy1 * h), a:b]
    if band.size == 0:
        return None
    frac = _fill_mask(band).mean(axis=0)
    ok = frac >= 0.25  # text and its outline cover part of a column
    dead = frac < 0.05  # a frame line (or scene): no fill at all
    cols = np.flatnonzero(ok)
    if cols.size == 0:
        return None
    gap = max(3, round(0.004 * w))  # glyph gaps are fill; a taller stroke may not be
    start = end = int(cols[0])
    stop = None  # the border column, if found
    for c in range(start + 1, frac.size):
        if dead[c]:
            stop = c
            break
        if ok[c]:
            if c - end > gap:
                break
            end = c
    if end - start < 0.05 * w:  # a stray patch, not a panel
        return None
    x1 = stop if stop is not None and stop - end <= gap + 1 else end + 1
    return (a + start) / w, (a + x1) / w


def _ink_at_right_edge(line: np.ndarray, w: int) -> bool:
    """True if text (anything not fill) reaches the last columns of an unpadded
    title-line crop: the read may be missing its last glyph (a numeral's "I")."""
    k = max(2, round(0.001 * w))
    edge = line[:, -k:]
    if edge.size == 0:
        return False
    return bool((~_fill_mask(edge)).mean(axis=0).max() > 0.15)


_TITLE_INK = 215  # title text is white: every channel at least this
_TITLE_BAND = (0.030, 0.100)  # rows searched for the two title lines (fractions of the height)
_TITLE_SHIFT = 0.015  # a found line may sit this far (of the height) from its static row
_TITLE_SNAP = 0.002  # ... and closer than this it is read at the static row (as measured before)


def _title_rows(frame: np.ndarray, span: tuple[float, float]) -> list[tuple[int, int]] | None:
    """[(y0, y1)] pixel rows for the street and name crops, centred on the two
    title lines found in the panel, or None (use the static rows).

    The title doesn't always sit at the same height: on some frames it is ~10 px
    (at 1440) higher, and the static name crop cut the tops off its glyphs, so
    "II" read as "in" or '"' (or a single "I")."""
    h, w = frame.shape[:2]
    a, b = int(span[0] * w), int(span[1] * w)
    top = int(_TITLE_BAND[0] * h)
    band = frame[top: int(_TITLE_BAND[1] * h), a:b, :3]
    if band.size == 0:
        return None
    frac = (band.min(axis=2) >= _TITLE_INK).mean(axis=1)
    text = (frac > 0.02) & (frac <= 0.7)  # a solid white row is the header bar or a border
    lines = [(top + y0, top + y1) for y0, y1 in _runs(text, max(3, round(0.006 * h)))]
    if len(lines) < 2:
        return None
    out, shifted = [], False
    for region, (y0, y1) in zip(("minimap_street", "minimap_name"), lines[:2]):
        _, r0, _, r1 = REGIONS[region]
        mid, half = (y0 + y1) / 2, (r1 - r0) * h / 2
        off = abs(mid - (r0 + r1) * h / 2)
        if off > _TITLE_SHIFT * h:
            return None
        shifted |= off > _TITLE_SNAP * h
        out.append((max(0, round(mid - half)), round(mid + half)))
    return out if shifted else None


def numeral_strokes(line: np.ndarray, frame_w: int = 2560) -> int | None:
    """How many thin full-height bars (1-3: I, II, III) make up the last word of a
    title-line crop, or None if that word isn't only bars.

    The recognizer often misreads the font's thin "I" strokes ("in", "ll", '"',
    a single "I" for "II"); the bars themselves are easy to count."""
    if line.size == 0:
        return None
    scale = frame_w / 2560  # measured at 2560 wide: strokes ~4 px wide, 2 px apart; words 8 apart
    ink = line[:, :, :3].min(axis=2) >= _TITLE_INK
    rows = ink.any(axis=1)
    if not rows.any():
        return None
    cols = _runs(ink.any(axis=0), 1)
    word_gap = max(3, round(5 * scale))
    word = [cols[-1]]
    for c in reversed(cols[:-1]):
        if word[0][0] - c[1] >= word_gap:
            break
        word.insert(0, c)
    if len(word) == len(cols) or not 1 <= len(word) <= 3:
        return None
    full = max(int(ink[:, x0:x1].any(axis=1).sum()) for x0, x1 in cols)  # the tallest glyph's height
    for x0, x1 in word:
        bar = ink[:, x0:x1]
        if x1 - x0 > max(2, round(6 * scale)) or bar.any(axis=1).sum() < 0.85 * full:
            return None
    return len(word)


def minimap_title(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool, bool]:
    """(street line, map name line, name clipped, panel found): the crops are
    trimmed to the minimap panel, or the static regions if it wasn't found.
    ``clipped`` says the name's ink touches the crop's right edge, so its last
    glyph may be cut; it is always False on the static fallback, whose right
    edge is scene, not panel."""
    h, w = frame.shape[:2]
    span = minimap_span(frame)
    rows = _title_rows(frame, span) if span is not None else None
    out = []
    clipped = False
    for i, region in enumerate(("minimap_street", "minimap_name")):
        _, y0, _, y1 = REGIONS[region]
        if span is None:
            out.append(crop(frame, region))
        else:
            r0, r1 = rows[i] if rows else (int(y0 * h), int(y1 * h))
            c = frame[r0:r1, int(span[0] * w): int(span[1] * w), :3]
            if region == "minimap_name":
                clipped = _ink_at_right_edge(c, w)
            # Pad with fill, not the border or scene, so glyphs aren't flush with the
            # crop edges (the recognizer drops or merges them there: 'III' -> 'II').
            pad = np.broadcast_to(_MINIMAP_FILL_BGR.astype(np.uint8),
                                  (c.shape[0], max(1, round(0.006 * w)), 3))
            out.append(np.concatenate((pad, c, pad), axis=1))
    return out[0], out[1], clipped, span is not None


def minimap_crops(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(street line, map name line) crops, trimmed to the minimap panel."""
    street, name, _, _ = minimap_title(frame)
    return street, name


def _runs(row: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """[(x0, x1)] of True runs at least ``min_len`` long (x1 exclusive)."""
    padded = np.concatenate(([0], row.astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= min_len]


def _masks(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(white, dark) pixel masks: HP bar frames are white inside a black outline.
    The 1 px white edge next to the red fill comes out pink-tinted in a video
    capture (chroma subsampling bleeds the red into it), e.g. BGR (227, 224,
    255) or (214, 214, 246): a strict white broke the edge and lost the bar
    (#29). Still pale on every channel, unlike damage numbers or the fill."""
    b, g, r = cv2.split(np.ascontiguousarray(img[:, :, :3]))
    lo, hi = cv2.min(cv2.min(b, g), r), cv2.max(cv2.max(b, g), r)
    return (lo >= 205) & (cv2.subtract(hi, lo) <= 45), hi <= 40


def _frame_boxes(img: np.ndarray, row_h: float, min_len: int, max_len: int | None = None,
                 x_from: int = 0, masks: tuple[np.ndarray, np.ndarray] | None = None,
                 outline_req: float = 0.7) -> list[tuple[int, int, int, int]]:
    """HP bar frames, whatever the HP -> [(y0, y1, x0, x1)]: each bar sits in a
    white-bordered rectangle (inside a black outline) over a translucent grey
    track that the red fill covers from the left. Find rectangles whose top
    and bottom white edges span the same columns and whose left and right
    sides are white too.
    """
    white, dark = _masks(img) if masks is None else masks
    edges = []  # (y, x0, x1) of long white runs
    for y in np.flatnonzero(white[:, x_from:].sum(axis=1) >= min_len):  # rows that can hold one
        for a, b in _runs(white[y, x_from:], min_len):
            if max_len is None or b - a <= max_len:
                edges.append((int(y), a + x_from, b + x_from))
    found = []
    for i, (yt, a, b) in enumerate(edges):
        for yb, a2, b2 in edges[i + 1:]:
            if yb - yt > 0.7 * row_h:
                break
            if yb - yt < 0.2 * row_h or abs(a - a2) > 3 or abs(b - b2) > 3:
                continue
            inner = slice(yt + 1, yb)
            left = white[inner, max(0, a - 3):a + 3].any(axis=1).mean()
            right = white[inner, b - 3:b + 3].any(axis=1).mean()
            # Not a white box (e.g. a window title bar): the inside is fill or
            # track, and a black outline runs just above the white edge.
            body = white[yt + 2:yb - 1, a + 3:b - 3].mean() if yb - yt > 4 else 1.0
            outline = max((dark[y, a:b].mean() for y in range(max(0, yt - 2), yt)), default=0.0)
            if left >= 0.7 and right >= 0.7 and body < 0.5 and outline >= outline_req:
                found.append((yt, yb + 1, a, b))
                break
    return found


def _bar_frames(img: np.ndarray) -> list[tuple[int, int]]:
    """HP bar frame rows in a party-widget crop."""
    h, w = img.shape[:2]
    boxes = _frame_boxes(img, h / 6.5, int(0.15 * w), x_from=int(0.4 * w))
    return _merge_rows([(y0, y1) for y0, y1, _, _ in boxes])


# The widget's layout at 2000x1125 (tests/test_party_widget.py): 316x219 px,
# HP bars 103 px wide at x=191, the first 27 px from the top, 27 px apart.
_WIDGET = dict(w=316, h=219, bar_x=191, bar_top=27, bar_w=103, pitch=27)


def _pool2(mask: np.ndarray) -> np.ndarray:
    """Half-resolution mask, a pixel set if any of its 2x2 block is: thin
    1-2 px frame edges survive (plain subsampling can drop them)."""
    h, w = (mask.shape[0] // 2) * 2, (mask.shape[1] // 2) * 2
    return mask[:h, :w].reshape(h // 2, 2, w // 2, 2).any(axis=(1, 3))


def _bar_boxes(frame: np.ndarray, L: dict) -> list[tuple[int, int, int, int]]:
    """HP-bar-sized frames anywhere in the frame. A coarse pass on half-size
    masks finds candidates (most of the cost of a full-res pass is scanning
    speckled play field); each is then confirmed at full resolution."""
    row_h, lo, hi = L["h"] / 6.5, int(0.7 * L["bar_w"]), int(1.3 * L["bar_w"]) + 4
    white, dark = _masks(frame)
    coarse = _frame_boxes(None, row_h / 2, lo // 2, hi // 2 + 2, masks=(_pool2(white), _pool2(dark)),
                          outline_req=0.0)  # pooling smears the 1 px outline into the edge: check it below
    h, w = white.shape
    out = set()
    for y0, y1, a, b in coarse:
        ys, xs = slice(max(0, 2 * y0 - 6), min(h, 2 * y1 + 6)), slice(max(0, 2 * a - 6), min(w, 2 * b + 6))
        for fy0, fy1, fa, fb in _frame_boxes(None, row_h, lo, hi, masks=(white[ys, xs], dark[ys, xs])):
            out.add((fy0 + ys.start, fy1 + ys.start, fa + xs.start, fb + xs.start))
    return sorted(out)


def find_party_widget(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Search the whole frame for the party widget (a movable window) by its
    column of HP-bar frames -> its box (x0, y0, x1, y1) in pixels, laid out
    like REGIONS['party'] so ``hp_bar_rows`` reads it. The column with the most
    bars wins; ties go to the one nearest the widget's default place."""
    h, w = frame.shape[:2]
    s = h / 1125
    L = {k: v * s for k, v in _WIDGET.items()}
    cols: list[list[tuple[int, int, int, int]]] = []
    for box in _bar_boxes(frame, L):
        y0, _, a, b = box
        col = next((c for c in cols if abs(c[-1][2] - a) <= 3 and abs(c[-1][3] - b) <= 3
                    and y0 - c[-1][0] <= 1.4 * L["pitch"]), None)
        if col is None:
            cols.append([box])
        elif y0 - col[-1][0] >= 0.6 * L["pitch"]:  # the next member, not the same frame again
            col.append(box)
    if not cols:
        return None
    dx, dy = REGIONS["party"][0] * w, REGIONS["party"][1] * h
    x_icon = (round(-170 * s), round(-140 * s))  # icon columns, from the bar's left edge
    best = max(cols, key=lambda c: (_icons(frame, c, x_icon) > 0, len(c),
                                    -abs(c[0][2] - L["bar_x"] - dx) - abs(c[0][0] - L["bar_top"] - dy)))
    x0 = max(0, round(best[0][2] - L["bar_x"]))
    y0 = max(0, round(best[0][0] - L["bar_top"]))
    return x0, y0, min(w, x0 + round(L["w"])), min(h, y0 + round(L["h"]))


def _icons(frame: np.ndarray, col, x_icon: tuple[int, int]) -> int:
    """How many of a column's rows have the leader star / member sun icon
    (orange) left of the name."""
    n = 0
    for y0, y1, a, _ in col:
        p = frame[y0:y1, max(0, a + x_icon[0]):max(0, a + x_icon[1]), :3].astype(np.int16)
        n += bool(((p[..., 2] > 200) & (p[..., 1] > 120) & (p[..., 0] < 100)).any())
    return n


def _merge_rows(rows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for y0, y1 in sorted(rows):
        if out and y0 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], y1))
        else:
            out.append((y0, y1))
    return out


def hp_bar_rows(img: np.ndarray) -> list[tuple[int, int]]:
    """Find party members' HP bars in the party region -> their (y0, y1) rows,
    top to bottom. A row is found by its bar's frame (white border in a black
    outline), so members at low HP or dead (little or no red) still count.
    Red alone never counts: damage numbers and skill effects over open play
    field are red too (a solo player read as a party)."""
    if img.size == 0:
        return []
    return _bar_frames(img)


class Reader:
    def __init__(self):
        from rapidocr import RapidOCR  # heavy import; load once

        self.ocr = RapidOCR()
        self._party_box: tuple[int, int, int, int] | None = None  # where the widget was last found
        self._party_search_at = 0.0
        self._party_misses = 0  # full-frame searches in a row that found nothing
        self._party_rows = 0  # rows in the last read that had any
        self._chat_sig: bytes | None = None
        self._msg_rows: tuple[float, float] | None = None  # measured (bottom, pitch), region pixels
        self._msg_cache: dict[bytes, str] = {}  # row text pixels -> text
        self._msg_misses = 0  # fast reads in a row with no message in them

    @staticmethod
    def _prep(img: np.ndarray) -> np.ndarray:
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return cv2.resize(img, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)

    def _line(self, img: np.ndarray) -> str:
        """Recognize a single fixed-position text line (no detection: ~20 ms)."""
        if img.size == 0:
            return ""
        r = self.ocr(self._prep(img), use_det=False, use_cls=False, use_rec=True)
        return r.txts[0] if r.txts else ""

    def _line_cached(self, region: str, img: np.ndarray) -> str:
        """``_line``, but OCR only when the crop's pixels changed since the last read of ``region``."""
        cache = self.__dict__.setdefault("_line_cache", {})  # region -> (pixel signature, text)
        sig = (img.shape, img.tobytes())
        hit = cache.get(region)
        if hit is not None and hit[0] == sig:
            return hit[1]
        text = self._line(img)
        cache[region] = (sig, text)
        return text

    def _lines(self, img: np.ndarray, upscale: float = UPSCALE) -> list[TextLine]:
        """Detect + recognize a variable number of lines."""
        if img.size == 0:
            return []
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if upscale != 1:
            img = cv2.resize(img, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
        r = self.ocr(img, use_det=True, use_cls=False, use_rec=True)
        if not r.txts:
            return []
        out = []
        for box, txt, score in zip(r.boxes, r.txts, r.scores):
            xs = [p[0] / upscale for p in box]
            ys = [p[1] / upscale for p in box]
            out.append(TextLine(txt, (min(ys) + max(ys)) / 2, float(score),
                                min(xs), max(xs), max(ys) - min(ys)))
        out.sort(key=lambda l: l.y)  # top (oldest) -> bottom (newest)
        return out

    def read_status(self, frame: np.ndarray, state: GameState) -> None:
        state.level = parse_level([self._line_cached("level", crop(frame, "level"))])
        if p := parse_status_exp(self._line_cached("status_exp", crop(frame, "status_exp"))):
            state.exp, state.exp_percent = p

    def read_map(self, frame: np.ndarray, state: GameState) -> None:
        street, name, state.map_clipped, found = minimap_title(frame)
        # Read several times a second: OCR only when the title's pixels change.
        sig = (street.shape, name.shape, street.tobytes(), name.tobytes())
        cached = getattr(self, "_map_cache", None)
        if cached is not None and cached[0] == sig:
            state.street, state.map_name, state.map_repaired = cached[1]
        else:
            state.street = self._line(street) or None
            state.map_name = self._line(name) or None
            # A clipped name may be missing bars ("III" cut to "II"): its own handling in match_map stands.
            if state.map_name and not state.map_clipped and (n := numeral_strokes(name, frame.shape[1])):
                from ..gamedata import with_strokes

                fixed = with_strokes(state.map_name, n)
                state.map_repaired = fixed != state.map_name
                state.map_name = fixed
            self._map_cache = (sig, (state.street, state.map_name, state.map_repaired))
        if not found:
            state.minimap_status = "missing"
        elif not (state.map_name or "").strip():
            state.minimap_status = "names_hidden"
        else:
            state.minimap_status = "ok"

    def _msg_layout(self, frame: np.ndarray) -> tuple[float, float]:
        """(newest row's centre, row pitch) in messages-region pixels."""
        if self._msg_rows:
            return self._msg_rows
        h = frame.shape[0]
        return MSG_BOTTOM * h - REGIONS["messages"][1] * h, MSG_PITCH * h

    def read_messages(self, frame: np.ndarray, state: GameState) -> None:
        """Full read (text detection, ~350 ms): the newest ``MSG_ROWS`` rows,
        so it sees what ``read_messages_fast`` sees. Re-measures the rows:
        near where they were, or anywhere once 2+ fast reads in a row found
        no message while this read finds some (the rows moved)."""
        lines = self._lines(crop(frame, "messages"))
        bottom, pitch = self._msg_layout(frame)
        gaps = sorted(b.y - a.y for a, b in zip(lines, lines[1:]))
        if gaps and 0.8 * pitch <= (g := gaps[len(gaps) // 2]) <= 1.2 * pitch:
            pitch = g
        newest = next((ln for ln in reversed(lines) if parse_message(ln.text)), None)
        if newest and (abs(newest.y - bottom) <= 0.5 * pitch or self._msg_misses >= 2):
            bottom = float(newest.y)
            self._msg_rows = bottom, float(pitch)
            self._msg_misses = 0
        state.messages = [ln for ln in lines if ln.y >= bottom - (MSG_ROWS - 0.5) * pitch]
        img = crop(frame, "messages")
        for ln in state.messages:
            y0, y1 = round(ln.y - ln.height / 2), round(ln.y + ln.height / 2)
            ln.colour = msg_row_colour(img[max(0, y0):y1, max(0, int(ln.x0)):int(ln.x1) + 1])

    def read_messages_fast(self, frame: np.ndarray, state: GameState) -> None:
        """The newest ``MSG_ROWS`` rows, each recognized at its fixed row (no
        detection). A row whose text pixels were read before (it scrolled up a
        row) reuses that text, so a read costs ~40 ms per new row."""
        img = crop(frame, "messages")
        bottom, pitch = self._msg_layout(frame)
        out = []
        for i in range(MSG_ROWS - 1, -1, -1):  # top (oldest) -> bottom (newest)
            y = bottom - i * pitch
            row = img[max(0, round(y - pitch / 2)):round(y + pitch / 2)]
            text = row[:, :, :3].max(axis=2) >= MSG_TEXT if row.size else None
            if text is None or not text.any():
                continue
            cols = np.flatnonzero(text.any(axis=0))
            x0, x1 = max(0, int(cols[0]) - 6), min(row.shape[1], int(cols[-1]) + 7)
            sig = repr(text[:, x0:x1].shape).encode() + np.packbits(text[:, x0:x1]).tobytes()
            if (t := self._msg_cache.get(sig)) is None:
                t = self._msg_cache[sig] = self._line(row[:, x0:x1]).strip()
                if len(self._msg_cache) > 256:
                    del self._msg_cache[next(iter(self._msg_cache))]
            if t:
                out.append(TextLine(t, y, 1.0, float(x0), float(x1), pitch, msg_row_colour(row[:, x0:x1])))
        state.messages = out
        self._msg_misses = 0 if any(parse_message(ln.text) for ln in out) else self._msg_misses + 1

    def read_chat(self, frame: np.ndarray, state: GameState) -> bool:
        """OCR the chat strip -> False (``state.chat`` untouched) if its text
        pixels haven't changed since the last read: nothing new to read."""
        img = crop(frame, "chat")
        sig = (img[::2, ::2, :3].max(axis=2) >= 200).tobytes()  # text is bright; the panel is dim
        if sig == self._chat_sig:
            return False
        self._chat_sig = sig
        pitch = img.shape[0] / CHAT_LINES
        rows = [img[round(i * pitch):round((i + 1) * pitch)] for i in range(CHAT_LINES)]
        state.chat = [TextLine(t, (i + 0.5) * pitch, 1.0, 0.0, float(img.shape[1]), pitch,
                               chat_row_colour(row))
                      for i, row in enumerate(rows) if (t := self._line(row).strip())]
        return True

    def _party_crop(self, frame: np.ndarray) -> np.ndarray:
        if self._party_box is None:
            return crop(frame, "party")
        x0, y0, x1, y1 = self._party_box
        return frame[y0:y1, x0:x1]

    def read_party(self, frame: np.ndarray, state: GameState) -> None:
        now = time.monotonic()
        img = self._party_crop(frame)
        bars = hp_bar_rows(img)
        # Search the whole frame when the widget isn't where it was, or has
        # lost rows there (moved partly out of the cached box), now and then.
        lost_rows = self._party_box is not None and 0 < len(bars) < self._party_rows
        if (not bars or lost_rows) and now >= self._party_search_at:
            box = find_party_widget(frame)
            if box is None:
                self._party_misses += 1
                self._party_box = None  # don't keep reading a stale spot of play field
            else:
                self._party_misses = 0
                x0, y0, x1, y1 = box
                if len(found := hp_bar_rows(frame[y0:y1, x0:x1])) >= len(bars):
                    self._party_box, img, bars = box, frame[y0:y1, x0:x1], found
            slow = self._party_misses >= PARTY_SEARCH_MISSES  # solo: back off
            self._party_search_at = now + (PARTY_SEARCH_SLOW_S if slow else PARTY_SEARCH_EVERY_S)
        self._party_rows = len(bars)
        # Rows only: who is in the party isn't used, and OCR of the names cost
        # ~180 ms a read (786 ms the first time).
        state.party = len(bars)

    def read_all(self, frame: np.ndarray) -> GameState:
        s = GameState()
        self.read_status(frame, s)
        self.read_map(frame, s)
        self.read_messages(frame, s)
        self.read_party(frame, s)
        return s
