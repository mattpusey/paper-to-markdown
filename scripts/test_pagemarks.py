#!/usr/bin/env python3
"""Regression tests for pagemarks.py.

    python3 scripts/test_pagemarks.py

The pure parts -- reading printed numbers off page text, aligning page text
against Markdown, writing and stripping markers -- are tested on inline
fixtures. One end-to-end test compiles a small paper and runs the real
pipeline; it is skipped without pdflatex and pdftotext.
"""

import os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pagemarks


def page(body, head=None, foot=None):
    """A page of layout text: optional head line, body, optional foot line."""
    lines = ([head, ""] if head else []) + [body] + (["", foot] if foot else [])
    return "\n".join(lines)


BODY = "Some ordinary sentence of body text that runs across the column."


class Labels(unittest.TestCase):
    """Only numbers that go up one per sheet are page numbers."""

    def test_footer_numbers_starting_mid_volume(self):
        pages = [page(BODY, foot=f"  {101 + i}  ") for i in range(4)]
        labels, how = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["101", "102", "103", "104"])
        self.assertEqual(set(how), {"printed"})

    def test_running_head_with_year_and_volume_is_not_a_page_number(self):
        # The year and the volume number repeat on every page, so they never
        # go up one per sheet; the page number at the end of the head does.
        pages = [page(BODY, head=f"PHYSICAL REVIEW A 98 (2018)   {12 + i}")
                 for i in range(4)]
        labels, _ = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["12", "13", "14", "15"])

    def test_equation_numbers_are_never_page_numbers(self):
        pages = [page(BODY, foot=f"x = y   ({i + 2})") for i in range(4)]
        labels, _ = pagemarks.detect_labels(pages)
        self.assertEqual(labels, [None] * 4)

    def test_unprinted_first_page_is_inferred(self):
        pages = [page(BODY)] + [page(BODY, foot=str(i + 1)) for i in range(1, 4)]
        labels, how = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["1", "2", "3", "4"])
        self.assertEqual(how[0], "inferred")

    def test_gap_inside_a_run_is_filled(self):
        pages = [page(BODY, foot=str(i + 7)) for i in range(5)]
        pages[2] = page(BODY)                         # a full-page figure
        labels, how = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["7", "8", "9", "10", "11"])
        self.assertEqual(how[2], "inferred")

    def test_roman_front_matter_then_arabic(self):
        pages = ([page(BODY, foot=r) for r in ("i", "ii", "iii")]
                 + [page(BODY, foot=str(i)) for i in (1, 2, 3)])
        labels, _ = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["i", "ii", "iii", "1", "2", "3"])

    def test_padded_footer_still_counts(self):
        """pdftotext -layout pads a footer out to the right margin. Measured
        with its padding, the Quantum journal's footer ("Accepted in Quantum
        2022-10-17, click title to verify. Published under CC-BY 4.0.    9")
        ran past the length limit on one page in seventeen, and that page's
        number was only inferred."""
        foot = ("Accepted in Quantum 2022-10-17, click title to verify. "
                "Published under CC-BY 4.0." + " " * 80 + "{}")
        pages = [page(BODY, foot=foot.format(i + 1)) for i in range(4)]
        _, how = pagemarks.detect_labels(pages)
        self.assertEqual(how, ["printed"] * 4)

    def test_prefixed_numbers(self):
        pages = [page(BODY, head=f"012345-{i + 1}   SMITH AND JONES")
                 for i in range(3)]
        labels, _ = pagemarks.detect_labels(pages)
        self.assertEqual(labels, ["012345-1", "012345-2", "012345-3"])

    def test_nothing_printed_means_no_labels(self):
        labels, _ = pagemarks.detect_labels([page(BODY)] * 4)
        self.assertEqual(labels, [None] * 4)

    def test_roman_round_trip(self):
        for n in range(1, 60):
            self.assertEqual(pagemarks.roman_value(pagemarks.to_roman(n)), n)
        self.assertIsNone(pagemarks.roman_value("mid"))


class Markers(unittest.TestCase):
    """Markers must never change how the Markdown around them parses, and
    stripping them must give back the input byte for byte."""

    def roundtrip(self, md, offset, label="7"):
        placed = [(offset, "matched")]
        out = pagemarks.insert(md, placed, [label])
        self.assertEqual(pagemarks.strip_markers(out), md)
        self.assertEqual(out.count(pagemarks.marker(label)), 1)
        return out

    def test_mid_paragraph(self):
        md = "First words here and then **bold** words.\n"
        out = self.roundtrip(md, md.index("bold"))
        self.assertIn("then <!-- page 7 --> **bold**", out)

    def test_new_block_gets_its_own_line(self):
        md = "Para one.\n\n## 2 Next section\n\nPara two.\n"
        out = self.roundtrip(md, md.index("## 2"))
        self.assertIn("Para one.\n\n<!-- page 7 -->\n\n## 2 Next", out)

    def test_never_opens_a_line(self):
        # "<!--" at the start of a line opens an HTML block, which would carry
        # the rest of the line off as raw HTML. Hang it on the line before.
        md = "A paragraph wrapped\nover two lines.\n"
        out = self.roundtrip(md, md.index("over"))
        self.assertIn("wrapped <!-- page 7 -->\nover", out)

    def test_not_on_a_fence_or_display_delimiter(self):
        for opener in ("```\nnodes\n```", "$$\nx = y\n$$"):
            md = f"Text before.\n{opener}\nafter the block it goes on.\n"
            out = self.roundtrip(md, md.index("after"))
            for line in out.split("\n"):
                if line.startswith(("```", "$$")):
                    self.assertNotIn("<!--", line)

    def test_list_item_indentation_is_kept(self):
        md = "- item one\n\n  continued item one\n"
        out = self.roundtrip(md, md.index("  continued"))
        self.assertIn("\n  <!-- page 7 -->\n\n  continued", out)

    def test_grep_for_one_page_does_not_hit_another(self):
        md = "w " * 10
        out = pagemarks.insert(md, [(0, "matched"), (4, "matched")], ["7", "70"])
        hits = [l for l in out.split("\n") if "<!-- page 7 -->" in l]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("page 70", hits[0].split("<!-- page 7 -->")[0])

    def test_masked_spans_hold_no_words(self):
        md = "Before $x_{abc}$ and `code_word` and [link](https://e.org/a_b).\n"
        words, _, _ = pagemarks.markdown_words(pagemarks.mask_markdown(md))
        self.assertEqual(words, ["before", "and", "and", "link"])


def unique_words(n, seed):
    """Prose whose every trigram is unique, as a real paper's mostly is."""
    import random
    rnd = random.Random(seed)
    stock = ("state channel outcome observer system hidden variable source "
             "detector setting graph node edge parent latent proof bound").split()
    def tag(k):                       # letters only, or it is no token
        out = ""
        while True:
            out = "bcdfghjklmnpqrstvwxz"[k % 20] + out
            k //= 20
            if not k:
                return "zq" + out
    return [tag(seed * 10000 + i) if i % 3 == 0 else rnd.choice(stock)
            for i in range(n)]


class Placement(unittest.TestCase):

    def test_boundary_mid_paragraph(self):
        words = unique_words(120, 1)
        md = " ".join(words) + "\n"
        flow = [" ".join(words[:50]), " ".join(words[50:])]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2"]),
                               ["1", "2"])
        self.assertTrue(out.startswith("<!-- page 1 -->\n\n"))
        self.assertIn(f"{words[49]} <!-- page 2 --> {words[50]}", out)

    def test_right_column_read_first(self):
        """pdftotext's reading order can put a page's right-hand column
        before its left when a float heads the left column. Chaining matches
        in PDF order then threw the left column away and put the marker at
        the top of the right column, half a page late."""
        words = unique_words(300, 2)
        md = " ".join(words) + "\n"
        p2_left, p2_right = words[100:180], words[180:260]
        flow = [" ".join(words[:100]),
                " ".join(p2_right) + "\n" + " ".join(p2_left),
                " ".join(words[260:])]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2", "3"]),
                               ["1", "2", "3"])
        self.assertIn(f"{words[99]} <!-- page 2 --> {words[100]}", out)
        self.assertIn(f"{words[259]} <!-- page 3 --> {words[260]}", out)

    def test_equation_at_foot_of_page_stays_on_that_page(self):
        a, b = unique_words(60, 3), unique_words(60, 4)
        md = " ".join(a) + "\n\n$$\nE = mc^2\n$$\n\n" + " ".join(b) + "\n"
        flow = [" ".join(a) + "\nE mc2 sum over terms", " ".join(b)]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2"]),
                               ["1", "2"])
        self.assertIn("$$\n\n<!-- page 2 -->\n\n" + b[0], out)

    def test_equation_at_head_of_page_goes_with_that_page(self):
        a, b = unique_words(60, 5), unique_words(60, 6)
        md = " ".join(a) + "\n\n$$\nE = mc^2\n$$\n\n" + " ".join(b) + "\n"
        flow = [" ".join(a), "E mc2 sum over terms\n" + " ".join(b)]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2"]),
                               ["1", "2"])
        self.assertIn("<!-- page 2 -->\n\n$$", out)

    def test_hyphenated_word_belongs_to_the_page_it_starts_on(self):
        pages = pagemarks.rejoin_across_pages(
            ["some text ending zy-\n", "dase relation value"])
        self.assertTrue(pages[0].rstrip().endswith("zydase"))
        self.assertTrue(pages[1].lstrip().startswith("relation"))
        # reading-order mode has already dropped the hyphen and glued on the
        # page number; an all-caps running head sits between the halves
        pages = pagemarks.rejoin_across_pages(
            ["causal value zy102\n", "SECTION 2 ABOUT X\n\ndase relation"],
            ["102", "103"])
        self.assertTrue(pages[0].rstrip().endswith("zydase"))


    def test_one_letter_word_opening_a_page(self):
        """Pusey & Rudolph, arXiv:1208.2550, p. 2 opens "a particular rho and
        sigma". One-letter words were not tokens, so the marker went after
        the "a"."""
        a, b = unique_words(60, 7), unique_words(60, 8)
        md = " ".join(a) + " tight for a particular $\\rho$ " + " ".join(b) + "\n"
        # ...and in reading order that page starts with its other column
        flow = [" ".join(a) + " tight for",
                " ".join(b[30:]) + "\na particular P ρ " + " ".join(b[:30])]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2"]),
                               ["1", "2"])
        self.assertIn("tight for <!-- page 2 --> a particular", out)

    def test_display_at_foot_with_debris_on_both_sides(self):
        """Caro Perez et al., arXiv:2110.08293, pp. 4-5: an array labelled
        "<- Frame 1..4" ends page 4 and "Clearly, {a_i, b_i, c_i, d_i}" opens
        page 5. Both sides leave unmatched letters in the PDF, and counting
        them put the marker before the array. What the letters ARE decides
        it: "frame" is in the array, "ai bi" in the sentence after it."""
        a, b = unique_words(60, 10), unique_words(60, 11)
        md = (" ".join(a) + " as follows:\n\n$$\n\\begin{array}{cc}\n"
              "a_1&\\mbox{$\\leftarrow$ Frame 1}\\\\\n"
              "b_1&\\mbox{$\\leftarrow$ Frame 2}\n\\end{array}\n$$\n\n"
              "Clearly, $\\{a_i,b_i,c_i,d_i\\}$, is " + " ".join(b) + "\n")
        flow = [" ".join(a) + " as follows:\na1\nb1\nFrame 1\nFrame 2",
                "Clearly,\n{ai , bi , ci , di }, is " + " ".join(b)]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["4", "5"]),
                               ["4", "5"])
        self.assertIn("$$\n\n<!-- page 5 -->\n\nClearly", out)


class ReviewFixes(unittest.TestCase):
    """One test per defect raised in review of PR #9."""

    def test_runs_without_pylatexenc(self):
        """srcdiff -> verify -> paper2md, whose missing-pylatexenc handler
        called sys.exit() at import, so a PDF-only install could not even
        print pagemarks.py --help. Imported, paper2md now raises ImportError,
        which verify and srcdiff already catch."""
        tmp = tempfile.mkdtemp()
        md = os.path.join(tmp, "x.md")
        with open(md, "w") as fh:
            fh.write("Some text <!-- page 3 --> more text.\n")
        block = ("import sys, runpy; sys.modules['pylatexenc'] = None; "
                 "sys.argv = %r; runpy.run_path(%r, run_name='__main__')")
        script = os.path.join(HERE, "pagemarks.py")
        for argv in (["pagemarks.py", "--help"], ["pagemarks.py", md, "--strip"]):
            r = subprocess.run([sys.executable, "-c", block % (argv, script)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
        with open(md) as fh:
            self.assertEqual(fh.read(), "Some text more text.\n")
        # and run as a script, paper2md still says what to install
        r = subprocess.run([sys.executable, "-c", block % (
            ["paper2md.py", "x.tex"], os.path.join(HERE, "paper2md.py"))],
            capture_output=True, text=True)
        self.assertIn("needs pylatexenc", r.stderr)
        shutil.rmtree(tmp)

    def test_labels_with_spaces_round_trip(self):
        """An explicit /PageLabels entry can be "Appendix 1". The marker
        patterns allowed no spaces, so --strip left it behind, the report
        missed it and a rerun duplicated it."""
        md = "w " * 10
        out = pagemarks.insert(md, [(0, "matched"), (4, "matched")],
                               ["Appendix 1", "Appendix 2"])
        self.assertEqual(pagemarks.MARKER_RE.findall(out),
                         ["Appendix 1", "Appendix 2"])
        self.assertEqual(pagemarks.strip_markers(out), md)
        self.assertEqual(pagemarks.clean_label("A  --> b"), "A -> b")

    def test_unreadable_pdf_is_unavailable(self):
        """A corrupt PDF raised pypdf's own exception, which paper2md does
        not catch, so optional page numbering aborted the whole conversion."""
        bad = os.path.join(tempfile.mkdtemp(), "bad.pdf")
        with open(bad, "wb") as fh:
            fh.write(b"%PDF-1.4 truncated")
        with self.assertRaises(pagemarks.Unavailable):
            pagemarks.annotate("text\n", bad)

    def test_two_page_paper_numbered_in_running_heads(self):
        """Two pages, each with its number at one end of a running head:
        weight 1 + 1 fell short of the threshold of 3, so a consistent
        sequence was reported as no page numbers at all."""
        pages = [page(BODY, head=f"012345-{i + 1}   SMITH AND JONES")
                 for i in range(2)]
        self.assertEqual(pagemarks.detect_labels(pages)[0],
                         ["012345-1", "012345-2"])

    def test_section_numbers_in_a_few_heads_are_not_page_numbers(self):
        # ...while two pages of a twenty-page paper whose heads happen to go
        # up by one (a section number) still do not make a page numbering
        pages = [page(BODY) for _ in range(20)]
        pages[6] = page(BODY, head="3   SECTION THREE")
        pages[7] = page(BODY, head="4   SECTION FOUR")
        self.assertEqual(pagemarks.detect_labels(pages)[0], [None] * 20)

    def test_equation_digits_are_not_page_number_glue(self):
        """"E mc2" ending a page and "where observers" opening the next were
        joined into "mcwhere": any trailing digits were taken for a page
        number glued on in place of a hyphen. Only the page's own number is."""
        pages = pagemarks.rejoin_across_pages(
            ["the energy E mc2\n", "where observers measure"], ["7", "8"])
        self.assertEqual(pages, ["the energy E mc2\n", "where observers measure"])
        pages = pagemarks.rejoin_across_pages(
            ["causal value zy7\n", "dase relation"], ["7", "8"])
        self.assertTrue(pages[0].rstrip().endswith("zydase"))

    def test_second_half_of_a_split_word_found_past_the_other_column(self):
        # reading order opens the page with its right column's heading; the
        # rest of "communi-" is on a later line, and the whole word decides
        pages = pagemarks.rejoin_across_pages(
            ["we need communi-\n", "5 Section 5 about it\n\ncation here"],
            ["4", "5"], {"communication", "here", "section", "about"})
        self.assertTrue(pages[0].rstrip().endswith("communication"))
        self.assertIn("Section 5 about it", pages[1])
        self.assertNotIn("cation", pages[1])

    def test_uppercase_heading_opening_a_page_is_kept(self):
        """Every line without a lowercase letter at a page's edge was taken
        for a running head, so a page opening on "RESULTS" lost it, and the
        marker went in after the heading."""
        self.assertIn("RESULTS",
                      pagemarks.strip_furniture("RESULTS\n\nWe find the bound."))
        # A running head goes: the layout shows it on one line with the page
        # number, though reading order splits it up (and here loses the 106).
        layout = "5     SECTION 5 ABOUT ARGUMENT        106\n\nRESULTS\n\nWe find."
        flow = "5\n\nSECTION 5 ABOUT ARGUMENT\n\nRESULTS\n\nWe find."
        kept = pagemarks.strip_furniture(
            flow, pagemarks.furniture_lines(layout, "106"))
        self.assertNotIn("ARGUMENT", kept)
        self.assertIn("RESULTS", kept)

        a, b = unique_words(60, 12), unique_words(60, 13)
        md = " ".join(a) + "\n\n## RESULTS\n\n" + " ".join(b) + "\n"
        flow = [" ".join(a), "RESULTS\n\n" + " ".join(b)]
        out = pagemarks.insert(md, pagemarks.place(md, flow, ["1", "2"]),
                               ["1", "2"])
        self.assertIn("<!-- page 2 -->\n\n## RESULTS", out)

    def test_marker_inside_a_reference_does_not_break_verify(self):
        """verify.prose_only kept HTML comments, so a marker between "Table 1
        of Schmid," and "Rosset [1]" broke the cited-elsewhere exemption and
        reported Table 1 as missing."""
        import verify
        md = ("Compare Table 1 of Schmid, <!-- page 2 --> Rosset and "
              "Buscemi [1], and Fig. <!-- page 3 --> 2 there.\n\n"
              "**Figure 2:** a caption.\n\n## References\n\n[1] X.\n")
        res, ok = verify.check_floats(md)
        self.assertTrue(ok, res)
        self.assertTrue(verify.check_citations(md)[1])

    def test_rerun_with_no_numbers_removes_old_markers(self):
        """With no page numbers found, the stripped Markdown was discarded:
        markers from an earlier run stayed in place, and -o wrote nothing."""
        tmp = tempfile.mkdtemp()
        md, out = os.path.join(tmp, "x.md"), os.path.join(tmp, "out.md")
        with open(md, "w") as fh:
            fh.write("Text <!-- page 3 --> more text.\n")
        real = pagemarks.annotate
        pagemarks.annotate = lambda m, pdf: (
            pagemarks.strip_markers(m),
            {"pdf": pdf, "sheets": 1, "source": None, "pages": [], "removed": 1})
        argv = sys.argv
        try:
            pdf = os.path.join(tmp, "x.pdf")
            open(pdf, "wb").close()
            sys.argv = ["pagemarks.py", md, "--pdf", pdf, "-o", out]
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                pagemarks.main()
        finally:
            pagemarks.annotate, sys.argv = real, argv
        with open(out) as fh:
            self.assertEqual(fh.read(), "Text more text.\n")
        shutil.rmtree(tmp)

    def test_missing_pdf_does_not_abort_conversion(self):
        """An explicit --pdf that does not exist reached os.path.getmtime()
        and aborted the conversion before the Markdown was written."""
        tmp = tempfile.mkdtemp()
        tex = os.path.join(tmp, "p.tex")
        with open(tex, "w") as fh:
            fh.write("\\documentclass{article}\\begin{document}Hello."
                     "\\end{document}\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "paper2md.py"),
                            tex, "--pdf", os.path.join(tmp, "nope.pdf")],
                           capture_output=True, text=True, cwd=tmp)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertIn("page markers: skipped (no such file", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(tmp, "p.md")))
        shutil.rmtree(tmp)


@unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"),
                     "needs pdflatex and pdftotext")
class EndToEnd(unittest.TestCase):
    """Compile a real two-column paper numbered from 7 and convert it."""

    def test_real_pdf(self):
        tmp = tempfile.mkdtemp()
        words = unique_words(2400, 9)
        paras = [" ".join(words[i:i + 80]) + "." for i in range(0, len(words), 80)]
        body = "\n\n".join(p if k % 7 != 3 else
                           p + "\n\\begin{equation} a = \\sum_i b_i \\end{equation}"
                           for k, p in enumerate(paras))
        tex = ("\\documentclass[twocolumn]{article}\n\\usepackage{amsmath}\n"
               "\\title{Page Marks}\n\\begin{document}\n\\setcounter{page}{7}\n"
               "\\maketitle\n\\section{One}\n" + body + "\n\\end{document}\n")
        src = os.path.join(tmp, "paper.tex")
        with open(src, "w") as fh:
            fh.write(tex)
        for _ in range(2):
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "paper.tex"],
                           cwd=tmp, capture_output=True)
        self.assertTrue(os.path.exists(os.path.join(tmp, "paper.pdf")))
        out = subprocess.run([sys.executable, os.path.join(HERE, "paper2md.py"),
                              src, "-o", os.path.join(tmp, "paper.md")],
                             cwd=tmp, capture_output=True, text=True)
        self.assertIn("page markers:", out.stdout)
        with open(os.path.join(tmp, "paper.md")) as fh:
            md = fh.read()

        labels = pagemarks.MARKER_RE.findall(md)
        sheets = int(re.search(r"Pages:\s+(\d+)", subprocess.run(
            ["pdfinfo", os.path.join(tmp, "paper.pdf")],
            capture_output=True, text=True).stdout).group(1)) \
            if shutil.which("pdfinfo") else len(labels)
        self.assertEqual(labels, [str(7 + i) for i in range(sheets)])

        # the first unique word after each marker is printed on that page
        _, flow = pagemarks.read_pdf(os.path.join(tmp, "paper.pdf"))
        for k, m in enumerate(pagemarks.MARKER_RE.finditer(md)):
            after = re.search(r"zq[a-z]+", md[m.end():]).group(0)
            self.assertIn(after, flow[k], f"page {m.group(1)}")
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
