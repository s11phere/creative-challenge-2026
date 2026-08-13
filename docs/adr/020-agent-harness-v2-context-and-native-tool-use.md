# ADR-020: Agent Harness v2 Context, Progressive Skill Loading, And Native Tool Use

- Status: Accepted
- Date: 2026-08-12
- Scope: Provisional engineering contracts only. This decision does not reopen or satisfy the
  formal quality gates recorded by ADR-010 and ADR-011.

## Context

The current autonomous Assistant loop serializes every active Skill instruction and all registered
Tool schemas into every model decision prompt. It additionally uses a text JSON decision shape in
which a Tool request and a potentially long user-facing answer share one 512-token response budget.
When that response is truncated, the runtime can issue a separate long-answer generation that does
not reuse the prior decision as an authoritative artifact.

The existing knowledge Tools expose `grounded_answer`, `verify_answer`, and `finalize_answer` to
the model. `KnowledgeLoopDecisionPolicy` then rewrites choices that do not meet the deterministic
Grounded QA sequence. This overlaps the model decision domain with service-owned publication and
verification policy. Current checkpoints preserve Tool observations, but the model has no bounded,
explicit decision-history projection that distinguishes audit digests from useful next-turn state.

## Decision

1. New Assistant Runs may opt into a versioned Harness v2 only through a disabled-by-default feature
   flag and a provider capability check. Existing v1 Runs retain their pinned prompt, text-JSON
   executor, checkpoint, and recovery path. No existing checkpoint is converted in place.
2. Harness v2 normalizes provider-native Tool use through a provider-neutral contract. A model turn
   either requests exactly one Tool with a stable call ID or returns non-empty terminal text. A Tool
   request always causes one server-validated Tool invocation and another model turn. If a provider
   returns multiple calls, the executor executes none of them and allows one bounded corrective
   model turn; a repeated violation fails with a stable schema error. A no-Tool response becomes the
   terminal direct response. Provider text accompanying a Tool call is not a publication candidate.
3. Harness v2 does not use `complete.final_response`, textual `call_tool` JSON, or the v1
   long-answer escalation. Direct text and Tool selection have separate protocol paths and
   publication remains exactly once through a server-owned finalizer.
4. The initial model context contains only a thin, trusted Skill routing catalog. `invoke_skill` is
   a bootstrap Tool; the server validates the active entry and pins name, version, and content hash.
   Only after a successful invocation can the selected Skill's complete instructions and Tool
   allowlist enter a later context. `list_skills` returns safe catalog metadata. Skills without a
   Runtime adapter are not callable by `invoke_skill`.
5. Tool Registry checks remain authoritative for Tool identity, permissions, Space, resource scope,
   approval, idempotency, cancellation, budgets, and model visibility. Model output cannot select
   a provider, grant permissions, expand a Space, select internal resource IDs, or publish directly.
6. Knowledge retrieval and Grounded QA stay on the existing `SearchService.search(...)` and
   Grounded QA Application Port. Harness v2 will expose retrieval and answer capabilities, while
   search coverage, claim/citation verification, conflict handling, and final publication are
   deterministic server orchestration steps. The v1 three-gate model surface remains only for v1
   recovery until its pinned Runs finish.
7. A versioned `agent-model-context-v2` keeps bounded selected Skill pins, decision-history items,
   Tool-specific model-observation projections, and a structured progress summary. Audit summaries
   and hashes are not automatically model-visible. Prompts, answers, document bodies, raw Tool
   output, credentials, and model reasoning are prohibited from this history.
8. Prompt caching may be used only as a provider capability optimization. Cache keys are derived
   from static prompt and schema digests plus provider/model identity. User content, Tool
   observations, and private content are excluded from shared keys and normal observability.
9. Step 1 freezes the provisional v2 contracts and a body-free synthetic development fixture. It
   adds development-local trace measurements for prompt/context byte estimates and repeated
   generation counts. These diagnostics remain outside PostgreSQL, SSE, normal logs, API responses,
   fixtures containing real content, and formal quality reporting.
10. Worker startup recovery may repair the narrowly identified pre-fix approval orphan: a parent
    Assistant Run in `waiting_approval` whose latest verified native v2 checkpoint contains a
    pending Tool call but no `approval_id`. Recovery only requeues that Run; the current executor
    creates a normal durable approval and remains stopped until the user decides it. Existing
    approvals, non-native checkpoints, cancellation requests, and invalid checkpoints are not
    requeued by this compatibility path.
11. A pending workspace write using the server-owned QA marker may resume on a new Worker only
    after the coordinator rehydrates the current Run's persisted QA record and repeats its existing
    publishability verification. The marker resolves to the verified server-owned answer only in
    memory for the normal Tool Registry invocation; it does not alter the checkpoint, bypass
    approval, constrain the selected path, or execute a side effect during recovery.

## Alternatives

- Keep every Skill prompt in the base prompt and rely on the model to ignore irrelevant text:
  rejected because context cost and instruction collisions grow with active Skills.
- Continue textual JSON Tool decisions: rejected because schemas duplicate Tool descriptions in the
  prompt and final answers compete with decision output capacity.
- Leave QA gates model-visible and correct them after the decision: rejected because it consumes
  avoidable model rounds and obscures which layer owns the workflow.
- Put raw Tool results or chain-of-thought into a general conversation summary: rejected because it
  broadens privacy exposure and makes recovery behavior dependent on unbounded content.
- Require prompt caching for Harness v2 correctness: rejected because provider availability and
  caching semantics must not weaken local-first or fail-closed behavior.

## Consequences

The v2 boundary adds new immutable schemas, synthetic fixtures, metrics, prompt identities, and
feature-gated execution paths. It does not add a second Worker, QA workflow, publication mechanism,
or retrieval implementation. Historical v1 API and event projections remain readable. Any public
API or persistence change introduced by later v2 steps must be explicitly versioned and, where
needed, migrated through Alembic.

All evaluation artifacts remain synthetic, development-only, hash-pinned, and provisional. They do
not read corpus bodies, enable an external provider, or authorize a formal holdout.

## Reassessment Triggers

Reassess this decision if a provider cannot represent one-call native Tool turns without losing
recovery safety, a new Skill requires a selected-context trust model that cannot be pinned and
bounded, a Tool observation cannot be safely summarized, or a persistence/API change invalidates
the v1 recovery guarantee.
