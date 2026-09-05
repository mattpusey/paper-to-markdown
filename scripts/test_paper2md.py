#!/usr/bin/env python3
"""Regression tests for paper2md.py.

    python3 scripts/test_paper2md.py

Each test names the defect it pins down. Fixtures live in tests/ next to
this file; they are minimal, compilable LaTeX so they can also be run
through the real pipeline by hand.
"""

import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paper2md


class EscapingRegimeCheck(unittest.TestCase):
    """run_checks() must see control SYMBOLS, not just control words.

    The commonest LaTeX accents are backslash + punctuation, so a scan for
    \\[A-Za-z]+ alone let \\"o, \\'e and \\~n through in silence while
    flagging the rarer \\H{o} and \\ss.
    """

    def check(self, md):
        before = len(paper2md.FLAGS)
        n = paper2md.run_checks(md)
        del paper2md.FLAGS[before:]
        return n

    def test_control_symbol_accents_are_flagged(self):
        for bad in [r'Schr\"{o}dinger', r'Schr\"odinger', r"caf\'{e}", r"Ala\~{n}on",
                    r"na\"ive", r"\=a", r"\.z", r"\`a", r"\^o"]:
            self.assertEqual(self.check(bad), 1, bad)

    def test_control_words_still_flagged(self):
        for bad in [r"Erd\H{o}s", r"\ss", r"\AA", r"\c{c}"]:
            self.assertEqual(self.check(bad), 1, bad)

    def test_legitimate_markdown_escapes_pass(self):
        # NB \_ is deliberately absent: the pre-existing rule flags ANY
        # markdown-visible underscore, escaped or not, and this change does
        # not touch that.
        for good in [r"50\% of cases", r"A \& B", r"\#1", r"\{x\}",
                     "plain prose", "math $\\alpha \\ne \\beta$ inline",
                     "```\nFIGURE 1 \\node raw\n```"]:
            self.assertEqual(self.check(good), 0, good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
