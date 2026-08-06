# Agent Conversation Evolution: Step 0 Baseline

- Recorded: 2026-08-06
- Scope: structural and contract baseline only; no formal holdout, corpus evaluation, or external
  Provider call is authorized by this record.
- Quality status: retrieval, grounded QA, and existing Skills remain provisional under ADR-010 and
  ADR-011.

## Observed Baseline

| Surface | Current behavior | Evidence |
| --- | --- | --- |
| Web conversation | `QAWorkspace` exposes five manual modes and defaults to `knowledge_agent`. | `apps/web/src/QAWorkspace.tsx` |
| Create work | `/api/v1/conversations/{conversation_id}/questions` creates a `knowledge_qa` run; `/api/v1/runs` accepts a caller-selected `skill_name`. | `apps/api/src/api/routers/qa.py` |
| Conversation facts | Conversation and Message are persisted; messages are append-only and history is returned with QA runs. | `packages/domain/src/domain/qa_persistence.py`, `apps/api/src/api/routers/qa.py` |
| Grounded work | The single Grounded QA path owns retrieval, terminal answer publication, citations, cancellation, and retry behavior. | `packages/application/src/application/qa/`, ADR-007 |
| Runtime work | Runtime has a fixed Skill identity at creation, with persisted checkpoints, leases, cancellation, and retries. | `packages/domain/src/domain/agent_runtime.py`, ADR-003 |
| Events | `qa-sse-v1` is the persisted-state projection used by the current QA client. | `packages/domain/src/domain/qa_sse.py`, ADR-007 |
| Public API | The checked-in OpenAPI document exposes the v1 surface only. | `docs/openapi.json`, `tests/unit/test_openapi.py` |

This baseline establishes that `ConversationRun`, API v2, `agent-run-sse-v2`, automatic Skill
routing, command parsing, context summaries, and the generic Assistant Agent do not exist before
the staged work begins. The existing QA and Runtime paths are compatibility inputs to be migrated,
not alternate implementations to duplicate.

## Data And Provider Review

`cases/evals/corpus/v0/manifest.yaml` declares `internal_team_only`; it includes both
`public_demo` and `private_local` sources. The demo-data policy and ADR-004 require private-local
or restricted content to remain local unless source policy, deployment policy, and visible user
consent all permit external transfer. Step 0 therefore adds only synthetic repository fixtures in
`cases/evals/datasets/assistant-routing-v1`; it does not load corpus source files, quotations,
controlled JSONL, embeddings, prompts, or Provider responses.

The synthetic routing set is explicitly provisional and has no holdout split. It is a contract and
safety development aid, not a retrieval, answer, or Skill quality report.

## Verification Record

The executed Step 0 verification command validated the frozen contract checks, the synthetic routing
dataset, and the existing API, persistence, SSE, and OpenAPI baselines without invoking a model or
evaluator:

```powershell
.\.venv\Scripts\pytest.exe -p no:cacheprovider tests/unit/test_assistant_contracts.py tests/unit/test_qa_api.py tests/unit/test_qa_persistence.py tests/unit/test_qa_sse.py tests/unit/test_openapi.py
```

Result: 37 passed. `ruff format` and `ruff check` were also run on the new Assistant contract
package and its test.
