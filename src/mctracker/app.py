from __future__ import annotations

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

from .price_panel import PricePanel

CREDIT = (
    'Game data: <a href="https://meowdb.com/msclassic">NiaDB</a> · '
    'Prices: <a href="https://mesos.watch">mesos.watch</a> · '
    "MapleStory © Nexon. Fan project, not affiliated."
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mc-tracker")
        self.resize(1100, 800)

        self.panel = PricePanel()

        # Manual search until screen-based item detection lands.
        self.query = QLineEdit(placeholderText="Item name")
        go = QPushButton("Price check")
        self.query.returnPressed.connect(self._search)
        go.clicked.connect(self._search)

        bar = QHBoxLayout()
        bar.addWidget(self.query)
        bar.addWidget(go)

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


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()
