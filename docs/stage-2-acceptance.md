# Stage 2 Acceptance

Acceptance date: 2026-07-28  
Scope: internal team distribution only

## Decision

Stage 2 is formally complete. Steps 0-8 provide the ingestion implementation and Step 9 now has
formal evidence against the Stage 0 frozen manifest. The accepted boundary covers Markdown, TXT,
and copyable-text PDF ingestion, stable 1-based locators, idempotent publication, failure recovery,
Space isolation, withdrawal/deletion, the ingestion API, and the Web data-source workflow.

This decision does not grant public redistribution rights, does not authorize external-provider use,
and does not complete the Stage 3 retrieval quality or holdout gates.

## Frozen Inputs

| Input | Version / hash |
| --- | --- |
| Corpus manifest | `v0` / `53d6f863060dd7d5e6affb0abda64348f0b498995b3d1cda5ef80f0e576ca738` |
| Manifest status | `frozen` |
| Distribution scope | `internal_team_only` |
| Required use | `local_evaluation` |
| Parser protocol | `1.0` / Python 3.12.13 / `PyMuPDF==1.28.0` |
| Normalizer / chunker | `1.0` / `1.0` |

Every evaluated source was selected from the manifest allowlist and its raw bytes were checked
against `content_sha256` before parsing. No source text, path, source key, chunk, embedding, prompt,
or Provider response is present in the aggregate report or this record.

## Parsing Gate

The formal command was:

```text
uv run --frozen python scripts/evaluate_ingestion.py --output tmp/stage-2-ingestion-evaluation.json
```

| Format | Attempted | Succeeded | Failed | Success rate |
| --- | ---: | ---: | ---: | ---: |
| Markdown | 35 | 35 | 0 | 100% |
| TXT | 9 | 9 | 0 | 100% |
| Copyable-text PDF | 30 | 30 | 0 | 100% |
| **Total** | **74** | **74** | **0** | **100%** |

The frozen threshold was at least 95%. All successful documents produced structural nodes, valid
1-based line locators, valid 1-based PDF page locators where applicable, and a non-empty chunk set.
Code and Notebook sources are outside the Stage 2 P0 format boundary and were not silently counted
as parser failures or successes.

## Lifecycle Evidence

- The isolated PostgreSQL lifecycle test covers ingest, publish, old-version visibility before the
  atomic switch, content modification, new-version publication, immediate withdrawal, asynchronous
  cleanup, and the published-candidate boundary.
- Source-registration and database tests cover serial/concurrent identities, scoped unique
  constraints, Unicode stable keys, exact-byte repeats, changed bytes, and cross-Space isolation.
- Orchestrator and Worker tests cover retry resume, parse/chunk/provider failures, cancellation at
  checkpoints, retry exhaustion, dead-letter handling, lease reconciliation, cleanup recovery, and
  preservation of the previously published version on failure.
- A full Compose E2E used the real API and Worker: Markdown upload reached `succeeded`; repeating the
  same upload returned `is_unchanged=true` with no new task. API, dependency readiness, and the Web
  same-origin health proxy all passed.
- Compose cold start succeeded. After `down` without volume deletion, the complete stack restarted
  healthy and the previously created source remained present.

## Verification

```text
uv run --frozen python cases/scripts/validate.py --max-issues 100
  spaces: 12; sources: 90; cases: 276; [OK]

uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy apps packages scripts/evaluate_ingestion.py
  all passed

uv run --frozen pytest
  488 passed, 41 skipped

RUN_INTEGRATION=1 uv run --frozen pytest tests/integration -q
  40 passed

corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
  all passed; 12 frontend tests passed

alembic downgrade base; alembic upgrade head; alembic current
  round trip passed; d4e5f6a7b8c9 (head)

python scripts/export_openapi.py; git diff --exit-code -- docs/openapi.json
  passed
```

The Windows sandbox still cannot create `.pytest_cache`; pytest reports the known cache warning, but
test execution is unaffected. The Stage 1 diagnostic actor log-line gap remains accepted under
ADR-005: PostgreSQL task state, Redis queue inspection, trace IDs, and dead-letter evidence are the
required completion and recovery signals.

## Exit Conditions

All 13 exit conditions in `docs/stage-2-implementation-plan.md` are satisfied. R2-01, R2-02, and
R2-03 are closed; migration compatibility with Stage 2 Step 1 data was recorded during Step 1 and
the current isolated database also passed a clean downgrade/upgrade round trip. No new schema or ADR
change was required for Step 9.

The next permitted work is the remaining Stage 3 formal sequence: real-model development ablation,
one default model/profile/index configuration freeze, and one confirmed holdout run. Stage 3 remains
incomplete until those independent gates pass.
