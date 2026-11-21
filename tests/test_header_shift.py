"""Header (title/legend) pushes the map down only when it collides.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mapper  # noqa: E402

# Header occupies x 0..100, y 0..72 (y down).
HEADER = [(0.0, 0.0, 100.0, 72.0)]


class HeaderShift(unittest.TestCase):
    def test_no_collision_no_shift(self):
        # Room far to the right of the header, reaching up into its y band.
        self.assertEqual(mapper._header_shift(HEADER, [(500, 10, 600, 200)]), 0.0)

    def test_room_below_header_no_shift(self):
        self.assertEqual(mapper._header_shift(HEADER, [(0, 200, 100, 300)]), 0.0)

    def test_no_elements_no_shift(self):
        self.assertEqual(mapper._header_shift(HEADER, []), 0.0)

    def test_no_header_no_shift(self):
        self.assertEqual(mapper._header_shift([], [(0, 0, 100, 100)]), 0.0)

    def test_collision_shifts_to_clear_with_margin(self):
        # Room top at y=40 -> header bottom (72) must end margin above it.
        s = mapper._header_shift(HEADER, [(50, 40, 150, 200)], margin=6.0)
        self.assertAlmostEqual(s, 72 - (40 - 6))
        # After shifting, header bottom == room top - margin: clear.
        self.assertAlmostEqual(72 - s, 40 - 6)

    def test_worst_offender_wins(self):
        s = mapper._header_shift(HEADER, [(0, 40, 10, 90), (20, 10, 30, 90)],
                                 margin=0.0)
        self.assertAlmostEqual(s, 62.0)

    def test_touching_edges_not_collision(self):
        # Horizontally adjacent (ex0 == hx1) does not count.
        self.assertEqual(mapper._header_shift(HEADER, [(100, 0, 200, 90)]), 0.0)


if __name__ == "__main__":
    unittest.main()
