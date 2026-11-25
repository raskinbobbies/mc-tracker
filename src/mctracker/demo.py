"""Fake session feed so the overlay can be tried without the game running.

Simulates grinding with a 3-person party: solo kills produce a fixed EXP line;
party kills (anyone's) produce a varying share plus a party-bonus line. Lines go
through the same message pipeline as the screen reader.
"""

from __future__ import annotations

import random
from collections import deque

from PySide6.QtCore import QObject, QTimer

from .exp_table import EXP_TABLE, bar_percent
from .kills import Mob
from .session import Tracker

# maplestory.io MCW 1.2 ids. The EXP values are made up. Party shares only count
# for the top mob, in a window above anything the second mob could give under an
# EXP event (see KillCounter.identify), so the top mob sits well clear of the rest.
# Stump and Slime are a close pair: their shares can't be told apart and stay in
# the "unknown (shared)" footer.
MOBS = [Mob(6, "Stump", 35), Mob(7, "Slime", 40), Mob(9, "Orange Mushroom", 100)]
EXP_RATE = 1  # 1x: lines carry each monster's listed EXP
SOLO_SHARE = 0.4  # fraction of kills made alone (no party bonus)
LOOT = [(4000001, "Snail Shell"), (4010000, "Bronze Ore"), (2000001, "Orange Potion")]
USED = [(2000000, "Red Potion"), (2000003, "Blue Potion")]
PARTY_SHARE = 0.95  # a share near full value, as in a small party
VISIBLE_LINES = 8
LINE_H = 22.0


class DemoFeed(QObject):
    def __init__(self, tracker: Tracker, parent=None):
        super().__init__(parent)
        self.tr = tracker
        tracker.item_resolver = {n: i for i, n in LOOT}.get
        for item_id, name in USED:  # demo shop prices
            tracker.prices.set(str(item_id), name, 50 if item_id == 2000000 else 200)
        tracker.on_map("Demo map", MOBS)
        self.level, self.exp = 10, 1_200
        self.screen: deque[str] = deque(maxlen=VISIBLE_LINES)
        self.timer = QTimer(self, interval=1000, timeout=self.tick)
        self.timer.start()
        self.tick()

    def tick(self) -> None:
        for _ in range(random.randint(2, 4)):  # party kills this second
            mob = random.choice(MOBS)
            if random.random() < SOLO_SHARE:
                gained = mob.exp * EXP_RATE  # solo EXP is the same every kill
                self.exp += gained
                self.screen.append(f"You received EXP (+{gained:,})")
            else:
                share = PARTY_SHARE * random.uniform(0.95, 1.05)
                gained = max(1, round(mob.exp * EXP_RATE * share))
                bonus = max(1, gained // 10)
                self.exp += gained + bonus
                self.screen.append(f"You received EXP (+{gained:,})")
                self.screen.append(f"Bonus EXP for PARTY (+{bonus:,})")
            if random.random() < 0.5:
                self.screen.append(f"You have gained mesos (+{random.randint(8, 40)})")
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
        self.tr.on_status(self.level, self.exp, bar_percent(self.exp, EXP_TABLE[self.level]))
