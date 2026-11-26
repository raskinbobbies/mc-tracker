"""The capture loop's CPU budget and self-recovery: no re-reading a frozen frame, rare full message reads,
cached status OCR, a reader crash that detaches (so the watcher restarts it)."""

import os
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import capture  # noqa: E402
from mctracker.capture import STALE_TEXT, CaptureWatcher, CaptureWorker  # noqa: E402
from mctracker.vision import quickslots  # noqa: E402
from mctracker.vision.reader import GameState, Reader  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeReader:
    made = 0

    def __init__(self, rows=None):
        FakeReader.made += 1
        self.ocr = None
        self._msg_rows, self._msg_misses = rows, 0
        self.calls = {"full": 0, "fast": 0}

    def read_messages(self, _f, _s):
        self.calls["full"] += 1

    def read_messages_fast(self, _f, _s):
        self.calls["fast"] += 1

    def read_chat(self, _f, _s):
        return False

    def read_map(self, _f, _s):
        pass

    def read_party(self, _f, _s):
        pass


def run_loop(monkeypatch, w, seconds, new_frames=True, step=0.05, start=100.0):
    """Run ``w._run`` on a fake clock for ``seconds``; each sleep advances it (and maybe brings a new frame)."""
    clock = {"t": start}

    def sleep(dt):
        clock["t"] += max(dt, step)
        if new_frames:
            w._frame, w._frame_at = np.zeros((4, 4, 3), np.uint8), clock["t"]
        if clock["t"] - start >= seconds:
            w._running = False

    monkeypatch.setattr(capture, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep))
    w._frame, w._frame_at = np.zeros((4, 4, 3), np.uint8), start
    w._running, w._closed = True, False
    w._run(w._gen)


@pytest.fixture
def worker(qapp, monkeypatch):
    FakeReader.made = 0
    monkeypatch.setattr(capture, "Reader", FakeReader)
    monkeypatch.setattr(capture, "read_status", lambda *_a: None)
    monkeypatch.setattr(capture, "QuickslotReader", lambda _ocr: types.SimpleNamespace(read=lambda _f: []))
    w = CaptureWorker("MapleStory")
    for name in ("_resolve_map", "_read_channel"):
        monkeypatch.setattr(w, name, lambda *_a, **_k: None)
    monkeypatch.setattr(w, "_refine_minimap_status", lambda *_a: "ok")
    return w


def test_frame_handler_converts_bgra_to_a_contiguous_bgr_copy(qapp):
    w = CaptureWorker("x")
    w._running, w._gen = True, 1
    buf = np.random.default_rng(0).integers(0, 255, (6, 8, 4), np.uint8)
    w._on_frame(types.SimpleNamespace(frame_buffer=buf), types.SimpleNamespace(stop=lambda: None), 1)
    f = w._latest()
    assert f.shape == (6, 8, 3) and f.flags.c_contiguous and not np.shares_memory(f, buf)
    assert (f == buf[:, :, :3]).all()


def test_a_frozen_frame_is_read_once_then_reported_stale(worker, monkeypatch):
    infos, statuses = [], []
    worker.info.connect(infos.append)
    worker.minimap_changed.connect(statuses.append)
    run_loop(monkeypatch, worker, seconds=12, new_frames=False)
    reader = worker._spare_reader
    assert reader.calls == {"full": 1, "fast": 0}  # one read of the frame, none of its repeats
    assert infos.count(STALE_TEXT) == 1 and statuses[-1] == "unknown"


def test_new_frames_after_a_stale_spell_say_so(worker, monkeypatch):
    infos = []
    worker.info.connect(infos.append)
    clock = {"t": 100.0, "n": 0}

    def sleep(dt):
        clock["t"] += max(dt, 0.05)
        clock["n"] += 1
        if 7.0 <= clock["t"] - 100 < 7.5:  # the game is back after 7 s
            worker._frame, worker._frame_at = np.zeros((4, 4, 3), np.uint8), clock["t"]
        if clock["t"] - 100 >= 8:
            worker._running = False

    monkeypatch.setattr(capture, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep))
    worker._frame, worker._frame_at, worker._running = np.zeros((4, 4, 3), np.uint8), 100.0, True
    worker._run(0)
    assert infos == [STALE_TEXT, "Watching 'MapleStory'"]


def test_full_message_read_is_rare_while_the_rows_are_measured(worker, monkeypatch):
    monkeypatch.setattr(capture, "Reader", lambda: FakeReader(rows=(100.0, 20.0)))
    run_loop(monkeypatch, worker, seconds=61)
    calls = worker._spare_reader.calls
    assert calls["full"] == 3  # at 0, 30 and 60 s
    assert calls["fast"] > 300  # every 0.15 s otherwise


def test_full_message_read_every_3s_while_the_rows_are_unknown(worker, monkeypatch):
    run_loop(monkeypatch, worker, seconds=30)
    assert worker._spare_reader.calls["full"] == 10


def test_full_message_read_every_3s_once_the_fast_read_misses(worker, monkeypatch):
    def lost():
        r = FakeReader(rows=(100.0, 20.0))
        r._msg_misses = 2
        return r

    monkeypatch.setattr(capture, "Reader", lost)
    run_loop(monkeypatch, worker, seconds=30)
    assert worker._spare_reader.calls["full"] == 10


def test_fast_message_reads_keep_their_pace_on_jittery_frames(worker, monkeypatch):
    run_loop(monkeypatch, worker, seconds=30, step=0.149)  # frames a hair under 0.15 s apart
    calls = worker._spare_reader.calls
    assert calls["full"] + calls["fast"] >= 195


def test_the_reader_is_reused_across_reattaches(worker, monkeypatch):
    run_loop(monkeypatch, worker, seconds=1)
    first = worker._spare_reader
    run_loop(monkeypatch, worker, seconds=1)
    assert FakeReader.made == 1 and worker._spare_reader is first


def test_a_reader_crash_detaches_so_the_watcher_restarts_it(qapp, monkeypatch):
    w = CaptureWorker("MapleStory")
    w._running, w._gen, w.hwnd, w._attached = True, 2, 42, True
    attached, infos = [], []
    w.attached_changed.connect(attached.append)
    w.info.connect(infos.append)
    w._spare_reader = "kept"

    def boom(_gen):
        w._spare_reader = None  # the loop took it
        raise RuntimeError("boom")

    monkeypatch.setattr(w, "_run", boom)
    w._loop(2)
    assert not w.running and attached == [False] and infos == ["Reader crashed: boom"]
    assert w._spare_reader is None  # a crashed reader isn't reused
    calls = []
    monkeypatch.setattr(w, "stop", lambda wait=True: calls.append("stop") or setattr(w, "hwnd", None))
    monkeypatch.setattr(w, "start", lambda: calls.append("start"))
    CaptureWatcher(w, probe=lambda: True, clock=lambda: 1000.0).tick()
    assert calls == ["stop", "start"]


def test_a_stale_loops_crash_leaves_the_new_capture_attached(qapp, monkeypatch):
    w = CaptureWorker("MapleStory")
    w._running, w._gen, w._attached = True, 3, True
    monkeypatch.setattr(w, "_run", lambda _gen: (_ for _ in ()).throw(RuntimeError("boom")))
    w._loop(2)  # the loop of a capture that was already replaced
    assert w.running and w._attached


def test_status_ocr_runs_only_when_its_pixels_change(qapp):
    r = Reader.__new__(Reader)
    seen = []
    r._line = lambda img: seen.append(img.sum()) or "EXP.66,700[14.24%]"
    frame = np.zeros((1440, 2560, 3), np.uint8)
    for _ in range(3):
        s = GameState()
        capture.read_status(r, frame, s)
        assert s.exp == 66700
    assert len(seen) == 2  # level and EXP, once each
    frame[:] = 200  # the text changed
    capture.read_status(r, frame, GameState())
    assert len(seen) > 2


def test_quickslot_panel_does_not_pin_the_frame():
    frame = np.zeros((1440, 2560, 3), np.uint8)
    p = quickslots.panel(frame)
    assert p.base is None and not np.shares_memory(p, frame)
