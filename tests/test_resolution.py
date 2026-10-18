"""Tests for mod dependency / slugcat inheritance resolution in mapper.py.

Run: python -m unittest discover -s tests
"""
import json
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


def _mod(root, folder, mod_id, requirements=(), name=None):
    p = os.path.join(root, folder)
    _write(os.path.join(p, "modinfo.json"), json.dumps({
        "id": mod_id, "name": name or mod_id,
        "requirements": list(requirements),
    }))
    return p


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self._tmp.name
        self.workshop = os.path.join(t, "workshop")
        self.local = os.path.join(t, "vanilla", "mods")
        self.vanilla = os.path.join(t, "vanilla")
        # Campaign mod: requires dep + msc; dep requires msc (diamond) and a
        # missing mod; cyc requires campaign back (cycle).
        self.camp = _mod(self.workshop, "111", "camp",
                         ["dep", "msc", "nowhere"], "Campaign")
        self.dep = _mod(self.workshop, "222", "dep", ["msc", "cyc"])
        self.cyc = _mod(self.workshop, "333", "cyc", ["camp"])
        self.msc = _mod(self.local, "moreslugcats", "msc")
        _write(os.path.join(self.camp, "slugbase", "camp.json"), json.dumps({
            "id": "Camp", "features": {"world_state": ["Camp", "Saint"]},
        }) + "\n")
        # Scene json with no `features` must be ignored.
        _write(os.path.join(self.camp, "slugbase", "scene.json"), '{"id": "x",}')
        # Map files: camp ships an own-suffix map for sb; msc ships saint +
        # plain sl; dep ships plain sb; vanilla ships plain sl + sb.
        _write(os.path.join(self.camp, "world", "sb", "map_sb-Camp.txt"))
        _write(os.path.join(self.camp, "world", "su", "map_su-saint.txt"))
        _write(os.path.join(self.msc, "world", "sl", "map_sl-saint.txt"))
        _write(os.path.join(self.msc, "world", "sl", "map_sl.txt"))
        _write(os.path.join(self.dep, "world", "sb", "map_sb.txt"))
        _write(os.path.join(self.dep, "world", "hi", "map_hi.txt"))
        _write(os.path.join(self.vanilla, "world", "sl", "map_sl.txt"))
        _write(os.path.join(self.vanilla, "world", "hi", "map_hi.txt"))

    def tearDown(self):
        self._tmp.cleanup()


class TestRequirements(Fixture):
    def test_recursive_order_cycle_and_missing(self):
        index = mapper.index_all_mods(self.workshop, self.local)
        deps, missing, tree = mapper.resolve_requirements([self.camp], index)
        # Topological: every mod sits above the mods it requires (dep above
        # msc even though camp lists msc directly); the diamond duplicate and
        # the cycle back to camp are skipped.
        self.assertEqual(deps, [self.dep, self.cyc, self.msc])
        self.assertEqual(missing, ["nowhere"])
        self.assertTrue(any("MISSING" in line for line in tree))


class TestLoadOrder(Fixture):
    def test_options_parse_and_sort(self):
        opts = os.path.join(self._tmp.name, "options")
        _write(opts, "<x>ModLoadOrder&lt;optB&gt;camp&lt;optD&gt;5&lt;optC&gt;"
                     "msc&lt;optD&gt;9&lt;optC&gt;dep&lt;optD&gt;1&lt;optA&gt;</x>")
        order = mapper.read_mod_load_order(opts)
        self.assertEqual(order, {"camp": 5, "msc": 9, "dep": 1})
        roots, unknown = mapper.sort_by_load_order(
            [self.camp, self.dep, self.cyc, self.msc], order)
        # Higher loadOrder = higher priority; mods without an entry keep
        # their relative order below the ranked ones.
        self.assertEqual(roots, [self.msc, self.camp, self.dep, self.cyc])
        self.assertEqual(unknown, [self.cyc])

    def test_enabled_mods(self):
        opts = os.path.join(self._tmp.name, "options")
        _write(opts, "<x>EnabledMods&lt;optB&gt;camp&lt;optC&gt;MSC&lt;optA&gt;</x>")
        self.assertEqual(mapper.read_enabled_mods(opts), {"camp", "msc"})
        self.assertIsNone(mapper.read_enabled_mods(
            os.path.join(self._tmp.name, "nope")))

    def test_missing_options_file(self):
        self.assertEqual(mapper.read_mod_load_order(
            os.path.join(self._tmp.name, "nope")), {})


class TestSlugcats(Fixture):
    def test_slugbase_chain_absorbs_inherited_suffixes(self):
        camps = mapper.detect_campaign_slugcats([self.camp])
        # `saint` suffix (map_su-saint.txt) belongs to Camp's chain, so it
        # is not reported as a separate campaign.
        self.assertEqual(list(camps), ["Camp"])
        self.assertEqual(camps["Camp"]["chain"], ["Camp", "Saint"])
        self.assertEqual(camps["Camp"]["source"], "slugbase")

    def test_suffix_only_slugcat(self):
        camps = mapper.detect_campaign_slugcats([self.dep, self.msc])
        self.assertEqual(camps["saint"]["chain"], ["saint"])
        self.assertEqual(camps["saint"]["source"], "map suffix")


class TestResolver(Fixture):
    def _resolver(self, chain):
        return mapper.Resolver([self.camp, self.dep, self.msc], self.vanilla,
                               chain)

    def test_inherited_variant_from_lower_layer(self):
        r = self._resolver(["Camp", "Saint"])
        path, _label, variant = r.resolve_region_file("sl", "map_sl.txt")
        self.assertTrue(path.lower().endswith("map_sl-saint.txt"))
        self.assertEqual(variant, "Saint")

    def test_variant_beats_plain_in_higher_layer(self):
        # dep (higher) has plain map_sb.txt, camp has map_sb-Camp.txt.
        r = self._resolver(["Camp", "Saint"])
        path, label, variant = r.resolve_region_file("sb", "map_sb.txt")
        self.assertTrue(path.lower().endswith("map_sb-camp.txt"))
        self.assertEqual((label, variant), ("Campaign", "Camp"))

    def test_plain_falls_back_by_layer_priority(self):
        r = self._resolver(["Camp", "Saint"])
        path, label, variant = r.resolve_region_file("hi", "map_hi.txt")
        self.assertEqual((label, variant), ("dep", None))

    def test_plain_only_skips_variants(self):
        r = self._resolver(["Camp", "Saint"])
        path, label, variant = r.resolve_region_file("sb", "map_sb.txt",
                                                     plain_only=True)
        self.assertTrue(path.lower().endswith("map_sb.txt"))
        self.assertEqual((label, variant), ("dep", None))

    def test_modify_is_not_a_layer(self):
        # modify/ holds merge patches, never whole files.
        _write(os.path.join(self.camp, "modify", "world", "hi", "map_hi.txt"),
               "[MERGE]\n")
        r = self._resolver(None)
        _path, label, _ = r.resolve_region_file("hi", "map_hi.txt")
        self.assertEqual(label, "dep")

    def test_mergedmods_is_top_layer(self):
        merged = os.path.join(self.vanilla, "mergedmods")
        _write(os.path.join(merged, "world", "hi", "map_hi.txt"))
        r = mapper.Resolver([self.camp, self.dep], self.vanilla, None,
                            merged_root=merged)
        path, label, _ = r.resolve_region_file("hi", "map_hi.txt")
        self.assertEqual(label, "mergedmods")
        # Variant-major still applies: an own-suffix file in a mod beats a
        # plain merged one.
        _write(os.path.join(merged, "world", "sb", "map_sb.txt"))
        r = mapper.Resolver([self.camp, self.dep], self.vanilla, ["Camp"],
                            merged_root=merged)
        _p, label, variant = r.resolve_region_file("sb", "map_sb.txt")
        self.assertEqual((label, variant), ("Campaign", "Camp"))

    def test_no_chain_is_plain_only(self):
        r = self._resolver(None)
        path, label, _ = r.resolve_region_file("sl", "map_sl.txt")
        self.assertTrue(path.lower().endswith("map_sl.txt"))
        self.assertEqual(label, "msc")


if __name__ == "__main__":
    unittest.main()
