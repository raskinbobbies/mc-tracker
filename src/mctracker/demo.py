"""Fake session feed so the overlay can be tried without the game running.

Simulates a 3-person party: every kill (anyone's) produces an EXP line plus a
party-bonus line, fed through the same message pipeline as the screen reader.
"""

from __future__ import annotations

import random
from collections import deque

from PySide6.QtCore import QObject, QTimer

from .kills import Mob
from .session import Tracker

# maplestory.io MCW 1.2 ids and EXP
MOBS = [Mob(6, "Stump", 10), Mob(7, "Slime", 12), Mob(9, "Orange Mushroom", 15)]
LOOT = [(4000001, "Snail Shell"), (4010000, "Bronze Ore"), (2000001, "Orange Potion")]
USED = [(2000000, "Red Potion"), (2000003, "Blue Potion")]
EXP_TABLE = {lvl: 15 * lvl * lvl for lvl in range(1, 201)}  # placeholder curve
PARTY_SHARE = 0.45
VISIBLE_LINES = 8
LINE_H = 22.0


class DemoFeed(QObject):
    def __init__(self, tracker: Tracker, parent=None):
        super().__init__(parent)
        self.tr = tracker
        tracker.exp_table.update(EXP_TABLE)
        tracker.item_resolver = {n: i for i, n in LOOT}.get
        tracker.on_map("Demo map", MOBS)
        self.level, self.exp = 12, 300
        self.screen: deque[str] = deque(maxlen=VISIBLE_LINES)
        self.timer = QTimer(self, interval=1000, timeout=self.tick)
        self.timer.start()
        self.tick()

    def tick(self) -> None:
        for _ in range(random.randint(2, 4)):  # party kills this second
            mob = random.choice(MOBS)
            gained = max(1, round(mob.exp * 20 * PARTY_SHARE * random.uniform(0.95, 1.05)))
            bonus = max(1, gained // 10)
            self.exp += gained + bonus
            self.screen.append(f"You received EXP (+{gained:,})")
            self.screen.append(f"Bonus EXP for PARTY (+{bonus:,})")
            if random.random() < 0.2:
                _, name = random.choice(LOOT)
                self.screen.append(f"{name} x1 earned. (Etc)")
        n = len(self.screen)
        self.tr.on_message_lines([(text, (i - n) * LINE_H) for i, text in enumerate(self.screen)])
        if self.tr.session and random.random() < 0.2:
            self.tr.session.on_use(*random.choice(USED))

        need = EXP_TABLE[self.level]
        if self.exp >= need:
            self.exp -= need
            self.level += 1
        self.tr.on_status(self.level, self.exp, self.exp / EXP_TABLE[self.level] * 100)
