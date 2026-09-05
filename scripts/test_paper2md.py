#!/usr/bin/env python3
"""Regression tests for paper2md.py.

    python3 scripts/test_paper2md.py

Each test names the defect it pins down. Fixtures live in tests/ next to
this file; they are minimal, compilable LaTeX so they can also be run
through the real pipeline by hand.
"""

import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paper2md

FIXTURES = os.path.join(HERE, "tests")


def convert(stem, *extra):
    """Run the real pipeline over a fixture; return (markdown, flags)."""
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, stem + ".md")
    subprocess.run([sys.executable, os.path.join(HERE, "paper2md.py"),
                    os.path.join(FIXTURES, stem + ".tex"), "-o", out] + list(extra),
                   check=True, capture_output=True, cwd=FIXTURES)
    with open(out, encoding="utf-8") as fh:
        md = fh.read()
    with open(out[:-3] + ".flags.json", encoding="utf-8") as fh:
        return md, json.load(fh)


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


class Accents(unittest.TestCase):
    """\~{n} used to come out as "\\ {n}": do_text() replaced "~" with a
    non-breaking space before anything had looked at the accents, eating the
    tilde and leaving invalid LaTeX behind. The rest simply survived as raw
    LaTeX."""

    def test_unit(self):
        self.assertEqual(
            paper2md.decode_accents(
                r"Erd\H{o}s and Schr\"{o}dinger met Ala\~{n}\'{o}n in G\"ottingen."),
            "Erdős and Schrödinger met Alañón in Göttingen.")
        self.assertEqual(
            paper2md.decode_accents(
                r"Fran\c{c}ois, \AA ke, and \O rsted wrote \ss{} and na\"ive caf\'e."),
            "François, Åke, and Ørsted wrote ß and naïve café.")

    def test_leaves_non_accent_commands_alone(self):
        src = r"\omega \ldots \int \vec x \bar y \dot z \begin{tabular}"
        self.assertEqual(paper2md.decode_accents(src), src)

    def test_url_targets_are_not_decoded(self):
        # \~ in a URL is an author writing a tilde, not an accent on the next
        # letter; decoding it would silently break the link.
        src = r"\url{http://x.edu/\~boyd/} and caf\'e"
        self.assertEqual(paper2md.decode_accents(src), r"\url{http://x.edu/\~boyd/} and café")

    def test_end_to_end(self):
        md, flags = convert("accents", "--bbl", "accents.bbl")
        self.assertIn("Erdős and Schrödinger met Alañón in Göttingen.", md)
        self.assertIn("François, Åke, and Ørsted wrote ß and naïve café.", md)
        self.assertIn("Braceless forms too: Gödel, Poincaré, çedilla, řeka.", md)
        # title block, which never passes through do_text()
        self.assertIn("# Accents: Erdős, Schrödinger and Alañón", md)
        self.assertIn("**Ondřej Turek, Zuzana Václavíková**", md)
        # bibliography: render_bib_body's unknown-command fallback used to keep
        # the letter and drop the accent
        self.assertIn("E. Schrödinger", md)
        self.assertIn("J.-C. Faugère, Gröbner bases, Birkhäuser", md)
        # ~ still means a non-breaking space
        self.assertIn("see Ref. [1]", md)
        # the one thing left raw is the URL tilde, and it is flagged, not silent
        self.assertEqual(sorted(f["kind"] for f in flags), ["escaping-regime", "no-aux"])
        self.assertTrue(any(r"\~boyd" in f["snippet"] for f in flags))


if __name__ == "__main__":
    unittest.main(verbosity=2)
