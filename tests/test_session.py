from mctracker.kills import Mob
from mctracker.session import Session

MAP_A = [Mob(6, "Stump", 10), Mob(9, "Orange Mushroom", 15)]
MAP_B = [Mob(2, "Snail", 2)]


def test_message_lines_become_party_kills():
    s = Session()
    s.on_map("A", MAP_A)
    s.on_message_lines(["You have gained experience (+10)"])
    s.on_message_lines(["You have gained experience (+10)", "You have acquired Snail Shell.",
                        "You have gained experience (+15)"])
    s.on_message_lines(["You have acquired Snail Shell.", "You have gained experience (+15)"])  # scrolled
    assert s.total_kills == 2
    assert s.kills == {6: 1, 9: 1}
    assert s.message_exp == 25


def test_kills_persist_across_maps():
    s = Session()
    s.on_map("A", MAP_A)
    s.on_message_lines(["You have gained experience (+10)"])
    s.on_map("B", MAP_B)
    s.on_message_lines(["You have gained experience (+2)"])
    assert s.kills == {6: 1, 2: 1}
    assert s.total_kills == 2
