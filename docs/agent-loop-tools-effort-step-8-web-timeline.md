# Agent Loop Step 8: Web Agent Run Timeline

## Implemented Scope

- Skill invocation cards now prefer durable `agent-run-sse-v3` history when it is present. The
  client follows the paginated history endpoint on initial and refresh recovery, then reconnects an
  active Run's stream with its latest numeric `Last-Event-ID`.
- A timeline groups the safe history by iteration and Tool. Read, retrieval, write, and command
  Tools use distinct icons and visual states. Each Tool card is a closed native `details` element by
  default; only its server-supplied digest summaries, duration, retry count, and stable error code
  become visible after it is expanded.
- Approval-required Tools show awaiting approval, execution after approval, and a rejected
  no-side-effect result when the durable Tool output carries an approval-rejection error. The timeline
  also projects requested/effective effort, actual model and token usage, model latency, and the
  terminal stop reason. It intentionally never shows budgets, remaining token/tool limits, event IDs,
  prompts, assistant answer text, document content, secrets, or raw Tool/shell output.
- The existing cancellation action remains attached to the active Run. Clarification and evidence
  actions keep using their existing Run/QA ports. The finalizer-owned Assistant answer remains one
  separate frame after the timeline; it is not copied into history or a Tool card.
- Runs without v3 history keep the v2 Skill invocation card, preserving historical recovery and the
  explicit v1 compatibility path. The timeline metadata and Tool detail grids collapse to one column
  on narrow viewports.

## Quality Boundary

This is provisional Web observability engineering. It does not change the Agent Loop's server-side
authority, default `knowledge_agent 0.3.0` routing, or ADR-010/ADR-011 quality boundary. No formal
retrieval, answer, or Skill holdout was run.
