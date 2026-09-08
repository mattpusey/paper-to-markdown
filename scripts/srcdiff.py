#!/usr/bin/env python3
"""
srcdiff.py — word-frequency diff between converted Markdown and its source.

This is SKILL.md section 5's last verification, the one that catches silent
omission. verify.py cannot do it: every check there is about the Markdown's
internal form -- does the math parse, do the cross-references resolve -- and
all of them pass just as happily over a paragraph that was dropped or one
that was never in the paper. Only a comparison against the source can tell.

Three source modes, because the comparison text comes from somewhere
different each time:

  tex       Strip the source to its prose with paper2md's own primitives
            (comments, macro definitions, math environments, the arguments
            of \\label/\\ref/\\cite/\\begin). Prose is a pass-through in
            paper2md.py, so this mode is mostly a regression check -- and it
            is the one mode where a hit is nearly always real.

  pdftext   pdftotext -layout (or pypdf), minus running heads and page
            numbers. For a PDF that was converted without its .tex.

  ocr       Render each page and OCR it with tesseract. For an image-only
            scan, where there is no text layer to extract and the Markdown
            was transcribed by eye. This is the mode that needs the noise
            filters below, and the mode where the check matters most.

This REPORTS, it does not gate. OCR of a mathematics paper produces a
constant drizzle of nonsense words from every equation it walks over
(`abort` for a set-off `\\alpha J_{k,n-k}`), and no threshold on that noise
is meaningful. Four filters cut it down to something readable:

  short   below --min-len; almost all of the math debris is 2-3 characters
  math    the token is in the Markdown's math or fenced blocks, which the
          prose comparison cannot see -- `p_{ABEXY}(a,b,e,x,y)` reaches the
          word stream from a PDF as `pabexy`
  split   a source token that splits into two words the Markdown has, which
          is line-break hyphenation rejoined ("dis-\\ncussion" -> discussion)
  near    a close match in the other side's vocabulary, which is OCR
          misreading a word that is present ("matriz" -> matrix)

What survives is short enough to read, and each entry carries its page and
the surrounding text so it can be checked against the source directly. On
the three papers this was built against, that is 0 unmatched words for a
.tex source, 0 for a text-layer PDF, and 8 for a 1994 scan -- all eight
visibly equation debris.

Two kinds of hit are normal and not defects. Words LaTeX generates rather
than the author typing them ("Abstract", "Figure") show as Markdown-only,
and so do author names that a .bst rendered from a \\citet key -- which is
the `citet-manual` flag in SKILL.md, arriving here on its own.

Usage
-----
    srcdiff.py paper.md --source paper.tex
    srcdiff.py paper.md --source paper.pdf              # mode auto-detected
    srcdiff.py paper.md --source scan.pdf --mode ocr --ocr-dpi 300
    srcdiff.py paper.md --source paper.pdf --save-source extracted.txt
    srcdiff.py paper.md --source-text extracted.txt     # reuse an extraction

Exit codes: 0 ok, 1 if --fail-on-missing is set and exceeded, 2 usage error.
"""

import argparse, collections, difflib, json, os, re, shutil, subprocess, sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify

try:
    import paper2md
except ImportError:                                   # pragma: no cover
    paper2md = None

# --------------------------------------------------------------------------
# normalising and tokenising
# --------------------------------------------------------------------------

# NFKD turns ligatures (ﬁ) and most accents into ASCII; these letters do not
# decompose, and OCR reads them as their ASCII lookalike anyway.
_EXTRA_FOLD = str.maketrans({"ø": "o", "Ø": "O", "đ": "d", "ł": "l",
                             "æ": "ae", "Æ": "AE", "œ": "oe", "ß": "ss"})

WORD_RE = re.compile(r"[a-z]{2,}")


def fold(text):
    """Lowercase ASCII-ish form: ligatures split, accents dropped."""
    text = text.translate(_EXTRA_FOLD)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def dehyphenate(text):
    """Rejoin words a line break split: 'corre-\\nlation' -> 'correlation'."""
    return re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1\2", text)


def tokens(text):
    return WORD_RE.findall(fold(text))


# --------------------------------------------------------------------------
# the markdown side
# --------------------------------------------------------------------------

HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
INLINE_CODE_RE = re.compile(r"`+")
LINK_TARGET_RE = re.compile(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
FOOTNOTE_RE = re.compile(r"\[\^[^\]]+\]")
URL_RE = re.compile(r"https?://\S+|doi:\s*\S+")
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
HTML_ENTITY_RE = re.compile(r"&[A-Za-z]+;|&#\d+;")

# Math commands that only set type. Their letters sit between the letters that
# are actually rendered, and a source token spans the gap: `\dim
# \operatorname{span}` prints as "dim span", which pdftotext and tesseract both
# hand back as `dimspan`, matching neither "dim" nor "span" on their own.
TYPOGRAPHIC_MATH_RE = re.compile(
    r"\\(?:operatorname|mathrm|mathbb|mathcal|mathbf|mathit|mathsf|mathtt|"
    r"text(?:rm|bf|it|sf|tt)?|left|right|bigg?[lr]?|frac|dfrac|tfrac|"
    r"displaystyle|limits|nolimits|quad|qquad)\b"
    r"|\\[,;:!]")            # \, \; \: \! are thin spaces, not letters


REF_HEAD_RE = re.compile(r"^(#{1,6})\s*(?:references|bibliography)\s*$", re.I | re.M)
FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^[^\]]+\]:")


def drop_references_md(md):
    """Remove the references section only -- not everything after it.

    verify.split_references cuts to end of file, which is right for its own
    citation check but wrong here: footnote definitions conventionally sit at
    the bottom of a Markdown file, below the references, and they are prose
    that is genuinely on the page. Cutting them made every word of a title
    footnote report as an omission.
    """
    m = REF_HEAD_RE.search(md)
    if not m:
        return md
    level = len(m.group(1))
    rest = md[m.end():]
    nxt = re.search(r"^#{1,%d}\s" % level, rest, re.M)
    if nxt:
        return md[:m.start()] + "\n" + rest[nxt.start():]
    kept = [l for l in rest.splitlines() if FOOTNOTE_DEF_RE.match(l)]
    return md[:m.start()] + "\n" + "\n".join(kept)


def markdown_prose(md, skip_refs=True):
    """Markdown reduced to prose: no math, fences, code, URLs or markup."""
    if skip_refs:
        md = drop_references_md(md)
    s = HTML_COMMENT_RE.sub(" ", md)
    s = verify.prose_only(s)             # fences, $$..$$ and $..$ -- verify's rule
    s = INLINE_CODE_RE.sub(" ", s)   # backticks only: a code span holds
    s = URL_RE.sub(" ", s)           # real text (an email, an identifier)
    s = LINK_TARGET_RE.sub("] ", s)
    s = FOOTNOTE_RE.sub(" ", s)
    s = HTML_TAG_RE.sub(" ", s)
    s = HTML_ENTITY_RE.sub(" ", s)
    return dehyphenate(s)


def markdown_nonprose(md, skip_refs=True):
    """What prose_only threw away: math spans and fenced blocks.

    A PDF text layer and an OCR pass both flatten display math into the word
    stream, so `p_{ABEXY}(a,b,e,x,y)` arrives from the source as the word
    `pabexy` -- which the Markdown does have, inside `$...$`, where the prose
    comparison cannot see it. Keeping the letters of every math span lets
    those be recognised for what they are instead of reported as omissions.

    Returns (blobs, vocab): each span's letters run together, and the ordinary
    words inside the spans (a fenced figure description is real content).
    """
    if skip_refs:
        md = drop_references_md(md)
    md = HTML_COMMENT_RE.sub(" ", md)
    blobs, vocab = set(), set()
    for rx in (verify.FENCE_RE, verify.DISP_RE, verify.INLINE_RE):
        for m in rx.finditer(md):
            frag = TYPOGRAPHIC_MATH_RE.sub(" ", fold(m.group(0)))
            letters = re.sub(r"[^a-z]", "", frag)
            if len(letters) >= 2:
                blobs.add(letters)
            vocab.update(WORD_RE.findall(frag))
    return blobs, vocab


# --------------------------------------------------------------------------
# the source side: .tex
# --------------------------------------------------------------------------

# Definitions whose bodies never reach the page.
DEF_COMMANDS = [("newcommand", 2), ("renewcommand", 2), ("providecommand", 2),
                ("NewDocumentCommand", 3), ("RenewDocumentCommand", 3),
                ("DeclareMathOperator", 2), ("DeclarePairedDelimiter", 3),
                ("newtheorem", 2), ("theoremstyle", 1), ("setlength", 2),
                ("definecolor", 3), ("tikzset", 1), ("pgfplotsset", 1),
                ("hypersetup", 1), ("usepackage", 1), ("documentclass", 1),
                ("RequirePackage", 1), ("bibliographystyle", 1),
                ("bibliography", 1), ("addbibresource", 1), ("input", 1),
                ("include", 1), ("includegraphics", 1), ("geometry", 1)]

# Commands whose argument is a key or a name, not words on the page.
KEY_COMMANDS = [("label", 1), ("ref", 1), ("eqref", 1), ("pageref", 1),
                ("cref", 1), ("Cref", 1), ("autoref", 1), ("nameref", 1),
                ("cite", 1), ("citep", 1), ("citet", 1), ("citealp", 1),
                ("citeauthor", 1), ("citeyear", 1), ("onlinecite", 1),
                ("nocite", 1),
                ("email", 1), ("homepage", 1), ("url", 1), ("href", 1),
                ("bibitem", 1), ("color", 1), ("textcolor", 1),
                ("hspace", 1), ("vspace", 1), ("captionsetup", 1)]

# Environments whose bodies are not prose.
DROP_ENVS = ["tikzpicture", "pgfpicture", "axis", "verbatim", "lstlisting",
             "minted", "equation", "equation*", "align", "align*", "alignat",
             "alignat*", "gather", "gather*", "multline", "multline*",
             "eqnarray", "eqnarray*", "displaymath", "math", "array",
             "matrix", "pmatrix", "bmatrix", "vmatrix", "Bmatrix", "smallmatrix",
             "split", "cases", "subequations", "thebibliography", "picture"]

TEX_DISPLAY_RE = re.compile(r"\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$")
TEX_INLINE_RE = re.compile(r"\\\([\s\S]*?\\\)|\$[^$\n]*?\$")
# \newcommand{\x}[2][default]{...}: the optional args sit between the two
# braced groups, where grab_args cannot step over them.
OPT_AFTER_DEF_RE = re.compile(
    r"(\\(?:new|renew|provide)command\s*\*?\s*\{[^}]*\})((?:\s*\[[^\]]*\])+)")
DEF_RE = re.compile(r"\\(?:g|x|e)?def\s*\\[A-Za-z@]+\s*(?=\{)")
BEGIN_END_RE = re.compile(r"\\(?:begin|end)\s*\{[^}]*\}(?:\s*\[[^\]]*\])?")
CMD_RE = re.compile(r"\\[A-Za-z@]+\*?")


def split_preamble(s):
    """(preamble, body). Nothing in a preamble reaches the page except the
    frontmatter, and its package options are full of words that read like
    prose -- 'caption', 'compress', 'article', 'most' -- so the body is taken
    whole and the preamble only for the few commands that do get typeset."""
    m = re.search(r"\\begin\s*\{document\}", s)
    return (s[:m.start()], s[m.end():]) if m else ("", s)


def keep_cmd_args(s, names):
    """Concatenate the braced arguments of \\name (optional args skipped)."""
    out = []
    pattern = re.compile(r"\\(?:%s)\s*(?:\[[^\]]*\])*\s*(?=\{)" % "|".join(names))
    for m in pattern.finditer(s):
        arg, _ = paper2md.balanced(s, m.end())
        if arg:
            out.append(arg)
    return "\n".join(out)


# Frontmatter: set in the preamble by most classes, typeset all the same.
PREAMBLE_KEEP = ["title", "subtitle", "author", "affil", "affiliation",
                 "address", "date", "thanks", "keywords"]


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def source_text_tex(path, skip_refs=True):
    s = read_text(path)
    if paper2md is None:                              # pragma: no cover
        sys.exit("srcdiff.py needs paper2md.py alongside it for --mode tex")

    s = paper2md.strip_comments(s)
    preamble, body = split_preamble(s)
    s = keep_cmd_args(preamble, PREAMBLE_KEEP) + "\n" + body
    # No truncation at \bibliography: an appendix conventionally follows it,
    # and cutting to end of file took the appendix with it. The bibliography
    # itself goes via DEF_COMMANDS (\bibliography) and DROP_ENVS
    # (thebibliography), which leave everything after them alone.
    s = OPT_AFTER_DEF_RE.sub(r"\1", s)
    for name, nargs in DEF_COMMANDS:
        s = paper2md.drop_cmd_arg(s, name, nargs)
    # \def\foo{...}: no argument count to look up, just take the group.
    while True:
        m = DEF_RE.search(s)
        if not m:
            break
        body, after = paper2md.balanced(s, m.end())
        if body is None:
            break
        s = s[:m.start()] + " " + s[after:]

    s = paper2md.replace_envs(s, DROP_ENVS, lambda node, body, anc: " ")
    s = TEX_DISPLAY_RE.sub(" ", s)
    s = TEX_INLINE_RE.sub(" ", s)
    for name, nargs in KEY_COMMANDS:
        s = paper2md.drop_cmd_arg(s, name, nargs)

    s = paper2md.decode_accents(s)
    s = BEGIN_END_RE.sub(" ", s)
    s = CMD_RE.sub(" ", s)
    s = re.sub(r"[{}~^_&]", " ", s)
    return [dehyphenate(s)]                           # one "page"


# --------------------------------------------------------------------------
# the source side: PDF text layer
# --------------------------------------------------------------------------

def _pdftotext_pages(path):
    proc = subprocess.run(["pdftotext", "-layout", path, "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.split("\f")


def _pypdf_pages(path):
    try:
        import pypdf
    except ImportError:
        sys.exit("srcdiff.py needs pdftotext on PATH or pypdf installed "
                 "(pip install pypdf) for --mode pdftext")
    return [(p.extract_text() or "") for p in pypdf.PdfReader(path).pages]


def strip_running_lines(pages):
    """Drop page numbers and any short line that repeats across pages.

    A running head ("EXTREME CORRELATION MATRICES") appears on every page and
    nowhere in the Markdown, so left in it reports as an omission on every
    page but one. Only the top and bottom few lines of each page are eligible,
    so a sentence that happens to recur in the body is never dropped.
    """
    if len(pages) < 3:
        return pages
    edge = collections.Counter()
    for text in pages:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # set(): on a short page lines[:3] and lines[-3:] overlap, and counting
        # a line twice let a one-off line clear the threshold on its own.
        for line in {l for l in lines[:3] + lines[-3:] if len(l) <= 90}:
            edge[fold(line)] += 1
    # A running head is on nearly every page. Requiring only half of them was
    # loose enough to take body text with it.
    threshold = max(3, (2 * len(pages) + 2) // 3)
    common = {k for k, v in edge.items() if v >= threshold}

    out = []
    for text in pages:
        kept = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            near_edge = i < 3 or i >= len(lines) - 3
            if near_edge and (fold(s) in common or re.fullmatch(r"[-—–\s\d]+", s)):
                continue
            kept.append(line)
        out.append("\n".join(kept))
    return out


def truncate_references(pages):
    """Cut everything from the last standalone 'References' heading onwards."""
    head_re = re.compile(r"^\s*(?:\d+\.?\s*)?(references|bibliography)\s*$", re.I)
    for i in range(len(pages) - 1, -1, -1):
        lines = pages[i].splitlines()
        for j in range(len(lines) - 1, -1, -1):
            if head_re.match(lines[j]):
                return pages[:i] + ["\n".join(lines[:j])]
    return pages


def source_text_pdftext(path, skip_refs=True):
    pages = _pdftotext_pages(path) if shutil.which("pdftotext") else None
    if pages is None:
        pages = _pypdf_pages(path)
    pages = strip_running_lines(pages)
    if skip_refs:
        pages = truncate_references(pages)
    return [dehyphenate(p) for p in pages]


# --------------------------------------------------------------------------
# the source side: OCR
# --------------------------------------------------------------------------

def source_text_ocr(path, dpi, psm, skip_refs=True, quiet=False):
    if not shutil.which("tesseract"):
        sys.exit("srcdiff.py --mode ocr needs tesseract on PATH "
                 "(apt-get install tesseract-ocr)")
    try:
        import pypdfium2
    except ImportError:
        sys.exit("srcdiff.py --mode ocr needs pypdfium2 (pip install pypdfium2)")
    import tempfile

    pdf = pypdfium2.PdfDocument(path)
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(len(pdf)):
            if not quiet:
                print(f"  OCR page {i + 1}/{len(pdf)}...", end="\r", file=sys.stderr)
            png = os.path.join(tmp, f"p{i + 1}.png")
            pdf[i].render(scale=dpi / 72.0).to_pil().save(png)
            proc = subprocess.run(["tesseract", png, "-", "--psm", str(psm)],
                                  capture_output=True, text=True)
            pages.append(proc.stdout)
    if not quiet:
        print(" " * 40, end="\r", file=sys.stderr)
    pages = strip_running_lines(pages)
    if skip_refs:
        pages = truncate_references(pages)
    return [dehyphenate(p) for p in pages]


# --------------------------------------------------------------------------
# mode detection
# --------------------------------------------------------------------------

def detect_mode(path):
    if os.path.splitext(path)[1].lower() in (".tex", ".ltx"):
        return "tex"
    try:
        import pypdfium2
        pdf = pypdfium2.PdfDocument(path)
        total = 0
        for page in pdf:
            try:
                total += len(page.get_textpage().get_text_range().strip())
            except Exception:
                pass
        per_page = total // max(1, len(pdf))
    except ImportError:
        pages = _pdftotext_pages(path) or _pypdf_pages(path)
        per_page = sum(len(p.strip()) for p in pages) // max(1, len(pages))
    from pdfpages import TEXT_LAYER_MIN_CHARS_PER_PAGE
    return "pdftext" if per_page >= TEXT_LAYER_MIN_CHARS_PER_PAGE else "ocr"


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

def classify(missing, other_vocab, min_len, fuzzy, math=None):
    """Split unmatched tokens into (real, {noise class: count}).

    Order matters: length is cheapest, math and hyphenation are exact, and the
    fuzzy match is both the most expensive and the most willing to explain
    something away, so it goes last.
    """
    math_blobs, math_vocab = math or (set(), set())
    real, noise = [], collections.Counter()
    vocab = list(other_vocab)
    for word, count in missing:
        if len(word) < min_len:
            noise["short"] += 1
            continue
        if word in math_vocab or any(word in blob for blob in math_blobs):
            noise["math"] += 1
            continue
        if _splits_into_known(word, other_vocab):
            noise["split"] += 1
            continue
        if fuzzy and difflib.get_close_matches(word, vocab, n=1, cutoff=0.82):
            noise["near"] += 1
            continue
        real.append((word, count))
    return real, noise


def _splits_into_known(word, vocab, min_part=3):
    """'deviceindependent' when the other side has 'device' and 'independent'."""
    for i in range(min_part, len(word) - min_part + 1):
        if word[:i] in vocab and word[i:] in vocab:
            return True
    return False


def context_for(word, pages, width=64):
    """First occurrence of `word`, with its surroundings, and its page."""
    pat = re.compile(r"\b%s\b" % re.escape(word), re.I)
    for n, text in enumerate(pages, 1):
        flat = re.sub(r"\s+", " ", fold(text))
        m = pat.search(flat)
        if m:
            lo = max(0, m.start() - width // 2)
            snippet = flat[lo:m.end() + width // 2].strip()
            return n, ("..." if lo else "") + snippet + "..."
    return None, ""


def run(md_path, source_pages, md_text, md_math, min_len, fuzzy, show):
    md_words = tokens(md_text)
    src_words = [w for page in source_pages for w in tokens(page)]
    md_count = collections.Counter(md_words)
    src_count = collections.Counter(src_words)

    absent_src = sorted((src_count - md_count).items())
    absent_src = [(w, c) for w, c in absent_src if w not in md_count]
    absent_md = sorted((md_count - src_count).items())
    absent_md = [(w, c) for w, c in absent_md if w not in src_count]

    missing, missing_noise = classify(absent_src, set(md_count), min_len, fuzzy,
                                      math=md_math)
    extra, extra_noise = classify(absent_md, set(src_count), min_len, fuzzy)

    deficits = [(w, src_count[w] - md_count[w]) for w in src_count
                if md_count.get(w, 0) and src_count[w] - md_count[w] >= 3]
    deficits.sort(key=lambda t: -t[1])

    return {
        "md_words": len(md_words), "source_words": len(src_words),
        "md_vocab": len(md_count), "source_vocab": len(src_count),
        "missing": missing, "missing_noise": dict(missing_noise),
        "extra": extra, "extra_noise": dict(extra_noise),
        "deficits": deficits[:5],
        "shown": show,
    }


def extract_source(source, mode="auto", skip_refs=True, ocr_dpi=300,
                   ocr_psm=6, quiet=False):
    """(mode, pages) for a .tex or .pdf, choosing the mode if asked to."""
    mode = mode if mode != "auto" else detect_mode(source)
    if mode == "tex":
        return mode, source_text_tex(source, skip_refs)
    if mode == "pdftext":
        return mode, source_text_pdftext(source, skip_refs)
    return mode, source_text_ocr(source, ocr_dpi, ocr_psm, skip_refs, quiet)


def compare(md_path, source_pages, skip_refs=True, min_len=4, fuzzy=True,
            show=40):
    """The whole comparison, for callers that already have the source text."""
    raw_md = read_text(md_path)
    md_text = markdown_prose(raw_md, skip_refs)
    md_math = markdown_nonprose(raw_md, skip_refs)
    res = run(md_path, source_pages, md_text, md_math, min_len, fuzzy, show)
    return res, md_text


def print_report(res, md_path, source_path, mode, npages, source_pages, md_text):
    print(f"srcdiff: {md_path}")
    print(f"     vs: {source_path}  [mode: {mode}, {npages} "
          f"page{'s' if npages != 1 else ''}]")
    print()
    print(f"  source prose {res['source_words']:>6} words, "
          f"{res['source_vocab']} distinct")
    print(f"  markdown     {res['md_words']:>6} words, "
          f"{res['md_vocab']} distinct")
    print()

    show = res["shown"]
    for key, noise_key, title, pages_for_ctx in (
            ("missing", "missing_noise",
             "in SOURCE, not in MARKDOWN (possible omission)", source_pages),
            ("extra", "extra_noise",
             "in MARKDOWN, not in SOURCE (possible invention)", [md_text])):
        items = res[key]
        noise = res[noise_key]
        filtered = sum(noise.values())
        detail = ", ".join(f"{v} {k}" for k, v in sorted(noise.items())) or "none"
        print(f"  {title}: {len(items)}")
        print(f"    (filtered as noise: {filtered} — {detail})")
        for word, count in items[:show]:
            page, ctx = context_for(word, pages_for_ctx)
            where = f"p.{page}" if page and len(pages_for_ctx) > 1 else "   "
            times = f" x{count}" if count > 1 else ""
            print(f"    {where}  {word}{times}")
            if ctx:
                print(f"          {ctx}")
        if len(items) > show:
            print(f"    ... {len(items) - show} more (raise --show)")
        print()

    if res["deficits"]:
        print("  largest count deficits (word present in both, fewer in markdown):")
        for word, delta in res["deficits"]:
            print(f"    {word}: -{delta}")
        print()

    print("  This is a report, not a pass/fail check — read the entries above")
    print("  against the source. Math debris and OCR misreads survive the")
    print("  filters routinely; a dropped sentence does not.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md", help="converted markdown file")
    ap.add_argument("--source", default=None, help="the .tex or .pdf it came from")
    ap.add_argument("--source-text", default=None,
                    help="a previous extraction to reuse (skips extraction)")
    ap.add_argument("--mode", choices=["auto", "tex", "pdftext", "ocr"],
                    default="auto")
    ap.add_argument("--min-len", type=int, default=4,
                    help="ignore unmatched tokens shorter than this (default 4)")
    ap.add_argument("--no-fuzzy", action="store_true",
                    help="do not excuse near-matches in the other vocabulary")
    ap.add_argument("--show", type=int, default=40,
                    help="max entries to print per direction (default 40)")
    ap.add_argument("--with-references", action="store_true",
                    help="compare the references section too (off by default: "
                         "a .tex usually cites a .bbl the source does not carry)")
    ap.add_argument("--ocr-dpi", type=int, default=300)
    ap.add_argument("--ocr-psm", type=int, default=6)
    ap.add_argument("--save-source", default=None,
                    help="write the extracted source text here")
    ap.add_argument("--json", default=None, help="also write the report here")
    ap.add_argument("--fail-on-missing", type=int, default=None,
                    help="exit 1 if more than N unmatched source words remain")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not a.source and not a.source_text:
        ap.error("need --source (or --source-text)")
    for path in (a.md, a.source, a.source_text):
        if path and not os.path.exists(path):
            ap.exit(2, f"no such file: {path}\n")

    skip_refs = not a.with_references
    if a.source_text:
        raw = read_text(a.source_text)
        source_pages = raw.split("\f")
        mode, source_path = "pre-extracted", a.source_text
    else:
        source_path = a.source
        mode, source_pages = extract_source(a.source, a.mode, skip_refs,
                                            a.ocr_dpi, a.ocr_psm, a.quiet)

    if a.save_source:
        with open(a.save_source, "w", encoding="utf-8") as fh:
            fh.write("\f".join(source_pages))

    res, md_text = compare(a.md, source_pages, skip_refs, a.min_len,
                           not a.no_fuzzy, a.show)

    if not a.quiet:
        print_report(res, a.md, source_path, mode, len(source_pages),
                     source_pages, md_text)

    if a.json:
        payload = dict(res)
        payload.update({"markdown": a.md, "source": source_path, "mode": mode})
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    if a.fail_on_missing is not None and len(res["missing"]) > a.fail_on_missing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
