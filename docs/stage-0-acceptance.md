# Stage 0 Acceptance

Acceptance date: 2026-07-28  
Scope: internal team distribution only

## Decision

Stage 0 is formally complete for the controlled internal evaluation corpus
`knowledge-qa-v0`. The corpus manifest is frozen with
`distribution_scope: internal_team_only`. This decision authorizes local
development and evaluation by the project team; it does not grant public
redistribution rights and does not authorize sending corpus content to an
external provider.

## Frozen Inputs

| Input | Version / hash |
| --- | --- |
| Corpus manifest | `v0` / `53d6f863060dd7d5e6affb0abda64348f0b498995b3d1cda5ef80f0e576ca738` |
| Evaluation cases | `knowledge-qa-v0` / `19b58c1471ff69b748233c61470cb19ce38c74d0bf6b83c4555cde18d7456fda` |
| Dataset schema | `4949f3f4a9212f5fa02c915850f06a64bde501df0568e57fab582057a3b45e16` |
| Development split | 127 cases / `6e870ee94cb64367d111557a2d1fdfc0d3f8b231e2d60cde7e7118e3881e239c` |
| Holdout split | 149 cases / `ec56aa778587f0b66b78a2aa2d55dad04e187930f1b75423bb42b5d31a077b28` |
| PDF visual review | 13 entries / `41b0b00404465d4d5b4e4f7b98b96cf78ec2840455fe5c1f9ec0438944f17b57` |

The corpus contains 12 Spaces and 90 manifest sources. Every source exists
locally and matches its manifest SHA-256. The dataset contains 276 cases and
all 597 evidence excerpt hashes and locators pass validation.

## Extraction Baseline

PDF extraction is fixed to Python 3.12, `PyMuPDF==1.28.0`,
`Page.get_text("text")`, physical one-based pages, and NFKC/whitespace
normalization. All 130 PDF evidence records were regenerated under this
protocol. Formula pages with unreliable embedded-font mappings are preserved
as raw protocol output and have verified human-readable transcriptions in
`cases/evals/corpus/v0/PDF-VISUAL-REVIEW.yaml`.

## Source Exceptions

- Source PDFs are not rewritten. The page-1 matrix extraction in
  `math/Linear-Algebra-Notes.pdf` is excluded from active evidence until its
  font mapping can be repaired.
- The sign printed in the Maxwell-Boltzmann expression on statistical
  mechanics page 17 is retained as source text; correcting the original PDF
  is outside this acceptance and would require a new source hash.
- Sources whose external license or redistribution status is not established
  remain `private_local`/`review_required` in the manifest. The internal-only
  freeze is an access-control decision, not a license assertion.

## Distribution Boundary

Raw corpus files, `cases/evals/datasets/knowledge-qa-v0/cases.jsonl`, parser
caches, screenshots, embeddings and reports remain outside Git and outside
external services. Git contains only the manifest, schema, control documents,
validation scripts, visual-review metadata and explicitly permitted fixtures.
The previous dataset is retained under `tmp/cases-old/`.

## Verification

```text
uv run --frozen python cases/scripts/validate.py --max-issues 100
  spaces: 12; sources: 90; cases: 276; visual reviews: 13
  [OK] all validation checks passed

uv run --frozen pytest
  554 passed, 41 skipped
```

Stage 0 is complete. Stage 2 Step 9, the formal Stage 3 development/holdout
quality gates, and later application stages remain separate gates and are not
implicitly completed by this record.
