# ADR-013: Product-Level Assistant Agent, Shared Conversation Runs, And Versioned Routing

- Status: Accepted
- Date: 2026-08-06
- Scope: Provisional engineering contracts only; this decision does not reopen or satisfy the
  formal quality gates recorded by ADR-010 and ADR-011.

## Context

The current Web and API surface requires a caller to choose a Skill before submitting a question.
That makes ordinary conversation appear to be grounded QA, forces a Skill identity before routing,
and prevents a product-level Agent from deciding whether a Skill is needed. Existing Conversation
and Message records already hold append-only conversation facts, while QA runs, Runtime checkpoints,
approvals, cancellation, retry, and `qa-sse-v1` have their own persisted contracts.

The product needs a direct-conversation default, safe automatic selection of active Skills, explicit
slash commands, and long-conversation context management. It must retain the local-first policy,
fixed Skill identity, Space and version isolation, approval rules, the single Grounded QA Application
Port, and the Worker boundary. It must not create a second queue, use model output as authority, or
turn provisional retrieval/QA evidence into a formal acceptance claim.

## Decision

### Product-level turn and parent identity

`AssistantAgentService` is the sole Application entry point for a new conversation turn. It is not
an installable Skill. A persisted `ConversationRun` becomes the parent identity for every durable
turn and records the conversation, Space, caller, user message, idempotency key, status, `run_kind`,
selection source (`auto`, `command`, or `none`), router/core-prompt/model versions, actual model
usage, and an optional fixed Skill identity.

The existing QA run becomes a grounded-QA projection of that parent identity. Runtime execution,
attempt lease, retry, checkpoint, cancellation, approval, and terminal publication use the same
parent ID. Existing `/api/v1` behavior remains a compatibility mapping during the migration; it does
not acquire a separate run model. A Worker receives only `run_id`, `trace_id`, and an explicit event
version, then dispatches by persisted `run_kind`. Long model work never runs in the request handler.

### Router, results, and commands

The frozen `assistant-router-decision-v1` schema permits exactly `respond`, `clarify`, and
`invoke_skill`. The router can choose only from a server-provided active Skill catalog. Its output is
untrusted intent: Application code validates it, resolves natural-language resources in the current
Space, pins `(name, version, content_sha256)`, validates input, and applies policy before execution.
The schema prevents model-created resource, Space, and version identifiers.

`assistant-base-prompt-v5` is the current top-level Assistant prompt; `assistant-base-prompt-v4` and
`v3` remain available for historical fixed Runs and the compatibility router. The active catalog
exposes `knowledge_agent 0.7.0` for requests that explicitly depend
on current-Space knowledge, while ordinary conversation remains the default. The `knowledge_qa`
packages remain installed only to validate and recover historical fixed Runs; they cannot be selected
by a new Assistant or v1 Run request. Existing Runs retain their pinned prompt identity for recovery
and audit.

The grounded QA contract continues to use the versioned `grounded-qa-v1-provisional` prompt for
the Skill's internal, citation-validated result. A separate finalizer uses that result as
reference material and produces the user-facing response.

Direct responses are a distinct terminal result and never include fabricated grounded citations.
Clarifications use the server-authored `assistant-clarification-v1` schema. Resource candidates
contain only safe display metadata. A candidate selection derives a confirmation idempotency key
from the original parent run; it cannot create duplicate work or broaden scope.

The continuation needed to resume a resource clarification is persisted only with that parent Run.
It contains the server-pinned Skill, original selection source, bounded question, and expected
resource type; it is never serialized into an API response. On selection, the Application layer
loads the waiting Run, checks the clarification identity and pinned active Skill, then resolves the
submitted candidate again inside the Run's current Space. A stale, forged, cross-Space, or otherwise
unavailable candidate fails without reopening the Run. A valid selection reopens and promotes the
same parent Run; it never creates a replacement conversation or Run.

The versioned command catalog combines base commands and active Skill invocation metadata without
exposing full prompts, inactive versions, internal budgets, remaining tokens, or Tool limits. The
server, not the client, parses the original input: a first non-whitespace `/` begins a
case-insensitive command; `//` is ordinary text; unknown commands return candidates rather than
being guessed. Explicit commands bypass model selection but never bypass pinning, authorization,
Space, schema, approval, or external-provider policy.

Step 4 exposes this catalog at `GET /api/v2/commands`. A turn may carry an untrusted client command
hint, but the server reparses the original content and rejects mismatches. Base commands return a
command result without creating a business Run; explicit Skill commands create the same parent Run
and use `selection_source=command`. `/compact` creates an idempotent `context_compaction` parent
Run and dispatches it through the existing Worker queue.

### Manifest v2 and progressive disclosure

Skill manifest v2 adds `invocation.command`, aliases, argument hint, bounded trigger summary,
examples, and input mode. Registry activation validates command and alias uniqueness across the
active catalog, bounded metadata, safe examples, and compatibility with the Skill input schema.
Any invocation metadata change creates a new immutable Skill version and content digest. Manifest
v1 remains read-only for historical run recovery.

The base prompt receives only safe catalog metadata. It never concatenates complete Skill prompts.
After the server pins a selected Skill, the existing registry loads that version's own prompt,
workflow, and Tool allowlist. An Assistant router decision is not a Runtime `call_tool` instruction
and cannot invoke a Tool directly.

### Context, usage, and events

Messages remain append-only facts. `ConversationContextService` creates one bounded snapshot from
the current message, a recent-turn window, and versioned rolling summaries. Summaries record their
covered message range, prompt/model version, digest, and sensitivity; they never replace source
messages. Soft-watermark compression and `/compact` create idempotent `context_compaction` Runs on
the existing Worker queue, with lease recovery and a failure fallback to the bounded recent window.
Router input, direct answers, resource-reference handling, and Skill standalone requests use this
same snapshot. The QA `ContextBuilder` remains evidence-isolated: it receives only the standalone
task request and grounded evidence, never a copied full conversation. The context snapshot is
bounded untrusted data and cannot supersede a system prompt.

`RunBudget` and provider limits remain server-side safety mechanisms for timeout, cancellation,
repeated-call detection, and recovery. Product UI may show actual input/output/total usage, model,
and elapsed time in collapsed run information, but not remaining token allowance, budget progress,
or Tool-call caps.

API v2 and `agent-run-sse-v2` are independently versioned. V2 lifecycle events are `accepted`,
`routing`, `clarification`, `skill_started`, `phase`, `completed`, `failed`, and `cancelled`.
They remain projections of PostgreSQL-persisted state with monotonic per-run sequencing and a single
terminal state. Existing `qa-sse-v1` remains available to v1 callers; grounded QA lifecycle events
are projected into v2 without changing the existing v1 event contract.

`GET /api/v2/conversations/{conversation_id}/runs` returns the durable v2 Run projection in
creation order. It is the recovery read model for an active Run, waiting clarification, completed
direct response, and the v2 identity of a grounded result after a browser refresh. Grounded result
and citation bodies continue to use the existing scoped v1 QA read endpoints; v2 does not expose
evidence bodies, internal continuation data, or runtime guardrail values.

When a grounded Skill reaches a business-terminal QA state, the QA Worker appends one `phase` event
with `phase=final_answer`, then runs the finalizer exactly once for the parent Run. The QA message
remains an internal, citation-validated Skill result. The finalizer publishes a separate parent
Assistant message, after which one terminal v2 event is appended. The Web workspace renders the
finalizer message in an expanded final-answer frame. Evidence is opened by the frame or its Skill
invocation record, and one shared Run-scoped evidence panel is used so different components cannot
open competing panels; the panel has an explicit close action.

The Web also preserves the original explicit slash command in the user message and composer. A
valid command prefix receives a metric-neutral accent rather than a font-weight change, so the
caret and text layout remain stable. Assistant messages and both Skill/finalizer result surfaces
render GFM Markdown and LaTeX; this is presentation only and does not expose raw prompts, Tool
payloads, document excerpts, or internal budgets.

### Privacy and quality boundary

The router sees the current request, bounded conversation context, and safe active catalog metadata,
not document bodies by default. The model cannot grant permissions, expand a Space, or select an
external provider. Private-local or restricted messages, summaries, and document excerpts remain
local unless source policy, deployment policy, and visible user consent all allow the external path.
Logs, traces, queues, fixtures, and reports retain only IDs, digests, safe summaries, counts,
versions, timings, usage, and stable error codes.

The routing development set is synthetic and provisional. No implementation step may run the
current formal holdout or describe routing, retrieval, answers, or Skills as formally quality
accepted.

### Controlled migration and release

Step 8 makes the Web v2 conversation workspace the default entry and exposes v1 only through an
explicit compatibility selector with a build-time UTC deadline. The deadline is fail-closed: an
expired or malformed value resolves to v2. The selector is a client entry choice, not a second QA
implementation; v1 submission, cancellation, Run reads, and citation reads continue through the
existing application ports.

The release sequence is fake/local Provider first, followed by a small, policy-approved external
Chat rollout. Existing `MODEL_ALLOW_EXTERNAL`, source sensitivity, deployment policy, and visible
consent remain the only external-provider gates. Rollback is configuration-only (`v1` default plus a
Web rebuild) and must preserve v2 records, historical Run identity, active Skill pointers, and old
Skill packages. Retirement of v1 requires a separate versioned decision after real usage shows that
the compatibility path is no longer needed and all historical Runs remain readable.

## Alternatives

- Keep client-selected Skill modes as the default. Rejected because it cannot support ordinary
  conversation or server-authoritative automatic routing.
- Let the router create resource UUIDs or directly invoke Skills and Tools. Rejected because it
  defeats immutable version pinning, Space isolation, schema checks, and approval.
- Add a second Agent queue or a synchronous request-bound assistant path. Rejected because existing
  Worker lifecycle and durable state already satisfy cancellation and recovery requirements.
- Put every Skill prompt in the base prompt. Rejected because it expands injection surface and
  context cost while making active-version changes difficult to audit.
- Remove budgets because the product hides them. Rejected because availability and recovery still
  need server-side ceilings.

## Consequences

Implementation proceeds in the staged order recorded by the conversation-evolution plan. The first
step freezes contract artifacts and synthetic routing fixtures; persistence, API, Worker, Skill,
context, and Web changes follow only after their respective stages. New APIs, events, manifests,
prompts, and error codes are versioned. Migration work must backfill legacy QA runs without changing
their identity, citation references, or historical Skill pin.

## Reassessment Triggers

Reassess this decision if shared parent identity cannot preserve legacy QA recovery, if context
compaction cannot retain safety and explicit references under provider limits, if a new Skill class
needs a different trust or approval model, if event compatibility cannot be maintained, or if a
reviewed data-policy change authorizes a different external-provider boundary.
