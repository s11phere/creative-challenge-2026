# ADR-022: Native Student Skill Suite v2

- Status: Accepted
- Date: 2026-08-18
- Scope: Provisional engineering contracts; formal quality gates remain open.

## Context

The built-in summary, comparison, and review-card Skills duplicated capabilities already available
through knowledge and exam workflows. Research and course-project packages used projected execution,
so they could appear in the route catalog without a native adapter and fell back to the legacy Skill
card. Exam preparation had a substantially larger prompt and output contract than adjacent Skills.

## Decision

- Remove `summarize_document`, `compare_sources`, and `create_review_cards` from the installed catalog,
  commands, and new-Run APIs. Existing records and derived knowledge remain readable but cannot start
  or resume through those removed identities.
- Keep `knowledge_agent 1.0.0` unchanged. Ordinary summary and comparison requests use its existing
  grounded knowledge path; exam review cards remain approval-gated Exam artifacts.
- Publish `research_reading_workflow`, `exam_preparation_workflow`, and
  `course_project_workflow` as current version `2.0.0`. All use native Tool execution with the same
  16-step, 12-call, 65,536-input-token, 16,384-output-token, 600-second budget envelope.
- Research locks one or two-to-eight current-Space published source versions in a persisted Tool
  observation before knowledge retrieval. Topic-only discovery requires user confirmation.
- Exam defaults to diagnosis, targeted review, and retesting. Optional materials do not impose a
  fixed stage sequence, and empty submissions cannot produce a score.
- Course Project advances one stage at a time. Only explicitly confirmed facts enter checkpoint Tool
  observations; model suggestions never become completion evidence.
- An active `native_tool_use` Skill without a matching Runtime adapter is a startup/catalog error.
  All new v2 workflow Runs use the existing Agent event timeline, checkpoint, cancellation, recovery,
  approval, retrieval, QA verification, and publication contracts.

## Consequences

- The deterministic fake gateway is part of the executable acceptance surface for all three v2
  Skills. Real-provider results remain optional and provisional.

The public command and OpenAPI surfaces are intentionally incompatible: `/summarize`, `/compare`,
`/cards`, and their dedicated Run endpoints are removed. The three upgraded Skill output schemas are
small terminal envelopes; business structures stay in versioned Tool and persistence contracts.
Historical projected Runs remain data, not an execution compatibility path.

All evaluation remains synthetic/development-only and provisional. This ADR does not reopen the
formal holdout or change private/restricted content policy.

## Reassessment

Reassess if a workflow needs more than four model-visible Tools, cannot fit the common budget without
measured quality loss, or requires a new durable lifecycle that cannot use existing Run checkpoints.
