from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication

from .assets import AssetStore
from .gui import MainWindow
from .overlay import Overlay
from .session import Tracker

DEFAULT_HOTKEY = "alt+a"


def main() -> int:
    parser = argparse.ArgumentParser(prog="mc-tracker")
    parser.add_argument("--demo", action="store_true", help="feed fake session data")
    parser.add_argument("--window", default="MapleStory", help="exact game window title, used only if MapleStory.exe isn't found")
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="price-check hotkey, e.g. alt+a")
    args, qt_args = parser.parse_known_args()

    logging.getLogger("RapidOCR").setLevel(logging.ERROR)

    app = QApplication([sys.argv[0], *qt_args])
    if args.demo:
        import tempfile
        from pathlib import Path

        tracker = Tracker(save_dir=Path(tempfile.mkdtemp(prefix="mc-tracker-demo-")))
    else:
        tracker = Tracker()
    assets = AssetStore()
    overlay = Overlay(tracker, assets)
    w = MainWindow(tracker, overlay, assets, args.hotkey)
    if args.demo:
        from .demo import DemoFeed

        app.feed = DemoFeed(tracker)  # keep a reference
    else:
        from . import gamedata
        from .capture import CaptureWorker

        tracker.item_resolver = gamedata.item_id
        cap = app.feed = CaptureWorker(args.window)
        cap.status_read.connect(tracker.on_status)
        cap.map_read.connect(tracker.on_map)
        cap.messages_read.connect(tracker.on_message_lines)
        cap.party_read.connect(tracker.on_party)
        cap.info.connect(lambda msg: w.statusBar().showMessage(msg))
        app.aboutToQuit.connect(cap.stop)
        app.aboutToQuit.connect(tracker.end_session)  # after capture stops: catch late updates
        cap.price_items.connect(w.show_price)
        cap.start()

        from .hotkey import GlobalHotkey

        hk = app.hotkey = GlobalHotkey(args.hotkey)
        hk.pressed.connect(cap.request_price_check)
        hk.failed.connect(lambda msg: w.statusBar().showMessage(msg))
        app.aboutToQuit.connect(hk.stop)
        hk.start()
    w.show()
    overlay.show()
    return app.exec()
