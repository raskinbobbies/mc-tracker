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

No public API, no per-item URLs, and it blocks embedded browsers (its API answers 403 to requests from an embedded Qt browser; framing is denied with `X-Frame-Options: DENY`). So a price check copies the item name to the clipboard and brings up mesos.watch in the user's own browser: it switches to an existing mesos.watch tab when one is open (found via Windows UI Automation in Chromium browsers and Firefox), otherwise opens a new one. The app never calls `api.mesos.watch`.
