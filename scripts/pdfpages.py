#!/usr/bin/env python3
"""
pdfpages.py — triage a PDF, and render its pages so they can be read.

Two jobs, both of which SKILL.md's PDF path needs before a conversion starts.

  --info    Does this PDF have a text layer at all? An image-only PDF (a
            scan) has none: pdftotext returns nothing, paper2md.py has no
            input, and the pages have to be read visually instead. That is a
            different job from converting a text-layer PDF, and it is worth
            one command to find out which one you are looking at rather than
            discovering it halfway through.

  --pages   Render pages to PNG. Dense math usually needs a second, closer
            look, so --band crops a fractional vertical slice of the page and
            renders just that, large enough to settle a subscript.

Rendering and the text probe both need pypdfium2 (pip install pypdfium2).

Usage
-----
    pdfpages.py paper.pdf --info
    pdfpages.py paper.pdf --pages 1-6 -o pages/
    pdfpages.py paper.pdf --pages 4 --band 0.22-0.40 --dpi 400 -o pages/

Exit codes: 0 ok, 2 usage error.
"""

import argparse, os, sys

# A page of body text carries thousands of characters; a scanned page carries
# none. Anything under this is not a usable text layer -- a handful of
# characters is a stamped page number or a watermark, not the paper.
TEXT_LAYER_MIN_CHARS_PER_PAGE = 100


def _pdfium():
    try:
        import pypdfium2
    except ImportError:
        sys.exit("pdfpages.py needs pypdfium2 (pip install pypdfium2)")
    return pypdfium2


def parse_pages(spec, npages):
    """'1-6', '4', '1,3,5-7', 'all' -> a sorted list of 1-based page numbers."""
    if spec.strip().lower() == "all":
        return list(range(1, npages + 1))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                sys.exit(f"bad page range: {part!r}")
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(part))
            except ValueError:
                sys.exit(f"bad page number: {part!r}")
    bad = [p for p in out if p < 1 or p > npages]
    if bad:
        sys.exit(f"page(s) out of range 1-{npages}: {sorted(bad)}")
    return sorted(out)


def parse_band(spec):
    """'0.22-0.40' -> (0.22, 0.40), fractions of page height from the top."""
    a, _, b = spec.partition("-")
    try:
        lo, hi = float(a), float(b)
    except ValueError:
        sys.exit(f"bad band: {spec!r} (want e.g. 0.22-0.40)")
    if not (0.0 <= lo < hi <= 1.0):
        sys.exit(f"bad band: {spec!r} (want 0 <= lo < hi <= 1)")
    return lo, hi


def page_text_lengths(pdf):
    """Characters the text layer yields for each page."""
    lengths = []
    for page in pdf:
        try:
            tp = page.get_textpage()
            lengths.append(len(tp.get_text_range().strip()))
        except Exception:
            lengths.append(0)
    return lengths


def report_info(path, pdf):
    lengths = page_text_lengths(pdf)
    n = len(lengths)
    total = sum(lengths)
    per_page = total // n if n else 0
    w, h = pdf[0].get_size() if n else (0, 0)

    print(f"{path} — {n} page{'s' if n != 1 else ''}, {w:.0f}x{h:.0f} pt")
    print(f"  text layer: {total} chars total, {per_page}/page")
    empty = [i + 1 for i, c in enumerate(lengths) if c < TEXT_LAYER_MIN_CHARS_PER_PAGE]
    if empty and len(empty) < n:
        shown = ", ".join(str(p) for p in empty[:12])
        more = f" (+{len(empty) - 12} more)" if len(empty) > 12 else ""
        print(f"  pages with no usable text: {shown}{more}")

    if per_page < TEXT_LAYER_MIN_CHARS_PER_PAGE:
        print("\n  IMAGE-ONLY — there is no text to extract.")
        print("  Read the pages visually (--pages), and verify the result with")
        print("  srcdiff.py --mode ocr; paper2md.py has no input here.")
    else:
        print("\n  Text layer present.")
        print("  Verify a conversion against it with srcdiff.py --mode pdftext.")
    return 0


def render(path, pdf, pages, dpi, band, outdir):
    os.makedirs(outdir, exist_ok=True)
    scale = dpi / 72.0
    stem = os.path.splitext(os.path.basename(path))[0]
    written = []
    for p in pages:
        img = pdf[p - 1].render(scale=scale).to_pil()
        suffix = ""
        if band:
            lo, hi = band
            w, h = img.size
            img = img.crop((0, int(h * lo), w, int(h * hi)))
            suffix = f"_{lo:g}-{hi:g}"
        name = os.path.join(outdir, f"{stem}_p{p}{suffix}.png")
        img.save(name)
        written.append((name, img.size))
    for name, (w, h) in written:
        print(f"{name}  {w}x{h}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--info", action="store_true",
                    help="report page count and whether a text layer exists")
    ap.add_argument("--pages", default=None,
                    help="pages to render: '1-6', '4', '1,3,5-7', 'all'")
    ap.add_argument("--dpi", type=int, default=200,
                    help="render resolution (default 200; use 400+ with --band)")
    ap.add_argument("--band", default=None,
                    help="crop a fractional vertical slice, e.g. 0.22-0.40")
    ap.add_argument("-o", "--out", default=".", help="output directory")
    a = ap.parse_args()

    if not a.info and not a.pages:
        ap.error("nothing to do: pass --info or --pages")
    if not os.path.exists(a.pdf):
        ap.exit(2, f"no such file: {a.pdf}\n")

    pdfium = _pdfium()
    pdf = pdfium.PdfDocument(a.pdf)
    rc = 0
    if a.info:
        rc |= report_info(a.pdf, pdf)
    if a.pages:
        pages = parse_pages(a.pages, len(pdf))
        band = parse_band(a.band) if a.band else None
        if a.info:
            print()
        rc |= render(a.pdf, pdf, pages, a.dpi, band, a.out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
