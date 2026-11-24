"""Game sprites and icons from maplestory.io (MapleStory Classic World data).

Assets are fetched at runtime and cached on disk. They are Nexon's property and
are never committed to this repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API = "https://maplestory.io/api/MCW/1.2"
USER_AGENT = b"mc-tracker/0.1 (+https://github.com/raskinbobbies/mc-tracker)"


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    d = Path(base) / "mc-tracker" / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mob_sprite_path(mob_id: int) -> tuple[str, str]:
    return f"/mob/{mob_id}/render/stand", f"mob_{mob_id}_stand.gif"


def item_icon_path(item_id: int) -> tuple[str, str]:
    return f"/item/{item_id}/icon", f"item_{item_id}.png"


class AssetStore(QObject):
    """Fetches each asset once; callbacks receive the local file path."""

    ready = Signal(str, str)  # key, local path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._dir = cache_dir()
        self._waiting: dict[str, list[Callable[[str], None]]] = {}

    def get(self, api_path: str, filename: str, cb: Callable[[str], None]) -> None:
        local = self._dir / filename
        if local.exists():
            cb(str(local))
            return
        if filename in self._waiting:
            self._waiting[filename].append(cb)
            return
        self._waiting[filename] = [cb]
        req = QNetworkRequest(QUrl(API + api_path))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        reply = self._nam.get(req)
        reply.finished.connect(lambda: self._done(reply, local))

    def mob_sprite(self, mob_id: int, cb: Callable[[str], None]) -> None:
        self.get(*mob_sprite_path(mob_id), cb)

    def item_icon(self, item_id: int, cb: Callable[[str], None]) -> None:
        self.get(*item_icon_path(item_id), cb)

    def _done(self, reply: QNetworkReply, local: Path) -> None:
        cbs = self._waiting.pop(local.name, [])
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        if ok:
            local.write_bytes(bytes(reply.readAll()))
        reply.deleteLater()
        if ok:
            for cb in cbs:
                cb(str(local))
            self.ready.emit(local.name, str(local))
