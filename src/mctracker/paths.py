r"""Where mc-tracker keeps its files (sprite cache, game data, sessions, browser profile).

Order: $MC_TRACKER_HOME, else a ``.mc-tracker`` folder in the source checkout
(when running from the repo), else %LOCALAPPDATA%\mc-tracker.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    env = os.environ.get("MC_TRACKER_HOME")
    if env:
        d = Path(env)
    else:
        repo = Path(__file__).resolve().parents[2]
        if (repo / "pyproject.toml").exists():
            d = repo / ".mc-tracker"
        else:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
            d = Path(base) / "mc-tracker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d
