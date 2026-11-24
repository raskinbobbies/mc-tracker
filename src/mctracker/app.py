from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .assets import AssetStore
from .overlay import Overlay
from .session import Tracker

DEFAULT_HOTKEY = "alt+a"

CREDIT = (
    'Game data: <a href="https://meowdb.com/msclassic">NiaDB</a> · '
    'Sprites: <a href="https://maplestory.io">maplestory.io</a> · '
    'Prices: <a href="https://mesos.watch">mesos.watch</a> · '
    "MapleStory © Nexon. Fan project, not affiliated."
)


class MainWindow(QMainWindow):
    def __init__(self, tracker: Tracker, overlay: Overlay, hotkey: str):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(560, 140)
        self.overlay = overlay

        self.query = QLineEdit(placeholderText=f"Item name (or hover an item in game and press {hotkey.upper()})")
        go = QPushButton("Price check")
        pop = QPushButton("Show overlay")
        self.query.returnPressed.connect(self._search)
        go.clicked.connect(self._search)
        pop.clicked.connect(self._show_overlay)

        bar = QHBoxLayout()
        bar.addWidget(self.query, 1)
        bar.addWidget(go)
        bar.addWidget(pop)

        layout = QVBoxLayout()
        layout.addLayout(bar)
        layout.addWidget(QLabel(CREDIT, openExternalLinks=True))
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

    def price_check(self, name: str, others: list[str] | None = None) -> None:
        """Copy the item name and bring up mesos.watch in the user's browser."""
        from .browser import focus_mesos_watch

        self.query.setText(name)
        QGuiApplication.clipboard().setText(name)
        what = focus_mesos_watch()
        extra = f"  (also seen: {', '.join(others)})" if others else ""
        self.statusBar().showMessage(f"{what}. '{name}' copied: press Ctrl+V in the search box.{extra}")

    def _search(self) -> None:
        name = self.query.text().strip()
        if name:
            self.price_check(name)

    def show_price(self, items) -> None:
        """Hotkey result: (name, item_id) candidates, hovered item first."""
        if items:
            self.price_check(items[0][0], [n for n, _ in items[1:]])

    def _show_overlay(self):
        self.overlay.show()
        self.overlay.raise_()

    def closeEvent(self, e):
        self.overlay.tracker.end_session()  # save what we have
        self.overlay.close()
        super().closeEvent(e)


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
    w = MainWindow(tracker, overlay, args.hotkey)
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
        cap.info.connect(lambda msg: w.statusBar().showMessage(msg))
        app.aboutToQuit.connect(cap.stop)
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
