# mc-tracker

A free, open-source desktop companion for **MapleStory Classic World**. It watches your game window and tracks your session, and helps you price-check items.

> Status: early development. The game is in maintenance, so screen recognition isn't calibrated yet.

## Features (planned)

- **EXP/hr**: live rate, time to level, session totals (handles level-ups and deaths)
- **Map**: read from the minimap name
- **Monsters**: kill counts inferred from EXP gain messages and the current map's monsters
- **Loot**: items and mesos picked up, from pickup messages
- **Items used**: potion/consumable usage from hotkey counts
- **Price check**: hover an item, press a hotkey, and the app reads the item name and searches it on [mesos.watch](https://mesos.watch) in a built-in panel

## How it works

It only **reads pixels** from the game window (Windows Graphics Capture), like sharing your screen on Discord. It never reads game memory, modifies game files, or sends input to the game. Video never leaves your computer.

## Required game settings

- **UI Size: Ideal Ratio** (Options → Graphics)
- **HDR off** in Windows
- **Minimap map name visible**

## Development

```sh
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
python -m mctracker
```

Game reference data (items, maps, monsters) is fetched locally with `tools/fetch_meowdb.py` once permission is granted. It is not committed to this repo. See [docs/data-sources.md](docs/data-sources.md).

## Credits

- Game reference data: [NiaDB (meowdb.com)](https://meowdb.com/msclassic), used with permission.
- Market prices: [mesos.watch](https://mesos.watch), shown in its own embedded browser panel. This app does not call its API.

MapleStory and its art are © Nexon. This is a fan project, not affiliated with or endorsed by Nexon, NiaDB, or mesos.watch.

## License

MIT for the code in this repo. Game data and assets belong to their respective owners.
