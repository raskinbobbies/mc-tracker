"""Editor for shop buy prices and for which item sits in which quickslot."""

from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .items import item_key, slot_name
from .session import Tracker
from .theme import GUI_QSS

UNKNOWN = "(unknown)"


def _pixmap(bgr: np.ndarray) -> QPixmap:
    img = np.ascontiguousarray(bgr)
    h, w = img.shape[:2]
    return QPixmap.fromImage(QImage(img.data, w, h, img.strides[0], QImage.Format.Format_BGR888).copy())


class ItemsDialog(QDialog):
    def __init__(self, tracker: Tracker, resolve: Callable[[str], int | None] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Items & prices")
        self.setStyleSheet(GUI_QSS)
        self.resize(560, 640)
        self.tracker, self.resolve = tracker, resolve

        # --- quickslots -----------------------------------------------------------
        hint = QLabel(
            "Pick which item each quickslot holds; it's recognised by its icon from then on. "
            "Unassigned slots are tracked as 'Slot N'.", wordWrap=True, objectName="muted")
        self.slot_grid = QGridLayout()
        self.slot_widgets = []
        for i in range(8):
            icon, count, combo = QLabel(), QLabel(objectName="muted"), QComboBox(editable=True)
            icon.setFixedSize(40, 40)
            combo.activated.connect(lambda _idx, i=i: self._assign(i))
            combo.lineEdit().editingFinished.connect(lambda i=i: self._assign(i))
            r, c = divmod(i, 2)
            self.slot_grid.addWidget(icon, r, c * 3)
            self.slot_grid.addWidget(count, r, c * 3 + 1)
            self.slot_grid.addWidget(combo, r, c * 3 + 2)
            self.slot_widgets.append((icon, count, combo))
        refresh = QPushButton("Re-read slots")
        refresh.clicked.connect(self._fill_slots)

        # --- prices ---------------------------------------------------------------
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Item", "Buy price (mesos)"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._price_edited)
        self.new_name = QLineEdit(placeholderText="Item name, e.g. Red Potion")
        self.new_price = QLineEdit(placeholderText="Price")
        self.new_price.setFixedWidth(90)
        add = QPushButton("Add", objectName="primary")
        add.clicked.connect(self._add)
        self.new_price.returnPressed.connect(self._add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        add_row = QHBoxLayout()
        add_row.addWidget(self.new_name, 1)
        add_row.addWidget(self.new_price)
        add_row.addWidget(add)
        add_row.addWidget(remove)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Quickslots", objectName="h2"))
        lay.addWidget(hint)
        lay.addLayout(self.slot_grid)
        lay.addWidget(refresh, 0, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(QLabel("Shop prices", objectName="h2"))
        lay.addWidget(QLabel("What you pay at the shop. Mesos spent = items used × price.",
                             objectName="muted", wordWrap=True))
        lay.addWidget(self.table, 1)
        lay.addLayout(add_row)
        self._fill_prices()

    def showEvent(self, e):
        self._fill_slots()
        super().showEvent(e)

    # --- quickslots -------------------------------------------------------------

    def _names(self) -> list[str]:
        names = {e["name"] for e in self.tracker.prices.items.values()}
        names |= {k.name for k in self.tracker.slot_items.known}
        return sorted(names)

    def _fill_slots(self) -> None:
        reads = self.tracker.slots
        names = self._names()
        for i, (icon, count, combo) in enumerate(self.slot_widgets):
            r = reads[i] if i < len(reads) else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([UNKNOWN, *names])
            known = self.tracker.slot_items.identify(r.signature) if r is not None else None
            combo.setCurrentText(known.name if known else UNKNOWN)
            combo.setEnabled(r is not None)
            combo.setToolTip(slot_name(i))
            combo.blockSignals(False)
            if r is None:
                icon.clear()
                count.setText(slot_name(i))
                continue
            icon.setPixmap(_pixmap(r.icon).scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio))
            count.setText("×?" if r.count is None else f"×{r.count:,}")

    def _assign(self, slot: int) -> None:
        reads = self.tracker.slots
        if slot >= len(reads):
            return
        name = self.slot_widgets[slot][2].currentText().strip()
        name = "" if name == UNKNOWN else name
        self.tracker.slot_items.assign(reads[slot].signature, self._id_for(name), name)

    def _id_for(self, name: str) -> int | None:
        for key, e in self.tracker.prices.items.items():
            if e["name"] == name and key.isdigit():
                return int(key)
        if name and self.resolve:
            try:
                return self.resolve(name)
            except Exception:  # offline etc.: keep it name-only
                return None
        return None

    # --- prices -----------------------------------------------------------------

    def _fill_prices(self) -> None:
        self.table.blockSignals(True)
        items = sorted(self.tracker.prices.items.items(), key=lambda kv: kv[1]["name"].lower())
        self.table.setRowCount(len(items))
        for r, (key, e) in enumerate(items):
            name = QTableWidgetItem(e["name"])
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setData(Qt.ItemDataRole.UserRole, key)
            name.setToolTip(f"Item id {key}" if key.isdigit() else "No item id found")
            price = QTableWidgetItem(str(e["price"]))
            price.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, price)
        self.table.blockSignals(False)

    def _price_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        name = self.table.item(item.row(), 0)
        try:
            price = int(item.text().replace(",", "").strip())
        except ValueError:
            self._fill_prices()
            return
        self.tracker.prices.set(name.data(Qt.ItemDataRole.UserRole), name.text(), max(0, price))
        self.tracker.prices.save()

    def _add(self) -> None:
        name = self.new_name.text().strip()
        try:
            price = int(self.new_price.text().replace(",", "").strip())
        except ValueError:
            self.new_price.setFocus()
            return
        if not name:
            return
        item_id = self._id_for(name)
        self.tracker.prices.set(item_key(item_id, name), name, max(0, price))
        self.tracker.prices.save()
        self.new_name.clear()
        self.new_price.clear()
        self._fill_prices()
        self._fill_slots()

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            self.tracker.prices.remove(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole))
        self.tracker.prices.save()
        self._fill_prices()
