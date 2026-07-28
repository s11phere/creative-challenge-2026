# PDF extractor comparison

This comparison was performed on 2026-07-28 before the corpus protocol was
switched. The source files and manifest were not changed by the comparison.

## Environment

- Python: 3.12.13
- Previous protocol: `pypdf 6.14.2` / `PdfReader.extract_text()`
- Current protocol: `PyMuPDF 1.28.0` / `Page.get_text("text")`
- PyMuPDF runtime: MuPDF 1.29.0
- pdfplumber: 0.11.10 (`pdfminer-six 20260107`)
- pypdfium2: 5.12.1

All four readers reported exactly the same physical page count for every one
of the 30 manifest PDFs. The table below compares the 36 records that failed
under the previous pypdf protocol; a match means the old stored quote occurred
verbatim after NFKC and whitespace normalization.

| source | previous failing records | PyMuPDF old-quote match | pdfplumber | pypdfium2 |
| --- | ---: | ---: | ---: | ---: |
| `math/linear-algebra-2` | 6 | 5 | 0 | 0 |
| `physics/electrodynamics` | 5 | 0 | 0 | 0 |
| `physics/mathphys1` | 1 | 1 | 0 | 0 |
| `physics/mathphys2` | 5 | 3 | 0 | 0 |
| `physics/quantum` | 10 | 7 | 0 | 0 |
| `physics/statmech` | 5 | 3 | 0 | 0 |
| `physics/theorymech` | 4 | 4 | 0 | 0 |
| **total** | **36** | **23** | **0** | **0** |

## Decision

PyMuPDF was accepted as the fixed protocol because it produced readable CJK
text and materially better formula glyphs on the affected pages, while page
counts remained stable across all readers. Its output also passed visual spot
checks on the previously healthy CS229, paper, Rudin and optics samples.

The 130 PDF evidence records were regenerated from PyMuPDF 1.28.0 and their
`excerpt_sha256` values recomputed. The current validator therefore checks the
new output, not the old pypdf strings.

Exact string equality is still not a semantic formula check. Thirteen records
contained vector arrows, integrals, matrices, or other glyphs whose Unicode
mapping is not fully trustworthy even though their pages are visually legible.
They were checked against rendered pages and transcribed in
`PDF-VISUAL-REVIEW.yaml`; the raw quotes were deliberately left unchanged.

`pdfplumber` and `pypdfium2` were not adopted: neither reproduced any of the
previous stored quotes and both lose formula/layout information on these pages.
