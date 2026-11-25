"""Generate the app icon (src/mctracker/res/icon.png and icon.ico). Original art, theme colours.

    python packaging/make_icon.py

Needs only PySide6. A maple leaf on a sky tile, with a little EXP bar underneath.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "src" / "mctracker" / "res"
sys.path.insert(0, str(ROOT / "src"))
from mctracker import theme  # noqa: E402

# Maple leaf outline on a 100x100 grid, symmetric about x=50 (right half; mirrored below).
_RIGHT = [(50, 6), (58, 22), (66, 17), (65, 33), (80, 28), (75, 44), (92, 42), (80, 58), (88, 66),
          (62, 66), (52, 62), (52, 80)]


def _leaf() -> QPainterPath:
    pts = [QPointF(x, y) for x, y in _RIGHT] + [QPointF(100 - x, y) for x, y in reversed(_RIGHT)]
    path = QPainterPath()
    path.addPolygon(QPolygonF(pts + [pts[0]]))
    return path


def render(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    p.scale(size / 100, size / 100)

    tile = QRectF(3, 3, 94, 94)
    bg = QLinearGradient(0, 3, 0, 97)
    bg.setColorAt(0, QColor(theme.SKY_TOP))
    bg.setColorAt(1, QColor(theme.SKY_BOTTOM))
    p.setBrush(QBrush(bg))
    p.setPen(QPen(QColor(theme.WOOD_DARK), 3))
    p.drawRoundedRect(tile, 20, 20)

    p.save()  # leaf, nudged up to make room for the bar
    p.translate(12, 2)
    p.scale(0.76, 0.76)
    leaf = QLinearGradient(0, 6, 0, 80)
    leaf.setColorAt(0, QColor(theme.ORANGE_LIGHT))
    leaf.setColorAt(1, QColor(theme.ORANGE))
    p.setBrush(QBrush(leaf))
    p.setPen(QPen(QColor(theme.ORANGE_DARK), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(_leaf())
    p.setPen(QPen(QColor(theme.ORANGE_DARK), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(50, 30), QPointF(50, 84))  # midrib + stem
    p.restore()

    bar = QRectF(16, 78, 68, 11)  # EXP bar: cream trough, green fill
    p.setBrush(QColor(theme.CREAM))
    p.setPen(QPen(QColor(theme.WOOD_DARK), 2.4))
    p.drawRoundedRect(bar, 5.5, 5.5)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.LEAF))
    p.drawRoundedRect(QRectF(18.2, 80.2, 43, 6.6), 3.3, 3.3)
    p.end()
    return img


def _png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba.data())


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    pngs = [(s, _png(render(s))) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for s, data in pngs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(head + entries + b"".join(d for _, d in pngs))


if __name__ == "__main__":
    app = QGuiApplication([])
    RES.mkdir(parents=True, exist_ok=True)
    render(256).save(str(RES / "icon.png"))
    write_ico(RES / "icon.ico")
    print("wrote", RES / "icon.png", "and icon.ico")
