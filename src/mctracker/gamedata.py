"""Map and monster data from maplestory.io (MapleStory Classic World), cached on disk."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from urllib.parse import quote
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from .assets import API, USER_AGENT
from .paths import sub
from .kills import Mob


def _data_dir() -> Path:
    return sub("data")


def _get_json(path: str, filename: str):
    local = _data_dir() / filename
    if local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    r = httpx.get(API + path, headers={"User-Agent": USER_AGENT.decode()}, timeout=30)
    r.raise_for_status()
    local.write_text(r.text, encoding="utf-8")
    return r.json()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@lru_cache(maxsize=1)
def maps() -> list[dict]:
    return _get_json("/map", "maps.json")


# A map name's last word, if it is a Roman numeral (I-X in the data). OCR reads
# the font's "I" as "l", "|", "1", "!" or "i" too, and may lowercase "V"/"X", and
# may add a stray quote or dot after it ("VII'").
_NUMERAL_WORD = re.compile(r"^(.*?)[\s,.]+([IVXivxlT|1!]{1,5})[\s.,'`\"’]*$")
# The same numeral glued to the last word with no space ("Primeval ForestT",
# "Forest1", "Foresti": the game's "I" is a thin bar the recognizer joins to the
# letter before it): one to three I-lookalikes at the end of the read.
_GLUED_TAIL = "Ii!l|1TJ"
_TRAILING_MARKS = re.compile(r"[\s.,'`\"’]+$")
_ROMAN = re.compile(r"^(X{0,3})(IX|IV|V?I{0,3})$")


_LOOKALIKE_I = str.maketrans("L|1!T", "IIIII")


def _numeral_of(raw: str) -> str | None:
    """The Roman numeral a raw token reads as (I-lookalikes are I), or None."""
    num = raw.upper().translate(_LOOKALIKE_I)
    return num if _ROMAN.match(num) else None


def split_numeral(name: str) -> tuple[str, str | None]:
    """("Forest Up North", "VII") from "Forest Up North VIl": the name without its
    Roman numeral suffix, and the numeral with I-lookalikes read as I.

    The recognizer also breaks a numeral's thin strokes apart ("Entrance I II",
    "Entrance I I I"): every numeral-like word that ends the name is part of the
    numeral, as long as the whole is still a Roman numeral. A read is never given
    strokes it doesn't have: "Entrance I" stays I."""
    m = _NUMERAL_WORD.match(name.strip())
    if not m:
        return name, None
    base, raw = m.group(1), m.group(2)
    if _numeral_of(raw) is None:
        return name, None
    while (prev := _NUMERAL_WORD.match(base)) and _numeral_of(prev.group(2) + raw):
        base, raw = prev.group(1), prev.group(2) + raw
    return base, _numeral_of(raw)


def exact_read(street: str, name: str, m: dict) -> bool:
    """True if the title read, taken literally, is map ``m``: the street and name
    match exactly once spacing and punctuation are ignored, and the numeral is a
    plain I, V or X word (no lookalike letters, no split strokes). A read that had
    to be fuzzy-matched, glued or repaired is not exact."""
    want_base, want_num = split_numeral(m.get("name", ""))
    mm = _NUMERAL_WORD.match(name.strip())
    if want_num is None:
        base, raw = name, None
    elif mm and mm.group(2) == want_num:
        base, raw = mm.group(1), mm.group(2)
    else:
        return False
    return _norm(street + base) == _norm(f"{m.get('streetName', '')}{want_base}") and raw == want_num


@lru_cache(maxsize=1)
def _map_index() -> tuple[list[str], list[str | None]]:
    """Per map: normalized street + name without numeral, and the numeral."""
    keys, nums = [], []
    for m in maps():
        base, num = split_numeral(m.get("name", ""))
        keys.append(_norm(f"{m.get('streetName', '')}{base}"))
        nums.append(num)
    return keys, nums


def match_map(street: str, name: str, clipped: bool = False,
              min_score: float = 80) -> tuple[dict | None, str | None]:
    """(map, place) for the minimap's two lines (street, map name).

    The street and name are fuzzy-matched without the name's Roman numeral; the
    numeral then has to match exactly, so "VI" is never taken for "VII" (#26).
    A place with no numbered maps takes a stray numeral-like word as OCR noise
    ("Henesys l" is Henesys). ``clipped`` (the name's ink runs into the crop's
    edge) means the numeral may be missing its last glyphs: the exact numeral
    still wins if the place has it, else the one longer numeral it is a prefix
    of, else nothing. ``place`` (normalized street + name without numeral) is
    set whenever the place matched, so a numeral that didn't resolve can be shown
    as the raw read with no mobs; None means the read matched nothing.
    """
    base, num = split_numeral(name)
    keys, nums = _map_index()
    index = dict(enumerate(keys))
    hit = process.extractOne(_norm(street + base), index, scorer=fuzz.ratio, score_cutoff=min_score)
    # "Primeval ForestT" / "Entrance I II": a numeral's thin strokes glued onto the last word,
    # alone or in front of a spaced numeral. Each split is taken only if the read without its
    # tail matches a place at least as well as the read with it (so "Deep Tunnel" stays "Deep
    # Tunnel", not "Deep Tunne" + "l"; a "|" or "!" counts for nothing in the match), the head
    # ends with the place's last letter, the place has the combined numeral, and the
    # best-matching split wins.
    text = _TRAILING_MARKS.sub("", (base if num else name).strip())
    best = None
    for k in (1, 2, 3):
        head, tail = text[:-k], text[-k:]
        if (len(head) < 2 or not head[-1].isalpha() or any(c not in _GLUED_TAIL for c in tail)
                or all(c in _GLUED_TAIL for c in head.split()[-1])):  # "VII" + "I" is a bad numeral, not glued
            continue
        g_hit = process.extractOne(_norm(street + head), index, scorer=fuzz.ratio, score_cutoff=min_score)
        # The place's name must end where the head ends: "Primeval Foresl" is a noisy
        # "Forest", not "Fores" + I (a lookalike letter would invent a numeral).
        if (g_hit and _norm(street + head).endswith(keys[g_hit[2]][-1:])
                and (best is None or g_hit[1] >= best[0][1])):
            best = g_hit, head, "I" * k + (num or "")
    if best and (hit is None or best[0][1] >= hit[1]):
        g_hit, head, g_num = best
        if g_num in {nums[i] for i, key in enumerate(keys) if key == keys[g_hit[2]]}:
            hit, base, num = g_hit, head, g_num
    if not hit:
        return None, None
    place = keys[hit[2]]
    siblings = {nums[i]: i for i, k in enumerate(keys) if k == place}
    if num in siblings:
        return maps()[siblings[num]], place
    if num and set(siblings) == {None}:
        return maps()[siblings[None]], place  # no numbered maps here: the "numeral" is noise
    if clipped and num:
        # Cutting trailing glyphs off a numeral leaves a prefix of it ("VII" ->
        # "VI"): with no exact match, the one numeral it is a prefix of.
        cands = [n for n in siblings if n and n.startswith(num)]
        if len(cands) == 1:
            return maps()[siblings[cands[0]]], place
    return None, place


def find_map(street: str, name: str, min_score: float = 80, clipped: bool = False) -> dict | None:
    """The map entry for the minimap's two lines (street, map name), or None."""
    return match_map(street, name, clipped, min_score)[0]


@lru_cache(maxsize=256)
def mob(mob_id: int) -> Mob | None:
    try:
        d = _get_json(f"/mob/{mob_id}", f"mob_{mob_id}.json")
    except httpx.HTTPError:
        return None
    return Mob(mob_id, d.get("name", str(mob_id)), int(d.get("meta", {}).get("exp", 0)))


def mob_level(mob_id: int) -> int | None:
    """Monster level from the cached maplestory.io meta; never hits the network."""
    f = _data_dir() / f"mob_{mob_id}.json"
    try:
        lvl = json.loads(f.read_text(encoding="utf-8")).get("meta", {}).get("level")
        return int(lvl) if lvl else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


@lru_cache(maxsize=256)
def map_mobs(map_id: int) -> tuple[Mob, ...]:
    d = _get_json(f"/map/{map_id}", f"map_{map_id}.json")
    ids = sorted({m["id"] for m in d.get("mobs") or []})
    return tuple(m for i in ids if (m := mob(i)) is not None)


def _search_items(query: str) -> list[dict]:
    key = _norm(query)
    try:
        return _get_json(f"/item?searchFor={quote(query)}&count=20", f"itemsearch_{key[:60]}.json")
    except httpx.HTTPError:
        return []


@lru_cache(maxsize=1024)
def item_id(name: str) -> int | None:
    """Resolve an item name (from a loot message) to its MCW item id.

    Server search is substring-based, so if OCR garbled a letter we retry with
    the longest word and fuzzy-match locally.
    """
    key = _norm(name)
    if not key:
        return None
    hits = _search_items(name)
    if not hits:
        words = sorted(re.findall(r"[A-Za-z]{3,}", name), key=len, reverse=True)
        hits = _search_items(words[0]) if words else []
    best = process.extractOne(key, {h["id"]: _norm(h.get("name", "")) for h in hits},
                              scorer=fuzz.ratio, score_cutoff=85)
    return best[2] if best else None
