#!/usr/bin/env python3
"""Regression tests for srcdiff.py.

    python3 scripts/test_srcdiff.py

Each test names the defect it pins down. Most were found by running the tool
against three real papers -- a .tex, a text-layer PDF and a 1994 scan -- and
reading what it reported; the ones that turned out to be the tool's fault
rather than the conversion's are here.

Fixtures are written inline rather than into tests/, which holds compilable
LaTeX for the paper2md pipeline; these are fragments that only have to be
stripped, not typeset.
"""

import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import srcdiff


def tex_prose(source):
    """Run the .tex stripper over a fragment; return its word list."""
    with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(source)
        path = fh.name
    try:
        return srcdiff.tokens(" ".join(srcdiff.source_text_tex(path)))
    finally:
        os.unlink(path)


class MarkdownReferences(unittest.TestCase):
    """Dropping the references section must not drop everything after it.

    verify.split_references cuts to end of file. Reused here that took the
    footnote definitions with it -- they conventionally sit at the bottom of
    the file, below the references -- and every word of a title footnote
    ("Tamkang", "supported", "Foundation") reported as an omission.
    """

    def test_footnote_definitions_after_references_survive(self):
        md = ("## 1 Body\n\nBody text.\n\n"
              "## References\n\n[1] Someone. A paper. 1994.\n\n"
              "[^dagger]: Department of Mathematics, Tamkang University.\n")
        kept = srcdiff.markdown_prose(md)
        self.assertIn("tamkang", srcdiff.tokens(kept))
        self.assertNotIn("someone", srcdiff.tokens(kept))

    def test_appendix_after_references_survives(self):
        md = ("## 1 Body\n\nBody text.\n\n"
              "## References\n\n[1] Someone. A paper. 1994.\n\n"
              "# Appendix\n\n## A. Proof\n\nThe quaternion argument.\n")
        kept = srcdiff.tokens(srcdiff.markdown_prose(md))
        self.assertIn("quaternion", kept)
        self.assertNotIn("someone", kept)

    def test_references_are_dropped_when_they_end_the_file(self):
        md = "## 1 Body\n\nBody text.\n\n## References\n\n[1] Someone. 1994.\n"
        self.assertNotIn("someone", srcdiff.tokens(srcdiff.markdown_prose(md)))


class TexPreamble(unittest.TestCase):
    """A preamble is not prose, but its package options read like prose.

    Taken whole, [dvipsnames]{xcolor}, [most]{tcolorbox} and savetrees'
    tracking=tight,charwidths=tight contributed 40-odd words -- 'caption',
    'compress', 'article', 'most', 'false' -- every one of them reported as
    missing from the Markdown, because they are.
    """

    PREAMBLE = (r"\documentclass[twocolumn]{article}"
                "\n" r"\usepackage[dvipsnames]{xcolor}"
                "\n" r"\usepackage[most]{tcolorbox}"
                "\n" r"\newcommand{\D}{\mathcal{D}}"
                "\n" r"\title{A Graphical Criterion}"
                "\n" r"\author[1]{Ada Lovelace}"
                "\n" r"\affil[1]{Institute for Analytical Engines}"
                "\n" r"\begin{document}" "\nBody sentence.\n" r"\end{document}")

    def test_package_options_do_not_leak(self):
        words = tex_prose(self.PREAMBLE)
        for junk in ("dvipsnames", "tcolorbox", "xcolor", "article", "most"):
            self.assertNotIn(junk, words, junk)

    def test_frontmatter_is_kept(self):
        words = tex_prose(self.PREAMBLE)
        for kept in ("graphical", "criterion", "lovelace", "engines", "sentence"):
            self.assertIn(kept, words, kept)


class TexBody(unittest.TestCase):
    """The body stripper keeps prose and drops everything typeset from keys."""

    def test_appendix_after_bibliography_survives(self):
        """\\bibliography{} is followed by \\appendix more often than not.

        Truncating the source there dropped 1200 words of appendix. Both
        sides then silently lacked it, so the diff still read 0 missing --
        the failure mode this whole tool exists to catch.
        """
        src = (r"\begin{document}" "\nBody sentence.\n"
               r"\bibliography{references}" "\n" r"\appendix" "\n"
               r"\section{Proof of Lemma}" "\nThe quaternion argument.\n"
               r"\end{document}")
        words = tex_prose(src)
        self.assertIn("quaternion", words)
        self.assertIn("body", words)

    def test_keys_and_definitions_are_dropped_but_prose_kept(self):
        src = (r"\begin{document}" "\n"
               r"\newcommand{\obviouslynotprose}{x}" "\n"
               r"Real prose here~\cite{Steudel_Ay} and \ref{lemma_main}."
               "\n" r"\label{sec_intro}" "\n" r"\end{document}")
        words = tex_prose(src)
        self.assertIn("prose", words)
        self.assertNotIn("obviouslynotprose", words)
        self.assertNotIn("steudel", words)      # a key; the .bst renders the name
        self.assertNotIn("lemma", words)
        self.assertNotIn("intro", words)

    def test_math_environments_are_dropped(self):
        src = (r"\begin{document}" "\nBefore.\n"
               r"\begin{equation}\alpha J_{k,n-k} \end{equation}" "\n"
               r"\begin{tikzpicture}\node[sv] (mediator) at (0,0) {};"
               r"\end{tikzpicture}" "\nAfter.\n" r"\end{document}")
        words = tex_prose(src)
        self.assertIn("before", words)
        self.assertIn("after", words)
        self.assertNotIn("mediator", words)


class NoiseFilters(unittest.TestCase):
    """The filters that make an OCR report readable, and their limits."""

    def classify(self, word, other=(), math=None, min_len=4):
        real, noise = srcdiff.classify([(word, 1)], set(other), min_len,
                                       fuzzy=True, math=math)
        return (real[0][0] if real else None), dict(noise)

    def test_short_tokens_are_filtered(self):
        self.assertEqual(self.classify("hn")[1], {"short": 1})

    def test_flattened_math_is_recognised(self):
        """p_{ABEXY}(a,b,e,x,y) reaches the word stream from a PDF as
        `pabexy`, which no amount of prose comparison can match."""
        math = srcdiff.markdown_nonprose("Text $p_{ABEXY}(a,b,e,x,y)$ text.")
        self.assertEqual(self.classify("pabexy", math=math)[1], {"math": 1})

    def test_typographic_math_commands_do_not_break_the_blob(self):
        """\\dim\\operatorname{span} prints as "dim span" and arrives as
        `dimspan`; the command name must not sit between the two words."""
        math = srcdiff.markdown_nonprose(r"Then $\dim \operatorname{span}\{x\}$.")
        self.assertEqual(self.classify("dimspan", math=math)[1], {"math": 1})

    def test_hyphenation_rejoin_is_filtered(self):
        self.assertEqual(self.classify("deviceindependent",
                                       other=("device", "independent"))[1],
                         {"split": 1})

    def test_ocr_misreadings_of_present_words_are_filtered(self):
        self.assertEqual(self.classify("matriz", other=("matrix",))[1],
                         {"near": 1})

    def test_a_genuinely_absent_word_survives_every_filter(self):
        word, noise = self.classify("lexicographic", other=("matrix", "span"))
        self.assertEqual(word, "lexicographic")
        self.assertEqual(noise, {})


class SourceText(unittest.TestCase):
    """Page furniture is not content."""

    def test_running_heads_and_page_numbers_are_dropped(self):
        # Real body text differs page to page, which is exactly why a running
        # head stands out; a fixture that repeats the same body line on every
        # page is testing something no PDF does.
        pages = ["EXTREME CORRELATION MATRICES\n"
                 "body line %da\nbody line %db\nbody line %dc\n%d"
                 % (i, i, i, 902 + i) for i in (1, 2, 3)]
        out = " ".join(srcdiff.strip_running_lines(pages))
        self.assertNotIn("extreme", srcdiff.tokens(out))
        self.assertNotIn("correlation", srcdiff.tokens(out))
        self.assertEqual(srcdiff.tokens(out).count("body"), 9)

    def test_a_line_on_one_page_only_is_never_furniture(self):
        """Counting a line once per page matters: on a short page the top and
        bottom windows overlap, and double-counting let a line that appears on
        one page clear the threshold by itself."""
        pages = ["opening line of the paper\nbody\n1",
                 "second page opening\nbody\n2",
                 "third page opening\nbody\n3"]
        out = " ".join(srcdiff.strip_running_lines(pages))
        self.assertIn("opening", srcdiff.tokens(out))
        self.assertEqual(srcdiff.tokens(out).count("opening"), 3)

    def test_body_text_is_never_dropped_as_furniture(self):
        """Only the top and bottom few lines are eligible, so a sentence in
        the middle of the page stays even when every page carries it."""
        page = ("running head\n" + "filler one\nfiller two\n"
                + "the same sentence repeated\n"
                + "filler three\nfiller four\n" + "42")
        out = " ".join(srcdiff.strip_running_lines([page, page, page]))
        self.assertEqual(out.count("the same sentence repeated"), 3)
        self.assertNotIn("running", srcdiff.tokens(out))

    def test_dehyphenation(self):
        self.assertEqual(srcdiff.tokens(srcdiff.dehyphenate("corre-\nlation")),
                         ["correlation"])

    def test_accents_and_ligatures_fold_to_ascii(self):
        self.assertEqual(srcdiff.tokens("Vesterstrøm"), ["vesterstrom"])
        self.assertEqual(srcdiff.tokens("ﬁnite"), ["finite"])


class EndToEnd(unittest.TestCase):
    """A conversion that lost nothing reports nothing."""

    def test_faithful_conversion_is_clean(self):
        tex = (r"\begin{document}" "\n"
               r"\section{Basic results}" "\n"
               "Given an correlation matrix, a nonzero Hermitian matrix is "
               "said to be a perturbation.\n" r"\end{document}")
        md = ("## 1. Basic results\n\nGiven an correlation matrix, a nonzero "
              "Hermitian matrix is said to be a perturbation.\n")
        with tempfile.TemporaryDirectory() as tmp:
            tex_path = os.path.join(tmp, "p.tex")
            md_path = os.path.join(tmp, "p.md")
            open(tex_path, "w").write(tex)
            open(md_path, "w").write(md)
            _, pages = srcdiff.extract_source(tex_path, "tex")
            res, _ = srcdiff.compare(md_path, pages)
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["extra"], [])

    def test_a_dropped_sentence_is_reported(self):
        tex = (r"\begin{document}" "\nKept sentence about matrices.\n"
               "The eavesdropper distributes a nonlocal correlation.\n"
               r"\end{document}")
        md = "Kept sentence about matrices.\n"
        with tempfile.TemporaryDirectory() as tmp:
            tex_path = os.path.join(tmp, "p.tex")
            md_path = os.path.join(tmp, "p.md")
            open(tex_path, "w").write(tex)
            open(md_path, "w").write(md)
            _, pages = srcdiff.extract_source(tex_path, "tex")
            res, _ = srcdiff.compare(md_path, pages)
        missing = [w for w, _ in res["missing"]]
        self.assertIn("eavesdropper", missing)
        self.assertIn("distributes", missing)
        self.assertIn("nonlocal", missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
