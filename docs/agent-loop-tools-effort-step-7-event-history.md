# Agent Loop Step 7: SSE v3 And Durable Invocation History

## Implemented Scope

- `agent-run-sse-v3` is now represented by a dedicated Domain contract and append-only
  `agent_run_events` history. Event payloads are flat and fail closed to a fixed allowlist of safe
  counters, digests, Tool identity, status, stable error codes, reasoning metadata, checkpoint
  metadata, stop reason, and publication identity.
- The generic `AgentLoopExecutor` records `accepted`, `iteration_started`, `tool_requested`,
  `tool_started`, `tool_output`, `approval_required`, `checkpoint_saved`, `finalizing`, and one
  terminal event. Tool inputs and outputs are SHA-256 summaries only.
- Each Tool request is checkpointed before invocation. The finalizing state is checkpointed with a
  stable `assistant-publication:<run-id>` identity and terminal action, so recovery can continue
  finalization without asking the model for a second plan.
- `KnowledgeLoopTools` rehydrates the fixed QA-owned terminal result before retrying a checkpointed
  finalizer. It still does not publish a parallel Assistant message; the existing QA/finalizer path
  remains the single publisher.
- `GET /api/v3/runs/{run_id}/events` supports `after_sequence` and `limit`. The matching
  `/stream` endpoint reconnects with numeric `Last-Event-ID`. A future or malformed stored event
  version is returned as `AGENT_EVENT_VERSION_UNSUPPORTED` or `AGENT_EVENT_HISTORY_INVALID`, never
  interpreted as v3.
- v1 QA SSE and v2 Assistant SSE retain their existing tables, endpoints, and event semantics.

## Persistence And Migration

`1b2c3d4e5f6a_add_agent_run_event_history` creates `agent_run_events`, keyed by the shared
`ConversationRun`. It enforces monotonic per-Run sequence numbers plus a durable event key. The
store returns the same event for an identical retried key, rejects a conflicting key, and rejects
any event after the first terminal outcome. Downgrade refuses to discard non-empty v3 history.

## Quality Boundary

This is provisional observability and recovery engineering only. The v3 Web timeline is documented
separately in Step 8; neither step alters the default `knowledge_agent 0.3.0` path, runs a formal
holdout, or closes any retrieval, answer, or Skill quality gate under ADR-010 and ADR-011.
