"""Fake session feed so the overlay can be tried without the game running."""

from __future__ import annotations

import random
import time

from PySide6.QtCore import QObject, QTimer

from .session import Session

# (mob id, name, exp) / (item id, name) in maplestory.io MCW 1.2 data
MOBS = [(6, "Stump", 10), (7, "Slime", 12), (9, "Orange Mushroom", 15)]
LOOT = [(4000001, "Snail Shell"), (4010000, "Bronze Ore"), (2000001, "Orange Potion")]
USED = [(2000000, "Red Potion"), (2000003, "Blue Potion")]
EXP_TABLE = {lvl: 15 * lvl * lvl for lvl in range(1, 201)}  # placeholder curve


class DemoFeed(QObject):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self.s.exp.exp_table = EXP_TABLE
        self.s.map_name = "Demo map"
        self.level, self.exp = 12, 300
        self.t0 = time.monotonic()
        self.timer = QTimer(self, interval=1000, timeout=self.tick)
        self.timer.start()
        self.tick()

    def tick(self) -> None:
        # Speed up time 60x so rates settle quickly.
        t = self.t0 + (time.monotonic() - self.t0) * 60
        for _ in range(random.randint(1, 3)):
            mob_id, name, exp = random.choice(MOBS)
            self.s.on_kill(mob_id, name)
            self.exp += exp * 20
            if random.random() < 0.3:
                self.s.on_loot(*random.choice(LOOT))
            if random.random() < 0.5:
                self.s.on_mesos(random.randint(5, 40))
        if random.random() < 0.2:
            self.s.on_use(*random.choice(USED))
        need = EXP_TABLE[self.level]
        if self.exp >= need:
            self.exp -= need
            self.level += 1
        self.s.on_exp(self.level, self.exp, self.exp / EXP_TABLE[self.level] * 100, t)
