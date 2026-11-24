"""Tooltip-box detection on synthetic frames (no OCR models needed)."""

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from mctracker.vision.reader import TextLine  # noqa: E402
from mctracker.vision.tooltip import BOX_THRESHOLDS, BoxThresholds, find_tooltip_boxes, hovered_item_names, strip_enhancement  # noqa: E402

NAVY = (140, 50, 50)  # BGR of the tooltip tint
ALPHA = 0.75  # the panel is semi-opaque: the scene shows through a little


@pytest.fixture
def thresholds() -> BoxThresholds:
    """The box-detection thresholds under test (calibrated on a 2560x1440 capture)."""
    return BOX_THRESHOLDS


def scenery(w: int, h: int) -> np.ndarray:
    """Tan rock with noise, a dark band and a light sky patch: busy, like the game."""
    rng = np.random.default_rng(1)
    img = np.empty((h, w, 3), np.uint8)
    img[:] = (90, 140, 190)
    img[: h // 4] = (235, 225, 210)  # pale sky
    img[h // 2:] = (60, 80, 110)  # dark rock
    noise = rng.integers(-12, 12, img.shape)
    return np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)


def panel(img, x0, y0, x1, y1, title, scale=1.0, divider=True) -> None:
    roi = img[y0:y1, x0:x1].astype(np.float32)
    img[y0:y1, x0:x1] = (ALPHA * np.array(NAVY) + (1 - ALPHA) * roi).astype(np.uint8)
    cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), (215, 215, 215), 2)  # light border
    cv2.putText(img, title, (x0 + int(30 * scale), y0 + int(38 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                1.1 * scale, (255, 255, 255), 2)
    for i in range(5):  # stat lines: small text
        cv2.putText(img, f"Type: Dagger {i}", (x0 + int(20 * scale), y0 + int((120 + 40 * i) * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7 * scale, (255, 255, 255), 1)
    cv2.rectangle(img, (x0 + int(20 * scale), y0 + int(60 * scale)), (x0 + int(100 * scale), y0 + int(110 * scale)),
                  (120, 220, 240), -1)  # item icon
    if divider:  # the line under the class row: not panel-coloured, so it cuts the rows into two bands (the panel stays one blob: the line stops short of the right edge)
        cv2.rectangle(img, (x0 + 2, y0 + int(49 * scale)), (x0 + int(0.92 * (x1 - x0)), y0 + int(49 * scale) + max(6, int(7 * scale))),
                      (200, 200, 200), -1)


def name_tag(img, cx, cy, text, scale=1.0) -> None:
    """Dark grey label with white text, like a pet/monster name tag."""
    cv2.rectangle(img, (cx - int(70 * scale), cy - int(18 * scale)), (cx + int(70 * scale), cy + int(14 * scale)),
                  (70, 70, 70), -1)
    cv2.putText(img, text, (cx - int(60 * scale), cy + int(8 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.8 * scale, (255, 255, 255), 2)


def build_frame(w=2000, h=1125, body=345):
    """Two adjacent tooltips (hovered on the left) plus a name tag above them.

    `body` is the left panel's height; the header above the divider is only ~50px,
    so any realistic body is taller than the header.
    """
    s = w / 2000
    img = scenery(w, h)
    left = (int(1260 * s), int(500 * s), int(1625 * s), int((500 + body) * s))
    right = (int(1632 * s), int(500 * s), int(1998 * s), int((500 + body - 25) * s))
    panel(img, *left, "Korean Fan", s)
    panel(img, *right, "Bazlud (+7)", s)
    tag = (int(1300 * s), int(340 * s))
    name_tag(img, *tag, "Black Pig", s)
    return img, left, right, tag


class FakeReader:
    """Stands in for RapidOCR: knows where each text was drawn and what it was asked to read."""

    def __init__(self, frame, texts):
        self.frame, self.texts = frame, texts  # texts: [((x0, y0, x1, y1), text, height)]
        self.asked: list[tuple[int, int, int, int]] = []

    def _origin(self, img):
        off = img.__array_interface__["data"][0] - self.frame.__array_interface__["data"][0]
        return (off % self.frame.strides[0]) // 3, off // self.frame.strides[0]

    def _lines(self, img, upscale=1.0):
        ox, oy = self._origin(img)
        h, w = img.shape[:2]
        self.asked.append((ox, oy, ox + w, oy + h))
        out = []
        for (x0, y0, x1, y1), text, height in self.texts:
            if ox <= x0 and x1 <= ox + w and oy <= y0 and y1 <= oy + h:
                out.append(TextLine(text, (y0 + y1) / 2 - oy, 0.9, x0 - ox, x1 - ox, height))
        return out


def texts_for(left, right, tag, s=1.0):
    def at(box, dy, text, height):
        return ((box[0] + 30, box[1] + int(dy * s), box[0] + 200, box[1] + int(dy * s) + 24), text, height)

    return [
        ((tag[0] - 60, tag[1] - 12, tag[0] + 60, tag[1] + 12), "Black Pig", 26),
        at(left, 14, "Korean Fan", 28),
        at(left, 120, "Type: Dagger", 16),
        at(right, 14, "Bazlud (+7)", 28),
        at(right, 120, "Type: Dagger", 16),
    ]


ITEMS = {"Korean Fan": 1, "Bazlud": 2, "Black Pig": 3}  # Black Pig is a pet item too


@pytest.mark.parametrize("w,h", [(2000, 1125), (2560, 1440), (1920, 1080)])
def test_finds_both_panels_left_to_right_and_ignores_name_tag(thresholds, w, h):
    img, left, right, tag = build_frame(w, h)
    boxes = find_tooltip_boxes(img, thresholds)
    assert len(boxes) == 2
    for found, want in zip(boxes, (left, right)):
        assert all(abs(a - b) <= 0.01 * w for a, b in zip(found[:3], want[:3]))  # x0, y0, x1
    assert boxes[0][0] < boxes[1][0]
    assert not any(b[0] <= tag[0] <= b[2] and b[1] <= tag[1] <= b[3] for b in boxes)


@pytest.mark.parametrize("w,h", [(2000, 1125), (2560, 1440)])
@pytest.mark.parametrize("body", [200, 345, 420])
def test_weak_divider_does_not_move_the_panel_top(w, h, body):
    img, left, right, _ = build_frame(w, h, body)
    boxes = find_tooltip_boxes(img)
    assert len(boxes) == 2
    for found, want in zip(boxes, (left, right)):
        assert abs(found[1] - want[1]) <= 0.01 * h  # top is the header's, not the divider's


@pytest.mark.parametrize("w,h", [(2000, 1125), (2560, 1440)])
def test_hovered_title_read_with_long_body_at_both_scales(w, h):
    img, left, right, tag = build_frame(w, h, body=420)
    s = w / 2000
    reader = FakeReader(img, texts_for(left, right, tag, s))
    names = hovered_item_names(reader, img, cursor=(left[0] - 5, left[1] + 10), resolve=ITEMS.get)
    assert names == [("Korean Fan", 1), ("Bazlud", 2)]


def test_unreadable_hovered_title_is_a_placeholder_not_the_comparison_item():
    img, left, right, tag = build_frame()
    texts = [t for t in texts_for(left, right, tag) if t[1] != "Korean Fan"]  # OCR misses the hovered title
    names = hovered_item_names(FakeReader(img, texts), img, resolve=ITEMS.get)
    assert names == [("", None), ("Bazlud", 2)]


def test_false_panel_with_unresolvable_label_is_not_the_hovered_item():
    img = scenery(2000, 1125)
    false_box = (500, 400, 865, 745)  # blue-tinted backdrop: panel-coloured, name-like label, no such item
    real = (1000, 500, 1365, 845)
    panel(img, *false_box, "Blue Backdrop")
    panel(img, *real, "Bazlud (+7)")
    texts = [
        ((false_box[0] + 30, false_box[1] + 14, false_box[0] + 200, false_box[1] + 38), "Blue Backdrop", 28),
        ((real[0] + 30, real[1] + 14, real[0] + 200, real[1] + 38), "Bazlud (+7)", 28),
    ]
    names = hovered_item_names(FakeReader(img, texts), img, cursor=(990, 520), resolve=ITEMS.get)
    assert names[0] == ("Bazlud", 2)
    assert ("Blue Backdrop", None) in names[1:]  # kept only as a low-priority leftover


def test_nearest_resolved_panel_to_the_cursor_wins_over_a_resolved_false_panel():
    img = scenery(2000, 1125)
    far = (300, 400, 665, 745)  # e.g. a second window that happens to read as an item
    near = (1000, 500, 1365, 845)
    panel(img, *far, "Black Pig")
    panel(img, *near, "Korean Fan")
    texts = [
        ((far[0] + 30, far[1] + 14, far[0] + 200, far[1] + 38), "Black Pig", 28),
        ((near[0] + 30, near[1] + 14, near[0] + 200, near[1] + 38), "Korean Fan", 28),
    ]
    names = hovered_item_names(FakeReader(img, texts), img, cursor=(990, 520), resolve=ITEMS.get)
    assert names[0] == ("Korean Fan", 1)


def test_cursor_area_fallback_when_no_panel_title_resolves():
    img = scenery(2000, 1125)
    box = (500, 400, 865, 745)
    panel(img, *box, "Blue Backdrop")
    name_tag(img, 1000, 900, "Black Pig")
    texts = [
        ((box[0] + 30, box[1] + 14, box[0] + 200, box[1] + 38), "Blue Backdrop", 28),
        ((940, 888, 1060, 912), "Black Pig", 26),
    ]
    names = hovered_item_names(FakeReader(img, texts), img, cursor=(1000, 920), resolve=ITEMS.get)
    assert names[0] == ("Black Pig", 3)


def test_reads_hovered_title_not_the_name_tag():
    img, left, right, tag = build_frame()
    reader = FakeReader(img, texts_for(left, right, tag))
    names = hovered_item_names(reader, img, cursor=(1300, 600), resolve=ITEMS.get)
    assert names == [("Korean Fan", 1), ("Bazlud", 2)]  # hovered first, "(+7)" stripped, no Black Pig
    boxes = find_tooltip_boxes(img)
    for ax0, ay0, ax1, ay1 in reader.asked:  # OCR only ever saw a strip inside a panel
        assert any(b[0] <= ax0 and ax1 <= b[2] and b[1] <= ay0 and ay1 <= b[3] for b in boxes)
    assert all(ay1 - ay0 < 0.06 * img.shape[0] for _, ay0, _, ay1 in reader.asked)  # just the title row


def test_single_tooltip():
    img = scenery(2000, 1125)
    box = (700, 300, 1065, 645)
    panel(img, *box, "Korean Fan")
    assert len(find_tooltip_boxes(img)) == 1


def test_no_panels_in_plain_scenery():
    assert find_tooltip_boxes(scenery(2000, 1125)) == []
    img = scenery(2000, 1125)
    name_tag(img, 1000, 500, "Black Pig")
    cv2.rectangle(img, (100, 100), (700, 500), (200, 150, 100), -1)  # a big pale-blue window-ish block
    assert find_tooltip_boxes(img) == []


def test_falls_back_to_cursor_area_when_no_panel_found():
    img = scenery(2000, 1125)
    name_tag(img, 1000, 500, "Black Pig")
    texts = [((940, 488, 1060, 512), "Black Pig", 26)]
    names = hovered_item_names(FakeReader(img, texts), img, cursor=(1000, 520), resolve=ITEMS.get)
    assert names == [("Black Pig", 3)]


def test_threshold_fixture_values(thresholds):
    t = thresholds
    assert 0 < t.min_blue_lead < 40 and t.min_blue < t.max_blue
    assert 0.05 <= t.min_width < t.max_width <= 0.5
    assert 0.05 <= t.min_height < t.max_height <= 0.8
    assert 0.5 <= t.min_fill <= 0.95
    assert 0.02 <= t.title_height <= 0.07
    # The panel tint used by these synthetic frames passes the colour test.
    b, g, r = (ALPHA * c + (1 - ALPHA) * s for c, s in zip(NAVY, (235, 225, 210)))
    assert b - max(g, r) >= t.min_blue_lead and t.min_blue <= b <= t.max_blue


@pytest.mark.parametrize("text,want", [
    ("Bazlud (+7)", "Bazlud"),
    ("Bazlud (+12)", "Bazlud"),
    ("Bazlud (+7", "Bazlud"),
    ("Bazlud [+3]", "Bazlud"),
    ("Korean Fan", "Korean Fan"),
    ("Maple Warrior (Lv. 3)", "Maple Warrior (Lv. 3)"),
])
def test_strip_enhancement(text, want):
    assert strip_enhancement(text) == want
