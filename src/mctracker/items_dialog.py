"""Editor for shop buy prices and for which item sits in which quickslot."""

from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
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
    QWidget,
)

from .items import item_key, norm_name, slot_name
from .session import Tracker
from .theme import GUI_QSS

UNKNOWN = "(not tracked)"


def _pixmap(bgr: np.ndarray) -> QPixmap:
    img = np.ascontiguousarray(bgr)
    h, w = img.shape[:2]
    return QPixmap.fromImage(QImage(img.data, w, h, img.strides[0], QImage.Format.Format_BGR888).copy())


class _Lookup(QObject, QRunnable):
    """Resolves an item name to its id off the GUI thread (HTTP, up to 30 s timeouts)."""

    done = Signal(str, object)  # name, item id or None

    def __init__(self, resolve: Callable[[str], int | None], name: str):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.setAutoDelete(False)  # the dialog holds it until ``done`` arrives
        self.resolve, self.name = resolve, name

    def run(self) -> None:
        try:
            item_id = self.resolve(self.name)
        except Exception:  # offline etc.: keep it name-only
            item_id = None
        self.done.emit(self.name, item_id)


class ItemsDialog(QDialog):
    def __init__(self, tracker: Tracker, resolve: Callable[[str], int | None] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Items & prices")
        self.setStyleSheet(GUI_QSS)
        self.resize(680, 680)
        self.tracker, self.resolve = tracker, resolve
        self._lookups: dict[str, _Lookup] = {}  # normalised name -> running lookup
        # slot -> name last assigned, None if the slot no longer shows the assigned
        # icon (activated + editingFinished both fire; reassigning refreshes the icon)
        self._assigned: dict[int, str | None] = {}
        self.error = QLabel(wordWrap=True, objectName="muted")
        self.error.setStyleSheet("color: #b3261e;")
        self.error.hide()

        # --- quickslots -----------------------------------------------------------
        hint = QLabel(
            "Items used are counted only for quickslots you assign an item to here. Assign while "
            "the slot shows the item: it's checked against that icon, so if you move items "
            "between slots, assign them again.", wordWrap=True, objectName="muted")
        self.slot_grid = QGridLayout()
        self.slot_widgets = []
        for i in range(8):
            icon, count, combo = QLabel(), QLabel(objectName="muted", wordWrap=True), QComboBox(editable=True)
            icon.setFixedSize(40, 40)
            combo.setMinimumWidth(150)
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
        lay.addWidget(self.error)
        lay.addWidget(QLabel("Quickslots", objectName="h2"))
        self.off_note = QLabel(
            "Items-used tracking is off. It is experimental (an estimate), so it is off by default. "
            "To assign items to quickslots, turn on “Experimental: track items used (estimate)” "
            "in the menu bar. Shop prices below stay editable.", wordWrap=True, objectName="muted")
        lay.addWidget(self.off_note)
        self.slots_box = QWidget()  # the slot assignment, shown only while tracking is on
        box = QVBoxLayout(self.slots_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(hint)
        box.addLayout(self.slot_grid)
        box.addWidget(refresh, 0, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.slots_box)
        lay.addWidget(QLabel("Shop prices", objectName="h2"))
        lay.addWidget(QLabel("What you pay at the shop. Mesos spent = items used × price.",
                             objectName="muted", wordWrap=True))
        lay.addWidget(self.table, 1)
        lay.addLayout(add_row)
        self._fill_prices()
        problems = [f.problem for f in (tracker.prices, tracker.slot_items) if f.problem]
        if problems:
            self._show_error(" ".join(problems))

    def _show_error(self, msg: str) -> None:
        self.error.setText(msg)
        self.error.show()

    def _save(self, store) -> bool:
        try:
            store.save()
            return True
        except OSError as e:
            self._show_error(f"Couldn't save {store.path.name if store.path else 'settings'}: {e}")
            return False

    def _sync_tracking(self) -> None:
        on = self.tracker.items_used
        self.slots_box.setVisible(on)
        self.off_note.setVisible(not on)

    def showEvent(self, e):
        self._sync_tracking()
        self._fill_slots()
        super().showEvent(e)

    # --- quickslots -------------------------------------------------------------

    def _names(self) -> list[str]:
        names = {e["name"] for e in self.tracker.prices.items.values()}
        names |= {a.name for a in self.tracker.slot_items.slots.values()}
        return sorted(names)

    def _fill_slots(self) -> None:
        self._sync_tracking()
        reads = self.tracker.slots
        names = self._names()
        for i, (icon, count, combo) in enumerate(self.slot_widgets):
            r = reads[i] if i < len(reads) else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([UNKNOWN, *names])
            a = self.tracker.slot_items.get(i)
            here = a is not None and r is not None and a.matches(r.signature)
            combo.setCurrentText(a.name if a else UNKNOWN)
            combo.lineEdit().setCursorPosition(0)
            self._assigned[i] = (a.name if here else None) if a else ""
            combo.setEnabled(r is not None or a is not None)
            combo.setToolTip(slot_name(i))
            combo.blockSignals(False)
            if r is None:
                icon.clear()
            else:
                icon.setPixmap(_pixmap(r.icon).scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio))
            n = "" if r is None or r.count is None else f"×{r.count:,} · "
            if a is None:
                count.setText(f"{slot_name(i)}\n{n}Assign an item to track usage")
            elif here:
                count.setText(f"{slot_name(i)}\n{n}Tracked")
            else:
                count.setText(f"{slot_name(i)}\nShows another icon: not counted. Assign again to track it")

    def _assign(self, slot: int) -> None:
        reads = self.tracker.slots
        name = self.slot_widgets[slot][2].currentText().strip()
        name = "" if name == UNKNOWN or not norm_name(name) else name
        if self._assigned.get(slot) == name:
            return  # the other of activated/editingFinished already handled it
        r = reads[slot] if slot < len(reads) else None
        signature = r.signature if r is not None else None
        if name and (signature is None or (r.raw if r.raw is not None else r.count) is None):
            # nothing to take the icon from, or an empty / unreadable slot: refuse, and reset the box
            self._show_error(f"{slot_name(slot)} has no readable count: put the item there first, then assign it")
            self._fill_slots()
            return
        try:
            self.tracker.slot_items.assign(slot, signature, self._local_id(name), name,
                                           shown=[x.signature for x in reads])
        except OSError as e:
            self._show_error(f"Couldn't save the slot assignment: {e}")
            return
        if name and self._local_id(name) is None:
            self._lookup(name)
        self._fill_slots()

    def _local_id(self, name: str) -> int | None:
        """An id we already know for this name, without going online."""
        n = norm_name(name)
        for key, e in self.tracker.prices.items.items():
            if key.isdigit() and norm_name(e["name"]) == n:
                return int(key)
        for a in self.tracker.slot_items.slots.values():
            if a.item_id is not None and a.key == n:
                return a.item_id
        return None

    def _lookup(self, name: str) -> None:
        """Resolve ``name`` in the background; ``_resolved`` upgrades name keys to the id."""
        if not self.resolve or not norm_name(name) or norm_name(name) in self._lookups:
            return
        job = self._lookups[norm_name(name)] = _Lookup(self.resolve, name)
        job.done.connect(self._resolved)
        QThreadPool.globalInstance().start(job)

    def _resolved(self, name: str, item_id) -> None:
        self._lookups.pop(norm_name(name), None)
        if item_id is None:
            return  # name-only; prices still match usage by name
        n = norm_name(name)
        si = self.tracker.slot_items
        changed = False
        for a in si.slots.values():
            if a.item_id is None and a.key == n:
                a.item_id, changed = int(item_id), True
        if changed:
            self._save(si)
        pl = self.tracker.prices
        for key, e in list(pl.items.items()):
            if not key.isdigit() and norm_name(e["name"]) == n and str(item_id) not in pl.items:
                pl.remove(key)
                pl.set(str(item_id), e["name"], e["price"])
                self._save(pl)
                self._fill_prices()

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
        self._save(self.tracker.prices)

    def _add(self) -> None:
        name = self.new_name.text().strip()
        try:
            price = int(self.new_price.text().replace(",", "").strip())
        except ValueError:
            self.new_price.setFocus()
            return
        if not name:
            return
        item_id = self._local_id(name)
        self.tracker.prices.set(item_key(item_id, name), name, max(0, price))
        if not self._save(self.tracker.prices):
            return
        if item_id is None:
            self._lookup(name)
        self.new_name.clear()
        self.new_price.clear()
        self._fill_prices()
        self._fill_slots()

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            self.tracker.prices.remove(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole))
        self._save(self.tracker.prices)
        self._fill_prices()
