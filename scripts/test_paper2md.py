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

    def test_underscore_in_a_link_url_is_not_a_violation(self):
        # A DOI routinely contains "_"; it sits inside the (...) target,
        # never in rendered text, so it can't trigger emphasis parsing the
        # way a bare "_" in prose would.
        self.assertEqual(self.check(
            "[65] K. Husimi, [paper](https://doi.org/10.11429/ppmsj1919.22.4_264)"), 0)

    def test_underscore_in_link_text_is_still_a_violation(self):
        self.assertEqual(self.check("see [my_var](https://example.com)"), 1)


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


class TikzConversion(unittest.TestCase):
    r"""convert_tikz() encodes a tikzpicture as a node/edge list. Two bugs
    fixed together here made real (TikZiT-generated) diagrams come out
    almost entirely unstyled and with garbled edges:

    1. TikZiT writes \node[style=NAME], never bare \node[NAME] -- the style
       lookup split "style=NAME" on '=' and kept [0] ("style" itself, never
       a real style name), so no node was ever recognised as styled.
    2. An edge endpoint is routinely a coordinate anchor on a node, e.g.
       (5.center) or (5.north east), not the bare node id -- looking those
       up verbatim in the name/label map always missed, so the edge list
       showed the raw "5.center" instead of node 5's actual label.
    """

    def test_style_equals_name_is_recognised(self):
        src = ("\\begin{tikzpicture}\n"
               "\\node [style=point] (0) at (0,0) {$A$};\n"
               "\\end{tikzpicture}")
        blk, ok = paper2md.convert_tikz(src, {"point": "..."}, {"point": "state"}, "FIG")
        self.assertIn("A   [state]", blk)
        self.assertTrue(ok)

    def test_multi_word_braced_style_name(self):
        src = ("\\begin{tikzpicture}\n"
               "\\node [style={small box}, minimum width={1.5 cm}] (0) at (0,0) {$T$};\n"
               "\\end{tikzpicture}")
        blk, _ = paper2md.convert_tikz(src, {"small box": "..."},
                                        {"small box": "process"}, "FIG")
        self.assertIn("T   [process]", blk)

    def test_edge_to_a_center_anchor_resolves_the_nodes_own_label(self):
        src = ("\\begin{tikzpicture}\n"
               "\\node [style=point] (0) at (0,0) {$A$};\n"
               "\\node [style=none] (1) at (1,0) {};\n"
               "\\draw (0) to (1.center);\n"
               "\\end{tikzpicture}")
        blk, _ = paper2md.convert_tikz(src, {"point": "..."}, {"point": "state"}, "FIG")
        self.assertIn("A -- 1", blk)
        self.assertNotIn(".center", blk)

    def test_bare_style_name_without_style_equals_still_works(self):
        # not TikZiT's convention, but \node[NAME] (no "style=") is valid
        # TikZ and used to be the only form this recognised -- must keep
        # working.
        src = ("\\begin{tikzpicture}\n"
               "\\node [point] (0) at (0,0) {$A$};\n"
               "\\end{tikzpicture}")
        blk, _ = paper2md.convert_tikz(src, {"point": "..."}, {"point": "state"}, "FIG")
        self.assertIn("A   [state]", blk)


class TikzStyleHarvesting(unittest.TestCase):
    r"""Preamble only harvested \tikzset{name/.style={...}}, missing the
    older (and, for TikZiT-generated diagrams, near-universal)
    \tikzstyle{name}=[...] form entirely -- every node in a paper using it
    fell back to [unstyled], with no flag ever raised to say so."""

    def test_tikzstyle_form_is_harvested(self):
        pre = paper2md.Preamble(r"\tikzstyle{point}=[regular polygon,draw]")
        self.assertIn("point", pre.tikz_styles)

    def test_multi_word_name_with_space(self):
        pre = paper2md.Preamble(r"\tikzstyle{small box}=[rectangle,draw]")
        self.assertIn("small box", pre.tikz_styles)

    def test_tikzset_form_still_works(self):
        pre = paper2md.Preamble(r"\tikzset{sv/.style={fill=blue}}")
        self.assertIn("sv", pre.tikz_styles)


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


class TableFloats(unittest.TestCase):
    r"""\begin{table} floats were not handled: the whole float leaked into the
    Markdown as raw LaTeX and the \caption never became a caption, so every
    "Table N" in the prose pointed at nothing."""

    def test_drop_cmd_arg_takes_all_the_arguments(self):
        self.assertEqual(
            paper2md.drop_cmd_arg(r"a \renewcommand{\arraystretch}{1.7} b", "renewcommand", 2),
            "a  b")
        # one-argument form, brace-matched rather than [^}]*
        self.assertEqual(paper2md.drop_cmd_arg(r"a \label{x{y}z} b", "label"), "a  b")

    def test_end_to_end(self):
        md, flags = convert("tables")
        self.assertIn("**Table 1:** Numerical results.", md)
        self.assertIn("**Table 2:** Grouped rows.", md)
        # the tabular inside each float still converts
        self.assertIn("| $3$ | $\\frac{1}{2}$ |", md)
        self.assertIn("| C | D |", md)
        # \centering followed by a { ... } group used to swallow the group,
        # table and all
        self.assertNotIn("{1.2}", md)
        self.assertNotIn("\\begin{table}", md)
        self.assertNotIn("\\caption", md)
        # unlabelled float still gets LaTeX's number, derived AND flagged
        self.assertEqual([f["kind"] for f in flags if f["kind"].startswith("table")],
                         ["table-derived-number"])
        self.assertEqual([f["kind"] for f in flags if f["kind"] == "escaping-regime"], [])


class EnvShorthandMacros(unittest.TestCase):
    r"""\newcommand{\beq}{\begin{equation}} (and \eeq/\ben/\een/\bit/\eit) is
    a common personal shorthand. do_figures/do_table_floats/do_math locate
    environments via pylatexenc's LatexWalker, which matches literal
    "\begin{...}"/"\end{...}" tokens -- a \beq invocation looks like nothing
    to it, so an unexpanded \beq...\eeq block used to be invisible to every
    structural pass and leak raw into the output."""

    def test_expands_only_bare_env_delimiter_macros(self):
        macros = {
            r"\beq": (0, r"\begin{equation}"),
            r"\eeq": (0, r"\end{equation}\par\noindent"),
            r"\ket": (1, r"|#1\rangle"),   # NOT an env macro: has an argument
        }
        out = paper2md.expand_env_macros(r"\beq a = b \eeq", macros)
        self.assertEqual(out, r"\begin{equation} a = b \end{equation}\par\noindent")

    def test_leaves_ordinary_macros_alone(self):
        macros = {r"\D": (0, r"\mathcal{D}")}
        src = r"\beq \D \eeq"
        self.assertEqual(paper2md.expand_env_macros(src, macros), src)

    def test_end_to_end_numbering_and_tikz_survive_the_shorthand(self):
        macros = {
            r"\beq": (0, r"\begin{equation}"),
            r"\eeq": (0, r"\end{equation}"),
        }
        src = r"\beq a = b \label{eq:x} \eeq" + "\n" + r"\beq c = d \eeq"
        body = paper2md.expand_env_macros(src, macros)

        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""),
                                   {"eq:x": "1"}, {}, _Args())
        before = len(paper2md.FLAGS)
        out = conv.restore(conv.do_math(body))
        del paper2md.FLAGS[before:]
        self.assertIn("\\tag{1}", out)
        self.assertIn("\\tag{2}", out)   # derived, continuing from 1


class BalancedBraceMatching(unittest.TestCase):
    r"""balanced() used to delegate to LatexWalker's structural parse, which
    treats a bare \begin{X} found INSIDE a {...} group as a real environment
    opener and hunts forward past the group's OWN closing brace for a
    matching \end{X} -- which does not have to be anywhere nearby.
    \newcommand{\bit}{\begin{itemize}} is exactly this: 'itemize' only
    closes inside a DIFFERENT \newcommand's body, possibly lines later."""

    def test_dangling_begin_does_not_swallow_a_later_unrelated_command(self):
        src = "{\\begin{itemize}}\n\\newcommand{\\eit}{\\end{itemize}}\n"
        content, after = paper2md.balanced(src, 0)
        self.assertEqual(content, "\\begin{itemize}")
        self.assertEqual(src[after], "\n")

    def test_ordinary_nesting_still_works(self):
        self.assertEqual(paper2md.balanced("{a {b} c}", 0)[0], "a {b} c")

    def test_comment_and_verb_braces_do_not_desync_the_count(self):
        self.assertEqual(paper2md.balanced("{a % } stray brace\nb}", 0)[0],
                         "a % } stray brace\nb")
        self.assertEqual(paper2md.balanced(r"{\verb|{|x}", 0)[0], r"\verb|{|x")


class IncludeResolution(unittest.TestCase):
    r"""\input/\include and the tikzit \tikzfig{path} convention
    (\InputIfFileExists{path.tikz}{}{...}) are both resolved by pdflatex at
    compile time. A converter reading only the handed-in .tex file would
    never see a \input'd preamble's macros, or a \tikzfig'd diagram at all."""

    def test_input_is_inlined_recursively(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "defs.tex"), "w") as fh:
            fh.write(r"\newcommand{\D}{\mathcal{D}}")
        with open(os.path.join(tmp, "preamble.tex"), "w") as fh:
            fh.write("\\input{defs}\n")
        out = paper2md.resolve_includes("before \\input{preamble} after", tmp)
        self.assertIn(r"\newcommand{\D}{\mathcal{D}}", out)
        self.assertTrue(out.startswith("before "))
        self.assertTrue(out.endswith(" after"))

    def test_missing_input_is_flagged_not_silently_dropped(self):
        before = len(paper2md.FLAGS)
        out = paper2md.resolve_includes("\\input{nope}", tempfile.mkdtemp())
        kinds = [f["kind"] for f in paper2md.FLAGS[before:]]
        del paper2md.FLAGS[before:]
        self.assertEqual(out, "")
        self.assertIn("input-missing", kinds)

    def test_tikzfig_inlines_the_tikz_file(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "wire.tikz"), "w") as fh:
            fh.write("\\begin{tikzpicture}\n\\node (0) at (0,0) {};\n\\end{tikzpicture}")
        out = paper2md.resolve_includes(r"see \tikzfig{wire} here", tmp)
        self.assertIn("\\begin{tikzpicture}", out)
        self.assertNotIn("\\tikzfig", out)

    def test_unresolved_tikzfig_is_left_alone(self):
        out = paper2md.resolve_includes(r"\tikzfig{missing}", tempfile.mkdtemp())
        self.assertEqual(out, r"\tikzfig{missing}")


class DiagramEquations(unittest.TestCase):
    r"""A math environment whose body is a raw tikzpicture is not an
    equation KaTeX can render (\node/\draw are not math syntax) -- it is a
    string-diagram identity typeset AS an equation, routine in
    categorical-quantum-mechanics papers. Wrapping the whole body in
    $$...$$ dumps raw TikZ source as "math" and corrupts everything after
    it: the diagram's own node labels can contain a bare \$, and a "$$"
    that never closes desyncs every display-math pair for the rest of the
    document."""

    def render(self, inner, eq_num=None):
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        before = len(paper2md.FLAGS)
        out = conv._render_diagram_equation(inner, eq_num)
        del paper2md.FLAGS[before:]
        return out

    def test_non_diagram_equation_returns_none(self):
        self.assertIsNone(self.render(r"a = b"))

    def test_tikzpicture_becomes_a_fenced_node_edge_block(self):
        out = self.render(
            "\\begin{tikzpicture}\n"
            "\\node (0) at (0,0) {$A$};\n\\node (1) at (1,0) {$B$};\n"
            "\\draw (0) to (1);\n\\end{tikzpicture}", "3")
        self.assertIn("**Equation 3:**", out)
        self.assertIn("```", out)
        self.assertIn("A -- B", out)
        self.assertNotIn("\\node", out)   # consumed, not leaked as text

    def test_math_around_the_diagram_stays_inline_and_is_stashed(self):
        out = self.render(
            "\\exists h : \\begin{tikzpicture}\\node (0) at (0,0) {};"
            "\\end{tikzpicture}.", "5")
        # the surrounding math is stashed (a placeholder, not raw "$...$"),
        # exactly so a later pass cannot swallow unrelated text into it
        self.assertNotIn("$\\exists", out)
        self.assertIn("\x00PM", out)

    def test_trailing_bare_backslash_does_not_produce_an_empty_dollar_pair(self):
        # "\ " (TeX control-space) right before the diagram, with its space
        # eaten by .strip() -- must never come out as a bare "$$": that
        # reads as an empty DISPLAY math delimiter and desyncs every
        # display-math open/close for the rest of the document.
        out = self.render(
            "\\exists h \\in\\mathcal{P} : \\ \n"
            "\\begin{tikzpicture}\\node (0) at (0,0) {};\\end{tikzpicture}", None)
        self.assertNotIn("$$", out)

    def test_internal_newline_in_the_surrounding_math_is_collapsed(self):
        # "= \Pr(E,P) \in [0,1]\n." -- two physical lines of one inline math
        # scrap, straight from the source. $...$ spanning a literal newline
        # is fragile (some renderers, and this tool's own escaping-regime
        # scan, only look for the closing $ on the same line as the opening
        # one), and there is no reason to keep a display-math-style break in
        # what renders as ordinary inline math.
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        conv._render_diagram_equation(
            "\\begin{tikzpicture}\\node (0) at (0,0) {};\\end{tikzpicture}\n"
            "= \\Pr(E,P) \\in [0,1]\n.", "22")
        stashed = " ".join(v for v in conv.store.values() if isinstance(v, str))
        self.assertIn("= \\Pr(E,P) \\in [0,1] .", stashed)

    def test_unbalanced_left_right_across_the_fence_is_flagged(self):
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        before = len(paper2md.FLAGS)
        conv._render_diagram_equation(
            "\\left\\{ \\begin{tikzpicture}\\node (0) at (0,0) {};"
            "\\end{tikzpicture}", None)
        kinds = [f["kind"] for f in paper2md.FLAGS[before:]]
        del paper2md.FLAGS[before:]
        self.assertIn("equation-diagram-split-delimiter", kinds)

    def test_bracket_display_math_diagram_is_also_split(self):
        # \[...\] is TeX's other display-math spelling and never passes
        # through do_math()'s environment-based rep() at all -- only
        # _stash_math()'s generic delimiter walk ever sees it.
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        src = ("\\[\\begin{tikzpicture}\\node (0) at (0,0) {$X$};"
               "\\end{tikzpicture}\\]")
        before = len(paper2md.FLAGS)
        out = conv.restore(conv._stash_math(src))
        del paper2md.FLAGS[before:]
        self.assertIn("```", out)
        self.assertIn("X", out)
        self.assertNotIn("\\begin{tikzpicture}", out)


class QuotedFenceDetection(unittest.TestCase):
    r"""A theorem/definition's blockquote wrapping prefixes EVERY line with
    "> ", fence and display-math delimiters included. run_checks() used to
    look only for a line starting with exactly "```" or "$$", so a diagram
    or display equation embedded in a theorem was never recognised as
    fenced/math and got scanned -- and flagged -- as loose prose."""

    def check(self, md):
        before = len(paper2md.FLAGS)
        n = paper2md.run_checks(md)
        del paper2md.FLAGS[before:]
        return n

    def test_quoted_fence_is_still_a_fence(self):
        md = "> **Lemma 1.** See below.\n>\n> ```\nEQUATION 1\n\\node raw\n> ```\n"
        self.assertEqual(self.check(md), 0)

    def test_quoted_display_math_is_still_display_math(self):
        md = "> **Lemma 1.**\n>\n> $$\n> \\widetilde{T} \\circ f\n> $$\n"
        self.assertEqual(self.check(md), 0)

    def test_unquoted_prose_is_still_checked(self):
        self.assertEqual(self.check("stray \\widetilde{T} outside math"), 1)


class ColorWrappers(unittest.TestCase):
    r"""--drop-color already strips a bare \color{...} declaration, which is
    invisible in the compiled PDF. \textcolor{c}{text} and \colorbox{c}{text}
    are the same kind of invisible revision markup, but wrap CONTENT that
    must survive -- dropping the whole command (as drop_cmd_arg would) loses
    the text, not just the color."""

    def test_textcolor_keeps_the_text_drops_the_wrapper(self):
        self.assertEqual(
            paper2md.unwrap_trailing_arg(r"a \textcolor{blue}{important} b", "textcolor", 2),
            "a important b")

    def test_colorbox_keeps_the_text(self):
        self.assertEqual(
            paper2md.unwrap_trailing_arg(r"\colorbox{PineGreen!20}{$M$}", "colorbox", 2),
            "$M$")

    def test_fcolorbox_three_args(self):
        self.assertEqual(
            paper2md.unwrap_trailing_arg(r"\fcolorbox{red}{white}{ok}", "fcolorbox", 3),
            "ok")


class BareFontDeclarations(unittest.TestCase):
    r"""{\em text} is LaTeX's older way of writing emphasis: a bare
    font-switching command as the first token of a group, rather than
    \emph{text}. do_text() only wrapped the \emph{...}/\textbf{...} call
    form, so {\em ...} survived as raw, visible LaTeX."""

    def render(self, src):
        class _Args:
            style_map, drop_color = {}, False
        conv = paper2md.Converter(paper2md.Preamble(""), {}, {}, _Args())
        return conv._wrap_bare_font_group(src, "em", "*")

    def test_group_becomes_the_emphasized_span(self):
        self.assertEqual(self.render("a {\\em fiducial} test"), "a *fiducial* test")

    def test_does_not_misfire_on_emph(self):
        # {\em ...} must not match INSIDE an unrelated \emph{...} call
        src = "a \\emph{fiducial} test"
        self.assertEqual(self.render(src), src)


class ProofMacros(unittest.TestCase):
    r"""amsthm still provides \proof/\endproof as bare pre-environment proof
    macros alongside \begin{proof}/\end{proof}, and some papers use them
    directly. Expanding them before the structural passes (the same
    treatment as \beq/\eeq) lets the existing proof-environment handling in
    do_text() pick them up."""

    def test_builtin_macros_expand_to_the_environment(self):
        out = paper2md.expand_env_macros(r"\proof a = b \endproof",
                                          paper2md.BUILTIN_MACROS)
        self.assertEqual(out, r"\begin{proof} a = b \end{proof}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
