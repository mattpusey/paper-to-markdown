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
3. Numbering comes from the .aux file, the bibliography from the .bbl file.
   No counter reimplementation, no PDF scraping, no staleness window.
4. Anything ambiguous is FLAGGED, not guessed.

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

# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------

FLAGS = []

def flag(kind, detail, snippet="", line=None):
    FLAGS.append({"kind": kind, "detail": detail,
                  "snippet": snippet[:400], "line": line})

# --------------------------------------------------------------------------
# brace matching
# --------------------------------------------------------------------------

def balanced(s, i, open_ch="{", close_ch="}"):
    """s[i] must be open_ch. Return (content, index_after_close)."""
    if i >= len(s) or s[i] != open_ch:
        return None, i
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None, i

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
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i:i + 2]); i += 2; continue
        if c == "%":
            j = s.find("\n", i)
            if j == -1: break
            i = j + 1
            # a comment consumes the newline plus leading whitespace (TeX rule)
            while i < len(s) and s[i] in " \t":
                i += 1
            out.append("\n")
            continue
        out.append(c); i += 1
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
            ws_start = j
            # \smashoperator[r]{...} — drop a bracket option if present
            while j < len(body) and body[j] in " \t":
                j += 1
            had_ws = j > ws_start  # source had a real space/tab here (TeX eats it)
            if j < len(body) and body[j] == "[":
                _, j = balanced(body, j, "[", "]")
            if nargs == 0:
                repl = tmpl
                out.append(repl)
                # Reinsert one space when the source had one here, or when none
                # did but gluing the expansion straight onto what follows would
                # merge into a different control word (e.g. \ot -> \otimes
                # directly touching "N" would read as \otimesN). A leading "\\"
                # or brace/symbol on the far side never needs this: only two
                # adjacent *letters* actually fuse into one TeX token.
                next_is_letter = j < len(body) and body[j].isalpha()
                if had_ws or (repl[-1:].isalpha() and next_is_letter):
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
    stray '{}' behind as visible text)."""
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

class Converter:
    def __init__(self, pre, labels, cite_order, args):
        self.pre, self.labels, self.cite_order, self.args = pre, labels, cite_order, args
        self.counters = {}
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
        def one(m):
            body = m.group(0)
            env = m.group(1)
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
                pics = re.findall(r"\\begin\{tikzpicture\}(.*?)\\end\{tikzpicture\}",
                                  body, re.S)
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
        return re.sub(r"\\begin\{(figure\*?|subfigure)\}.*?\\end\{\1\}", one, s, flags=re.S)

    # ---- math ----
    def do_math(self, s):
        # display environments
        for env in MATH_ENVS:
            pat = re.compile(r"\\begin\{%s(\*?)\}(.*?)\\end\{%s\1\}" % (env, env), re.S)
            def rep(m, env=env):
                inner = m.group(2)
                lm = re.search(r"\\label\s*\{([^}]*)\}", inner)
                tag = ""
                if lm:
                    num = self.labels.get(lm.group(1))
                    if num: tag = "\n\\tag{%s}" % num
                    else: flag("equation-unnumbered",
                               f"no .aux entry for equation label '{lm.group(1)}'", inner)
                inner = re.sub(r"\\label\s*\{[^}]*\}", "", inner)
                inner = re.sub(r"\\nonumber", "", inner)
                if env in ("multline", "gather", "eqnarray", "align"):
                    inner = "\\begin{aligned}%s\\end{aligned}" % inner
                return self.stash("$$\n%s%s\n$$" % (inner.strip(), tag))
            s = pat.sub(rep, s)
        s = re.sub(r"\\(?:begin|end)\{subequations\}", "", s)
        # \[ ... \]
        s = re.sub(r"\\\[(.*?)\\\]", lambda m: self.stash("$$\n%s\n$$" % m.group(1).strip()),
                   s, flags=re.S)
        # inline
        s = re.sub(r"\\\((.*?)\\\)", lambda m: self.stash("$%s$" % m.group(1).strip()),
                   s, flags=re.S)
        s = re.sub(r"(?<!\\)\$([^$]+)\$", lambda m: self.stash("$%s$" % m.group(1)), s)
        return s

    # ---- text markup ----
    def do_text(self, s):
        # theorem-like environments
        for env, disp in list(self.pre.theorems.items()) + [("proof", "Proof")]:
            pat = re.compile(r"\\begin\{%s\}(\[[^\]]*\])?(.*?)\\end\{%s\}" % (env, env), re.S)
            def rep(m, env=env, disp=disp):
                note = (m.group(1) or "").strip("[]")
                inner = m.group(2)
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
            s = pat.sub(rep, s)

        # restatable: \begin{restatable}{lemma}{MacroName} body \end{restatable}
        def restate(m):
            env, macro, inner = m.group(1), m.group(2), m.group(3)
            disp = self.pre.theorems.get(env, env.capitalize())
            lm = re.search(r"\\label\s*\{([^}]*)\}", inner)
            num = self.labels.get(lm.group(1), "") if lm else ""
            inner = re.sub(r"\\label\s*\{[^}]*\}", "", inner).strip()
            self.store.setdefault("__restate__", {})
            body = f"**{disp} {num}.**".replace(" .", ".") + " " + inner
            RESTATE[macro] = body
            quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in body.split("\n"))
            return "\n\n" + quoted + "\n\n"
        s = re.sub(r"\\begin\{restatable\}\{(\w+)\}\{(\w+)\}(.*?)\\end\{restatable\}",
                   restate, s, flags=re.S)
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
        def one(m):
            body = m.group(2)
            if re.search(r"\\multirow|\\multicolumn", body):
                flag("table-complex",
                     "tabular uses multirow/multicolumn — convert by hand", body)
                return self.stash("```\n[COMPLEX TABLE — see flags]\n```")
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
        return re.sub(r"\\begin\{tabular\}\{([^}]*)\}(.*?)\\end\{tabular\}",
                      one, s, flags=re.S)

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
        stripped = re.sub(r"\$[^$]*\$", "", ln)
        stripped = re.sub(r"\[\^\d+\]:?", "", stripped)
        if "_" in stripped or re.search(r"\\[A-Za-z]+", stripped):
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
    if pre.title:
        head.append("# " + pre.title)
    else:
        flag("no-title", "No \\title found in the preamble.")
    if pre.authors:
        head.append(", ".join(f"**{n}**" + (f"<sup>{k}</sup>" if k else "")
                              for k, n in pre.authors))
    for k, v in pre.affils:
        head.append(f"<sup>{k}</sup> {v}")
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
