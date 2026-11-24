from mctracker.kills import Mob
from mctracker.session import Session

MAP_A = [Mob(6, "Stump", 10), Mob(9, "Orange Mushroom", 15)]
MAP_B = [Mob(2, "Snail", 2)]


def test_message_lines_become_party_kills():
    s = Session()
    s.on_map("A", MAP_A)
    s.on_message_lines(["You received EXP (+10)"])
    s.on_message_lines(["You received EXP (+10)", "Snail Shell x1 earned. (Etc)",
                        "You received EXP (+15)"])
    s.on_message_lines(["Snail Shell x1 earned. (Etc)", "You received EXP (+15)"])  # scrolled
    assert s.total_kills == 2
    assert s.kills == {6: 1, 9: 1}
    assert s.message_exp == 25


def test_kills_persist_across_maps():
    s = Session()
    s.on_map("A", MAP_A)
    s.on_message_lines(["You received EXP (+10)"])
    s.on_map("B", MAP_B)
    s.on_message_lines(["You received EXP (+2)"])
    assert s.kills == {6: 1, 2: 1}
    assert s.total_kills == 2


def test_party_screenshot_sequence():
    # Lines from the real party screenshot (top -> bottom), y in px.
    s = Session(item_resolver=lambda name: {"Echopus Leg": 4000500}.get(name))
    s.on_map("Primeval Forest I", [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)])
    s.on_message_lines([
        ("Bonus EXP for PARTY (+6)", 770), ("You received EXP (+33)", 792),
        ("Bonus EXP for PARTY (+3)", 814), ("You received EXP (+64)", 836),
        ("Bonus EXP for PARTY (+6)", 858), ("Echopus Leg x1 earned. (Etc)", 880),
    ])
    assert s.total_kills == 2
    assert s.message_exp == 6 + 33 + 3 + 64 + 6
    assert s.loot == {4000500: 1}
