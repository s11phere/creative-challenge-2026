# ADR-015: Autonomous Assistant Loop And Skill/Tool Orchestration

- Status: Accepted
- Date: 2026-08-10
- Scope: Provisional engineering contracts; formal retrieval, answer, Skill, and browser quality
  gates remain governed by ADR-010 and ADR-011.

## Context

The previous Assistant path separated Skill selection from Skill execution. A router produced one
Skill projection, and the projected Worker then followed that Skill's fixed workflow. This caused
three observable failures: a knowledge request could be answered before its loop completed,
unrelated requests could be routed into knowledge retrieval, and the knowledge Agent commonly
stopped after one search. It also made a complex turn unable to compose independent Skills in one
recoverable execution.

## Decision

1. A normal Assistant turn is executed by one top-level `AgentLoopExecutor`. The model receives the
   base Assistant prompt, safe active Skill context, and the server-registered Tool definitions at
   the same decision boundary. After every Tool result it receives a bounded, model-visible
   observation and decides whether to call another Tool, clarify, refuse, or complete.
2. Skill selection is therefore an ordinary Loop decision. The server still pins every installed
   Skill and Tool by name, version, and digest, and constructs the allowlist and permissions. Model
   output cannot add a Skill, Tool, Space, resource, permission, budget, or approval.
3. Knowledge Tools provide local guidance such as `recommended_next`; this is advisory input for
   the model, not a product-wide state machine. Server-side checks remain authoritative for QA
   ownership, citations, verification, finalization, publication, cancellation, approval, Space
   scope, and terminal state. An early direct response cannot bypass a required grounded-QA
   publication when the knowledge path has been entered.
4. The top-level Loop keeps the existing Worker queue, `ConversationRun`, Runtime checkpoint,
   `AgentRunEventStore`, Assistant SSE projection, cancellation and lease handling. Checkpoints
   persist a bounded observation payload so a resumed process can continue model reasoning without
   replaying completed Tool side effects. Existing `agent-loop-v1` checkpoints remain readable.
5. Grounded QA continues to use the single QA Application Port. A knowledge Tool creates or resumes
   the QA projection in the current parent Run; it does not enqueue a parallel QA workflow or
   publish a second Assistant message. Its verified claims are passed to the existing
   `ConversationFinalizer`, which synthesizes the single coherent user-facing answer while the QA
   projection remains the authority for citations and refusal status. Legacy v1/v3/v4/v5/v6 Skill
   Runs and explicit commands keep their existing projection and are dispatched by their persisted
   identity.
6. The Web workspace subscribes to the existing v3 Agent event history for `assistant_turn` Runs,
   including Runs with no legacy `selection.skill`. It renders the timeline immediately while a Run
   is active and then appends events from the same stream. `knowledge_search` may show its bounded
   `query_preview` for caller debugging; it does not expose arbitrary Tool arguments. Evidence is
   offered only after the QA projection has returned both a result and citations.
7. `QA_DEBUG_TRACE_ENABLED` remains disabled by default and development-local. When enabled, the
   autonomous path writes to the existing QA debug trace through the top-level decision gateway,
   Grounded QA generation gateway, Tool registry, and final answer writer; no durable SSE event is
   broadened for this purpose.

## Alternatives

- Keep a global retrieval state machine: rejected because it forces an order the model may not need
  and prevents composition of unrelated read-only Skills.
- Let prompts alone authorize Tools or publication: rejected because prompts are untrusted model
  inputs and cannot enforce permissions, Space boundaries, idempotency, or terminal publication.
- Create one independent Worker Run per Skill call: rejected because it breaks one-turn recovery,
  cancellation, usage accounting, and single-publication semantics.

## Consequences

The loop can make multiple serial Tool/Skill calls and can answer ordinary conversation without
activating knowledge retrieval. Tool schemas and local next-step guidance improve model behavior,
while server checks remain deliberately narrow. The current production adapter exposes the v7
knowledge Tools plus the read-only `summarize_document` adapter when the server resource resolver
is available; additional Skill adapters can be registered without changing the outer Loop contract.
This rollout is provisional and must not be described as a formal quality acceptance.

## Reassessment Triggers

Reassess if a new Skill needs a side effect, approval type, or publication contract that cannot be
represented by the existing Tool Registry and finalizer, or if checkpointed model-visible outputs
cannot be bounded without reducing recovery correctness.
