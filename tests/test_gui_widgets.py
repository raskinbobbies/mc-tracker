"""Widget tests; run offscreen."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from collections import Counter  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.gui import EntryGrid, Sparkline, best_index  # noqa: E402
from mctracker.history import Entry  # noqa: E402
from mctracker.overlay import TileRow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeAssets:
    def mob_sprite(self, key, cb):
        pass

    def item_icon(self, key, cb):
        pass


def test_tile_row_layout_does_not_grow_or_rebuild_when_order_is_unchanged(app):
    row = TileRow("mob", FakeAssets(), empty="none")
    counts = Counter({1: 5, 2: 3, 3: 1})
    row.update_counts(counts, {})
    n = row.row.count()
    tiles = [row.row.itemAt(i).widget() for i in range(3)]
    assert n == 4  # three tiles + one stretch
    for i in range(500):
        counts[1] += 1  # counts change, order does not
        row.update_counts(counts, {})
    assert row.row.count() == n
    assert [row.row.itemAt(i).widget() for i in range(3)] == tiles  # not rebuilt
    assert tiles[0].count.text().endswith("505")


def test_tile_row_relayouts_when_order_changes(app):
    row = TileRow("mob", FakeAssets(), empty="none")
    counts = Counter({1: 5, 2: 3})
    row.update_counts(counts, {})
    counts[2] = 9
    row.update_counts(counts, {})
    assert row.row.itemAt(0).widget() is row.tiles[2]
    assert row.row.itemAt(1).widget() is row.tiles[1]
    assert row.row.count() == 3
    row.update_counts(Counter({7: 1}), {})  # tiles from the old set go away
    assert set(row.tiles) == {7} and row.row.count() == 2


def test_tile_row_empty_label_toggles(app):
    row = TileRow("loot", FakeAssets(), empty="none")
    row.update_counts(Counter(), {})
    assert row.row.itemAt(0).widget() is row.empty and not row.empty.isHidden()
    row.update_counts(Counter({4: 1}), {})
    assert row.empty.isHidden() and row.row.count() == 2


def test_entry_grid_skips_unchanged_entries(app):
    grid = EntryGrid("kill", FakeAssets())
    entries = [Entry(1, "A", 2), Entry(2, "B", 3)]
    grid.show_entries(entries)
    first = list(grid._tiles)
    grid.show_entries([Entry(1, "A", 2), Entry(2, "B", 3)])
    assert grid._tiles == first
    grid.show_entries([Entry(1, "A", 9)])
    assert len(grid._tiles) == 1 and grid.grid.count() == 1


@pytest.mark.parametrize("values", [[0, 0, 0], [0, 0], [1, 5, 2, 5], [3, 0, 1]])
def test_sparkline_paints_without_error(app, values, capsys):
    s = Sparkline()
    s.resize(300, 60)
    s.set_values(values)
    s.grab()  # exceptions in paintEvent are printed, not raised
    assert capsys.readouterr().err == ""


def test_best_index():
    assert best_index([]) is None
    assert best_index([0, 0, 0]) is None
    assert best_index([1, 5, 2, 5]) == 1
