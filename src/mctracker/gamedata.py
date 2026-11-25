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
# the font's "I" as "l", "|", "1", "!" or "i" too, and may lowercase "V"/"X".
_NUMERAL_WORD = re.compile(r"^(.*?)[\s,.]+([IVXivxl|1!]{1,5})[\s.]*$")
_ROMAN = re.compile(r"^(X{0,3})(IX|IV|V?I{0,3})$")


def split_numeral(name: str) -> tuple[str, str | None]:
    """("Forest Up North", "VII") from "Forest Up North VIl": the name without its
    Roman numeral suffix, and the numeral with I-lookalikes read as I."""
    m = _NUMERAL_WORD.match(name.strip())
    if not m:
        return name, None
    num = m.group(2).upper().translate(str.maketrans("L|1!", "IIII"))
    if not _ROMAN.match(num):
        return name, None
    return m.group(1), num


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
              min_score: float = 80) -> tuple[dict | None, bool]:
    """(map, known place) for the minimap's two lines (street, map name).

    The street and name are fuzzy-matched without the name's Roman numeral; the
    numeral then has to match exactly, so "VI" is never taken for "VII" (#26).
    ``clipped`` (the name's ink runs into the crop's edge) means the numeral may be
    missing its last glyphs: the read then resolves only to the one numeral on the
    same place it is a prefix of (itself or a longer one), and to nothing if there
    are several. ``known place`` is True whenever the place matched, so a numeral
    that didn't resolve can be shown as the raw read with no mobs.
    """
    base, num = split_numeral(name)
    keys, nums = _map_index()
    hit = process.extractOne(_norm(street + base), dict(enumerate(keys)),
                             scorer=fuzz.ratio, score_cutoff=min_score)
    if not hit:
        return None, False
    siblings = {nums[i]: i for i, k in enumerate(keys) if k == keys[hit[2]]}
    if clipped and num:
        # Cutting trailing glyphs off a numeral leaves a prefix of it ("VII" ->
        # "VI", "IV" -> "I"): any sibling the read is a prefix of may be the map.
        cands = [n for n in siblings if n and n.startswith(num)]
        return (maps()[siblings[cands[0]]] if len(cands) == 1 else None), True
    if num in siblings:
        return maps()[siblings[num]], True
    return None, True


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
