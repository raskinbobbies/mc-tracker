"""Fake session feed so the overlay can be tried without the game running.

Simulates a 3-person party: every kill (anyone's) produces an EXP message for
you worth a noisy share of the monster's EXP, fed through the same
message-line pipeline the screen reader will use.
"""

from __future__ import annotations

import random
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer

from .kills import Mob
from .session import Session

# maplestory.io MCW 1.2 ids and EXP
MOBS = [Mob(6, "Stump", 10), Mob(7, "Slime", 12), Mob(9, "Orange Mushroom", 15)]
LOOT = [(4000001, "Snail Shell"), (4010000, "Bronze Ore"), (2000001, "Orange Potion")]
USED = [(2000000, "Red Potion"), (2000003, "Blue Potion")]
EXP_TABLE = {lvl: 15 * lvl * lvl for lvl in range(1, 201)}  # placeholder curve
PARTY_SHARE = 0.45
VISIBLE_LINES = 6


class DemoFeed(QObject):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self.s.exp.exp_table = EXP_TABLE
        self.s.on_map("Demo map", MOBS)
        self.level, self.exp = 12, 300
        self.screen = deque(maxlen=VISIBLE_LINES)  # message area
        self.t0 = time.monotonic()
        self.timer = QTimer(self, interval=1000, timeout=self.tick)
        self.timer.start()
        self.tick()

    def tick(self) -> None:
        t = self.t0 + (time.monotonic() - self.t0) * 60  # 60x time for quick rates
        for _ in range(random.randint(2, 5)):  # party kills this second
            mob = random.choice(MOBS)
            gained = max(1, round(mob.exp * 20 * PARTY_SHARE * random.uniform(0.95, 1.05)))
            self.exp += gained
            self.screen.append(f"You have gained experience (+{gained:,})")
            if random.random() < 0.2:
                item_id, name = random.choice(LOOT)
                self.s.on_loot(item_id, name)
                self.screen.append(f"You have acquired {name}.")
            if random.random() < 0.4:
                self.s.on_mesos(random.randint(5, 40))
        if random.random() < 0.2:
            self.s.on_use(*random.choice(USED))
        self.s.on_message_lines(list(self.screen))

        need = EXP_TABLE[self.level]
        if self.exp >= need:
            self.exp -= need
            self.level += 1
        self.s.on_exp(self.level, self.exp, self.exp / EXP_TABLE[self.level] * 100, t)
