# PDF extraction protocol

PDF evidence in corpus v0 is defined against one reproducible extraction
environment. Do not update a PDF quote with another library or another
PyMuPDF release.

- Python: 3.12.x (the repository lock was generated and validated with 3.12.13;
  `.python-version` selects the 3.12 line).
- `PyMuPDF`: exactly `1.28.0`, pinned in the root project and infrastructure
  package and recorded in `uv.lock` (MuPDF runtime `1.29.0`).
- Reader: `fitz.open(stream=<source bytes>, filetype="pdf")` for ingestion and
  `fitz.open(<manifest path>)` for validation.
- Text: `page.get_text("text")` once per physical page; an empty result is the
  empty string.
- Page numbers: physical pages are one-based (`page=1` is `document[0]`).
- Locator comparison: NFKC Unicode normalization followed by collapsing runs
  of whitespace to one ASCII space and trimming both ends.

The protocol identifier used by `cases/scripts/validate.py` is:

`python3.12-pymupdf-1.28.0-Page.get_text-text-nfkc-whitespace-v1`

All PDF evidence quotes were regenerated from this exact environment. Some
PDFs contain embedded-font/ToUnicode mappings that expose delimiters as control
or private-use characters. Their pages render normally. The affected evidence
is visually checked in `PDF-VISUAL-REVIEW.yaml`; the sidecar contains a readable
transcription while the quote remains the exact protocol output.
