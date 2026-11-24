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


def find_map(street: str, name: str, min_score: float = 80) -> dict | None:
    """Fuzzy-match the minimap's two lines (street, map name) to a map entry."""
    keys = {i: _norm(f"{m.get('streetName', '')}{m.get('name', '')}") for i, m in enumerate(maps())}
    hit = process.extractOne(_norm(street + name), keys, scorer=fuzz.ratio, score_cutoff=min_score)
    return maps()[hit[2]] if hit else None


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
