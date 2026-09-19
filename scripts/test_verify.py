#!/usr/bin/env python3
"""Regression tests for verify.py.

    python3 scripts/test_verify.py

Each test names the defect it pins down.
"""

import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify


class FloatsInCitedPapers(unittest.TestCase):
    """"Table 1 of [1]" names a table in someone ELSE's paper.

    Reported as "referenced but absent", it failed a conversion that had
    nothing wrong with it, and no edit to the markdown could ever clear it.
    """

    def missing(self, md):
        report, _ = verify.check_floats(md)
        return (report["referenced_but_absent_figures"],
                report["referenced_but_absent_tables"])

    def test_a_table_in_a_cited_paper_is_not_this_papers_to_caption(self):
        self.assertEqual(self.missing("The two open cells of Table 1 of [1] are crosses."),
                         ([], []))

    def test_author_names_may_stand_between_the_float_and_the_citation(self):
        self.assertEqual(
            self.missing("Table 1 of Schmid, Rosset and Buscemi [1] is open."),
            ([], []))

    def test_a_ref_qualifier_works_too(self):
        self.assertEqual(self.missing("See Fig. 2 of Ref. [3] for the layout."),
                         ([], []))

    def test_this_papers_own_float_is_still_checked(self):
        figs, tabs = self.missing("As Table 2 in the appendix [4] shows, it holds.")
        self.assertEqual(tabs, ["2"])

    def test_a_reference_to_a_float_that_exists_still_resolves(self):
        md = "**Table 1:** A caption.\n\nAs Table 1 shows, it holds."
        self.assertEqual(self.missing(md), ([], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
