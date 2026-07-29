# Dataset: knowledge-qa-v1

## Purpose

This dataset is the versioned successor to the frozen internal `knowledge-qa-v0` baseline. It
keeps the v0 corpus and split membership while repairing confirmed development annotations that
made exact locator recall disagree with claim support.

The raw `cases.jsonl` remains local-only and is ignored by Git. The holdout cases are copied
without semantic edits; only their `dataset_version` marker changes to `v1`.

## Distribution and Privacy

This dataset is covered by the corpus manifest's `internal_team_only` distribution scope. Share it
only with collaborators who are authorized to access the source corpus. Do not upload `cases.jsonl`,
source files, quotes, derived chunks, embeddings, or model responses to a public repository, issue,
or external model/API. The `private_local` and `restricted` corpus sources remain local and must not
be sent to an external Provider.

The tracked README and schema are not a complete dataset handoff. A collaborator needs the following
items through an approved private channel:

1. This README and `schema.json`.
2. The local `cases.jsonl`, placed at
   `cases/evals/datasets/knowledge-qa-v1/cases.jsonl`.
3. The manifest-declared corpus files, or an approved local corpus installation with the same
   source hashes.
4. The versioned v1 retrieval configuration at
   `cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml`.

The Git checkout intentionally omits `cases.jsonl`; do not remove the ignore rule to make a public
commit. A handoff is valid only after the recipient has independently run the validation command
below and obtained zero validation issues.

## Corpus Identity

v1 uses corpus `v0`, not a new corpus release. The corpus is frozen, contains 90 manifest sources
across 12 spaces, and has this manifest SHA-256:

`53d6f863060dd7d5e6affb0abda64348f0b498995b3d1cda5ef80f0e576ca738`

The authoritative corpus manifest is `cases/evals/corpus/v0/manifest.yaml`. Source files must pass
its `content_sha256` checks before they are used. The corpus's `internal_team_only` boundary and the
manifest's `allowed_uses` take precedence over this README.

## Version Record

This version is `provisional` and has not been used for a formal holdout run.

| Input | Count / SHA-256 |
| --- | --- |
| Cases | 276 / `a16a953cff715b4de95ade05d5b8aa173ddbd4fdc28c380d54eb7dc2014270c8` |
| Schema | `daddbcb380db21fa3f05b5daedd88ccfe5278e52727aaae094e60b57bf3b9588` |
| Development split | 127 / `86411f3a6b4dbe4c500eb32182d46829a727b4f0436da207666554f90eb57ba8` |
| Holdout split | 149 / `bcf559fcaf4c67d0fd958afae22c461a770c8c1176e85e6c233055dafe461019` |

The v1 JSONL and schema hashes are also pinned in
`cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml`; do not edit either file without creating a
new dataset version and updating the configuration hashes.

## Claim-Level Evidence Protocol

- `answer_claims` are the required answer units. Revised development cases split compound claims
  when one assertion can be evaluated independently from another.
- `evidence[].supports_claims` binds each source excerpt to the claims it can independently support.
- Every evidence record has a stable case-local ID. A claim's `acceptable_evidence_sets` uses an
  outer OR and an inner AND: `[["e1"], ["e2"]]` accepts either alternative, while
  `[["e1", "e2"]]` requires both passages for a synthesis claim.
- A compound claim is split when its assertions can be checked independently. The AND form is
  reserved for genuinely cross-source conclusions that cannot be reduced without losing meaning.
- Exact evidence-unit recall remains useful as a locator diagnostic, but it must not be interpreted
  as claim coverage when alternatives exist.

The current `scripts/evaluate_retrieval.py` runner still scores every evidence record as an
independent required unit. It does not yet consume `acceptable_evidence_sets`. Therefore its v1
`evidence_recall` and `full_evidence_coverage` values are strict locator diagnostics, not the
official claim-level score. Use the OR/AND protocol above for analysis only until a claim-aware
evaluator is versioned.

## v1 Development Repairs

- Narrowed oversized, overlapping text locators in `qa-177`, `qa-178`, `qa-179`, `qa-181`,
  `qa-189`, `qa-190`, and `qa-197` to the passages that support each claim.
- Added verified alternative evidence for `qa-177`, `qa-178`, `qa-179`, `qa-197`, `qa-201`,
  `qa-208`, `qa-212`, `qa-213`, `qa-214`, `qa-222`, and `qa-224`.
- Kept `qa-241` unchanged as a confirmed same-source retrieval miss. Its `/proc`,
  `NewParentProcess`, and `setUpMount` excerpts all support distinct answer claims.

These repairs were based on the frozen source bytes and the established Python 3.12 +
`PyMuPDF==1.28.0` extraction protocol. Retrieved chunks were treated only as audit candidates;
they were added after source-level claim review, not automatically promoted to gold evidence.

## Evaluation Status

This is a provisional development dataset. It has not been used for a formal holdout run, and the
v1 retrieval configuration keeps `formal_runs_enabled: false`. Do not tune against or execute the
holdout split as part of ordinary development. The current v1 configuration is intended for
development validation and comparison only.

The latest local Qwen3/BGE development comparison, using strict locator scoring, reported Dense
exact at 48.95% Recall@5. An offline claim-aware diagnostic reported 56.40% claim coverage for the
same run. These are provisional engineering results, not a formal Stage 3 acceptance result.

## Validation

Run from the repository root with Python 3.12, `uv`, and the frozen environment:

```powershell
uv sync --frozen
uv run --frozen python cases/scripts/validate.py `
  --dataset-dir cases/evals/datasets/knowledge-qa-v1 `
  --max-issues 100
```

The expected result is `[OK] all validation checks passed`. This verifies the JSONL/schema, source
hashes, source versions, locators, quotes, excerpt hashes, split membership, claim/evidence links,
and v1 acceptable evidence-set references.

To validate the retrieval configuration without executing retrieval:

```powershell
uv run --frozen python scripts/evaluate_retrieval.py `
  --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml `
  --split development --validate-only
```

Actual retrieval execution requires `EVALUATION_DATABASE_ISOLATED=1`, an isolated PostgreSQL
database containing the manifest corpus, and the approved local model configuration. Use the
environment and isolation procedure in `docs/stage-3-acceptance.md`; never point evaluation at a
shared or production database. The command must use `--split development` while this config is
provisional.
