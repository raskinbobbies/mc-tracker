r"""Where mc-tracker keeps its files (sprite cache, game data, sessions, browser profile).

Order: $MC_TRACKER_HOME, else a ``.mc-tracker`` folder in the source checkout
(when running from the repo), else %LOCALAPPDATA%\mc-tracker.

The frozen .exe never writes next to itself: it uses ``D:\Maple\.mc-tracker`` (where the
dev build keeps its data) when ``D:\Maple`` exists, else %LOCALAPPDATA%\mc-tracker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN_DEFAULT_HOME = Path(r"D:\Maple\.mc-tracker")  # same place the dev build uses


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")) / "mc-tracker"


def home() -> Path:
    env = os.environ.get("MC_TRACKER_HOME")
    if env:
        d = Path(env)
    elif frozen():
        d = FROZEN_DEFAULT_HOME if FROZEN_DEFAULT_HOME.parent.is_dir() else _localappdata()
    else:
        repo = Path(__file__).resolve().parents[2]
        if (repo / "pyproject.toml").exists():
            d = repo / ".mc-tracker"
        else:
            d = _localappdata()
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d
