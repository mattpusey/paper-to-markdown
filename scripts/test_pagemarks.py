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
    return [f"w{seed}x{i}" if i % 3 == 0 else rnd.choice(stock) for i in range(n)]


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
            ["causal value zy102\n", "SECTION 2 ABOUT X\n\ndase relation"])
        self.assertTrue(pages[0].rstrip().endswith("zydase"))


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
            after = re.search(r"w9x\d+", md[m.end():]).group(0)
            self.assertIn(after, flow[k], f"page {m.group(1)}")
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
