# Agent Loop Tools Effort: Step 0 Baseline

> Baseline date: 2026-08-09
> Status: provisional engineering evidence only

This document records the compatibility baseline before the generic Agent Loop implementation.
It is intentionally descriptive; it does not claim formal retrieval, answer, or Skill quality
acceptance and does not enable the current formal holdout.

## Existing contracts

| Surface | Current contract | Compatibility boundary |
| --- | --- | --- |
| Assistant HTTP | `/api/v2` conversation and Run projections | Keep v1 compatibility routes and persisted Run identity readable. |
| Assistant SSE | `agent-run-sse-v2` (`accepted`, `routing`, `clarification`, `skill_started`, `phase`, `completed`, `failed`, `cancelled`) | Add v3 as a projection; unknown v3 events must not be silently interpreted by v2 clients. |
| Grounded QA SSE | `qa-sse-v1` | Preserve the existing QA projection and Application Port. |
| Tool registry | `ToolDefinition` and `InMemoryToolRegistry` in `agent_runtime.tools` | Server remains authoritative for permissions, Space, approval, retry, timeout, and idempotency. |
| Run recovery | `ConversationRunRepository`, runtime checkpoints, Worker lease recovery | Recovery is keyed by the persisted parent `run_id`; no replacement Run is created. |
| Finalizer | `ConversationFinalizer` publishes one parent Assistant message after the Skill result | Finalization remains the only user-facing publication path. |
| Web | Assistant conversation workspace with Skill invocation cards and final-answer frame | v3 timeline is additive; v1/v2 rendering and explicit rollback remain available. |

## Step 0 artifacts

The frozen provisional contracts are in
`packages/agent_runtime/src/agent_runtime/contracts/` and are hash-pinned by `manifest.json`:

- `agent-loop-v1.schema.json`
- `tool-invocation-v1.schema.json`
- `reasoning-profile-v1.schema.json`
- `agent-run-sse-v3.schema.json`
- `assistant-final-answer-v2.schema.json`

`cases/evals/datasets/agent-loop-v1/` is a synthetic development fixture. It covers ordinary chat,
multi-round retrieval, insufficient/conflicting evidence, prompt injection, cross-Space access,
write approval, and command overreach. `formal_runs_enabled: false` is a hard boundary.

## Privacy and rollback review

- Contract payloads forbid prompts, answer bodies, document excerpts, credentials, and complete
  command output; Tool and event tests exercise representative negative cases.
- Model output is untrusted intent and cannot select a Space, resource identity, Provider, approval,
  or publication identity.
- Existing v1/v2 projections remain the default rollback path. Enabling v3 or non-`auto` reasoning
  requires a later feature flag and preserves historical Run and Skill identities.
- No corpus source, private content, Provider response, embedding, or formal holdout was added or
  executed for Step 0.

## Verification

Step 0 was checked with:

```text
uv run pytest -q tests/unit/test_agent_loop_contracts.py
uv run pytest -q tests/contract tests/unit/test_assistant_contracts.py
uv run ruff check tests/unit/test_agent_loop_contracts.py
uv run ruff format --check tests/unit/test_agent_loop_contracts.py
git diff --check
```

The first command passed 5 tests and the compatibility contract selection passed 36 tests. The
local pytest cache emitted a permissions warning in this managed environment; it does not affect
the test result.
