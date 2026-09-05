---
name: paper-to-markdown
description: "Convert an academic paper (LaTeX source, or PDF-only) into clean Markdown an LLM can read accurately: exact text, equations in $…$, semantic structure, figures as node/edge data. Driven by the paper2md.py script (github.com/mattpusey/paper-to-markdown), which does the mechanical work and flags what needs judgement. Use when asked to turn a paper, preprint, manuscript or .tex into markdown, or to make a document LLM-readable."
---

# Paper → LLM-readable Markdown

The target is a Markdown file a language model can read as accurately as a human reads the PDF: same words, working math, intact structure, figures as data rather than pictures.

**Do not hand-transcribe the prose.** A script does the mechanical 90% and emits a manifest of what it refused to guess; you work only that manifest. Hand transcription fails by silent omission, scales with paper length, and forces an expensive verification pass to catch what it drops. The script's cost scales with the number of *hard figures* instead.

## 0. Check the target is right

Markdown is not always the answer. Before converting, ask what consumes the output:

- **A human will also read it, it must render, or it leaves a model's context** → Markdown. Proceed.
- **Pure LLM input, nothing else** → a cleaned `.tex` is a legitimate competitor: no transcription risk at all, and semantics like `\begin{theorem}` are explicit rather than conventional. Say so rather than converting on autopilot.

One rule if you do go to Markdown: **one escaping regime**. LaTeX math belongs in `$…$`, which is a delimiter that stops Markdown cleanly. Undelimited LaTeX (`\begin{theorem}` in a `.md`) is not a hybrid, it is an ambiguity — nothing says whether `*` inside it is emphasis or multiplication, and `X_U` becomes an italics trigger. Pick one regime and hold it.

## 1. Get the script

The script should be in the `scripts/` directory of this skill, or it may already be somewhere on the system  — check before cloning:

```bash
command -v paper2md.py || ls ./paper2md.py ../paper2md.py 2>/dev/null \
  || git clone https://github.com/mattpusey/paper-to-markdown.git
```

Entry point is `paper2md.py`. It needs **pylatexenc** (`pip install pylatexenc`, 2.11 is fine) — the project's only runtime dependency, used for LaTeX structure scanning (nesting-aware `\begin`/`\end` matching, brace matching, comment stripping) and for turning accent constructs into Unicode. Nothing else is needed to convert; verification additionally wants `npm install katex` (step 5) and Node on `PATH`.

If the repo is unreachable and no local copy exists, the design is specified below well enough to rebuild — but prefer the real thing, which has been tested end to end (`python3 scripts/test_paper2md.py`).

## 2. Find the source, and compile it

List the whole folder first: people hand over a PDF while the `.tex` sits beside it. Preference is `.tex` > `.docx` (use the `docx` skill) > PDF alone (use the `pdf` skill, and expect to repair ligatures, hyphenation and two-column reflow).

Then **compile the paper**, because `.aux` and `.bbl` are what make the conversion exact:

- `.aux` carries `\newlabel{key}{{2.2}{7}...}` — the label→number map for every figure, equation, theorem and section. No counter reimplementation, no `\counterwithin`/appendix-reset edge cases, no scraping numbers out of `pdftotext`.
- `.bbl` carries the bibliography already rendered and numbered by the `.bst`. No reimplementing `apsrev4-2`.

Both regenerate on compile, so they can never be stale relative to the source — unlike a PDF sitting in the folder.

```bash
pdflatex -interaction=nonstopmode paper && bibtex paper && pdflatex paper && pdflatex paper
```

If the compile fails on a missing package, **drop typography-only packages** — they never affect counters. `newtxtext`, `microtype` and `savetrees` are the usual offenders; the last two enable pdfTeX font expansion, which dies with "auto expansion is only possible with scalable fonts" when the font package is missing. Never drop `chngcntr`, `thm-restate`, `subcaption`, `appendix` or `amsmath` — those *do* change numbering. Compile a patched copy, never the user's original.

If a document class (e.g. `revtex4-1`) or a `.bst` isn't installed and the environment has no `tlmgr`/CTAN access, check whether a broader package (`texlive-publishers` etc.) is installable via the system package manager instead — that's often reachable even when CTAN itself is blocked. Compile wherever a working LaTeX toolchain is actually reachable (a cloud sandbox is fine); only the resulting `.aux`/`.bbl` need to travel back to wherever `paper2md.py` runs.

Frontmatter note: `\title`/`\author`/`\email`/`\affiliation`/`\date` sometimes sit *after* `\begin{document}` rather than in the preamble (REVTeX classes do this). The script's title/author detection only looks in the preamble, so on these papers it emits `no-title` and leaves the whole frontmatter block as raw unexpanded commands — rebuild it by hand as a heading plus author/affiliation lines (see step 4).

If you genuinely cannot compile, run without `--aux`/`--bbl`: every reference becomes a flag rather than a wrong number.

## 3. Run it

```bash
python3 paper2md.py paper.tex -o paper.md \
    --aux paper.aux --bbl paper.bbl \
    --styles sv=selected,mv=latent,rv=visible \
    --drop-color
```

- `--styles` maps TikZ node styles to their meaning. The `tikzset` definitions live in the preamble; without this the semantic distinction the figures exist to show is lost. Read the preamble and supply the mapping.
- `--drop-color` strips `\color{...}` revision markup (e.g. a `\mma`/`\blk` review-comment convention), which otherwise surfaces as literal text mid-sentence. Mention to the user that you dropped it — it is invisible in the PDF and authors often forget it is there. Content it was wrapping (an inline review comment, an author to-do list) is real text still live in the compiled PDF, not something the conversion invented — set it apart clearly (e.g. a blockquote labelled "authors' comment") rather than blending it into the prose.
- `--macro-override-text` / `--macro-override-math` handle macros that switch math mode inside their own body, e.g. `\newcommand{\flip}{{\tt ancS${\rightarrow}$chM}}`. No single expansion is correct for these — they need `$…$` in text and bare inside math — so the script refuses to expand them and asks for both forms.
- `--macro-override NAME=REPLACEMENT` also covers commands that are real LaTeX but never appear as a `\newcommand` in the paper's own source — typically class/package symbols like `\openone` (identity operator, from `revsymb4-1.sty` on REVTeX papers) or `\Zset` from a physics-symbols package. The script only expands macros it found defined in the document, so these pass through untouched and KaTeX will reject them; override with a KaTeX-renderable equivalent (`--macro-override 'openone=\mathbb{1}'`).

The script inserts a separating space itself wherever expanding a macro would otherwise glue two letters into a different control word (e.g. a `\newcommand{\ot}{\otimes}` used as `\ot N` used to come out as `\otimesN`, and a template like `\langle#2` filled with a letter-starting argument used to come out as `\langlem_c`) — so this class of KaTeX "Undefined control sequence" failure should be rare now. It can still happen with a macro from `--macro-override` itself if the replacement text doesn't anticipate what follows it; if verification (step 5) turns one up, check whether the surrounding source has a letter running straight into the override.

## 4. Work the flag manifest

`paper.flags.json` is the work queue. Resolve every entry; do not ship with flags outstanding.

| Flag | What to do |
|---|---|
| `figure-image` | `\includegraphics` — actually look at the image (stage it and Read it), then write the description |
| `tikz-unparsed-commands` | `\foreach`, `\path`, plots, decorations — read the TikZ and encode it by hand |
| `tikz-style-unmapped` | rerun with the style in `--styles` |
| `tikz-duplicate-node-name` | the same TikZ node id is defined more than once inside one `tikzpicture` — almost always several sub-panels (`\begin{scope}[xshift=...]`) sharing one picture and reusing node names panel to panel. TikZ allows this (each `\draw` binds to whichever definition precedes it) but the script's flat per-picture node/edge extraction doesn't track scope, so the emitted Nodes/Edges list silently merges panels and mislabels edges. Read the `\tikzpicture` source, work out which `\node`/`\draw` lines belong to which `\scope`, and write each panel's node/edge list separately (one fenced block per subfigure, per "why the figures are encoded this way" below) |
| `tikz-multi-scope` | multiple `\scope` blocks in one `tikzpicture` with *no* reused node id — check by hand anyway: panels commonly use distinct TikZ ids (`A1`, `A2`, ...) while every panel's node is still *displayed* as the same letter (`$A$`), which the script also can't tell apart from one shared diagram. Same fix as `tikz-duplicate-node-name`: split and rebuild per panel |
| `macro-mode-switching` | supply `--macro-override-text` and `--macro-override-math` |
| `macro-optional-arg`, `macro-complex-signature` | `\newcommand[n][default]` or a `\NewDocumentCommand` with `o`/`s` — expand by hand |
| `table-complex` | multirow/multicolumn — write the Markdown table yourself |
| `citet-manual` | `\citet` renders author names via the `.bst`; check the rendering against the PDF |
| `ref-unresolved`, `cite-unresolved`, `no-aux`, `no-bbl` | go back and compile (step 2) |
| `no-title` | `\title` wasn't found in the preamble — likely a REVTeX-style paper with frontmatter after `\begin{document}` (see step 2); rebuild the title/author/affiliation block by hand |
| `table-nested` | a `tabular` inside another `tabular`'s cell. The outer table is emitted in full with a `[nested table — see flags]` marker in that cell; write the inner table out by hand (or inline it, if it is short) |
| `accent-unrendered` | an accent construct that produced no character — `\~{}` used as a literal tilde inside a URL is the usual case. Rewrite it by hand |
| `escaping-regime` | a real defect — a bare `\command` or `_` escaped into text. Fix the cause, not the symptom. A `\command{color}{...}` wrapper (e.g. `\textcolor{blue}{...}`) surviving as literal text is the same class of defect as `--drop-color` handles for `\color{...}` — strip the wrapper, keep the content, note it to the user |

## 5. Verify

`paper2md.py` self-checks the escaping regime as it converts; **zero is the only acceptable number**. Everything else is `verify.py`, which also works on output the script never produced — a PDF-only conversion, or a file you hand-edited:

```bash
npm install katex --silent          # verify.py skips the math check without it
python3 verify.py paper.md --outline
```

(`paper2md.py` itself only needs `pylatexenc`; KaTeX is a verification-time dependency.)

It runs four checks and exits non-zero if any fails:

- **Every expression parses**, via `katex_check.js`. All four LaTeX delimiter pairs are recognised on the way in (`$…$`, `$$…$$`, `\[…\]`, `\(…\)`), so a paper that uses TeX's own `$$…$$` converts like any other. Watch for accents LaTeX tolerates and KaTeX rejects — `\tilde\mathcal{H}` needs to be `\tilde{\mathcal{H}}` — and for `$a$$b$` from adjacent inline groups, which reads as a display-math opener. A leftover `\doibase` inside a `[text](\doibase 10.xxxx/...)` link (from REVTeX's `.bst`, expands to `http://dx.doi.org/`) is a common one on APS papers — rewrite the link target by hand if the override table didn't already catch it.
- **Escaping regime**, reusing `paper2md.run_checks` so there is one implementation of the rule.
- **Equation numbering** — `\tag{}` gaps, duplicates and ordering. Sectioned tags (`2.2`, `A.1`) are reported rather than treated as gaps.
- **Cross-references** — every `[n]` resolves to a reference entry, every `Fig. n`/`Table n` in the prose has a caption. Float numbers are compared as strings, so `\counterwithin` numbering (`Figure 1.1`) and appendix numbering (`A.1`) work; a subfigure reference (`Fig. 4a`) resolves to its parent caption.

`--outline` prints the heading tree; check the numbering against the PDF and that the appendices survived. `--require-math` turns a skipped KaTeX check into a failure, for CI.

Still manual, and the one that catches silent omission:

- **No content lost.** Word-frequency diff the Markdown prose against the source, both directions. Strip fenced blocks and math from the Markdown side; strip comments, `tikzpicture` bodies, and the *arguments* of `\label`/`\ref`/`\cite`/`\begin` from the `.tex` side, or label names like `main_theorem_classical` masquerade as prose. Against a PDF instead of a `.tex`, strip the page markers and running page numbers, and expect PDF line-break hyphenation (`corre-`/`lation`) to show up as a diff on both sides.

A pandoc differential (`pandoc -f latex -t markdown`) is available as a third opinion, but it is largely redundant now that prose is a pass-through: pandoc drops all figures, flattens theorem environments, loses equation and citation numbering, and leaks `\label` names and `\color` markup into the text. Reach for it only when something looks reordered.

## Why the figures are encoded this way

A TikZ graph figure *is already an edge list* — `\draw[->] (m1) -- (m2);` is a directed edge, and `\node[sv]` carries the type. Converters that render TikZ to SVG or PNG destroy structured data to produce pixels; an image of a DAG is strictly worse for a model than the edges. On a measured example the node/edge encoding came out at **half the tokens of the raw TikZ** while carrying semantics the raw TikZ had lost with its preamble.

So: node list with bracketed type tags, edge list in source order, original caption verbatim underneath, one fenced block per subfigure. For non-graph figures — a plot with data in the source, a table typeset as a figure — emit the data as a table instead. Only genuine images get prose descriptions.

A single `tikzpicture` containing several `\begin{scope}` blocks (a common way to lay out side-by-side sub-figures without separate `\begin{tikzpicture}` environments) is still one subfigure as far as the script's splitting logic is concerned — it only splits on multiple `tikzpicture`/`subfigure` environments. `tikz-duplicate-node-name` and `tikz-multi-scope` (step 4) exist to catch this and route it to manual per-panel encoding instead of a silently merged one.

## Reporting back

Say which source you converted from (and that you checked for `.tex` before settling for a PDF), that you compiled for `.aux`/`.bbl`, how figures are represented, and the verification results **as counts, not adjectives** — "13 display and 513 inline expressions parse, zero escaping violations, one flag outstanding". Flag any normalisation applied to the author's own text, with source line numbers, and never silently fix their typos.
