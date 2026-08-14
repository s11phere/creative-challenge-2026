# Architecture Decision Records

`docs/adr/` is the authoritative location for architecture decisions in this repository.

ADR-001 through ADR-004 were established during stage 0 under `../cases/docs/adr/` and migrated
here without changing their accepted decisions. The original files are historical sources and are
not maintained as a second authoritative copy.

ADR numbers are never reused. ADR-005 records the stage 2 ingestion identity, version, publication,
task, and deletion semantics. ADR-006 records the stage 5 Skill manifest, versioning, trust,
permission, recovery, and event semantics. ADR-007 records grounded QA persistence, citation,
execution, and SSE semantics. ADR-008 remains reserved for the service-split or multi-Agent decision
listed in `docs/project-implementation-plan.md`; the stage 1 queue decision therefore uses ADR-009.
ADR-010 records the explicit Stage 3 termination and preserves the boundary between engineering
completion and formal retrieval quality acceptance.
ADR-011 records the explicit provisional continuation gate that permits Stage 4/5 engineering to
 continue without treating the non-passing retrieval result as formal quality acceptance.
ADR-012 records the feedback review lifecycle, Space-scoped metadata-only review API, and
privacy-safe candidate export rules.
ADR-013 records the product-level Assistant Agent, shared ConversationRun identity, safe automatic
Skill routing, manifest v2 invocation metadata, context compression, and the versioned v2 event
projection. It explicitly preserves the ADR-010/011 provisional-quality boundary.
ADR-014 records the generic Agent Loop, Tool trust, provider-neutral reasoning profile, v3 event
envelope, finalization-only publication gate, and synthetic development contract boundary.
ADR-015 records the autonomous top-level Assistant Loop, model-directed serial Skill/Tool
orchestration, advisory knowledge next-step guidance, and preservation of the existing Worker,
QA Application Port, checkpoint, cancellation, and single-publication contracts.
ADR-016 records conversation-scoped local workspaces, their local-provider visibility boundary,
relative-path Tools, and the durable approval flow for filesystem writes and command execution.
ADR-017 simplifies the current Skill contracts to the single read-only `/api/v1/skills` view.
ADR-018 records the personal Skill storage root, trust boundary, CRUD API, and activation reuse.
ADR-019 records the Skill Creator draft lifecycle: `_drafts` storage, deterministic eval-as-gate,
creator Tools with durable approval, and frequency-thresholded usage suggestions.
ADR-020 records the feature-gated Agent Harness v2: progressive Skill instruction loading,
provider-neutral native Tool-use, service-owned knowledge gates, bounded decision history, and
body-free synthetic development baselines. It preserves v1 Run recovery and the ADR-010/011
provisional-quality boundary.
ADR-021 records durable, live Skill activation: default-enabled packages, immediate registry
updates, and the fixed/personal catalog toggle controls.
