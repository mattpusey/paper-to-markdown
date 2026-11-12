#!/usr/bin/env python3
"""
pagemarks.py — put the paper's own printed page numbers into the Markdown.

A citation says "p. 7", and the 7 is the number printed in the corner of the
page -- 1123 in a journal, iii in front matter, 012345-2 on an APS article --
not "the seventh sheet of the PDF". This finds those printed numbers and
writes one marker into the Markdown where each page begins:

    <!-- page 7 -->

An HTML comment, so it renders as nothing and leaves the prose, the math and
every check in verify.py alone; and one fixed form, so a page is found with

    grep -n -- '<!-- page 7 -->' paper.md

(the trailing ` -->` is what stops page 7 also matching 70-79). A page that
begins mid-paragraph gets the marker mid-paragraph, in front of the first
word printed on it; a page that begins with a new paragraph, heading or
display gets it on a line of its own.

Two jobs, both against the compiled (or published) PDF:

  labels     Read the top and bottom lines of every page for a number, and
             keep only numbers that go up one per page -- a year, a volume or
             an equation number in a running head does not. Numbers that run
             in step on most pages also fill the pages between them that print
             none (the first page of an article, a page that is all figure),
             and at most two pages at either end. If nothing is printed at
             all, explicit PDF /PageLabels are used instead -- but only when
             they say something the page count does not (a roman front
             matter, a numbering that starts at 101). Nothing printed and
             nothing labelled means no markers: a sheet count is not a page
             number.

  placement  Align each page's text-layer words against the Markdown's prose
             words (math, fences, URLs and markup masked out) on word
             trigrams that occur once on each side, keeping the longest chain
             whose page numbers never go down through the Markdown -- so the
             order of text WITHIN a page, which pdftotext gets wrong around
             floats in two-column layouts, does not matter. Where a page
             boundary falls between two matched words with an equation or
             float in between, the leftover unmatched text on either side of
             the boundary in the PDF decides which side of it the marker
             goes. A word hyphenated over the break counts as the page it
             starts on. Where the PDF's own text layer interleaves the last
             lines of two columns mid-word, the marker can land a word early.

Markers this script wrote before are removed first, so it can be re-run --
including after hand edits. A PDF with no text layer (a scan) has nothing to
align against: put the markers in by hand while transcribing it, page by page,
in the same form.

Needs pdftotext (poppler-utils) or pypdf. paper2md.py calls this itself when it
finds the compiled PDF next to the .tex.

Not done yet: TeX's own reading order, from SyncTeX
---------------------------------------------------
Most of the remaining misplacements come from pdftotext, not from the
alignment: its reading order sometimes runs a page's right-hand column before
the left (around a float), or mixes the last lines of two columns mid-word.
When the paper is compiled from source, TeX knows the real order, and
`pdflatex -synctex=1` writes it down. paper.synctex.gz holds, for every page,
one record per typeset line, in TeX's own order (left column before right).
Each record carries the source file and line, and the box's position and size.

The idea: get the words with their coordinates (pdftotext -bbox-layout), put
each word into the SyncTeX line box that contains it, and rebuild each page's
text in record order. Pass that text to place() in place of pdftotext's flow
text. Nothing else changes, and paper2md.py is not touched. This fixes the
column order at its source. It also makes the "which side of this display
is the page break" decision more reliable, because the equation's leftover
words then sit at the true end of one page or start of the next. SyncTeX is
decisive on that question by itself: on arXiv:2110.08293 pp. 4-5 the
\\end{equation*} at source line 120 is recorded only on page 4, and line 121
("Clearly, ...") is the first line recorded only on page 5.

What SyncTeX cannot replace is the alignment itself. It knows source lines,
not words, and a source line is often a whole paragraph: on arXiv:1208.2550
both page breaks fall inside one source line (lines 57 and 91). The Markdown
also carries no source lines, because conversion inlines \\input, expands
macros and moves floats. So this would only improve the input to the
alignment, and only for papers converted from .tex. PDF-only conversions keep
using pdftotext.

Things to handle if built: records that belong to running heads and feet (on
arXiv:2110.08293, \\begin{document}'s line turns up on every page), and floats
(their records sit where the float was typeset, not in the text flow); the
coordinate conversion between SyncTeX (scaled points, with an offset) and
pdftotext (PDF points); and falling back to pdftotext's order whenever there
is no .synctex.gz, or it is older than the PDF. Rough cost: a day, plus
testing on two-column papers with floats and with displays at page breaks.
Revisit if real papers turn up misplaced markers of that kind. On the two
arXiv papers above, all 20 markers are already exact.

Usage
-----
    pagemarks.py paper.md --pdf paper.pdf           # rewrites paper.md
    pagemarks.py paper.md --pdf paper.pdf -o out.md
    pagemarks.py paper.md --pdf paper.pdf --dry-run # report only
    pagemarks.py paper.md --strip                   # remove markers

Exit codes: 0 ok (including "no page numbers found"), 2 usage error.
"""

import argparse, bisect, collections, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import srcdiff
import verify


class Unavailable(Exception):
    """The PDF cannot be read for page numbers (missing tools, no text)."""


# --------------------------------------------------------------------------
# the marker
# --------------------------------------------------------------------------

def marker(label):
    return f"<!-- page {label} -->"


# Exactly the three shapes insert() writes, so stripping restores the text
# byte for byte: a block marker on its own line (indented to match a list
# item), an inline one before a word, an inline one at the end of a line.
_STRIP_BLOCK_RE = re.compile(r"^[ \t]*<!-- page \S+ -->\n\n", re.M)
_STRIP_BEFORE_RE = re.compile(r"<!-- page \S+ --> ")
_STRIP_AFTER_RE = re.compile(r" <!-- page \S+ -->")
MARKER_RE = re.compile(r"<!-- page (\S+) -->")


def strip_markers(md):
    md = _STRIP_BLOCK_RE.sub("", md)
    md = _STRIP_BEFORE_RE.sub("", md)
    return _STRIP_AFTER_RE.sub("", md)


# --------------------------------------------------------------------------
# reading the PDF
# --------------------------------------------------------------------------

def _pdftotext(path, layout):
    args = ["pdftotext", "-enc", "UTF-8"] + (["-layout"] if layout else [])
    proc = subprocess.run(args + [path, "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    pages = proc.stdout.split("\f")
    if pages and not pages[-1].strip():               # trailing form feed
        pages = pages[:-1]
    return pages


def read_pdf(path):
    """(layout_pages, flow_pages): the first for finding page numbers at the
    edges of the page, the second -- reading order, columns unwoven -- for
    aligning against the Markdown."""
    if shutil.which("pdftotext"):
        layout, flow = _pdftotext(path, True), _pdftotext(path, False)
        if layout is not None and flow is not None and len(layout) == len(flow):
            return layout, flow
    try:
        import pypdf
    except ImportError:
        raise Unavailable("needs pdftotext on PATH or pypdf installed "
                          "(pip install pypdf)")
    pages = [(p.extract_text() or "") for p in pypdf.PdfReader(path).pages]
    return pages, pages


def pdf_page_labels(path):
    """Explicit /PageLabels, or None. pypdf invents '1', '2', ... for a PDF
    that has none, which is exactly the sheet count this must not report."""
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        if "/PageLabels" not in reader.trailer["/Root"]:
            return None
        labels = list(reader.page_labels)
    except Exception:
        return None
    if labels == [str(i + 1) for i in range(len(labels))]:
        return None
    return labels


# --------------------------------------------------------------------------
# finding the printed numbers
# --------------------------------------------------------------------------

_ROMAN_RE = re.compile(r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_value(s):
    if not s or not _ROMAN_RE.match(s):
        return None
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN[a]
        total += -v if b != " " and _ROMAN[b] > v else v
    return total


def to_roman(n):
    out = []
    for v, s in [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                 (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
                 (5, "v"), (4, "iv"), (1, "i")]:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


# "012345-2" (APS article-number pages), "S-3", "A.2", "S3".
_PREFIXED_RE = re.compile(r"^([A-Za-z0-9]*[-–.]|[A-Z]{1,2})(\d{1,4})$")
_DASHES = "-–—"
_STANDALONE_RE = re.compile(
    r"^[-–—\s]*(?:page\s+)?(\S+?)(?:\s+of\s+\d+)?[-–—\s]*$", re.I)
_PAGE_WORD_RE = re.compile(r"\bpage\s+(\d{1,5})\b", re.I)


def parse_number(tok):
    """A token that could be a printed page number -> (scheme, value, text)
    where scheme is (kind, prefix). Parenthesised tokens are equation numbers
    and never page numbers."""
    tok = tok.strip()
    if not tok or tok[0] in "([" or tok[-1] in ")]":
        return None
    tok = tok.strip(_DASHES + ".,;:")
    if re.fullmatch(r"\d{1,5}", tok):
        v = int(tok)
        return (("arabic", ""), v, tok) if v >= 1 else None
    r = roman_value(tok.lower()) if tok.islower() or tok.isupper() else None
    if r and r <= 60:
        return ("roman", ""), r, tok
    m = _PREFIXED_RE.match(tok)
    if m and int(m.group(2)) >= 1:
        return ("arabic", m.group(1)), int(m.group(2)), tok
    return None


def page_candidates(text):
    """Numbers at the edges of a page, each with a weight: a line that is
    nothing but the number (a centred footer, "Page 3", "- 3 -") counts for
    more than a number at one end of a running head, which in turn is the
    only other place one is looked for."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    edge = lines[:3] + [l for l in lines[-3:] if l not in lines[:3]]
    found = {}
    for line in edge:
        m = _STANDALONE_RE.match(line)
        if m:
            c = parse_number(m.group(1))
            if c:
                key = (c[0], c[1])
                found[key] = max(found.get(key, (0, ""))[0], 3), c[2]
                continue
        if len(" ".join(line.split())) > 120:          # padding is not text
            continue
        toks = line.split()
        for tok in {toks[0], toks[-1]}:
            c = parse_number(tok)
            if c:
                key = (c[0], c[1])
                found.setdefault(key, (1, c[2]))
        for m in _PAGE_WORD_RE.finditer(line):
            key = (("arabic", ""), int(m.group(1)))
            found[key] = max(found.get(key, (0, ""))[0], 2), m.group(1)
    return [(scheme, value, w, text) for (scheme, value), (w, text) in found.items()]


def _render(scheme, value, like=None):
    kind, prefix = scheme
    if kind == "roman":
        return to_roman(value).upper() if like and like.isupper() else to_roman(value)
    return f"{prefix}{value}"


MAX_EDGE_FILL = 2


def detect_labels(layout_pages):
    """One printed label per PDF page (None where there is none), and a
    per-page note saying which were read and which inferred.

    A run is a (scheme, offset) pair -- printed number minus sheet index --
    and only runs supported by at least two pages are believed: a page number
    goes up by one per sheet, a year or a volume in a running head does not.
    """
    n = len(layout_pages)
    cands = [page_candidates(t) for t in layout_pages]
    votes, pages_for = collections.Counter(), collections.defaultdict(set)
    for i, cs in enumerate(cands):
        for scheme, value, w, _ in cs:
            run = (scheme, value - i)
            votes[run] += w
            pages_for[run].add(i)
    need = 2 if n > 1 else 1
    good = {r for r in votes if len(pages_for[r]) >= need and votes[r] >= 3}

    labels, how, runs, texts = [None] * n, [None] * n, [None] * n, [None] * n
    for i, cs in enumerate(cands):
        best = None
        for scheme, value, w, text in cs:
            run = (scheme, value - i)
            if run in good and (best is None or votes[run] > votes[best[0]]):
                best = (run, value, text)
        if best:
            runs[i], texts[i] = best[0], best[2]
            labels[i] = best[2]
            how[i] = "printed"

    # Fill the pages a run passes over without printing on them.
    def fill(i, run, like):
        value = i + run[1]
        if value < 1:
            return
        labels[i] = _render(run[0], value, like)
        runs[i], how[i] = run, "inferred"

    known = [i for i in range(n) if runs[i] is not None]
    for a, b in zip(known, known[1:]):
        if runs[a] == runs[b]:
            for i in range(a + 1, b):
                fill(i, runs[a], texts[a])
    if known:
        first, last = known[0], known[-1]
        for i in range(max(0, first - MAX_EDGE_FILL), first):
            fill(i, runs[first], texts[first])
        for i in range(last + 1, min(n, last + 1 + MAX_EDGE_FILL)):
            fill(i, runs[last], texts[last])
    return labels, how


# --------------------------------------------------------------------------
# the markdown side: prose words with their offsets
# --------------------------------------------------------------------------

MASK = "\x00"
_PROTECT = [verify.FENCE_RE,
            verify.DISP_RE,
            re.compile(r"\\\[[\s\S]*?\\\]"),
            re.compile(r"\\\([\s\S]*?\\\)"),
            verify.INLINE_RE,
            srcdiff.HTML_COMMENT_RE,
            re.compile(r"`[^`\n]+`"),
            srcdiff.LINK_TARGET_RE,
            srcdiff.URL_RE,
            srcdiff.HTML_TAG_RE,
            srcdiff.FOOTNOTE_RE]
_LETTERS_RE = re.compile(r"[^\W\d_]+")
# Words of two letters or more, as in srcdiff. A PDF text layer spells
# $x_5$ as a stray "x", and one-letter tokens let that debris break the very
# trigrams that anchor a page boundary; a page opening on a real one-letter
# word ("a particular state") is handled after placement instead.
_WORD_RE = srcdiff.WORD_RE
_ANY_WORD_RE = re.compile(r"[a-z]+")


def mask_markdown(md):
    """md with every non-prose span overwritten by MASK, offsets unchanged.
    Newlines inside a span are masked too, so a blank line inside a fence or
    a display is never mistaken for a paragraph break."""
    s = md
    for rx in _PROTECT:
        s = rx.sub(lambda m: MASK * len(m.group(0)), s)
    return s


def markdown_words(masked):
    words, starts, ends = [], [], []
    for m in _LETTERS_RE.finditer(masked):
        for w in _WORD_RE.findall(srcdiff.fold(m.group(0))):
            words.append(w)
            starts.append(m.start())
            ends.append(m.end())
    return words, starts, ends


def block_starts(masked):
    """Offsets of lines that follow a blank line: where a new block begins."""
    out = [0]
    for m in re.finditer(r"\n(?:[ \t]*\n)+", masked):
        out.append(m.end())
    return out


# --------------------------------------------------------------------------
# alignment
# --------------------------------------------------------------------------

K = 3


def _unique_kgrams(words):
    seen = collections.Counter(tuple(words[i:i + K]) for i in range(len(words) - K + 1))
    return {tuple(words[i:i + K]): i for i in range(len(words) - K + 1)
            if seen[tuple(words[i:i + K])] == 1}


def _longest_nondecreasing(items):
    """Longest subsequence of (j, page, i) -- sorted by j -- whose pages
    never decrease."""
    tails, tails_idx, prev = [], [], [None] * len(items)
    for n, (_, page, _) in enumerate(items):
        k = bisect.bisect_right(tails, page)
        if k == len(tails):
            tails.append(page); tails_idx.append(n)
        else:
            tails[k] = page; tails_idx[k] = n
        prev[n] = tails_idx[k - 1] if k else None
    out, n = [], tails_idx[-1] if tails_idx else None
    while n is not None:
        out.append(items[n]); n = prev[n]
    return out[::-1]


def align(src, src_page, md):
    """{md word index: (page, src index)} for Markdown words placed on a page.

    Anchors are word trigrams that occur exactly once on each side. They are
    chained in MARKDOWN order, keeping the longest run whose page numbers
    never go down -- not the longest run in PDF order, because pdftotext's
    reading order is only trustworthy between pages: a float at the head of
    a column regularly makes it read the right-hand column of a page before
    the left. Within a page, order does not matter at all. Each surviving
    anchor is then grown word by word in both directions for as long as the
    two sides agree, which is what pins a boundary down to the exact word.
    """
    a, b = _unique_kgrams(src), _unique_kgrams(md)
    hits = {}
    for g, i in a.items():
        j = b.get(g)
        if j is None:
            continue
        for d in range(K):
            hits.setdefault(j + d, (src_page[i + d], i + d))
    for j, (_, i) in sorted(hits.items()):
        for step in (1, -1):
            jj, ii = j + step, i + step
            while 0 <= jj < len(md) and 0 <= ii < len(src) \
                    and jj not in hits and md[jj] == src[ii]:
                hits[jj] = (src_page[ii], ii)
                jj, ii = jj + step, ii + step
    kept = _longest_nondecreasing(sorted((j, p, i) for j, (p, i) in hits.items()))
    return {j: (p, i) for j, p, i in kept}


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------

def drop_label_lines(text, label):
    """Remove the head or foot line that carries the page's own number.

    strip_running_lines only knows a running head by its repeating, and a
    head that names the current section ("103  SECTION 2 ABOUT X") changes
    from section to section. Knowing the printed number, the line is found
    without guessing -- and with it gone, the page's first words really are
    its first words."""
    if not label:
        return text
    lines = text.split("\n")
    nonblank = [k for k, l in enumerate(lines) if l.strip()]
    edge = set(nonblank[:3] + nonblank[-3:])
    keep = []
    for k, line in enumerate(lines):
        toks = line.split()
        if k in edge and toks and label in (toks[0], toks[-1]) \
                and len(" ".join(toks)) <= 120:
            continue
        keep.append(line)
    return "\n".join(keep)


# pdftotext's reading-order mode has already removed the hyphen and glued on
# the next line -- which at the foot of a page is the page number: "zy102".
_TAIL_HYPHEN_RE = re.compile(r"([^\W\d_]{2})(?:-|\d{1,5})\s*$")
_HEAD_FRAGMENT_RE = re.compile(r"^\s*([^\W\d_]+)")


def _is_furniture(line):
    """A line with no lowercase letter at the very top or bottom of a page is
    a page number or an all-caps running head ("SECTION 5 ABOUT ARGUMENT"),
    which in reading order can sit between a hyphenated word's two halves."""
    return not any(c.islower() for c in line)


def _skip_furniture(lines, reverse=False):
    order = range(len(lines) - 1, -1, -1) if reverse else range(len(lines))
    seen = 0
    for k in order:
        if not lines[k].strip():
            continue
        if _is_furniture(lines[k]) and seen < 3:
            seen += 1
            continue
        return k
    return None


def strip_furniture(text):
    """Drop page numbers and all-caps running heads from the top and bottom
    of a page. Left in, they are unmatched text at the edge of the page --
    exactly the signal place() reads to decide which side of an equation a
    page boundary falls."""
    lines = text.split("\n")
    top = _skip_furniture(lines)
    if top is None:
        return ""
    bottom = _skip_furniture(lines, reverse=True)
    return "\n".join(lines[top:bottom + 1])


def rejoin_across_pages(pages):
    """A word hyphenated over a page break belongs to the page it starts on:
    "zy-" ending one page and "dase relation" opening the next become
    "zydase" and "relation". Left split, the boundary lands inside the word
    on some pages and before it on others."""
    pages = list(pages)
    for p in range(1, len(pages)):
        before, after = pages[p - 1].split("\n"), pages[p].split("\n")
        kb, ka = _skip_furniture(before, reverse=True), _skip_furniture(after)
        if kb is None or ka is None:
            continue
        tail = _TAIL_HYPHEN_RE.search(before[kb])
        head = _HEAD_FRAGMENT_RE.match(after[ka])
        if tail and head and head.group(1)[0].islower():
            before[kb] = before[kb][:tail.end(1)] + head.group(1)
            after[ka] = after[ka][head.end():]
            pages[p - 1], pages[p] = "\n".join(before), "\n".join(after)
    return pages


_ONE_LETTER_BEFORE_RE = re.compile(r"(?<![\w\x00])([A-Za-z])[ \t]+$")


def _one_letter_back(md, masked, off, before, after):
    """Move a marker back over a one-letter word ("a", "I") that the page
    really opens with: "tight for a <!-- page 2 --> particular" becomes
    "tight for <!-- page 2 --> a particular" when page 2 has "a particular"
    without the "for" in front of it, and page 1 does not end on "a". (Not
    "page 2 starts with": in reading order it may start with the other
    column.)"""
    base = max(0, off - 4)
    m = _ONE_LETTER_BEFORE_RE.search(masked[base:off])
    nxt = _ANY_WORD_RE.match(srcdiff.fold(masked[off:off + 40]))
    prv = re.search(r"([a-z]+)[^a-z]*$",
                    srcdiff.fold(masked[max(0, off - 60):base + m.start(1)])) if m else None
    if not m or not nxt:
        return off
    letter, word = m.group(1).lower(), nxt.group(0)
    page = re.sub(r"\s+", " ", srcdiff.fold(after))
    there = re.search(r"\b%s %s\b" % (letter, word), page)
    glued = prv and re.search(r"\b%s %s %s\b" % (prv.group(1), letter, word), page)
    ends = re.search(r"\b%s\s*$" % letter, srcdiff.fold(before.rstrip()))
    return base + m.start(1) if there and not glued and not ends else off


_TEX_CMD_RE = re.compile(r"\\[A-Za-z]+")


def _gap_tokens(text):
    """Words in a stretch of Markdown, math included, spelled the way a PDF
    text layer spells them: \\mbox{Frame 1} gives "frame", a_i gives "ai"."""
    text = srcdiff.HTML_COMMENT_RE.sub(" ", text)
    text = re.sub(r"[_^{}]", "", _TEX_CMD_RE.sub(" ", text))
    return _ANY_WORD_RE.findall(srcdiff.fold(text))


def _gap_bag(words):
    return collections.Counter(w for w in words if len(w) >= 2)


def place(md, flow_pages, labels):
    """[(offset, how) or None] for each PDF page, in page order."""
    masked = mask_markdown(md)
    mwords, mstarts, mends = markdown_words(masked)
    blocks = block_starts(masked)

    src, src_page, page_lo = [], [], []
    stripped = [strip_furniture(drop_label_lines(t, l))
                for t, l in zip(flow_pages, labels)]
    stripped = rejoin_across_pages(srcdiff.strip_running_lines(stripped))
    for p, text in enumerate(stripped):
        page_lo.append(len(src))
        toks = _WORD_RE.findall(srcdiff.fold(srcdiff.dehyphenate(text)))
        src.extend(toks)
        src_page.extend([p] * len(toks))
    page_lo.append(len(src))

    placed = align(src, src_page, mwords)
    placed_src = {i for _, i in placed.values()}
    first_j, last_j, first_i, last_i = {}, {}, {}, {}
    for j in sorted(placed):
        p, i = placed[j]
        first_j.setdefault(p, j)
        last_j[p] = j
        first_i[p] = min(first_i.get(p, i), i)
        last_i[p] = max(last_i.get(p, i), i)

    def first_in_block(j):
        """Is md word j the first word of its block? Then the block start."""
        b = blocks[bisect.bisect_right(blocks, mstarts[j]) - 1]
        k = bisect.bisect_left(mstarts, b)
        return b if k == j else None

    offsets = [None] * len(flow_pages)
    prev = None                                       # last matched page
    for p in range(len(flow_pages)):
        if p not in first_j:
            continue
        if prev is None:
            offsets[p] = 0
            prev = p
            continue
        j_l, j_f = last_j[prev], first_j[p]
        lo, hi = mends[j_l], mstarts[j_f]
        # Between the last word placed on the page before and the first word
        # placed on this one there is usually an equation or a float, and
        # the boundary can be on either side of it. The PDF says which: the
        # page holding it has unmatched text where it sits -- the equation's
        # letters, a caption -- and the Markdown has the same letters on one
        # side of the right split and not the other.
        tail = [src[i] for i in range(last_i[prev] + 1, page_lo[prev + 1])
                if i not in placed_src]
        head = [src[i] for i in range(page_lo[p], first_i[p])
                if i not in placed_src]
        t1, t2 = len(tail), len(head)
        frac = t1 / (t1 + t2) if t1 + t2 else 1.0
        target = lo + frac * (hi - lo)

        cands = [b for b in blocks if lo < b <= hi]
        for j in range(j_l + 1, j_f + 1):
            cands.append(first_in_block(j) or mstarts[j])
        tail_c, head_c = _gap_bag(tail), _gap_bag(head)

        def score(c):
            agree = (sum((_gap_bag(_gap_tokens(md[lo:c])) & tail_c).values())
                     + sum((_gap_bag(_gap_tokens(md[c:hi])) & head_c).values()))
            return (-agree, abs(c - target), c not in blocks)
        offsets[p] = _one_letter_back(md, masked, min(set(cands), key=score),
                                      stripped[prev], stripped[p])
        prev = p

    # A page with no text matched (all figure, blank) goes where the next
    # placed page does; one after the last placed page is not placed at all.
    out, nxt = [None] * len(flow_pages), None
    for p in range(len(flow_pages) - 1, -1, -1):
        if offsets[p] is not None:
            nxt = offsets[p]
            out[p] = (nxt, "matched")
        elif nxt is not None:
            out[p] = (nxt, "unmatched")
    # keep offsets monotone in page order
    floor = 0
    for p in range(len(out)):
        if out[p]:
            off = max(out[p][0], floor)
            out[p] = (off, out[p][1])
            floor = off
    return out


# --------------------------------------------------------------------------
# insertion
# --------------------------------------------------------------------------

_LINE_LEAD_RE = re.compile(r"^[ \t>]*(?:(?:[-*+]|\d+[.)])[ \t]+)?$")
_UNSAFE_LINE_RE = re.compile(r"^\s*(?:```|~~~|\$\$|\\\[|\\\])|(?:\$\$|\\\]|```)\s*$")


def _insertion(md, masked, off, label, blocks):
    """(offset, text) to insert for a marker that belongs at `off`."""
    m = marker(label)
    if off in blocks:
        eol = md.find("\n", off)
        line = md[off: eol if eol >= 0 else len(md)]
        indent = re.match(r"[ \t]*", line).group(0)
        return off, indent + m + "\n\n"
    # back up to the start of the whitespace-delimited chunk holding the word,
    # so the marker sits before "**word" or "(word", not inside them
    k = off
    while k > 0 and not md[k - 1].isspace() and masked[k - 1] != MASK:
        k -= 1
    bol = md.rfind("\n", 0, k) + 1
    if not _LINE_LEAD_RE.match(md[bol:k]):
        return k, m + " "
    # At the start of a line, "<!--" would open an HTML block and swallow the
    # rest of the line raw. Hang it on the end of the line before instead...
    if bol > 0:
        pbol = md.rfind("\n", 0, bol - 1) + 1
        prev_line = md[pbol:bol - 1]
        unquoted = re.sub(r"^(?:>\s?)+", "", prev_line)
        if unquoted.strip() and not _UNSAFE_LINE_RE.search(unquoted):
            return bol - 1, " " + m
    # ...or, where that line is a fence or a display delimiter, after the
    # first word of this one.
    e = off
    while e < len(md) and not md[e].isspace():
        e += 1
    return e, " " + m


def insert(md, placed, labels):
    masked = mask_markdown(md)
    blocks = set(block_starts(masked))
    edits = []
    for p, pl in enumerate(placed):
        if pl is None or labels[p] is None:
            continue
        pos, text = _insertion(md, masked, pl[0], labels[p], blocks)
        edits.append((pos, p, text))
    out, i = [], 0
    for pos, _, text in sorted(edits):
        out.append(md[i:pos]); out.append(text); i = pos
    out.append(md[i:])
    return "".join(out)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def annotate(md, pdf_path):
    """(new_md, report). Raises Unavailable if the PDF cannot be read."""
    md = strip_markers(md)
    layout, flow = read_pdf(pdf_path)
    if sum(len(p.strip()) for p in flow) < 50 * max(1, len(flow)):
        raise Unavailable("the PDF has no usable text layer (a scan?) -- "
                          "add the markers by hand while transcribing")
    labels, how = detect_labels(layout)
    source = "printed"
    if not any(labels):
        explicit = pdf_page_labels(pdf_path)
        if explicit and len(explicit) == len(flow):
            labels, how, source = explicit, ["PDF /PageLabels"] * len(flow), "pdf-labels"
    report = {"pdf": pdf_path, "sheets": len(flow), "source": source,
              "pages": []}
    if not any(labels):
        report["source"] = None
        return md, report

    placed = place(md, flow, labels)
    new = insert(md, placed, labels)

    line_of = {}
    for m in MARKER_RE.finditer(new):
        line_of.setdefault(m.group(1), new.count("\n", 0, m.start()) + 1)
    for p in range(len(flow)):
        report["pages"].append({
            "sheet": p + 1, "label": labels[p], "label_from": how[p],
            "placed": (placed[p][1] if placed[p] else "no")
                      if labels[p] else None,
            "line": line_of.get(labels[p]) if labels[p] and placed[p] else None})
    return new, report


def print_report(report, out_path):
    if report["source"] is None:
        print(f"pagemarks: no printed page numbers found in {report['pdf']} "
              f"({report['sheets']} sheets) and no explicit PDF page labels; "
              "no markers written")
        return
    n = sum(1 for r in report["pages"] if r["line"])
    print(f"pagemarks: {n} page markers written to {out_path} "
          f"(from {report['pdf']}, {report['sheets']} sheets, "
          f"labels {report['source']})")
    for r in report["pages"]:
        if r["label"] is None:
            where = "no page number printed; no marker"
        elif r["placed"] == "no":
            where = "NOT PLACED: no text from this page found after the last placed page"
        else:
            where = f"line {r['line']}"
            if r["placed"] == "unmatched":
                where += " (no text matched; placed with the next page)"
        note = " [inferred]" if r["label_from"] == "inferred" else ""
        print(f"    sheet {r['sheet']:>3}  page {str(r['label'] or '-'):>8}{note}  {where}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md", help="converted markdown file")
    ap.add_argument("--pdf", help="the paper's PDF (compiled or published)")
    ap.add_argument("-o", "--out", default=None,
                    help="write here instead of rewriting the input")
    ap.add_argument("--dry-run", action="store_true",
                    help="report where the markers would go; write nothing")
    ap.add_argument("--strip", action="store_true",
                    help="remove page markers and exit")
    ap.add_argument("--json", default=None, help="also write the report here")
    a = ap.parse_args()

    if not os.path.exists(a.md):
        ap.exit(2, f"no such file: {a.md}\n")
    with open(a.md, encoding="utf-8") as fh:
        md = fh.read()
    out = a.out or a.md

    if a.strip:
        if not a.dry_run:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(strip_markers(md))
        return 0
    if not a.pdf:
        ap.error("need --pdf (or --strip)")
    if not os.path.exists(a.pdf):
        ap.exit(2, f"no such file: {a.pdf}\n")

    try:
        new, report = annotate(md, a.pdf)
    except Unavailable as exc:
        ap.exit(2, f"pagemarks: {exc}\n")
    if not a.dry_run and report["source"] is not None:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(new)
    print_report(report, out if not a.dry_run else "(dry run)")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
