# Corpus v0 Engineering Audit

Audit date: 2026-07-28

## Scope

This record covers repository structure, Git distribution boundaries, manifest source hashes,
dataset JSON Schema validation, source/version/Space links, quote hashes, declared locators and
the internal-only freeze decision. It does not claim public redistribution rights.

## Verified

- The package contains 12 Spaces, 90 manifest sources, and 276 unique evaluation cases.
- All 90 source files exist locally and match their declared `content_sha256`.
- All 276 JSONL records pass the Draft 2020-12 schema.
- All evidence source keys, source versions, Space boundaries, claim links, and 597
  `excerpt_sha256` values are internally consistent.
- The dataset has 127 development cases and 149 holdout cases across all seven declared categories.
- Raw corpus directories and `cases.jsonl` are excluded from Git; only control metadata, dataset
  schema and documentation, validation tooling, and small explicit fixtures are allowlisted.
- PDF extraction is fixed by `PDF-EXTRACTION.md`: Python 3.12, `PyMuPDF==1.28.0`,
  `Page.get_text("text")`, physical one-based pages, and NFKC/whitespace normalization.
- All 130 PDF evidence records now contain the exact output of that protocol and have refreshed
  `excerpt_sha256` values. `python cases/scripts/validate.py` passes with zero locator findings.
- Alternative-reader results and the protocol-switch decision are recorded in
  `PDF-EXTRACTOR-COMPARISON.md`.
- The 13 formula records whose extracted glyphs contain controls or private-use characters were
  visually checked against rendered pages. Their human-readable transcriptions are recorded in
  `PDF-VISUAL-REVIEW.yaml`, and the validator checks each case/evidence index, source, page and
  transcription status.

## Accepted Internal Exceptions

- All 467 text evidence records have a valid line range containing their stored quote. This was
  repaired in 85 locator records across 21 cases; the original JSONL is retained outside the
  repository under `tmp/cases.jsonl.before-locator-repair-20260727.jsonl`.
- Four PDF summary evidence records were replaced with verbatim excerpts from the cited PDFs
  (`qa-177`, `qa-178`, `qa-181`, and `qa-198`).
- The 13 formula records listed above are no longer unresolved: visual review confirmed their
  page images and the corrected transcriptions are in `PDF-VISUAL-REVIEW.yaml`. The original
  `quote` values intentionally remain the exact PyMuPDF output so their hashes and locators stay
  reproducible; a transcription is not substituted for extracted text.
- `math/Linear-Algebra-Notes.pdf` has no active evidence case, but page 1 still produces
  `BBBBB@` and control characters for a matrix. It must not become an evidence source until its
  font mapping is repaired.
- Several sources retain `license: undetermined` and `redistribution: review_required`. This is
  accepted for the internal-only freeze: those files remain `private_local`, are not repository
  fixtures, and may only be used by authorized team members for local development/evaluation.
  This record makes no public redistribution claim.
- The validator checks hashes, schema semantics, quote provenance, locator containment and the
  visual-review sidecar. It enforces the internal-only distribution boundary but does not turn an
  unresolved external license into a redistribution grant.

## Decision

The corpus is `frozen` for internal team use under `docs/stage-0-acceptance.md`. Raw sources,
`cases.jsonl` and derived artifacts remain local and excluded from Git. Stage 2 Step 9 and formal
Stage 3 quality gates remain separate and are not implied by this Stage 0 decision.
