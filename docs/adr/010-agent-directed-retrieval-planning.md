# ADR-010: Agent-Directed Retrieval Planning Within Grounded QA

- Status: Accepted
- Date: 2026-08-01

## Context

The provisional QA profile applied small global caps to evidence per Source, per Document and per
item. Browser uploads group several files under one Source, so the Source cap can discard relevant
cross-document evidence before generation. `knowledge_agent 0.1.0` could call the opaque
`grounded_qa` Tool only once; it could not adapt query wording or context needs to a question.

## Decision

`knowledge_agent 0.2.0` receives two read-only Tools in the existing QA Run/Worker boundary:

- `inspect_retrieval 1.0.0` accepts bounded additional queries and retrieval/context preferences,
  then returns only safe coverage counts. It may run up to three times.
- `grounded_qa 1.0.0` accepts the same bounded plan exactly once and remains the only Tool that
  executes the Grounded QA Application Port and publishes a validated answer/refusal.

The server always adds the original question, validates query uniqueness and restricts dynamic
values to the active trusted profile. It fixes Space, Source, Document and DocumentVersion scope
before every search. Tool output contains counts rather than document text; only the existing
Grounded QA generator receives evidence text under the configured local-first policy. Hard budgets
remain finite for availability and abuse control, but defaults favor document coverage rather than
a low Source cap.

## Consequences

Existing `knowledge_agent 0.1.0` remains installed for pinned Run recovery and rollback. New runs
must explicitly activate `0.2.0`. Retrieval plans and Tool calls are checkpointed by the existing
runtime, while QA evidence/citation persistence remains owned by the single Grounded QA Port.

## Alternatives

Allowing arbitrary unbounded model loops was rejected because it defeats timeout, cost and recovery
semantics. Exposing document text to planning Tool output was rejected because it expands the
external-model data boundary without a separate policy decision. Replacing the QA port with a
parallel Agent-owned answer path was rejected because it would duplicate citation validation and
terminal publication rules.

## Reassessment Triggers

Reassess after a frozen retrieval/answer evaluation shows whether coverage summaries are enough for
query refinement, whether a local-only evidence-reading Tool provides measurable benefit, or whether
the bounded defaults need adjustment for observed context windows and latency.
