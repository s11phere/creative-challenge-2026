# ADR-014: Generic Agent Loop, Tool Trust, Reasoning Profiles, And Event Contracts

- Status: Accepted
- Date: 2026-08-09
- Scope: Provisional engineering contracts; this decision does not satisfy the formal retrieval,
  answer, or Skill quality gates in ADR-010 and ADR-011.

## Context

The product-level Assistant currently routes to fixed Skill workflows. The next implementation
steps need a recoverable loop that can plan, invoke an allowlisted Tool, observe a summary, and
decide whether to continue or finalize. The existing Tool Registry, ConversationRun, checkpoint,
finalizer, Worker, and `agent-run-sse-v2` contracts must remain compatible while this work is
introduced incrementally.

## Decision

1. Freeze the provider-neutral contracts in `agent_runtime/contracts/`:
   `agent-loop-v1`, `tool-invocation-v1`, `reasoning-profile-v1`, `agent-run-sse-v3`, and
   `assistant-final-answer-v2`. Their manifest pins SHA-256 hashes; incompatible changes require a
   new schema version.
2. Treat model output as untrusted intent. The server resolves Tool definitions, Space and Skill
   scope, permissions, approvals, leases, idempotency, cancellation, and checkpoint ownership.
   Schemas do not authorize a write, command, provider, resource UUID, or publication.
3. Use `finalize` as the only action that can enter finalization. Once finalization starts, no Tool
   may run; the existing finalizer remains the only publisher of a user-facing Assistant message.
   Other terminal outcomes are clarification, refusal, failure, cancellation, and timeout.
4. The v3 SSE envelope carries only stable event names, sequence, status, timing, counts, and
   digested summaries. Prompts, answer bodies, document excerpts, credentials, and complete command
   output are forbidden. v1/v2 projections remain readable during migration.
5. Reasoning effort is a provider-neutral persisted profile. `auto` may be downgraded by a
   capability mapping; explicit requests are fail-closed when unsupported. Requested and effective
   values plus the mapping version and downgrade reason are auditable. The exact
   `openai-compatible/deepseek-v4-flash` capability uses `reasoning-mapping-v2` and sends the
   native DeepSeek Chat Completion `reasoning_effort` field; documented provider reductions such as
   `xhigh -> high` are reflected in `effective_effort`. Generic OpenAI-compatible capabilities
   remain `coarse`. Provider `reasoning_content` is transient request-chain data and must not enter
   Conversation persistence, Run state, SSE, logs, traces, fixtures, or Web responses.
6. The `agent-loop-v1` development fixture is synthetic and provisional. It is contract evidence,
   not a formal holdout, and `formal_runs_enabled` is false. Current retrieval/answer quality
   claims remain unchanged.
7. Read-only filesystem Tools use configured, non-link trusted roots plus a per-Space manifest
   allowlist. They reject parent traversal, device paths, links/junctions, hash drift, oversized
   files, malformed UTF-8, and cancellation. Their payloads are explicitly untrusted data and
   Registry audit records retain digests only. Model-visible access defaults to `public_demo`;
   private or restricted content requires an explicit deployment-policy decision.
8. Side-effect Tools are opt-in and require a durable approval for every invocation identity.
   `WRITE_KNOWLEDGE` covers allowlisted atomic file writes and `EXECUTE_PROCESS` is a separate
   process-launch permission. Approvals bind Run, Space, caller, Tool name/version, idempotency
   key, and digested input. `fs_write` denies protected configuration, credential, system-prompt,
   and Skill-package targets even when accidentally allowlisted. `shell_exec` uses an executable
   alias, fixed Space-scoped cwd, policy-owned minimal environment, argv-only execution, bounded
   output, timeout/cancellation cleanup, and no `EXTERNAL_NETWORK` grant.
9. Knowledge Loop Tools are application-layer adapters, not retrieval implementations. The default
   local/provisional `knowledge_agent 0.5.0` package exposes `knowledge_search`, `knowledge_inspect`,
   `grounded_answer`, `verify_answer`, and `finalize_answer`. Search calls must use the
   `SearchService.search(SearchRequest, RetrievalProfileV1)` port with the server-fixed Space and
   retrieval scope. Only metadata and safe identifiers cross the Tool boundary. Grounded QA remains
   the sole owner of context construction, claims, citations, refusal/conflict semantics, and the
   user-visible Assistant publication; the Loop finalizer may return a routing projection but may
   not publish a second message. `AGENT_LOOP_V5_ENABLED=false` remains a fail-closed rollback to
   `0.3.0` and does not alter existing Run pins.

## Alternatives

- Reuse the fixed Skill workflow as the loop contract. Rejected because it cannot express repeated
  Tool observations or a finalization-only gate.
- Put provider-specific reasoning fields in Domain. Rejected because it couples business contracts
  to one Provider and makes capability fallback unauditable.
- Emit full Tool inputs and outputs over SSE. Rejected because prompts, private content, secrets,
  and command output can cross the observability boundary.

## Consequences

The next steps can add the loop state machine and read-only Tools against stable contracts. Existing
v1/v2 API and SSE clients remain on their projections until an explicit feature flag enables v3.
The synthetic fixture provides repeatable security and terminal-state cases without introducing
controlled corpus content or formal evaluation results. Side-effect Tools are not part of the
default Skill or Web command surface. The in-memory Registry serializes duplicate side-effect
invocations for one Run/key; a future durable invocation-result store is required before claiming
exactly-once semantics across process restart.

## Reassessment Triggers

Reassess if a Tool needs a trust or approval model not representable by the Registry, if v3 cannot
preserve exactly-once terminal publication across Worker recovery, or if a Provider requires a
reasoning capability that cannot be represented without leaking provider-specific fields into Domain.
