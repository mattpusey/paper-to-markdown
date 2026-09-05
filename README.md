# Paper to Markdown

A Codex plugin for converting LaTeX or PDF academic papers into accurate, LLM-readable Markdown. It preserves equations, document structure, citations, tables, and converts graph figures into semantic node/edge data where possible.

## What the plugin includes

- A `paper-to-markdown` skill with a source-first conversion workflow
- `paper2md.py` for deterministic LaTeX-to-Markdown conversion
- `verify.py` and KaTeX checks for output validation
- Regression fixtures and tests for accents, math delimiters, nesting, and tables

## Requirements

- Python 3
- `pylatexenc` (2.11 is supported)
- Optional verification dependencies: Node.js and `katex`
- A LaTeX toolchain when compiling source to generate exact `.aux` and `.bbl` metadata

## Development

Run the regression suite from the plugin root:

    python3 skills/paper-to-markdown/scripts/test_paper2md.py

Validate the plugin manifest and structure with the plugin-creator validator before publishing.

## License

MIT
