# Data sources

## maplestory.io (sprites, icons, items, monsters, maps)

Public API with MapleStory Classic World data under `https://maplestory.io/api/MCW/1.2`: monster stats including EXP (`/mob/{id}`), animated sprites (`/mob/{id}/render/stand`), item names and icons (`/item/{id}`, `/item/{id}/icon`), and maps (`/map/{id}`). Note: MCW monster IDs differ from classic GMS IDs (e.g. Snail is `2`). Assets are fetched at runtime, cached locally, and never committed.

## NiaDB / meowdb.com (items, maps, monsters, EXP table)

NiaDB's [reuse policy](https://meowdb.com/reuse) requires permission before crawling the site or its API, and asks approved tools to:

1. Send **one request every 10 seconds**, one at a time (never in parallel).
2. Put the tool name and a contact in the **User-Agent**.
3. Use the **same URL for the same question** (no cache-busting params).
4. **Stop on 429 or 403.**
5. Credit **NiaDB, linked to meowdb.com**, wherever the data is shown (README + app footer here).

`tools/fetch_meowdb.py` enforces 1–4 and refuses to run unless `MEOWDB_APPROVED=1` is set. Fetched data goes to `data/` (git-ignored) and is **not redistributed**.

### Permission request

Send to `contact@meowdb.com`, subject `Permission request: mc-tracker`, four lines only:

```
Project name: mc-tracker
Live URL: https://github.com/raskinbobbies/mc-tracker
What it does: Open-source desktop EXP/loot tracker for MapleStory Classic that reads the game window; uses NiaDB item, map and monster data locally, credited in the README and app.
User-Agent your tool sends: mc-tracker/0.1 (+https://github.com/raskinbobbies/mc-tracker)
```

## mesos.watch (prices)

No public API. Its private API is protected by Cloudflare Turnstile and signed request tokens, and the site forbids framing (`X-Frame-Options: DENY`). The app loads mesos.watch as a normal page in its own top-level web view (you browse it as yourself) and types the item name into the search box. It never calls `api.mesos.watch` directly.

The site has no per-item URLs, so the search automation depends on its search box (`input[placeholder="Search or ask the market"]`) and may break if the site changes.
