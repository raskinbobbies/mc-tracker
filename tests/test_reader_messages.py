"""Messages fast read: newest rows at fixed positions, cached by text pixels."""
import numpy as np

from mctracker.vision import reader as R


def make_reader(texts):
    r = object.__new__(R.Reader)  # no OCR model: _line/_lines are stubbed
    r._msg_rows, r._msg_cache, r._msg_misses, r.calls = None, {}, 0, []

    def line(img):
        r.calls.append(img.shape)
        return texts.pop(0) if texts else ""
    r._line = line
    return r


def frame_with_rows(rows, h=1125, w=2000):
    """A frame with bright 'text' on the given rows (0 = newest)."""
    f = np.zeros((h, w, 3), np.uint8)
    for i in rows:
        y = round(R.MSG_BOTTOM * h - i * R.MSG_PITCH * h)
        f[y - 5:y + 5, 1800 - 20 * i:1900] = 255  # rows differ in length
    return f


def test_fast_read_reads_only_rows_with_text_and_caches_them():
    r = make_reader(["You received EXP (+43)", "You received EXP (+30)"])
    s = R.GameState()
    r.read_messages_fast(frame_with_rows([0, 1]), s)
    assert [m.text for m in s.messages] == ["You received EXP (+43)", "You received EXP (+30)"]
    assert s.messages[0].y < s.messages[1].y  # oldest first
    assert len(r.calls) == 2  # empty rows are skipped
    r.read_messages_fast(frame_with_rows([0, 1]), s)
    assert len(r.calls) == 2 and len(s.messages) == 2  # same pixels: no new OCR


def test_full_read_keeps_the_fast_rows_and_measures_them():
    r = make_reader([])
    bottom, pitch = r._msg_layout(np.zeros((1125, 2000, 3), np.uint8))
    ys = [bottom + 3 - i * (pitch + 1) for i in range(9)][::-1]
    r._lines = lambda img: [R.TextLine(f"You received EXP (+{i})", y, 1.0) for i, y in enumerate(ys)]
    s = R.GameState()
    r.read_messages(np.zeros((1125, 2000, 3), np.uint8), s)
    assert r._msg_rows[0] == ys[-1] and abs(r._msg_rows[1] - (pitch + 1)) < 1e-6
    assert [m.text for m in s.messages] == [f"You received EXP (+{i})" for i in range(9 - R.MSG_ROWS, 9)]


def test_full_read_remeasures_after_fast_reads_find_nothing():
    r = make_reader(["garbled", "", "garbled", ""] * 2)
    s = R.GameState()
    frame = np.zeros((1125, 2000, 3), np.uint8)
    bottom, pitch = r._msg_layout(frame)
    far = bottom + 0.7 * pitch  # the rows moved by more than half a row
    r._lines = lambda img: [R.TextLine("You received EXP (+43)", far, 1.0)]
    r.read_messages(frame, s)
    assert r._msg_rows is None  # one full read alone doesn't move the rows that far
    for _ in range(2):
        r.read_messages_fast(frame_with_rows([0, 1]), s)
    r.read_messages(frame, s)
    assert r._msg_rows[0] == far and [m.text for m in s.messages] == ["You received EXP (+43)"]
