#!/usr/bin/env python3
"""Regression tests for paper2md.py.

    python3 scripts/test_paper2md.py

Each test names the defect it pins down. Fixtures live in tests/ next to
this file; they are minimal, compilable LaTeX so they can also be run
through the real pipeline by hand.
"""

import json, os, re, subprocess, sys, tempfile, unittest

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


class Nesting(unittest.TestCase):
    """Every \\begin{X}(.*?)\\end{X} regex stopped at the FIRST inner
    \\end{X}, so a tabular inside a cell truncated the outer table and the
    rows after it disappeared without a flag."""

    def test_env_spans_sees_nesting(self):
        src = ("\\begin{tabular}{cc}\nA & B \\\\\n"
               "C & \\begin{tabular}{c} i1 \\\\ i2 \\end{tabular} \\\\\n"
               "D & E \\\\\n\\end{tabular}\n")
        spans = paper2md.env_spans(src, {"tabular"})
        self.assertEqual(len(spans), 1)                  # outermost only
        node, anc = spans[0]
        self.assertEqual(anc, ())
        self.assertEqual(src[node.pos:node.pos + node.len].count("D & E"), 1)
        self.assertTrue(src[node.pos:node.pos + node.len].endswith("\\end{tabular}"))
        # and the inner one is reachable from the outer body
        body = paper2md.env_body(src, node)
        self.assertEqual(len(paper2md.env_spans(body, {"tabular"})), 1)

    def test_ancestors_are_reported(self):
        src = ("\\begin{subequations}\n\\begin{equation} a \\end{equation}\n"
               "\\end{subequations}\n\\begin{equation} b \\end{equation}")
        spans = paper2md.env_spans(src, {"equation"})
        self.assertEqual([anc for _, anc in spans], [("subequations",), ()])

    def test_comment_cannot_unbalance_a_group(self):
        self.assertEqual(paper2md.balanced("{a % }\nb}", 0)[0], "a % }\nb")

    def test_end_to_end(self):
        md, flags = convert("nesting")
        # the rows that used to vanish
        self.assertIn("| D | E |", md)
        self.assertIn("| A | B |", md)
        # the inner table is not guessed at, it is marked and flagged
        self.assertIn("[nested table — see flags]", md)
        nested = [f for f in flags if f["kind"] == "table-nested"]
        self.assertEqual(len(nested), 1)
        self.assertIn("inner1", nested[0]["detail"])
        self.assertIn("inner2", nested[0]["detail"])
        # subequations: 2a is read from the .aux, 2b derived inside the block,
        # 3 derived after leaving it
        self.assertEqual(re.findall(r"\\tag\{([^}]*)\}", md), ["1", "2a", "2b", "3"])
        # tikz survives a nested \scope
        self.assertIn("A, B, C   [unstyled]", md)
        self.assertIn("A -> C", md)


class DollarDisplayMath(unittest.TestCase):
    r"""$$...$$ was not handled. The inline-$ regex that ran instead could not
    pair it, so from the first $$ in a paper onwards every inline pair was
    offset by one and prose was swallowed into math placeholders."""

    def stash_math(self, src):
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        return conv.restore(conv._stash_math(src))

    def test_all_four_delimiter_pairs(self):
        self.assertEqual(self.stash_math(r"a $x$ b"), "a $x$ b")
        self.assertEqual(self.stash_math(r"a \( x \) b"), "a $x$ b")
        self.assertEqual(self.stash_math("a $$ x $$ b"), "a \n\n$$\nx\n$$\n\n b")
        self.assertEqual(self.stash_math(r"a \[ x \] b"), "a \n\n$$\nx\n$$\n\n b")

    def test_display_does_not_offset_the_inline_pairs_after_it(self):
        # the actual defect: "y" and "z" are math, " and " is prose
        out = self.stash_math("$$ x $$ then $y$ and $z$")
        self.assertIn("$y$ and $z$", out)

    def test_escaped_dollar_is_not_a_delimiter(self):
        self.assertEqual(self.stash_math(r"costs \$5 and \$6, with $x$"),
                         r"costs \$5 and \$6, with $x$")

    def test_end_to_end(self):
        md, flags = convert("dollars")
        self.assertIn("$$\nc = d\n$$", md)
        # prose after the display used to arrive as raw LaTeX
        self.assertIn("*this must still be emphasis*", md)
        self.assertNotIn("\\emph{", md)
        self.assertIn("$e+f$", md)
        self.assertIn("## Later", md)
        self.assertEqual([f["kind"] for f in flags if f["kind"] == "escaping-regime"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
