"""When to nag about the minimap: the reader's status, held long enough to be real.

No Qt in here, so the rule is easy to test with injected times.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

HOLD_S = 10.0  # a bad status must hold this long, unbroken, before the prompt shows

MESSAGES = {
    "missing": "Open the minimap (top left) so the map can be read",
    "names_hidden": "Turn on map names in the minimap",
}


class MinimapPrompt:
    """Turns a stream of minimap statuses into the prompt to show (None: nothing).

    Only "missing" and "names_hidden" ask for something. They show after holding for
    ``HOLD_S`` seconds; "ok" and "unknown" (login, loading, Cash Shop) clear it at once.
    """

    def __init__(self, hold_s: float = HOLD_S):
        self.hold_s = hold_s
        self._status: str | None = None  # the bad status being timed
        self._since = 0.0

    def update(self, status: str, now: float) -> str | None:
        """Feed the current status; returns the prompt text to show, or None."""
        if status not in MESSAGES:
            self._status = None
            return None
        if status != self._status:
            self._status, self._since = status, now
        return MESSAGES[status] if now - self._since >= self.hold_s else None


class MinimapPromptRelay:
    """Feeds statuses through a ``MinimapPrompt`` and hands the result to every window's setter."""

    def __init__(self, setters: Iterable[Callable[[str | None], None]], clock: Callable[[], float] = time.monotonic):
        self.prompt = MinimapPrompt()
        self.setters = list(setters)
        self.clock = clock

    def on_status(self, status: str) -> None:
        text = self.prompt.update(status, self.clock())
        for set_prompt in self.setters:
            set_prompt(text)
