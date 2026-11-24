"""Polite NiaDB (meowdb.com) fetcher.

Runs only after NiaDB grants permission (see docs/data-sources.md), and follows
their tool rules: one request per 10 s, sequential, identifying User-Agent,
stable URLs, stop on 429/403. Output goes to data/ (git-ignored).

Usage:
    set MEOWDB_APPROVED=1
    python tools/fetch_meowdb.py <path> [<path> ...]

Paths are relative to https://meowdb.com, e.g. /msclassic/item-db/all.
Fill in the concrete endpoints NiaDB approves in ENDPOINTS below.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

BASE = "https://meowdb.com"
USER_AGENT = "mc-tracker/0.1 (+https://github.com/raskinbobbies/mc-tracker)"
MIN_INTERVAL_S = 10.0
OUT = Path(__file__).resolve().parent.parent / "data" / "meowdb"

# Endpoints to mirror once approved (path -> output file name).
ENDPOINTS: dict[str, str] = {}


class Stop(Exception):
    pass


def out_file(path: str) -> Path:
    safe = path.strip("/").replace("/", "__") or "index"
    return OUT / safe


def main(argv: list[str]) -> int:
    if os.environ.get("MEOWDB_APPROVED") != "1":
        print("Refusing to run: NiaDB permission not recorded. See docs/data-sources.md.")
        return 2

    paths = argv or list(ENDPOINTS)
    if not paths:
        print("No paths given and ENDPOINTS is empty.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    last = 0.0
    with httpx.Client(base_url=BASE, headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for path in paths:
            dest = out_file(ENDPOINTS.get(path, path))
            if dest.exists():
                print(f"cached  {path}")
                continue
            wait = MIN_INTERVAL_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            r = client.get(path)
            if r.status_code in (403, 429):
                print(f"{r.status_code} on {path}: stopping as required by NiaDB.")
                return 3
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"saved   {path} -> {dest.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
