from __future__ import annotations

import argparse
import sys

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
from .price_panel import PricePanel
from .session import Session

CREDIT = (
    'Game data: <a href="https://meowdb.com/msclassic">NiaDB</a> · '
    'Sprites: <a href="https://maplestory.io">maplestory.io</a> · '
    'Prices: <a href="https://mesos.watch">mesos.watch</a> · '
    "MapleStory © Nexon. Fan project, not affiliated."
)


class MainWindow(QMainWindow):
    def __init__(self, session: Session, overlay: Overlay):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(1100, 800)
        self.overlay = overlay

        self.panel = PricePanel()

        # Manual search until screen-based item detection lands.
        self.query = QLineEdit(placeholderText="Item name")
        go = QPushButton("Price check")
        pop = QPushButton("Show overlay")
        self.query.returnPressed.connect(self._search)
        go.clicked.connect(self._search)
        pop.clicked.connect(self._show_overlay)

        bar = QHBoxLayout()
        bar.addWidget(self.query)
        bar.addWidget(go)
        bar.addWidget(pop)

        credit = QLabel(CREDIT, openExternalLinks=True)

        layout = QVBoxLayout()
        layout.addLayout(bar)
        layout.addWidget(self.panel, 1)
        layout.addWidget(credit)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

    def _search(self):
        name = self.query.text().strip()
        if name:
            self.panel.search(name)

    def _show_overlay(self):
        self.overlay.show()
        self.overlay.raise_()

    def closeEvent(self, e):
        self.overlay.close()
        super().closeEvent(e)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mc-tracker")
    parser.add_argument("--demo", action="store_true", help="feed fake session data")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0], *qt_args])
    session = Session()
    assets = AssetStore()
    overlay = Overlay(session, assets)
    if args.demo:
        from .demo import DemoFeed

        app.demo = DemoFeed(session)  # keep a reference
    w = MainWindow(session, overlay)
    w.show()
    overlay.show()
    return app.exec()
