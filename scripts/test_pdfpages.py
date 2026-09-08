#!/usr/bin/env python3
"""Regression tests for pdfpages.py's argument parsing.

    python3 scripts/test_pdfpages.py

Only the pure functions are covered here. Rendering and the text-layer probe
need pypdfium2 and a real PDF, and are exercised by running the tool.
"""

import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pdfpages


class ParsePages(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(pdfpages.parse_pages("4", 6), [4])
        self.assertEqual(pdfpages.parse_pages("1-3", 6), [1, 2, 3])
        self.assertEqual(pdfpages.parse_pages("1,3,5-6", 6), [1, 3, 5, 6])
        self.assertEqual(pdfpages.parse_pages("all", 3), [1, 2, 3])

    def test_overlapping_ranges_are_not_duplicated(self):
        self.assertEqual(pdfpages.parse_pages("1-3,2-4", 6), [1, 2, 3, 4])

    def test_reversed_range_is_read_the_way_it_was_meant(self):
        self.assertEqual(pdfpages.parse_pages("3-1", 6), [1, 2, 3])

    def test_out_of_range_is_refused(self):
        """Silently clamping would render the wrong page and say nothing."""
        with self.assertRaises(SystemExit):
            pdfpages.parse_pages("7", 6)
        with self.assertRaises(SystemExit):
            pdfpages.parse_pages("0", 6)

    def test_nonsense_is_refused(self):
        with self.assertRaises(SystemExit):
            pdfpages.parse_pages("first", 6)


class ParseBand(unittest.TestCase):

    def test_fractions(self):
        self.assertEqual(pdfpages.parse_band("0.22-0.40"), (0.22, 0.40))
        self.assertEqual(pdfpages.parse_band("0-1"), (0.0, 1.0))

    def test_bounds_are_enforced(self):
        for bad in ("0.4-0.2", "0.5-0.5", "-0.1-0.5", "0.5-1.5", "half"):
            with self.assertRaises(SystemExit, msg=bad):
                pdfpages.parse_band(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
