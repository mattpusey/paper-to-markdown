#!/usr/bin/env python3
"""
verify.py — post-conversion checks for a paper converted to Markdown.

SKILL.md section 5 asks for three verifications before a conversion ships.
This runs the two that are purely mechanical:

  1. Every math expression parses (delegated to katex_check.js).
  2. Structure is self-consistent — equation numbering has no gaps or
     duplicates, every citation resolves to a reference, every figure and
     table referenced in the prose exists.

Plus the escaping-regime check, reused from paper2md.run_checks() rather
than reimplemented, so it also covers output that paper2md.py never
produced (a PDF-only conversion, or a hand-edited file).

The third verification in SKILL.md — the word-frequency diff against the
source — is not here. It needs a different stripper per source type
(.tex comments and tikzpicture bodies vs. a PDF text layer) and is a
separate piece of work.

Usage
-----
    python3 verify.py paper.md [--json report.json] [--require-math]

Exit codes: 0 clean, 1 problems found, 2 usage error.
"""

import argparse, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paper2md import run_checks as _paper2md_run_checks
    import paper2md as _paper2md
except ImportError:                                   # pragma: no cover
    _paper2md_run_checks = None

# --------------------------------------------------------------------------
# stripping
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"```[\s\S]*?```")
DISP_RE = re.compile(r"\$\$[\s\S]*?\$\$")
INLINE_RE = re.compile(r"\$[^$\n]+?\$")


def prose_only(md):
    """Markdown with fences and math removed, for scanning cross-references."""
    s = FENCE_RE.sub(" ", md)
    s = DISP_RE.sub(" ", s)
    s = INLINE_RE.sub(" ", s)
    return s


def split_references(md):
    """Return (body, references_section). Either may be empty."""
    m = re.search(r"^##+\s*References\s*$", md, re.M)
    if not m:
        return md, ""
    return md[:m.start()], md[m.end():]


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_math(md_path, require):
    """Run every expression through KaTeX. Returns (section_dict, ok)."""
    js = os.path.join(HERE, "katex_check.js")
    node = shutil.which("node")
    if not node or not os.path.exists(js):
        why = "node not on PATH" if not node else "katex_check.js missing"
        return ({"status": "SKIPPED", "reason": why}, not require)

    proc = subprocess.run([node, js, md_path], capture_output=True, text=True)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ({"status": "ERROR",
                 "reason": (proc.stderr or proc.stdout).strip()[:300]}, not require)

    if data.get("error"):
        return ({"status": "SKIPPED",
                 "reason": data["error"], "hint": data.get("hint", "")}, not require)

    data["status"] = "OK" if not data["failures"] and not data["stray_dollars"] else "FAIL"
    return (data, data["status"] == "OK")


def check_escaping(md):
    """Reuse paper2md's own escaping-regime scan; do not duplicate the rule."""
    if _paper2md_run_checks is None:
        return ({"status": "SKIPPED", "reason": "paper2md.py not importable"}, True)
    before = len(_paper2md.FLAGS)
    n = _paper2md_run_checks(md)
    found = _paper2md.FLAGS[before:]
    return ({"status": "OK" if not n else "FAIL",
             "violations": n,
             "detail": [f"line {f['line']}: {f['snippet']}" for f in found]},
            n == 0)


def check_equations(md):
    """\\tag{} continuity. Tags may be numeric (7) or sectioned (2.2, A.1)."""
    tags = re.findall(r"\\tag\{([^}]*)\}", md)
    numeric, other, dupes, seen = [], [], [], set()
    for t in tags:
        t = t.strip()
        if t in seen:
            dupes.append(t)
        seen.add(t)
        (numeric if t.isdigit() else other).append(t)

    out = {"total": len(tags), "numeric": len(numeric),
           "sectioned": sorted(set(other)), "duplicates": sorted(set(dupes))}
    ok = not dupes

    if numeric:
        ints = [int(t) for t in numeric]
        out["range"] = [min(ints), max(ints)]
        out["ascending"] = ints == sorted(ints)
        gaps = [n for n in range(min(ints), max(ints) + 1) if n not in set(ints)]
        out["gaps"] = gaps
        # a gap is only meaningful when numbering starts at 1; papers that
        # number per-section legitimately skip in the flat integer view
        if gaps and not other:
            ok = False
        if not out["ascending"]:
            ok = False
    out["status"] = "OK" if ok else "FAIL"
    return out, ok


def check_citations(md):
    """Every [n] in the prose resolves to a reference entry, and vice versa."""
    body, refs = split_references(md)
    if not refs.strip():
        return {"status": "SKIPPED", "reason": "no References section"}, True

    defined = set(re.findall(r"^\[(\d+)\]", refs, re.M))
    cited = set()
    for group in re.findall(r"\[([\d,\s]+)\]", prose_only(body)):
        for n in re.findall(r"\d+", group):
            cited.add(n)

    dangling = sorted(cited - defined, key=int)
    uncited = sorted(defined - cited, key=int)
    ok = not dangling
    return ({"status": "OK" if ok else "FAIL",
             "defined": len(defined), "cited": len(cited),
             "dangling": dangling, "uncited": uncited}, ok)


def check_floats(md):
    """Figure/table captions, encoded blocks, and references to them."""
    body = prose_only(md)
    figs = [int(n) for n in re.findall(r"\*\*Figure (\d+):", md)]
    tabs = [int(n) for n in re.findall(r"\*\*Table (\d+):", md)]
    blocks = len(re.findall(r"```figure", md))

    ref_figs = set(int(n) for n in
                   re.findall(r"\bFig(?:ure)?s?\.?\s*(\d+)", body))
    ref_tabs = set(int(n) for n in re.findall(r"\bTables?\s*(\d+)", body))

    missing_f = sorted(ref_figs - set(figs))
    missing_t = sorted(ref_tabs - set(tabs))
    ok = not missing_f and not missing_t

    return ({"status": "OK" if ok else "FAIL",
             "figure_captions": len(figs), "figure_blocks": blocks,
             "table_captions": len(tabs),
             "referenced_but_absent_figures": missing_f,
             "referenced_but_absent_tables": missing_t}, ok)


def outline(md):
    """Heading tree, for eyeballing against the PDF. Informational only."""
    heads = []
    for line in FENCE_RE.sub(" ", md).split("\n"):
        m = re.match(r"^(#{2,6})\s+(.*)$", line)
        if m:
            heads.append("  " * (len(m.group(1)) - 2) + m.group(2).strip())
    return heads


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md", help="converted markdown file")
    ap.add_argument("--json", default=None, help="also write the report here")
    ap.add_argument("--require-math", action="store_true",
                    help="treat a skipped KaTeX check as a failure (for CI)")
    ap.add_argument("--outline", action="store_true",
                    help="print the heading tree for comparison against the PDF")
    a = ap.parse_args()

    if not os.path.exists(a.md):
        print(f"no such file: {a.md}", file=sys.stderr)
        return 2

    md = open(a.md, encoding="utf-8").read()

    report, oks = {}, []
    for name, (section, ok) in [
        ("math", check_math(a.md, a.require_math)),
        ("escaping_regime", check_escaping(md)),
        ("equation_numbering", check_equations(md)),
        ("citations", check_citations(md)),
        ("floats", check_floats(md)),
    ]:
        report[name] = section
        oks.append(ok)

    # ---- report ----
    print(f"verifying {a.md}\n")

    m = report["math"]
    if m["status"] in ("SKIPPED", "ERROR"):
        print(f"  math                {m['status']}: {m.get('reason','')}"
              f" {m.get('hint','')}")
    else:
        print(f"  math                {m['status']}  "
              f"({m['display']} display, {m['inline']} inline, "
              f"{m['fences']} fenced blocks)")
        for f in m["failures"]:
            print(f"      FAIL {f['kind']} #{f['index']}: {f['message']}")
            print(f"           {f['excerpt']}")
        if m["stray_dollars"]:
            print(f"      {m['stray_dollars']} unmatched $ remaining")

    e = report["escaping_regime"]
    print(f"  escaping regime     {e['status']}"
          + (f"  ({e['violations']} violations)" if "violations" in e else
             f": {e.get('reason','')}"))
    for d in e.get("detail", [])[:10]:
        print(f"      {d}")

    q = report["equation_numbering"]
    bits = [f"{q['total']} tags"]
    if "range" in q:
        bits.append(f"{q['range'][0]}-{q['range'][1]}")
        bits.append("ascending" if q["ascending"] else "OUT OF ORDER")
    print(f"  equation numbering  {q['status']}  ({', '.join(bits)})")
    if q.get("gaps"):
        print(f"      gaps: {q['gaps']}")
    if q["duplicates"]:
        print(f"      duplicates: {q['duplicates']}")
    if q["sectioned"]:
        print(f"      non-integer tags: {q['sectioned']}")

    c = report["citations"]
    if c["status"] == "SKIPPED":
        print(f"  citations           SKIPPED: {c['reason']}")
    else:
        print(f"  citations           {c['status']}  "
              f"({c['cited']} cited, {c['defined']} defined)")
        if c["dangling"]:
            print(f"      cited but not defined: {c['dangling']}")
        if c["uncited"]:
            print(f"      defined but never cited: {c['uncited']}")

    fl = report["floats"]
    print(f"  figures & tables    {fl['status']}  "
          f"({fl['figure_captions']} figure captions, {fl['figure_blocks']} blocks, "
          f"{fl['table_captions']} table captions)")
    if fl["referenced_but_absent_figures"]:
        print(f"      referenced but absent: Fig {fl['referenced_but_absent_figures']}")
    if fl["referenced_but_absent_tables"]:
        print(f"      referenced but absent: Table {fl['referenced_but_absent_tables']}")

    if a.outline:
        print("\n  outline:")
        for h in outline(md):
            print("    " + h)

    print()
    failed = [k for k, v in report.items() if v.get("status") == "FAIL"]
    if failed:
        print("FAILED: " + ", ".join(failed))
    else:
        print("all checks passed"
              + (" (some skipped)" if any(v.get("status") == "SKIPPED"
                                          for v in report.values()) else ""))

    if a.json:
        json.dump(report, open(a.json, "w"), indent=2)
        print(f"wrote {a.json}")

    return 0 if all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
