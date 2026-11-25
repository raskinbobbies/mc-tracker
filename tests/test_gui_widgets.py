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


def _saved(**data):
    from pathlib import Path

    from mctracker.history import SavedSession

    return SavedSession(Path("x.json"), {"started_at": "2026-11-25T10:00:00", **data})


def test_mesos_cell_text_colour_and_sort(app):
    from mctracker.gui import GAIN, LOSS, _mesos_cell

    gain = _mesos_cell(_saved(active_seconds=1200, net_mesos=10_980))
    assert gain.text() == "+10,980 \u00b7 549/min"
    assert gain.foreground().color().name() == GAIN
    loss = _mesos_cell(_saved(active_seconds=60, net_mesos=-500))
    assert loss.text() == "\u2212500 \u00b7 \u2212500/min"
    assert loss.foreground().color().name() == LOSS
    assert loss < gain


def test_potions_cell_text_and_missing_sorts_last(app):
    from mctracker.gui import _potions_cell

    used = _potions_cell(_saved(active_seconds=1260, items_used_estimate=True, used={"Red Potion": 1575}))
    assert used.text() == "\u22121,575 \u00b7 75/min"
    none = _potions_cell(_saved(active_seconds=60))
    assert none.text() == "\u2014"
    assert used < none
    zero = _potions_cell(_saved(active_seconds=60, items_used_estimate=True, used={}))
    assert zero.text() == "0 \u00b7 0/min"


def test_mesos_cell_dash_without_mesos_fields_and_gross_tooltip(app):
    from mctracker.gui import _mesos_cell

    assert _mesos_cell(_saved(active_seconds=60)).text() == "—"
    gross = _mesos_cell(_saved(active_seconds=60, mesos=600))
    assert gross.text() == "+600 · 600/min"
    assert "Gross mesos" in gross.toolTip()
    tracked = _mesos_cell(_saved(active_seconds=60, mesos=600, mesos_spent=100, items_used_estimate=True, used={}))
    assert tracked.toolTip() == ""
