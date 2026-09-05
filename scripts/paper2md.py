#!/usr/bin/env python3
"""
paper2md.py — deterministic LaTeX -> Markdown converter for LLM-readable output.

Does the mechanical 90% and refuses to guess the rest: anything requiring
judgement is written to a flag manifest for a human (or an LLM) to resolve.

Design rules
------------
1. Prose is PASS-THROUGH. It is never retyped, so it cannot be silently dropped.
2. Math is extracted to placeholders BEFORE any text-level markup conversion,
   and restored afterwards. This guarantees one escaping regime: every LaTeX
   command and every underscore ends up inside $...$, a $$ block, or a fence.
   All four delimiter pairs -- $...$, $$...$$, \[...\] and \(...\) -- are
   found by the same LatexWalker pass, so an unpaired-looking delimiter
   cannot shift the pairing of everything after it.
3. Numbering comes from the .aux file, the bibliography from the .bbl file:
   no PDF scraping and no staleness window, since both regenerate on every
   compile. LaTeX's counter machinery is NOT reimplemented -- but where
   LaTeX numbers something the .aux does not record (an unlabelled equation
   or theorem), a local counter continues from the last number that WAS
   read, and the result is flagged as derived rather than passed off as
   authoritative.
4. Anything ambiguous is FLAGGED, never silently dropped. Where a value can
   be derived instead of left missing it is emitted AND flagged, on the
   grounds that a visible guess beats an invisible hole.
5. Structure is PARSED, not pattern-matched: pylatexenc's LatexWalker does
   the \begin/\end matching, brace matching and comment stripping, so
   nesting is seen (a tabular inside a tabular no longer truncates the outer
   one at the first inner \end{tabular}). pylatexenc also converts accent
   constructs to Unicode -- but only fragment by fragment. latex2text is
   never run over the document: it deletes macros it does not know and
   strips math outright, which would destroy both things rule 1 and rule 2
   exist to protect. Macro harvesting and expansion stay ours; LatexWalker
   does not expand \newcommand.

Requires pylatexenc (pip install pylatexenc; 2.11 is fine).

Usage
-----
    python3 paper2md.py paper.tex -o paper.md \
        [--aux paper.aux] [--bbl paper.bbl] \
        [--styles sv=selected,mv=latent,rv=visible] \
        [--drop-color]

Outputs paper.md and paper.flags.json, and prints a summary.
"""

import argparse, json, os, re, sys
from collections import OrderedDict, Counter

try:
    from pylatexenc.latex2text import LatexNodes2Text
    from pylatexenc.latexwalker import (LatexWalker, LatexCommentNode,
                                        LatexEnvironmentNode, LatexGroupNode,
                                        LatexMathNode)
except ImportError:                                   # pragma: no cover
    sys.exit("paper2md.py needs pylatexenc (pip install pylatexenc)")

# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------

FLAGS = []

def flag(kind, detail, snippet="", line=None):
    FLAGS.append({"kind": kind, "detail": detail,
                  "snippet": snippet[:400], "line": line})

# --------------------------------------------------------------------------
# scanning primitives
# --------------------------------------------------------------------------
#
# All structural scanning goes through pylatexenc's LatexWalker. The regexes
# these replaced could not see nesting: every \begin{X}(.*?)\end{X} stopped at
# the FIRST inner \end{X}, so a tabular inside a tabular truncated the outer
# one and dropped every row after it -- silently, which is the one thing this
# converter is not allowed to do. LatexWalker parses properly and hands back
# .pos/.len, so the layers above still work on strings and spans.
#
# What LatexWalker does NOT do is expand \newcommand; macro harvesting and
# expansion below stay ours.

def _walker(s):
    return LatexWalker(s, tolerant_parsing=True)


def balanced(s, i, open_ch="{", close_ch="}"):
    """s[i] must be open_ch. Return (content, index_after_close).

    Delimiter matching is LatexWalker's, so a brace inside a % comment or a
    \\verb argument cannot throw the count off.
    """
    if i >= len(s) or s[i] != open_ch:
        return None, i
    try:
        w = _walker(s)
        if open_ch == "{":
            node, pos, ln = w.get_latex_expression(pos=i)
        else:
            node, pos, ln = w.get_latex_maybe_optional_arg(pos=i)
    except Exception:
        return None, i
    if node is None or not isinstance(node, LatexGroupNode) or ln < 2:
        return None, i
    return s[pos + 1:pos + ln - 1], pos + ln


def grab_args(s, i, n):
    """Grab n balanced {..} groups starting at s[i] (skipping whitespace)."""
    args = []
    for _ in range(n):
        while i < len(s) and s[i] in " \t\n":
            i += 1
        content, ni = balanced(s, i)
        if content is None:
            return None, i
        args.append(content)
        i = ni
    return args, i


def strip_comments(s):
    """Drop % comments, reproducing TeX's rule that a comment eats the rest of
    the line plus the following line's leading whitespace."""
    try:
        nodes, _, _ = _walker(s).get_latex_nodes()
    except Exception:
        return s
    out, i = [], 0
    def visit(ns):
        nonlocal i
        for n in ns:
            if isinstance(n, LatexCommentNode):
                out.append(s[i:n.pos])
                ps = n.comment_post_space
                # The comment node spans "%...", its newline and the leading
                # whitespace of the next line. TeX ends the line and eats that
                # indentation; anything past it (a blank line, i.e. a paragraph
                # break) is ordinary text and has to survive.
                out.append("\n" + ps[1:].lstrip(" \t")
                           if ps.startswith("\n") else ps)
                i = n.pos + n.len
                continue
            for sub in _child_nodelists(n):
                visit(sub)
    visit(nodes)
    out.append(s[i:])
    return "".join(out)


def _child_nodelists(node):
    """Every child node list hanging off `node` — body, group content and
    macro/environment arguments alike."""
    lists = []
    nl = getattr(node, "nodelist", None)
    if nl:
        lists.append(nl)
    argd = getattr(node, "nodeargd", None)
    if argd is not None and getattr(argd, "argnlist", None):
        lists.append([a for a in argd.argnlist if a is not None])
    return lists


def env_body(s, node):
    """The source between \\begin{X}<args> and \\end{X}, verbatim."""
    if not node.nodelist:
        return ""
    first, last = node.nodelist[0], node.nodelist[-1]
    return s[first.pos:last.pos + last.len]


def env_spans(s, names):
    """[(node, ancestor_env_names)] for every environment in `names`, in
    DOCUMENT ORDER, outermost first. A match is not descended into, so the
    spans never overlap; everything else is, so an equation nested inside a
    subequations block is still found -- and knows it is inside one."""
    found = []
    try:
        nodes, _, _ = _walker(s).get_latex_nodes()
    except Exception:
        return found
    def visit(ns, anc):
        for n in ns:
            if isinstance(n, LatexEnvironmentNode):
                if n.environmentname in names:
                    found.append((n, anc))
                    continue
                anc2 = anc + (n.environmentname,)
            else:
                anc2 = anc
            for sub in _child_nodelists(n):
                visit(sub, anc2)
    visit(nodes, ())
    return found


def replace_envs(s, names, fn):
    """Rewrite every environment in `names` via fn(node, body, ancestors)."""
    out, i = [], 0
    for node, anc in env_spans(s, names):
        if node.pos < i:                      # defensive: never go backwards
            continue
        out.append(s[i:node.pos])
        out.append(fn(node, env_body(s, node), anc))
        i = node.pos + node.len
    out.append(s[i:])
    return "".join(out)


# --------------------------------------------------------------------------
# accents
# --------------------------------------------------------------------------

# pylatexenc is used SURGICALLY here: each accent construct is located by
# regex and only that fragment is handed to LatexNodes2Text. Running
# latex2text over the whole document is not an option -- it deletes macros it
# does not know and strips math outright:
#
#   latex_to_text(r"\newcommand{\D}{\mathcal{D}}...The graph $\D$ and $\sel(\D)$.")
#     ->  'The graph  and ().'
#
# i.e. it destroys the two things this converter exists to preserve.

_L2T = LatexNodes2Text(math_mode="verbatim", strict_latex_spaces="based-on-source")

# \"{o}, \'e, \v{r}, \c c, \'\i ... -- control-symbol and control-word accents,
# braced or braceless. The (?![A-Za-z]) after a control-word accent keeps \v
# from matching inside \vec and \b from matching inside \bar.
_ACCENT_RE = re.compile(r"""
    \\ (?: (?P<sym>["'`^~=.])
         | (?P<word>[uvHtcdbrk])(?![A-Za-z]) )
    [ \t]*
    (?: \{ (?P<braced>[^{}]*) \}
      | \\(?P<dotless>[ij])(?![A-Za-z])
      | (?P<bare>[A-Za-z]) )
""", re.X)

# Standalone glyphs. Longest name first, and (?![A-Za-z]) so \o does not fire
# inside \omega, \l inside \ldots or \i inside \int.
_GLYPH_RE = re.compile(
    r"\\(AA|aa|AE|ae|OE|oe|DH|TH|SS|ss|O|o|L|l|i|j)(?![A-Za-z])(\{\})?([ \t]*)")


def _fragment_to_text(frag):
    """Unicode for one accent/glyph fragment, or None if pylatexenc has
    nothing to offer. None means LEAVE THE SOURCE ALONE: an accent silently
    replaced by an empty string is exactly the corruption this is fixing,
    and the untouched command is then caught by run_checks()."""
    try:
        out = _L2T.latex_to_text(frag)
    except Exception:
        return None
    return out if out and out != frag else None


# The target of a \url/\href is not prose: a "\~" in there is an author
# writing a literal tilde, and turning it into a combining accent silently
# breaks the link. Left alone, it stays visible to run_checks() instead.
_URL_ARG_RE = re.compile(r"\\(?:url|href)(?:@noop)?\s*\{[^{}]*\}")


def decode_accents(s):
    r"""Turn LaTeX accent constructs into the Unicode characters they denote.

    MUST run before the "~" -> non-breaking-space replacement in do_text():
    that replacement is a plain str.replace, so on \~{n} it eats the tilde and
    leaves "\ {n}", which is not even valid LaTeX any more.
    """
    def accent(m):
        out = _fragment_to_text(m.group(0))
        if out is None:
            flag("accent-unrendered",
                 f"accent construct {m.group(0)!r} produced no character and was "
                 f"left as raw LaTeX (\\~{{}} in a URL is the usual case) — "
                 f"fix it by hand", m.group(0))
            return m.group(0)
        return out

    def glyph(m):
        out = _fragment_to_text("\\" + m.group(1))
        if out is None:
            flag("accent-unrendered",
                 f"glyph command \\{m.group(1)} did not render and was left as raw "
                 f"LaTeX — fix it by hand", m.group(0))
            return m.group(0)
        # TeX discards the whitespace that terminates a control word (\AA ke ->
        # "Åke"), but an explicit \ss{} terminates it itself, so the space after
        # THAT is a real one.
        return out + (m.group(3) if m.group(2) else "")

    def decode(t):
        return _GLYPH_RE.sub(glyph, _ACCENT_RE.sub(accent, t))

    out, i = [], 0
    for m in _URL_ARG_RE.finditer(s):
        out.append(decode(s[i:m.start()]))
        out.append(m.group(0))
        i = m.end()
    out.append(decode(s[i:]))
    return "".join(out)


# --------------------------------------------------------------------------
# preamble harvesting
# --------------------------------------------------------------------------

BUILTIN_MACROS = {
    # braket
    r"\ket": (1, r"|#1\rangle"),
    r"\bra": (1, r"\langle #1|"),
    r"\braket": (1, r"\langle #1\rangle"),
    # mathtools noise that carries no meaning
    r"\smashoperator": (1, r"#1"),
    r"\mathrlap": (1, r"#1"),
    r"\mathllap": (1, r"#1"),
}

class Preamble:
    def __init__(self, text):
        self.raw = text
        self.macros = OrderedDict()      # name -> (nargs, body)
        self.tikz_styles = OrderedDict() # name -> style body
        self.theorems = OrderedDict()    # env -> display name
        self.title = None
        self.authors = []
        self.affils = []
        self._harvest()

    def _harvest(self):
        s = self.raw
        # \newcommand / \renewcommand / \providecommand
        for m in re.finditer(r"\\(?:new|renew|provide)command\s*\*?\s*(\{\\(\w+)\}|\\(\w+))", s):
            name = "\\" + (m.group(2) or m.group(3))
            i = m.end()
            nargs, default = 0, None
            while i < len(s) and s[i] in " \t\n":
                i += 1
            if i < len(s) and s[i] == "[":
                j = s.find("]", i)
                try: nargs = int(s[i + 1:j])
                except ValueError: nargs = 0
                i = j + 1
                while i < len(s) and s[i] in " \t\n":
                    i += 1
                if i < len(s) and s[i] == "[":     # optional-arg default
                    default, i = balanced(s, i, "[", "]")
            body, _ = balanced(s, i)
            if body is None:
                continue
            if default is not None:
                flag("macro-optional-arg",
                     f"{name} has an optional argument; expansion skipped",
                     m.group(0))
                continue
            # A macro whose body contains $ switches math mode internally. Expanding
            # it inside an existing $...$ produces nested delimiters and destroys the
            # escaping regime, so refuse and ask for an explicit replacement.
            if re.search(r"(?<!\\)\$", body):
                flag("macro-mode-switching",
                     f"{name} body contains '$' (switches math mode): {body.strip()} — "
                     f"not expanded. Supply one with "
                     f"--macro-override {name.lstrip(chr(92))}='<replacement>'",
                     m.group(0))
                continue
            self.macros[name] = (nargs, body)

        # \DeclareMathOperator
        for m in re.finditer(r"\\DeclareMathOperator\s*\*?\s*\{\\(\w+)\}\s*", s):
            body, _ = balanced(s, m.end())
            if body is not None:
                self.macros["\\" + m.group(1)] = (0, r"\operatorname{%s}" % body)

        # \NewDocumentCommand — only all-mandatory signatures are safe
        for m in re.finditer(r"\\(?:New|Renew|Provide)DocumentCommand\s*\{?\\(\w+)\}?\s*", s):
            sig, i = balanced(s, m.end())
            if sig is None:
                continue
            body, _ = balanced(s, i)
            name = "\\" + m.group(1)
            sig_clean = sig.replace(" ", "")
            if body is not None and set(sig_clean) <= {"m"}:
                self.macros[name] = (len(sig_clean), body)
            else:
                flag("macro-complex-signature",
                     f"{name} has signature '{sig}' (optional/starred); expansion skipped",
                     m.group(0))

        # \tikzset{name/.style={...}}
        for m in re.finditer(r"([A-Za-z][\w]*)\s*/\.style\s*(?:2 args)?\s*=\s*", s):
            body, _ = balanced(s, m.end())
            if body is not None:
                self.tikz_styles.setdefault(m.group(1), body.strip())

        # \newtheorem{env}{Display}
        for m in re.finditer(r"\\newtheorem\s*\*?\s*\{(\w+)\}(?:\[\w+\])?\s*\{([^}]*)\}", s):
            self.theorems[m.group(1)] = m.group(2)

        # title block
        m = re.search(r"\\title\s*(\{)", s)
        if m:
            t, _ = balanced(s, m.end() - 1)
            if t:
                self.title = re.sub(r"\\vspace\s*\{[^}]*\}", "", t).strip()
        for m in re.finditer(r"\\author\s*(?:\[([^\]]*)\])?\s*(\{)", s):
            a, _ = balanced(s, m.end() - 1)
            if a: self.authors.append((m.group(1) or "", a.strip()))
        for m in re.finditer(r"\\affil\s*(?:\[([^\]]*)\])?\s*(\{)", s):
            a, _ = balanced(s, m.end() - 1)
            if a: self.affils.append((m.group(1) or "", a.strip()))

# --------------------------------------------------------------------------
# macro expansion
# --------------------------------------------------------------------------

_ARG_RE = re.compile(r"#(\d)")

def _substitute_args(tmpl, args):
    """Fill #1, #2, ... into a macro's template text.

    Real TeX substitutes at the token level, so e.g. \\langle#2 in a
    \\newcommand body needs no separator: \\langle is already one token, and
    whatever #2 expands to starts a new one. This function works on TEXT, so
    it has to add back a space by hand wherever that token boundary would
    otherwise vanish — i.e. wherever a letter in the template ends up sitting
    directly against a letter that starts (or ends) the substituted argument.
    Checks the *template's* neighbouring characters (not the partially-built
    output), so earlier substitutions can't shift what a later one sees.
    """
    def one(m):
        k = int(m.group(1))
        a = args[k - 1] if k - 1 < len(args) else ""
        pre, post = tmpl[:m.start()], tmpl[m.end():]
        lead = " " if pre and pre[-1].isalpha() and a[:1].isalpha() else ""
        trail = " " if post and post[0].isalpha() and a[-1:].isalpha() else ""
        return lead + a + trail
    return _ARG_RE.sub(one, tmpl)

def expand_macros(body, macros, max_passes=12):
    table = dict(BUILTIN_MACROS)
    table.update(macros)
    # longest names first so \Qpa is not matched as \Q
    names = sorted(table, key=len, reverse=True)
    for _ in range(max_passes):
        changed = False
        out, i = [], 0
        while i < len(body):
            if body[i] != "\\":
                out.append(body[i]); i += 1; continue
            m = re.match(r"\\([A-Za-z@]+)\*?", body[i:])
            if not m:
                out.append(body[i:i + 2]); i += 2; continue
            name = "\\" + m.group(1)
            if name not in table:
                out.append(body[i:i + m.end()]); i += m.end(); continue
            nargs, tmpl = table[name]
            j = i + m.end()
            # TeX discards whitespace after a control word, so the source's own
            # space is NOT preserved here -- reproducing that is what keeps
            # "\sel (\D)" rendering as "\mathtt{sel}(\D)" the way LaTeX does.
            # \smashoperator[r]{...} — drop a bracket option if present
            while j < len(body) and body[j] in " \t":
                j += 1
            if j < len(body) and body[j] == "[":
                _, j = balanced(body, j, "[", "]")
            if nargs == 0:
                repl = tmpl
                out.append(repl)
                # One space is still needed where dropping it would glue the
                # expansion onto what follows and merge them into a different
                # control word: \ot -> \otimes touching "N" reads as \otimesN.
                # This is a text-level artefact only -- TeX substitutes tokens,
                # so it never needs the space. A "\\", brace or symbol on the far
                # side is already a token boundary; only two adjacent *letters*
                # actually fuse.
                next_is_letter = j < len(body) and body[j].isalpha()
                if repl[-1:].isalpha() and next_is_letter:
                    out.append(" ")
                i = j; changed = True
                continue
            args, nj = grab_args(body, j, nargs)
            if args is None:
                out.append(body[i:i + m.end()]); i += m.end(); continue
            repl = _substitute_args(tmpl, args)
            out.append(repl); i = nj; changed = True
        body = "".join(out)
        if not changed:
            break
    return body

# --------------------------------------------------------------------------
# .aux / .bbl
# --------------------------------------------------------------------------

def parse_aux(path):
    """label -> printed number, from \\newlabel{key}{{2.2}{7}...}"""
    labels = {}
    if not path or not os.path.exists(path):
        return labels
    s = open(path, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"\\newlabel\s*\{([^}]*)\}\s*\{", s):
        grp, _ = balanced(s, m.end() - 1)
        if grp is None:
            continue
        inner, _ = balanced(grp, 0)
        if inner is not None:
            labels[m.group(1)] = inner.strip()
    return labels

def _skip_bibitem_label(s, i):
    """s[i] is '[' starting a \\bibitem optional label, e.g. [{\\citenamefont
    {Steudel}\\ and\\ \\citenamefont {Ay}(2015)}]. The label commonly contains
    one or more balanced {...} groups (which may themselves be nested), so a
    naive scan to the first ']' or first '}' is wrong. Skip whole {...} groups
    as units and return the index just after the matching ']'."""
    if i >= len(s) or s[i] != "[":
        return i
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            _, j2 = balanced(s, j)
            j = j2 if j2 != j else j + 1
            continue
        if c == "]":
            return j + 1
        j += 1
    return i  # unterminated; caller will fail the subsequent {key} match

_BIB_DROP_ARGLESS = {"BibitemOpen"}
_BIB_DROP_1ARG = {"BibitemShut", "EOS"}
_BIB_UNWRAP_1 = {"bibnamefont", "bibfnamefont", "citenamefont", "natexlab", "translation"}
_BIB_EMPH_1 = {"emph"}
_BIB_BOLD_1 = {"textbf"}
_BIB_DROP_KEEP_2ND = {"bibfield", "bibinfo"}
_BIB_LINK_2 = {"href", "href@noop", "Eprint", "url"}

def _render_bib_walk(s):
    """Recursive worker for render_bib_body. Returns raw text with no
    whitespace normalization, so field-boundary spaces (e.g. the ',\\ '
    between an apsrev author list and the title that follows it, which sit
    right at the edge of two different {...} argument groups) survive
    unharmed through nested calls; only the public wrapper collapses/strips
    once, over the fully-assembled string."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "%" and (i == 0 or s[i - 1] != "\\"):
            # LaTeX line comment: drop to end of line
            k = s.find("\n", i)
            i = n if k == -1 else k + 1
            continue
        if c == "\\":
            # command names may contain '@' (\makeatletter is in effect in
            # .bbl preambles), e.g. \href@noop -- must be matched as one
            # token, not split into \href + literal "@noop".
            # accents and standalone glyphs, before the generic dispatch: the
            # unknown-command fallback below would drop \"{o} down to "o".
            am = _ACCENT_RE.match(s, i) or _GLYPH_RE.match(s, i)
            if am:
                dec = decode_accents(am.group(0))
                if dec != am.group(0):
                    out.append(dec)
                    i = am.end()
                    continue
            m = re.match(r"\\([A-Za-z@]+\*?|.)", s[i:], re.S)
            if not m:
                i += 1
                continue
            name = m.group(1)
            j = i + m.end()
            if name in (" ", "\\"):
                out.append(" ")
                i = j
                continue
            if name in ("%", "&", "_", "#", "{", "}", "~", "$"):
                out.append(name)
                i = j
                continue
            k = j
            while k < n and s[k] in " \t\r\n":
                k += 1

            def take_brace(k):
                if k < n and s[k] == "{":
                    return balanced(s, k)
                return None, k

            if name in _BIB_DROP_ARGLESS:
                i = j
                continue
            if name in _BIB_DROP_1ARG:
                arg, k2 = take_brace(k)
                i = k2 if arg is not None else j
                continue
            if name in _BIB_UNWRAP_1 or name in _BIB_EMPH_1 or name in _BIB_BOLD_1:
                arg, k2 = take_brace(k)
                if arg is None:
                    i = j
                    continue
                inner = _render_bib_walk(arg)
                if name in _BIB_EMPH_1:
                    inner = "*" + inner + "*"
                elif name in _BIB_BOLD_1:
                    inner = "**" + inner + "**"
                out.append(inner)
                i = k2
                continue
            if name in _BIB_DROP_KEEP_2ND:
                arg1, k2 = take_brace(k)
                if arg1 is None:
                    i = j
                    continue
                k3 = k2
                while k3 < n and s[k3] in " \t\r\n":
                    k3 += 1
                arg2, k4 = take_brace(k3)
                if arg2 is None:
                    i = k2
                    continue
                out.append(_render_bib_walk(arg2))
                i = k4
                continue
            if name in _BIB_LINK_2:
                arg1, k2 = take_brace(k)
                if arg1 is None:
                    i = j
                    continue
                k3 = k2
                while k3 < n and s[k3] in " \t\r\n":
                    k3 += 1
                arg2, k4 = take_brace(k3)
                if arg2 is None:
                    # \url{X} form: single arg is both the link text and target
                    url = arg1.strip()
                    out.append(f"[{url}]({url})" if url else "")
                    i = k2
                    continue
                text = _render_bib_walk(arg2)
                url = arg1.strip()
                out.append(f"[{text}]({url})" if url else text)
                i = k4
                continue
            # unknown command: drop the name; if a brace group follows, keep
            # its (recursively rendered) content rather than discarding it
            arg, k2 = take_brace(k)
            if arg is not None:
                out.append(_render_bib_walk(arg))
                i = k2
            else:
                i = j
            continue
        if c in "{}":
            i += 1
            continue
        if c == "~":
            out.append(" ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)

def render_bib_body(s):
    """Render a .bbl \\bibitem body (apsrev/REVTeX-style \\bibfield/\\bibinfo/
    \\bibnamefont/\\href markup) to plain text, keeping the actual field
    content and dropping the semantic-markup scaffolding, rather than the
    naive 'delete backslash-commands, keep their brace arguments' approach
    (which leaves literal field names like 'author'/'title'/'journal' and
    stray '{}' behind as visible text).

    Accents are resolved by _render_bib_walk itself: its unknown-command
    fallback ("drop the name, keep the brace argument") turns \\"{o} into a
    bare "o", silently losing the umlaut — and a bibliography is where
    accented names actually live. Decoding inside the walk rather than over
    the whole string keeps \\href/\\url TARGETS out of it, where a literal
    \\~ is an author writing a tilde rather than an accent."""
    text = _render_bib_walk(s)
    text = text.replace("``", '"').replace("''", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text

def parse_bbl(path):
    """Return (cite_key -> number, [rendered entries in order])."""
    order, entries = {}, []
    if not path or not os.path.exists(path):
        return order, entries
    s = open(path, encoding="utf-8", errors="replace").read()
    # (?![A-Za-z]) so this doesn't also split on \bibitemStop / \bibitemNoStop,
    # which apsrev-style preambles \providecommand before any real \bibitem.
    items = re.split(r"\\bibitem(?![A-Za-z])", s)[1:]
    for n, it in enumerate(items, 1):
        j = 0
        while j < len(it) and it[j] in " \t\r\n":
            j += 1
        if j < len(it) and it[j] == "[":
            j = _skip_bibitem_label(it, j)
            while j < len(it) and it[j] in " \t\r\n":
                j += 1
        if j >= len(it) or it[j] != "{":
            flag("bbl-unparsed-entry",
                 f"\\bibitem #{n} in {os.path.basename(path)}: no citation key found "
                 f"(malformed entry, or an unterminated optional label). Any \\cite "
                 f"of this reference will not resolve, and it will be missing from "
                 f"the reference list.", it[:300])
            continue
        key, after = balanced(it, j)
        if key is None:
            flag("bbl-unparsed-entry",
                 f"\\bibitem #{n} in {os.path.basename(path)}: citation key group is "
                 f"unterminated. Any \\cite of this reference will not resolve, and it "
                 f"will be missing from the reference list.", it[:300])
            continue
        order[key] = n
        body = it[after:]
        body = re.split(r"\\end\{thebibliography\}", body)[0]
        body = re.sub(r"\\newblock\b", " ", body)
        body = render_bib_body(body)
        entries.append((n, body))
    return order, entries

# --------------------------------------------------------------------------
# tikz -> node/edge block
# --------------------------------------------------------------------------

NODE_RE = re.compile(r"\\node\s*(?:\[([^\]]*)\])?\s*\(([^)]*)\)\s*(?:at\s*\(([^)]*)\))?\s*\{")
EDGE_RE = re.compile(
    r"\\(?:draw|path)\s*(?:\[([^\]]*)\])?\s*\(([^)]*)\)\s*"
    r"(--|to)\s*(?:\[[^\]]*\])?\s*\(([^)]*)\)\s*;")
SAFE_CMDS = {"node", "draw", "path", "coordinate", "begin", "end", "tikzset", "centering"}

def convert_tikz(src, styles, style_map, fig_id):
    """Return (block_text, ok) for one tikzpicture body."""
    nodes, edges = [], []
    for m in NODE_RE.finditer(src):
        label, _ = balanced(src, m.end() - 1)
        stylelist = [x.strip() for x in (m.group(1) or "").split(",") if x.strip()]
        prim = None
        for st in stylelist:
            base = st.split("=")[0].strip()
            if base in styles or base in style_map:
                prim = base; break
        lab = (label or "").strip()
        lab = re.sub(r"^\$(.*)\$$", r"\1", lab).strip()
        nodes.append({"name": m.group(2).strip(), "label": lab, "style": prim})
    for m in EDGE_RE.finditer(src):
        a, b = m.group(2).strip(), m.group(4).strip()
        if a.startswith("$") or b.startswith("$"):
            continue  # computed coordinate, not a real endpoint
        directed = "->" in (m.group(1) or "")
        edges.append((a, b, directed))

    # anything we did not consume that looks structural?
    residue = NODE_RE.sub(" ", src)
    residue = EDGE_RE.sub(" ", residue)
    unknown = set(re.findall(r"\\([A-Za-z]+)", residue)) - SAFE_CMDS
    ok = True

    # A \node name defined more than once in the SAME tikzpicture is a strong
    # signal that this picture actually contains multiple sub-panels (typically
    # \begin{scope}[xshift=...]...\end{scope} blocks, one per panel) that reuse
    # node identifiers panel to panel. TikZ itself allows this — the later
    # \node silently rebinds the name, and each \draw resolves against whichever
    # definition came before it — but this extractor collects every \node/\draw
    # in the whole tikzpicture into one flat list keyed by name, so a reused name
    # makes the "Nodes:"/"Edges:" output below wrong for every panel: duplicate
    # entries in the node list, and edges from an earlier panel silently
    # relabelled with a later panel's node of the same name.
    name_counts = Counter(n["name"] for n in nodes)
    dup_names = sorted(name for name, c in name_counts.items() if c > 1)
    if dup_names:
        ok = False
        flag("tikz-duplicate-node-name",
             f"{fig_id}: node name(s) {dup_names} defined more than once in this "
             f"tikzpicture. TikZ allows this (each \\draw uses whichever "
             f"definition precedes it), but it usually means multiple sub-panels "
             f"share one tikzpicture and reuse node names/labels across panels — "
             f"the Nodes/Edges list below merges all panels and is unreliable. "
             f"Split the tikzpicture by \\scope block and rebuild each panel's "
             f"node/edge list by hand.",
             src)

    # Even without a reused node NAME, multiple \scope blocks (especially ones
    # offset with xshift/yshift, the usual way of laying out side-by-side
    # sub-figures) mean this tikzpicture is likely several logically separate
    # diagrams sharing one picture. Node IDS can differ per scope while the
    # DISPLAYED LABELS still repeat (e.g. every panel has its own node happening
    # to be labelled "$A$"), which this extractor also cannot tell apart from a
    # single shared diagram — so flag it even when dup_names is empty.
    scope_count = len(re.findall(r"\\begin\{scope\}", src))
    if scope_count > 1 and not dup_names:
        ok = False
        flag("tikz-multi-scope",
             f"{fig_id}: {scope_count} \\scope blocks in one tikzpicture, with no "
             f"reused node names — check by hand whether these are separate panels "
             f"whose node LABELS repeat (e.g. each panel has its own node shown as "
             f"\"$A$\"); the flat Nodes/Edges list below concatenates every scope "
             f"as if it were one diagram.",
             src)

    if unknown:
        ok = False
        flag("tikz-unparsed-commands",
             f"{fig_id}: unhandled TikZ commands {sorted(unknown)} — check the figure by hand",
             src)
    if not nodes:
        ok = False
        flag("tikz-no-nodes", f"{fig_id}: no \\node found; probably a plot or picture", src)

    by_style = OrderedDict()
    for n in nodes:
        sem = style_map.get(n["style"], None)
        key = sem or (n["style"] or "unstyled")
        by_style.setdefault(key, []).append(n)
        if n["style"] and n["style"] not in style_map:
            flag("tikz-style-unmapped",
                 f"{fig_id}: node style '{n['style']}' has no semantic name "
                 f"(--styles {n['style']}=...); raw definition: "
                 f"{styles.get(n['style'], '?')}", "")

    name2label = {n["name"]: (n["label"] or n["name"]) for n in nodes}
    lines = [f"{fig_id}", "Nodes:"]
    for key, group in by_style.items():
        labels = ", ".join(n["label"] or n["name"] for n in group)
        lines.append(f"  {labels}   [{key}]")
    lines.append("Edges:")
    for a, b, directed in edges:
        arrow = "->" if directed else "--"
        lines.append(f"  {name2label.get(a, a)} {arrow} {name2label.get(b, b)}")
    return "\n".join(lines), ok

# --------------------------------------------------------------------------
# main conversion
# --------------------------------------------------------------------------

MATH_ENVS = ["equation", "multline", "align", "gather", "eqnarray", "displaymath"]

def next_eq_number(prev, in_subequations=False):
    """The number LaTeX would give the equation after `prev`.

    Handles flat numbering (4 -> 5), \counterwithin sectioning (2.2 -> 2.3)
    and subequations, where the letter advances inside the block (5a -> 5b)
    but the parent counter resumes on leaving it (8b -> 9). Returns None when
    `prev` is None or has no numeric tail -- the caller then flags instead of
    guessing.
    """
    if not prev:
        return None
    m = re.match(r"^(.*?)(\d+)([a-zA-Z]?)$", prev)
    if not m:
        return None
    head, num, suffix = m.groups()
    if in_subequations:
        if suffix:                                # 5a -> 5b, still inside
            return head + num + chr(ord(suffix) + 1)
        return head + str(int(num) + 1) + "a"     # 4 -> 5a, entering
    return head + str(int(num) + 1)               # 8b -> 9, or 4 -> 5

class Converter:
    def __init__(self, pre, labels, cite_order, args):
        self.pre, self.labels, self.cite_order, self.args = pre, labels, cite_order, args
        self.counters = {}
        self.eq_last = None      # last equation number emitted, for derivation
        self.store = {}
        self.n = 0
        self.footnotes = []

    def stash(self, text):
        self.n += 1
        key = f"\x00PM{self.n}\x00"
        # a $$ block must sit alone between blank lines or the checker (and most
        # renderers) will not recognise it as display math
        if text.startswith("$$"):
            text = "\n\n" + text + "\n\n"
        self.store[key] = text
        return key

    def restore(self, s):
        for _ in range(6):
            changed = False
            for k, v in self.store.items():
                if k in s:
                    s = s.replace(k, v); changed = True
            if not changed:
                break
        return s

    # ---- figures ----
    def do_figures(self, s):
        def one(node, _inner, _anc):
            body = s[node.pos:node.pos + node.len]
            caps = []
            for cm in re.finditer(r"\\caption\s*\{", body):
                c, _ = balanced(body, cm.end() - 1)
                if c: caps.append(c.strip())
            subcaps = re.findall(r"\\begin\{subfigure\}", body)
            if subcaps and len(caps) > len(subcaps):
                cap = caps[-1] + " " + " ".join(
                    f"({chr(97+i)}) {c}" for i, c in enumerate(caps[:-1]))
            else:
                cap = caps[-1] if caps else ""
            lm = re.search(r"\\label\s*\{([^}]*)\}", body)
            num = self.labels.get(lm.group(1), "?") if lm else "?"
            fig_id = f"FIGURE {num}"
            if re.search(r"\\includegraphics", body):
                flag("figure-image",
                     f"{fig_id}: uses \\includegraphics — needs a human/LLM description",
                     body)
                block = f"{fig_id} — [IMAGE: needs description]"
                parts = [f"```\n{block}\n```"]
            else:
                # RAW SOURCE, span by span: the node/edge extractor parses the
                # TikZ itself and a nested scope/tikzpicture must not truncate it.
                pics = [body[n.pos:n.pos + n.len]
                        for n, _ in env_spans(body, {"tikzpicture"})]
                parts = []
                for i, p in enumerate(pics):
                    sub = fig_id + (f"({chr(97+i)})" if len(pics) > 1 else "")
                    blk, _ = convert_tikz(p, self.pre.tikz_styles,
                                          self.args.style_map, sub)
                    parts.append(f"```\n{blk}\n```")
                if not pics:
                    flag("figure-unknown", f"{fig_id}: no tikzpicture and no image", body)
            cap_md = f"\n**Figure {num}:** {cap}\n" if cap else ""
            return "\n\n" + "\n\n".join(parts) + "\n" + cap_md + "\n"
        return replace_envs(s, {"figure", "figure*", "subfigure"}, one)

    # ---- math ----
    def do_math(self, s):
        # Display environments, visited in ONE walk so they come in document
        # order: batching all the \begin{equation}s before any \begin{align}
        # would make a sequential equation counter meaningless. Whether an
        # environment sits inside \begin{subequations} -- which is what
        # decides 5a -> 5b rather than 8b -> 9 -- is now an ancestor test on
        # the node tree rather than an offset comparison against separately
        # matched spans.
        if True:
            names = set(MATH_ENVS) | {e + "*" for e in MATH_ENVS}
            def rep(node, inner, anc):
                env = node.environmentname
                starred = env.endswith("*")
                env = env[:-1] if starred else env
                labs = re.findall(r"\\label\s*\{([^}]*)\}", inner)
                tag = ""
                if starred:
                    # \begin{equation*} etc. are unnumbered in LaTeX too
                    pass
                elif labs:
                    num = self.labels.get(labs[0])
                    if num:
                        tag = "\n\\tag{%s}" % num
                        self.eq_last = num
                    else:
                        flag("equation-unnumbered",
                             f"no .aux entry for equation label '{labs[0]}' — "
                             f"equation emitted without a number", inner)
                    if len(labs) > 1:
                        # an align/subequations block carrying several \label's is
                        # several numbered equations in LaTeX, but becomes one $$
                        # block here, which can hold only one \tag
                        others = [(l, self.labels.get(l, "?")) for l in labs[1:]]
                        self.eq_last = others[-1][1] if others[-1][1] != "?" else self.eq_last
                        flag("equation-multi-label",
                             f"block tagged {self.labels.get(labs[0], '?')} also carries "
                             f"{', '.join('%s=%s' % o for o in others)}; only the first "
                             f"number is emitted. Split the rows by hand if the extra "
                             f"numbers are referenced.", inner)
                else:
                    prev = self.eq_last
                    derived = next_eq_number(prev, "subequations" in anc)
                    if derived:
                        tag = "\n\\tag{%s}" % derived
                        self.eq_last = derived
                        flag("equation-derived-number",
                             f"unlabelled {env} numbered {derived}, continuing from {prev}. "
                             f"It has no \\label, so this is derived rather than read from "
                             f"the .aux — check it against the PDF.", inner)
                    else:
                        flag("equation-unnumbered",
                             f"unlabelled {env} and no previous number to continue from — "
                             f"equation emitted without a number", inner)
                inner = re.sub(r"\\label\s*\{[^}]*\}", "", inner)
                inner = re.sub(r"\\nonumber", "", inner)
                if env in ("multline", "gather", "eqnarray", "align"):
                    inner = "\\begin{aligned}%s\\end{aligned}" % inner
                return self.stash("$$\n%s%s\n$$" % (inner.strip(), tag))
            s = replace_envs(s, names, rep)
        s = re.sub(r"\\(?:begin|end)\{subequations\}", "", s)
        return self._stash_math(s)

    def _stash_math(self, s):
        r"""$...$, $$...$$, \[...\] and \(...\) -> placeholders, in ONE walk.

        $$...$$ is TeX's own display form and used not to be handled at all.
        The inline-$ regex that ran instead could not pair it: on "$$ x $$" it
        found no match at the first delimiter, matched "$ x $" from the second,
        and left a lone $ behind — so from the first $$ in a paper onwards
        every inline pair was offset by one, and whole paragraphs of prose were
        stashed as "math" and came back into the Markdown as raw LaTeX.
        LatexWalker knows all four delimiter pairs, and knows \$ is not one.
        """
        try:
            top, _, _ = _walker(s).get_latex_nodes()
        except Exception:
            return s
        found = []
        def visit(ns):
            for n in ns:
                if isinstance(n, LatexMathNode):
                    found.append(n)
                    continue
                for sub in _child_nodelists(n):
                    visit(sub)
        visit(top)
        out, i = [], 0
        for n in found:
            if n.pos < i:
                continue
            open_d, close_d = n.delimiters
            inner = s[n.pos + len(open_d):n.pos + n.len - len(close_d)]
            out.append(s[i:n.pos])
            if n.displaytype == "display":
                out.append(self.stash("$$\n%s\n$$" % inner.strip()))
            else:
                # $...$ is passed through verbatim, as it always was; only the
                # \(...\) spelling was ever stripped.
                out.append(self.stash("$%s$" % (inner.strip() if open_d != "$" else inner)))
            i = n.pos + n.len
        out.append(s[i:])
        return "".join(out)

    # ---- text markup ----
    def do_text(self, s):
        # Accents first: they must be resolved before the "~" -> space
        # replacement at the end of this method, which is a plain str.replace
        # and would otherwise eat the tilde out of \~{n} and leave "\ {n}".
        # Math is already stashed as placeholders by now (design rule 2), so
        # this only ever sees text.
        s = decode_accents(s)

        # theorem-like environments. Still one environment name at a time, so
        # a lemma inside a proof is converted before the proof wraps it.
        for env, disp in list(self.pre.theorems.items()) + [("proof", "Proof")]:
            def rep(node, inner, anc, env=env, disp=disp):
                # \begin{theorem}[Note] — pylatexenc leaves the optional
                # argument of an unknown environment at the head of the body.
                note = ""
                nm = re.match(r"\s*\[([^\]]*)\]", inner)
                if nm:
                    note, inner = nm.group(1).strip(), inner[nm.end():]
                lm = re.search(r"\\label\s*\{([^}]*)\}", inner)
                num = self.labels.get(lm.group(1), "") if lm else ""
                if env != "proof":
                    # LaTeX still prints a number for an unlabelled environment,
                    # so keep our own counter in step with the .aux ones
                    self.counters[env] = self.counters.get(env, 0) + 1
                    if not num:
                        num = str(self.counters[env])
                    else:
                        try: self.counters[env] = int(str(num).split(".")[-1])
                        except ValueError: pass
                inner = re.sub(r"\\label\s*\{[^}]*\}", "", inner).strip()
                if env == "proof":
                    return "\n\n*Proof.* %s $\\square$\n\n" % inner
                head = f"**{disp} {num}".strip() + (f" ({note})" if note else "") + ".**"
                quoted = "\n".join("> " + ln if ln.strip() else ">"
                                   for ln in (head + " " + inner).split("\n"))
                return "\n\n" + quoted + "\n\n"
            s = replace_envs(s, {env}, rep)

        # restatable: \begin{restatable}{lemma}{MacroName} body \end{restatable}
        def restate(node, inner, _anc):
            rm = re.match(r"\s*\{(\w+)\}\s*\{(\w+)\}", inner)
            if rm is None:
                flag("restate-unparsed",
                     "\\begin{restatable} without the {env}{MacroName} arguments; "
                     "left as raw LaTeX", inner[:200])
                return s[node.pos:node.pos + node.len]
            env, macro, inner = rm.group(1), rm.group(2), inner[rm.end():]
            disp = self.pre.theorems.get(env, env.capitalize())
            lm = re.search(r"\\label\s*\{([^}]*)\}", inner)
            num = self.labels.get(lm.group(1), "") if lm else ""
            inner = re.sub(r"\\label\s*\{[^}]*\}", "", inner).strip()
            self.store.setdefault("__restate__", {})
            body = f"**{disp} {num}.**".replace(" .", ".") + " " + inner
            RESTATE[macro] = body
            quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in body.split("\n"))
            return "\n\n" + quoted + "\n\n"
        s = replace_envs(s, {"restatable", "restatable*"}, restate)
        # \MacroName*  -> the stored statement
        def unrestate(m):
            body = RESTATE.get(m.group(1))
            if body is None:
                flag("restate-missing", f"\\{m.group(1)}* has no stored statement", m.group(0))
                return m.group(0)
            return "\n\n" + "\n".join("> " + ln if ln.strip() else ">"
                                      for ln in body.split("\n")) + "\n\n"
        if RESTATE:
            s = re.sub(r"\\(" + "|".join(map(re.escape, RESTATE)) + r")\*(?!\w)",
                       unrestate, s)

        # sections
        s = re.sub(r"\\section\*?\s*\{", lambda m: "\n\n## ", s)
        s = re.sub(r"\\subsection\*?\s*\{", lambda m: "\n\n### ", s)
        s = re.sub(r"\\subsubsection\*?\s*\{", lambda m: "\n\n#### ", s)
        s = self._close_heading(s)

        # footnotes
        def fn(m):
            body, _ = balanced(s2[0], m.end() - 1)
            return ""
        out, i = [], 0
        while True:
            m = re.search(r"\\footnote\s*\{", s[i:])
            if not m: out.append(s[i:]); break
            start = i + m.start()
            body, after = balanced(s, i + m.end() - 1)
            out.append(s[i:start])
            self.footnotes.append(body.strip() if body else "")
            out.append("[^%d]" % len(self.footnotes))
            i = after
        s = "".join(out)
        if self.footnotes:
            s += "\n\n" + "\n\n".join("[^%d]: %s" % (i, t)
                                         for i, t in enumerate(self.footnotes, 1))
            self.footnotes_spliced = True

        # emphasis / fonts
        for cmd, wrap in [("emph", "*"), ("textit", "*"), ("textbf", "**"),
                          ("texttt", "`"), ("textsc", "")]:
            s = self._wrap(s, cmd, wrap)

        # lists
        s = re.sub(r"\\begin\{itemize\}", "\n", s)
        s = re.sub(r"\\end\{itemize\}", "\n", s)
        s = re.sub(r"\\begin\{enumerate\}", "\n", s)
        s = re.sub(r"\\end\{enumerate\}", "\n", s)
        s = re.sub(r"\\item\s+", "\n- ", s)

        # cross-references
        def ref(m):
            key = m.group(2)
            num = self.labels.get(key)
            if num is None:
                flag("ref-unresolved",
                     f"\\{m.group(1)}{{{key}}} — no .aux entry (run with --aux)", m.group(0))
                return f"[{key}]"
            return f"({num})" if m.group(1) == "eqref" else num
        s = re.sub(r"\\(ref|eqref)\s*\{([^}]*)\}", ref, s)

        def cite(m):
            keys = [k.strip() for k in m.group(2).split(",")]
            nums = []
            for k in keys:
                n = self.cite_order.get(k)
                if n is None:
                    flag("cite-unresolved", f"\\cite{{{k}}} — no .bbl entry (run with --bbl)", "")
                    nums.append(k)
                else:
                    nums.append(str(n))
            if m.group(1) == "citet":
                flag("citet-manual",
                     f"\\citet{{{m.group(2)}}} renders author names via the .bst; "
                     f"emitted as a bare number — check against the PDF", m.group(0))
            return "[" + ", ".join(nums) + "]"
        s = re.sub(r"\\(cite[tp]?)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", cite, s)

        # tables
        s = self._tables(s)

        # leftover structural noise
        s = re.sub(r"\\(?:label|nocite|bibliographystyle|bibliography|maketitle|centering"
                   r"|setlength|setcounter|addtolength|counterwithin|renewcommand"
                   r"|vspace|hspace|bigskip|medskip|smallskip|noindent|onecolumn|twocolumn"
                   r"|appendix|FloatBarrier|center|par)\b\s*(\{[^}]*\}|\[[^\]]*\])?", "", s)
        s = re.sub(r"\\begin\{(document|strip|abstract|center|subfigure)\}", "", s)
        s = re.sub(r"\\end\{(document|strip|abstract|center|subfigure)\}", "", s)
        if self.args.drop_color:
            s = re.sub(r"\\color\s*\{[^}]*\}", "", s)
        s = re.sub(r"\\ ", " ", s)          # control-space after a macro
        s = re.sub(r"\\([&%#_{}])", r"\1", s)  # escaped special characters
        s = s.replace("---", "—").replace("~", " ")
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s

    def _close_heading(self, s):
        """Our section replacement left a dangling '{'; close it at the brace."""
        out, i = [], 0
        while True:
            m = re.search(r"\n\n(#{2,4}) ", s[i:])
            if not m:
                out.append(s[i:]); break
            start = i + m.start()
            out.append(s[i:start + m.end() - m.start()])
            j = start + (m.end() - m.start())
            # s[j-1] is the space after the hashes; the '{' was consumed already
            depth, k = 1, j
            while k < len(s) and depth:
                if s[k] == "{": depth += 1
                elif s[k] == "}": depth -= 1
                k += 1
            out.append(s[j:k - 1])
            i = k
        return "".join(out)

    def _wrap(self, s, cmd, wrap):
        out, i = [], 0
        while True:
            m = re.search(r"\\%s\s*\{" % cmd, s[i:])
            if not m:
                out.append(s[i:]); break
            start = i + m.start()
            body, after = balanced(s, i + m.end() - 1)
            out.append(s[i:start])
            out.append(f"{wrap}{body}{wrap}" if body else "")
            i = after
        return "".join(out)

    def _tables(self, s):
        def one(node, body, _anc):
            if re.search(r"\\multirow|\\multicolumn", body):
                flag("table-complex",
                     "tabular uses multirow/multicolumn — convert by hand", body)
                return self.stash("```\n[COMPLEX TABLE — see flags]\n```")
            # A tabular inside a cell used to truncate the outer table at the
            # inner \end{tabular}, silently dropping every row after it. The
            # walker gets the boundary right; the inner table still cannot be
            # rendered inside a Markdown cell, so it is emitted as a marker and
            # flagged (design rule 4) rather than dropped or guessed at.
            inner = env_spans(body, {"tabular"})
            if inner:
                out, i = [], 0
                for n, _ in inner:
                    out.append(body[i:n.pos])
                    out.append("[nested table — see flags]")
                    i = n.pos + n.len
                out.append(body[i:])
                flag("table-nested",
                     f"{len(inner)} nested tabular(s) inside a table cell; the outer "
                     f"table is emitted in full with a marker in that cell, but the "
                     f"inner table(s) need writing by hand: "
                     + " || ".join(body[n.pos:n.pos + n.len] for n, _ in inner),
                     body)
                body = "".join(out)
            rows = [r.strip() for r in re.split(r"\\\\", body) if r.strip()]
            grid = []
            for r in rows:
                r = re.sub(r"\\hline|\\toprule|\\midrule|\\bottomrule", "", r).strip()
                if not r: continue
                grid.append([c.strip() for c in r.split("&")])
            if not grid: return ""
            w = max(len(r) for r in grid)
            grid = [r + [""] * (w - len(r)) for r in grid]
            out = ["| " + " | ".join(grid[0]) + " |",
                   "|" + "---|" * w]
            for r in grid[1:]:
                out.append("| " + " | ".join(r) + " |")
            return self.stash("\n" + "\n".join(out) + "\n")
        return replace_envs(s, {"tabular", "tabular*"}, one)

RESTATE = {}

# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

ACCENTS = "tilde|hat|bar|vec|dot|ddot|widetilde|widehat|overline|check|acute|grave"
FONTS = "mathcal|mathbb|mathrm|mathtt|mathbf|mathsf|mathfrak|boldsymbol"

def normalise_math(s, overrides):
    """Fixes LaTeX tolerates but KaTeX/MathJax do not."""
    # \tilde\mathcal{H} -> \tilde{\mathcal{H}}  (accent needs a braced argument)
    s = re.sub(r"\\(%s)\s*\\(%s)\s*\{([^{}]*)\}" % (ACCENTS, FONTS),
               r"\\\1{\\\2{\3}}", s)
    # \text{<math-only expansion>} -> <expansion>: an override substituted inside
    # a \text{} wrapper from the source is not valid text mode
    for rep in overrides:
        if rep and re.search(r"\\(?:math|operatorname|to|frac|sum|prod)", rep):
            s = s.replace("\\text{%s}" % rep, rep)
            s = s.replace("\\text{{%s}}" % rep, rep)
    return s

def run_checks(md):
    """Verify the one-escaping-regime property and look for leftovers."""
    lines = md.split("\n")
    in_fence = in_disp = False
    viol = []
    for i, ln in enumerate(lines, 1):
        if ln.startswith("```"): in_fence = not in_fence; continue
        if in_fence: continue
        if re.match(r"^\$\$\s*$", ln): in_disp = not in_disp; continue
        if in_disp: continue
        stripped = re.sub(r"\$\$[^$]*\$\$", "", ln)   # display math on one line
        stripped = re.sub(r"\$[^$]*\$", "", stripped)
        stripped = re.sub(r"\[\^\d+\]:?", "", stripped)
        # A bare command is not always a control WORD: LaTeX's commonest accents
        # are control SYMBOLS (\"o, \'e, \~n, \=a, \.z), so a \\[A-Za-z]+ scan
        # walks straight past exactly the constructs that corrupt text silently.
        # Look for a backslash before a non-letter too, minus the six escapes
        # that are legitimately how Markdown carries those characters.
        if ("_" in stripped
                or re.search(r"\\[A-Za-z]+", stripped)
                or re.search(r"\\[^A-Za-z&%#_{}]", stripped)):
            viol.append((i, ln.strip()[:100]))
    if viol:
        for i, t in viol[:20]:
            flag("escaping-regime",
                 f"line {i}: markdown-visible '_' or bare LaTeX outside math/fence", t, i)
    return len(viol)

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tex")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--aux", default=None)
    ap.add_argument("--bbl", default=None)
    ap.add_argument("--styles", default="",
                    help="comma list mapping tikz styles to semantics, e.g. sv=selected,mv=latent")
    ap.add_argument("--drop-color", action="store_true",
                    help="strip \\color{...} revision markup")
    ap.add_argument("--macro-override", action="append", default=[],
                    metavar="NAME=REPLACEMENT",
                    help="force a macro's expansion (used in both modes)")
    ap.add_argument("--macro-override-text", action="append", default=[],
                    metavar="NAME=REPLACEMENT",
                    help="expansion to use OUTSIDE math (e.g. flip='$\\mathtt{ancS}\\to\\mathtt{chM}$')")
    ap.add_argument("--macro-override-math", action="append", default=[],
                    metavar="NAME=REPLACEMENT",
                    help="expansion to use INSIDE math (e.g. flip='\\mathtt{ancS}\\to\\mathtt{chM}')")
    a = ap.parse_args()
    a.style_map = dict(p.split("=", 1) for p in a.styles.split(",") if "=" in p)

    src = open(a.tex, encoding="utf-8", errors="replace").read()
    src = strip_comments(src)
    if "\\begin{document}" in src:
        pre_txt, body = src.split("\\begin{document}", 1)
    else:
        pre_txt, body = "", src
    pre = Preamble(pre_txt)

    # auto-locate aux/bbl next to the source
    stem = os.path.splitext(a.tex)[0]
    aux = a.aux or (stem + ".aux" if os.path.exists(stem + ".aux") else None)
    bbl = a.bbl or (stem + ".bbl" if os.path.exists(stem + ".bbl") else None)
    if not aux:
        flag("no-aux", "No .aux file: cross-references cannot be numbered. "
                       "Compile the paper and re-run with --aux.")
    if not bbl:
        flag("no-bbl", "No .bbl file: citations cannot be numbered and no reference "
                       "list will be emitted. Run bibtex and re-run with --bbl.")
    labels = parse_aux(aux)
    cite_order, bib_entries = parse_bbl(bbl)

    for st in pre.tikz_styles:
        if st not in a.style_map:
            pass  # reported per-use in convert_tikz

    for ov in a.macro_override:
        if "=" not in ov:
            continue
        nm, rep = ov.split("=", 1)
        pre.macros["\\" + nm.lstrip("\\")] = (0, rep)
        FLAGS[:] = [f for f in FLAGS
                    if not (f["kind"] == "macro-mode-switching"
                            and f["detail"].startswith("\\" + nm.lstrip("\\") + " "))]
    def apply(ovs, table):
        for ov in ovs:
            if "=" in ov:
                nm, rep = ov.split("=", 1)
                table["\\" + nm.lstrip("\\")] = (0, rep)
        return table
    common = apply(a.macro_override, {})
    text_macros = dict(pre.macros); text_macros.update(common)
    math_macros = dict(pre.macros); math_macros.update(common)
    apply(a.macro_override_text, text_macros)
    apply(a.macro_override_math, math_macros)
    resolved = {("\\" + o.split("=")[0].lstrip("\\"))
                for o in a.macro_override + a.macro_override_text + a.macro_override_math}
    FLAGS[:] = [f for f in FLAGS
                if not (f["kind"].startswith("macro-")
                        and (f["detail"].split()[0] in resolved
                             or f["detail"].split()[0] not in body))]

    conv = Converter(pre, labels, cite_order, a)
    body = conv.do_figures(body)
    body = conv.do_math(body)                      # math -> placeholders
    for k, v in list(conv.store.items()):          # expand inside math
        if isinstance(v, str):
            conv.store[k] = expand_macros(v, math_macros)
    body = expand_macros(body, text_macros)        # expand outside math
    body = conv.do_text(body)
    ovr = [o.split("=", 1)[1] for o in
           a.macro_override + a.macro_override_text + a.macro_override_math if "=" in o]
    for k, v in list(conv.store.items()):
        if isinstance(v, str):
            conv.store[k] = normalise_math(v, ovr)
    body = conv.restore(body)
    # $a$$b$ is ambiguous (reads as a display opener); merge adjacent inline
    # groups. Display blocks sit alone on their line, so requiring non-newline
    # on both sides leaves them untouched.
    body = re.sub(r"(?<=[^\n])\$\$(?=[^\n])", "", body)

    # front matter
    head = []
    # The title block never passes through do_text(), so decode its accents here.
    if pre.title:
        head.append("# " + decode_accents(pre.title))
    else:
        flag("no-title", "No \\title found in the preamble.")
    if pre.authors:
        head.append(", ".join(f"**{decode_accents(n)}**" + (f"<sup>{k}</sup>" if k else "")
                              for k, n in pre.authors))
    for k, v in pre.affils:
        head.append(f"<sup>{k}</sup> {decode_accents(v)}")
    md = "\n\n".join(head) + "\n\n" + body.strip() + "\n"

    if conv.footnotes and not getattr(conv, "footnotes_spliced", False):
        md += "\n\n" + "\n\n".join(f"[^{i}]: {t}" for i, t in
                                   enumerate(conv.footnotes, 1)) + "\n"
    if bib_entries:
        md += "\n\n## References\n\n" + "\n\n".join(f"[{n}] {t}" for n, t in bib_entries) + "\n"

    md = re.sub(r"\n{3,}", "\n\n", md)
    out = a.out or (stem + ".md")
    open(out, "w", encoding="utf-8").write(md)

    nviol = run_checks(md)
    fl = out.rsplit(".", 1)[0] + ".flags.json"
    json.dump(FLAGS, open(fl, "w"), indent=2)

    by_kind = {}
    for f in FLAGS:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    print(f"wrote {out}  ({len(md)} bytes)")
    print(f"wrote {fl}  ({len(FLAGS)} flags)")
    print(f"escaping-regime violations: {nviol}")
    if by_kind:
        print("\nflags by kind:")
        for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
            print(f"  {v:4d}  {k}")

if __name__ == "__main__":
    main()
