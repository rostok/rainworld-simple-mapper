"""Tests for mapdiff.py: mini ModMerger, region diff metrics, copy detection,
and the --show-changed SVG tinting in mapper.render_region.

Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mapdiff  # noqa: E402
import mapper  # noqa: E402


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def room_text(name, w, h, solid=()):
    """Minimal room file: header on line 1, tiles on line 11 (x-major,
    y from h-1 down, like Room.cs)."""
    solid = set(solid)
    entries = []
    for x in range(w):
        for y in range(h - 1, -1, -1):
            entries.append("1" if (x, y) in solid else "0")
    lines = [name, f"{w}*{h}|-1|0"] + [""] * 9 + ["|".join(entries) + "|"]
    return "\n".join(lines) + "\n"


BORDER = lambda w, h: {(x, y) for x in range(w) for y in range(h)
                       if x in (0, w - 1) or y in (0, h - 1)}


class PatchTests(unittest.TestCase):
    def test_findlinestart_replace_nth(self):
        p = mapdiff.Patch(["[FINDLINESTART]DS_A05 :", "[REPLACE_2]DS_A05 : 3-Snail"])
        out = p.apply_modifications(["DS_A05 : 1-Bat", "X : 1", "DS_A05 : 2-Bat"])
        self.assertEqual(out, ["DS_A05 : 1-Bat", "X : 1", "DS_A05 : 3-Snail"])

    def test_find_substring_and_add(self):
        p = mapdiff.Patch(["[FIND]Green", "[REPLACE]Pink", "[ADD]NEW LINE"])
        out = p.apply_modifications(["a : 4-Green", "", "b : 5-Green"])
        # blank lines dropped, like ApplyModifications
        self.assertEqual(out, ["a : 4-Pink", "b : 5-Pink", "NEW LINE"])

    def test_find_word_occurrence(self):
        p = mapdiff.Patch(["[FIND]A", "[REPLACE_2]Z"])
        self.assertEqual(p.apply_modifications(["A B A A"]), ["A B Z A"])

    def test_merge_block_collected(self):
        p = mapdiff.Patch(["[MERGE]", "ROOMS", "X : Y", "END ROOMS[ENDMERGE]", "[ADD]tail"])
        self.assertEqual(p.merge_lines, ["ROOMS", "X : Y", "END ROOMS"])
        self.assertEqual(p.add_at_end, ["tail"])


class MergeTests(unittest.TestCase):
    def test_world_merge_rooms_and_creatures(self):
        base = ["ROOMS", "A : B, DISCONNECTED", "B : A", "END ROOMS",
                "CREATURES", "A : 1-Green", "(Saint)B : 2-Pink", "END CREATURES"]
        merge = ["ROOMS", "A : B, C", "C : A : SHELTER", "END ROOMS",
                 "CREATURES", "A : 1-Red", "END CREATURES"]
        w = mapdiff.WorldFile(mapdiff.merge_world(base, merge))
        rooms = {r.name: (r.conns, r.special) for r in w.rooms}
        self.assertEqual(rooms["A"][0], ["B", "C"])
        self.assertEqual(rooms["C"], (["A"], "SHELTER"))
        spawns = [str(c) for c in w.creatures]
        self.assertIn("A : 1-Red", spawns)
        self.assertNotIn("A : 1-Green", spawns)
        self.assertIn("(Saint)B : 2-Pink", spawns)

    def test_settings_placed_objects_union(self):
        base = ["Palette: 1", "PlacedObjects: Rock><1><2><, Spear><3><4><, "]
        merge = ["Palette: 7", "PlacedObjects: Spear><3><4><, DataPearl><5><6><"]
        out = mapdiff.merge_settings(base, merge)
        self.assertIn("Palette: 7", out)
        objs = [l for l in out if l.startswith("PlacedObjects")][0]
        self.assertEqual(objs.count("Spear"), 1)
        self.assertIn("DataPearl", objs)
        self.assertIn("Rock", objs)

    def test_properties_merge(self):
        base = ["Palette: 1", "Room_Attr: SU_A01: Lizard-Like", "Subregion: Outskirts"]
        merge = ["Room_Attr: SU_A01: Lizard-Avoid", "Subregion: Drainage"]
        out = mapdiff.merge_unique_items(base, merge, "properties.txt")
        self.assertIn("Room_Attr: SU_A01: Lizard-Avoid", out)
        self.assertIn("Subregion: Outskirts", out)
        self.assertIn("Subregion: Drainage", out)

    def test_map_merge(self):
        base = ["A: 1><2><10><20><0><", "B: 1><2><30><20><0><",
                "Connection: A,B,1,1,2,2,0,0"]
        merge = ["B: 1><2><99><20><0><", "C: 1><2><50><50><1><",
                 "Connection: B,A,3,3,4,4,1,1"]
        out = mapdiff.merge_map(base, merge)
        self.assertIn("B: 1><2><99><20><0><", out)
        self.assertIn("C: 1><2><50><50><1><", out)
        self.assertEqual(sum(1 for l in out if l.startswith("Connection")), 1)

    def test_map_merge_skips_unrelated_section(self):
        out = mapdiff.merge_map(["A: 1><2><10><20><0><"], ["Z: 1><2><3><4><0><"])
        self.assertEqual(out, ["A: 1><2><10><20><0><"])


class Fixture(unittest.TestCase):
    """vanilla (SU: A, B, C) + DLC patching SU world + a 'mod' layer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self._tmp.name
        self.vanilla = os.path.join(t, "sa")
        self.dlc = os.path.join(t, "sa", "mods", "dlc")
        self.mod = os.path.join(t, "mod")
        V = os.path.join(self.vanilla, "world")
        _write(os.path.join(V, "su", "world_su.txt"),
               "ROOMS\nSU_A : SU_B\nSU_B : SU_A, SU_C\nSU_C : SU_B\nEND ROOMS\n"
               "CREATURES\nSU_A : 1-Green\nEND CREATURES\n")
        _write(os.path.join(V, "su", "map_su.txt"),
               "SU_A: 0><0><0><0><0><\nSU_B: 0><0><100><0><0><\nSU_C: 0><0><200><0><0><\n")
        _write(os.path.join(V, "su", "properties.txt"), "Palette: 1\n")
        for n, (w, h) in {"su_a": (10, 8), "su_b": (12, 8), "su_c": (9, 9)}.items():
            _write(os.path.join(V, "su-rooms", n + ".txt"), room_text(n, w, h, BORDER(w, h)))
        # DLC: adds a creature to SU_B via [MERGE]; not a world change for the mod diff
        _write(os.path.join(self.dlc, "modify", "world", "su", "world_su.txt"),
               "[MERGE]\nCREATURES\nSU_B : 2-Pink\nEND CREATURES\n[ENDMERGE]\n")
        self.base_merged, _ = mapdiff.build_baseline_merged(
            [self.dlc], self.vanilla, os.path.join(t, "cache", "baseline"))
        # "Real" mergedmods for the modded side: same DLC patch result
        self.merged = os.path.join(t, "merged")
        import shutil
        shutil.copytree(self.base_merged, self.merged)

    def tearDown(self):
        self._tmp.cleanup()

    def resolvers(self, mod_roots=()):
        mod = mapper.Resolver(list(mod_roots) + [self.dlc], self.vanilla, None, self.merged)
        base = mapper.Resolver([self.dlc], self.vanilla, None, self.base_merged)
        return mod, base


class BaselineTests(Fixture):
    def test_dlc_patch_baked(self):
        p = os.path.join(self.base_merged, "world", "su", "world_su.txt")
        self.assertTrue(os.path.isfile(p))
        rooms, cr = mapdiff.parse_world(p, "White")
        self.assertEqual(cr[("SU_B", "Pink")], 1)
        self.assertEqual(cr[("SU_A", "Green")], 1)
        self.assertIn("SU_C", rooms)

    def test_unchanged_region_is_same(self):
        mod, base = self.resolvers()
        d = mapdiff.diff_region(mod, base, "su")
        self.assertEqual(d["class"], "same")
        self.assertEqual(d["score"], 0)


class DiffTests(Fixture):
    def setUp(self):
        super().setUp()
        M = os.path.join(self.mod, "world")
        # geometry change in SU_B (extra solid tiles), SU_C moved, SU_A creatures,
        # new SU_D = copy of vanilla SU_A, new SU_E original
        w, h = 12, 8
        _write(os.path.join(M, "su-rooms", "su_b.txt"),
               room_text("su_b", w, h, BORDER(w, h) | {(5, 3), (6, 3), (7, 3)}))
        _write(os.path.join(M, "su-rooms", "su_d.txt"), room_text("su_d", 10, 8, BORDER(10, 8)))
        _write(os.path.join(M, "su-rooms", "su_e.txt"),
               room_text("su_e", 30, 5, {(x, y) for x in range(30) for y in range(5) if (x + y) % 3 == 0}))
        # the mod's own world file replaces the merged one only via mergedmods; write it there
        _write(os.path.join(self.merged, "world", "su", "world_su.txt"),
               "ROOMS\nSU_A : SU_B, SU_D\nSU_B : SU_A, SU_C\nSU_C : SU_B\nSU_D : SU_A\nSU_E : DISCONNECTED\nEND ROOMS\n"
               "CREATURES\nSU_A : 1-Green, 2-Red\nSU_B : 2-Pink\nEND CREATURES\n")
        _write(os.path.join(M, "su", "map_su.txt"),
               "SU_A: 0><0><0><0><0><\nSU_B: 0><0><100><0><0><\nSU_C: 0><0><250><0><0><\n"
               "SU_D: 0><0><0><100><0><\nSU_E: 0><0><100><100><0><\n")
        self.index = mapdiff.build_room_index([os.path.join(self.vanilla, "world")])

    def test_room_statuses(self):
        mod, base = self.resolvers([self.mod])
        d = mapdiff.diff_region(mod, base, "su", self.index)
        r = d["rooms"]
        self.assertEqual(r["SU_A"]["status"], "content")
        self.assertEqual(r["SU_A"]["creatures"], {"Red": 1})
        self.assertEqual(r["SU_B"]["status"], "geometry")
        self.assertAlmostEqual(r["SU_B"]["geometry"], 3 / 96, places=3)
        self.assertEqual(r["SU_C"]["status"], "moved")
        self.assertEqual(r["SU_D"]["status"], "new-copy")
        self.assertEqual(r["SU_D"]["from"], "SU_A")
        self.assertEqual(r["SU_E"]["status"], "new")
        self.assertIn(d["class"], ("tweaked", "reworked"))
        self.assertGreater(d["dims"]["rooms"], 0)
        self.assertGreater(d["dims"]["connections"], 0)
        self.assertEqual(d["origins"], {"su": 1, "original": 1})

    def test_whole_map_shift_is_not_a_move(self):
        _write(os.path.join(self.mod, "world", "su", "map_su.txt"),
               "SU_A: 0><0><500><40><0><\nSU_B: 0><0><600><40><0><\nSU_C: 0><0><700><40><0><\n")
        mod, base = self.resolvers([self.mod])
        d = mapdiff.diff_region(mod, base, "su", self.index)
        self.assertEqual(d["dims"]["layout"], 0.0)
        self.assertNotEqual(d["rooms"]["SU_C"]["status"], "moved")

    def test_removed_room(self):
        _write(os.path.join(self.merged, "world", "su", "world_su.txt"),
               "ROOMS\nSU_A : SU_B\nSU_B : SU_A\nEND ROOMS\n"
               "CREATURES\nSU_A : 1-Green\nSU_B : 2-Pink\nEND CREATURES\n")
        mod, base = self.resolvers()
        d = mapdiff.diff_region(mod, base, "su", self.index)
        self.assertEqual(d["removed"], ["SU_C"])
        self.assertEqual(d["base_map"]["SU_C"][0], 200.0)

    def test_new_region(self):
        _write(os.path.join(self.mod, "world", "xx", "map_xx.txt"), "XX_A: 0><0><0><0><0><\n")
        _write(os.path.join(self.mod, "world", "xx", "world_xx.txt"), "ROOMS\nXX_A : DISCONNECTED\nEND ROOMS\n")
        _write(os.path.join(self.mod, "world", "xx-rooms", "xx_a.txt"), room_text("xx_a", 9, 9, BORDER(9, 9)))
        mod, base = self.resolvers([self.mod])
        d = mapdiff.diff_region(mod, base, "xx", self.index)
        self.assertEqual(d["class"], "new")
        self.assertEqual(d["rooms"]["XX_A"]["status"], "new-copy")
        self.assertEqual(d["rooms"]["XX_A"]["from"], "SU_C")

    def test_derived_match(self):
        w, h = 20, 20
        base_solid = BORDER(w, h)
        sig_a = mapdiff.room_signature((w, h, [(x, y, 1, False, False) for x, y in base_solid]))
        sig_b = mapdiff.room_signature((w, h, [(x, y, 1, False, False)
                                               for x, y in base_solid | {(10, 10)}]))
        idx = {"exact": {}, "rooms": [("R", "su", w, h, sig_a[3])]}
        hit = mapdiff.match_room(sig_b, idx)
        self.assertEqual(hit[0], "derived")
        self.assertGreaterEqual(hit[3], mapdiff.DERIVED_MIN)

    def test_report_lines(self):
        mod, base = self.resolvers([self.mod])
        d = mapdiff.diff_region(mod, base, "su", self.index)
        lines = mapdiff.format_report([d])
        self.assertTrue(lines[0].startswith("REGION"))
        self.assertTrue(any(l.startswith("su ") for l in lines))
        self.assertTrue(any("summary:" in l for l in lines))


class SvgTests(DiffTests):
    def _render(self, changes):
        mod, _ = self.resolvers([self.mod])
        mod.style = "filled"
        mod.palette = mapper.PALETTES["filled"]
        out = os.path.join(self._tmp.name, "out")
        os.makedirs(out, exist_ok=True)
        mapper.render_region(mod, "su", out, changes=changes)
        with open(os.path.join(out, "su.svg"), encoding="utf-8") as f:
            return f.read()

    def test_no_tint_without_show_changed(self):
        svg = self._render(None)
        self.assertNotIn('id="changes"', svg)
        self.assertNotIn(mapdiff.STATUS_STYLE["geometry"][0], svg)

    def test_tint_and_legend(self):
        mod, base = self.resolvers([self.mod])
        d = mapdiff.diff_region(mod, base, "su", self.index)
        svg = self._render(d)
        self.assertIn('id="legend"', svg)
        self.assertIn(mapdiff.STATUS_STYLE["geometry"][0], svg)
        self.assertIn(mapdiff.STATUS_STYLE["new-copy"][0], svg)
        self.assertIn("SU_B: geometry changed", svg)


if __name__ == "__main__":
    unittest.main()
