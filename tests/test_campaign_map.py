"""Tests for campaign-map completion (rooms other mods add to the plain map)
and the closing campaign summary in mapper.py.

Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mapper  # noqa: E402


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


WORLD = """CONDITIONAL LINKS
Camp : HIDEROOM : SB_HID
Other : EXCLUSIVEROOM : SB_EXC
Camp : EXCLUSIVEROOM : SB_MINE
END CONDITIONAL LINKS

ROOMS
SB_A : SB_B
GATE_X_SB : DISCONNECTED, SB_A : GATE
(X-Camp)SB_NOTCAMP : SB_A
(Camp,Other)SB_CAMP : SB_A
(Other)SB_OTHER : SB_A
SB_HID : SB_A
SB_EXC : SB_A
SB_MINE : SB_A
// SB_COMMENT : SB_A
END ROOMS
"""


class TestWorldMapRooms(unittest.TestCase):
    def test_timeline_filters_and_hidden_rooms(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "world_sb.txt")
            _write(p, WORLD)
            # Timeline match is case-insensitive: suffix-only campaigns come
            # lower-cased from file names (map_sl-saint.txt).
            self.assertEqual(mapper.world_map_rooms(p, "camp"),
                             {"SB_A", "GATE_X_SB", "SB_CAMP", "SB_MINE"})


class TestMergePlainRooms(unittest.TestCase):
    def test_adds_kept_rooms_with_shared_offset(self):
        rooms = {"A": (0.0, 0.0, 0, ""), "B": (100.0, 0.0, 0, "")}
        conns = [("A", "B", 1, 1, 2, 2, 0, 0)]
        plain = {"A": (10.0, 20.0, 0, ""), "B": (110.0, 20.0, 0, ""),
                 "C": (50.0, 70.0, 1, "sub"), "H": (0.0, 0.0, 0, "")}
        plain_conns = [("A", "B", 1, 1, 2, 2, 0, 0),
                       ("A", "C", 3, 3, 4, 4, 0, 0),
                       ("C", "Z", 5, 5, 6, 6, 0, 0)]
        added = mapper.merge_plain_rooms(rooms, conns, plain, plain_conns,
                                         keep={"A", "B", "C"})
        self.assertEqual(added, ["C"])
        self.assertEqual(rooms["C"], (40.0, 50.0, 1, "sub"))
        self.assertNotIn("H", rooms)
        # Only connections touching an added room; A-B is not duplicated.
        self.assertEqual(conns, [("A", "B", 1, 1, 2, 2, 0, 0),
                                 ("A", "C", 3, 3, 4, 4, 0, 0),
                                 ("C", "Z", 5, 5, 6, 6, 0, 0)])

    def test_no_shared_rooms_adds_nothing(self):
        rooms = {"A": (0.0, 0.0, 0, "")}
        added = mapper.merge_plain_rooms(rooms, [], {"C": (1.0, 1.0, 0, "")},
                                         [], keep={"C"})
        self.assertEqual(added, [])
        self.assertEqual(list(rooms), ["A"])


ROOM = "\n".join(["ROOM", "2*2|-1|0"] + [""] * 9 + ["1|1|1|1|"]) + "\n"


class TestRenderCampaignMap(unittest.TestCase):
    def test_campaign_map_gets_rooms_from_plain_map(self):
        with tempfile.TemporaryDirectory() as t:
            merged = os.path.join(t, "vanilla", "mergedmods")
            camp = os.path.join(t, "camp")
            mw = os.path.join(merged, "world")
            _write(os.path.join(camp, "world", "sb", "map_sb-Camp.txt"),
                   "SB_A: 0><0><0><0><0><><2><2\n")
            _write(os.path.join(mw, "sb", "map_sb.txt"),
                   "SB_A: 0><0><10><10><0><><2><2\n"
                   "SB_NEW: 0><0><30><10><0><><2><2\n"
                   "SB_HID: 0><0><50><10><0><><2><2\n"
                   "Connection: SB_A,SB_NEW,1,0,0,0,3,1\n")
            _write(os.path.join(mw, "sb", "world_sb.txt"),
                   "CONDITIONAL LINKS\nCamp : HIDEROOM : SB_HID\n"
                   "END CONDITIONAL LINKS\nROOMS\nSB_A : SB_NEW\n"
                   "SB_NEW : SB_A\nSB_HID : SB_A\nEND ROOMS\n")
            for n in ("sb_a", "sb_new", "sb_hid"):
                _write(os.path.join(mw, "sb-rooms", n + ".txt"), ROOM)
            r = mapper.Resolver([camp], os.path.join(t, "vanilla"), ["Camp"],
                                merged_root=merged)
            out = os.path.join(t, "out")
            os.makedirs(out)
            _region, msg = mapper.render_region(r, "sb", out)
            self.assertIn("+1 rooms from map_sb.txt[mergedmods]", msg)
            with open(os.path.join(out, "sb.svg"), encoding="utf-8") as f:
                svg = f.read()
            self.assertIn(">SB_NEW<", svg)
            self.assertNotIn(">SB_HID<", svg)
            # SB_NEW keeps its plain-map distance to SB_A (offset -10,-10).
            self.assertIn('<text x="22.0" y="-2.0"', svg)
            self.assertIn("<line ", svg)


class TestCampaignSummary(unittest.TestCase):
    def test_several_campaigns_list_folders(self):
        lines = mapper.campaign_summary(
            [("A", os.path.join("out", "A")), ("Bee", os.path.join("out", "Bee"))],
            ["A", "Bee"])
        self.assertEqual(lines[0], "campaigns (2), one folder each:")
        self.assertEqual(lines[1], f"  A    -> {os.path.join('out', 'A')}")
        self.assertEqual(len(lines), 3)

    def test_unrendered_offered_campaigns_named(self):
        lines = mapper.campaign_summary([("a", "out")], ["A", "Bee"])
        self.assertEqual(len(lines), 1)
        self.assertIn("also offer Bee", lines[0])

    def test_single_campaign_is_silent(self):
        self.assertEqual(mapper.campaign_summary([("A", "out")], ["A"]), [])
        self.assertEqual(mapper.campaign_summary([], []), [])


if __name__ == "__main__":
    unittest.main()
