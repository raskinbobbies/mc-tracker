"""Run the screen reader on saved screenshots and print what it sees.

Usage: python tools/read_screenshot.py screenshots/*.png
"""

from __future__ import annotations

import sys
import time

import cv2

from mctracker import gamedata
from mctracker.kills import parse_message
from mctracker.vision.reader import Reader


def main(paths: list[str]) -> int:
    reader = Reader()
    for path in paths:
        frame = cv2.imread(path)
        t = time.perf_counter()
        s = reader.read_all(frame)
        ms = (time.perf_counter() - t) * 1000
        print(f"\n== {path}  ({frame.shape[1]}x{frame.shape[0]}, {ms:.0f} ms)")
        print(f"level={s.level} exp={s.exp} pct={s.exp_percent}")
        print(f"minimap: {s.street!r} / {s.map_name!r}")
        m = gamedata.find_map(s.street or "", s.map_name or "")
        if m:
            mobs = gamedata.map_mobs(m["id"])
            print(f"  -> map {m['id']} {m['streetName']} / {m['name']}; mobs: "
                  + ", ".join(f"{x.name} ({x.exp} EXP)" for x in mobs))
        for line in s.messages:
            print(f"  msg y={line.y:6.1f} {line.text!r} -> {parse_message(line.text)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
