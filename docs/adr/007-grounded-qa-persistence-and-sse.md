# ADR-007: Grounded QA Persistence, Citation, Execution, And SSE Semantics

- Status: Accepted
- Date: 2026-07-23
- Scope: Contract decision only; Stage 4 remains provisional until its formal gates close

## Context

Stage 4 must expose one grounded-answer application path to Web, HTTP, evaluation, and later
`knowledge_qa` Skill callers. The repository already fixes the modular-monolith/Worker boundary,
PostgreSQL as the system of record, immutable document versions, local-first model access, and
Redis/Dramatiq as at-least-once delivery. It does not yet define the identity and lifecycle of a
grounded answer, the persisted relationship among conversations and runtime executions, citation
resolution after source changes, or reconnectable SSE behavior.

The Stage 0 corpus gate, Stage 2 Step 9 acceptance, and formal Stage 3 exit are still open. This ADR
therefore fixes contracts and ownership without authorizing migrations, private-corpus processing,
real-model tuning, formal answer evaluation, or a claim that grounded QA is available.

## Decision

### Application and domain boundary

There is one Grounded QA Application Port. API, Web, evaluation, Worker, and future Skill adapters
call that port; none may reproduce retrieval, grounding, persistence, or failure rules. Candidate
evidence is obtained only through `SearchService.search(SearchRequest, RetrievalProfileV1)`.

The version-1 business outcomes are `answer`, `refusal`, and `conflict`. Infrastructure or contract
errors produce `failed`; cancellation and timeout remain runtime terminal states and are not
reclassified as business refusals. A published answer consists of immutable claims and citations.
Every verifiable claim has at least one citation, and every citation targets a server-assigned
Evidence identity from the same run.

### Entity ownership and persistence

- A Conversation belongs permanently to one Space and one caller/owner scope.
- Messages are append-only conversation facts. A user question creates a new run; retry creates a
  new attempt and never rewrites the earlier result.
- A run may persist an explicit retrieval scope containing Space-owned Source, Document, and immutable
  DocumentVersion identities. Execution uses that fixed scope only; a later publication, withdrawal,
  deletion, or selector mismatch fails rather than following a new current version or widening scope.
- The persisted AgentRun identity is shared with the existing bounded runtime contract. Stage 4 may
  add a QA-specific projection linked to that identity, but must not create a parallel runtime model.
- Evidence records preserve Space, source, document, immutable document version, chunk, locator,
  evidence digest, and resolution status. They do not copy source text unless a reviewed retention
  requirement makes that unavoidable.
- Claims and Citations are published atomically with the terminal answer. Partial generated text is
  not a business fact.
- Feedback belongs to a message and run, uses an idempotency key, and enters a review queue. It never
  mutates a frozen evaluation dataset directly.

### Assistant parent run and SSE v2 amendment (2026-08-06)

ADR-013 makes `ConversationRun` the durable parent identity for a product-level Assistant turn.
Grounded QA is its fixed-scope projection rather than a parallel run system. The migration backfills
legacy QA records before Runtime execution, checkpoints, approvals, and QA projections reference the
shared parent ID. Existing run, attempt, citation, message, and Skill-pin identities remain stable
for v1 reads, recovery, and rollback.

`agent-run-sse-v2` is a separately versioned projection of the persisted parent state. Its lifecycle
types are `accepted`, `routing`, `clarification`, `skill_started`, `phase`, `completed`, `failed`,
and `cancelled`; events preserve the existing monotonic per-run sequence, replay cursor, safe payload
rules, and one-terminal-event rule. A grounded-QA run projects its lifecycle to this vocabulary.
`qa-sse-v1` remains available and unchanged for v1 clients. Unknown event versions are rejected or
safely ignored according to their own transport contract; they are never silently reinterpreted.

Database constraints prove same-Space ownership where ordinary foreign keys can express it;
Application checks repeat cross-aggregate ownership and current-publication checks. PostgreSQL is
the state and idempotency authority. Queue messages contain only IDs and control metadata.

### Citation lifecycle

New citations may target only the current published, non-tombstoned DocumentVersion observed by the
run. Publication revalidates Space and the Source -> Document -> DocumentVersion -> Chunk chain,
locator bounds, Evidence ownership, and current-version status to close retrieval/publication races.
The model may select only server-assigned Evidence IDs and cannot emit storage identities.

Retrieval hits marked `context_only` may be included to explain surrounding material and may
co-support a claim, but they cannot be the sole support for any claim and never count as retrieval
gold hits. At least one `matched` Evidence ID is required for every published claim.

A historical citation never moves to a newer version. Source withdrawal changes its resolution
status to `withdrawn`; retention cleanup may change it to `unavailable`. Both preserve the original
identity and are distinct from `invalid`, which means the stored ownership, digest, or locator cannot
be verified. Citation resolution returns the smallest authorized excerpt and is not a general
document-read API.

### Execution, cancellation, and retry

QA runs use the existing Worker and Redis/Dramatiq delivery boundary because generation, recovery,
and cancellation can outlive an HTTP request. Run creation and its idempotency key commit before
dispatch. Duplicate delivery is expected; an attempt lease and persisted transition guard prevent
duplicate execution and duplicate terminal publication.

Cancellation is an explicit persisted request. Workers check it before each expensive phase and
before terminal publication. Client disconnect does not cancel a run. Retry creates a new attempt
linked to the original run and rechecks caller, Space, source policy, profile, model, and budget.
Only bounded dependency failures are retryable; invalid input, invalid model output, insufficient
evidence, and source conflict are not infrastructure retries.

### SSE version 1

SSE is a projection of persisted run state, not the state authority. Every event has
`schema_version=1`, `event_id`, `run_id`, a strictly increasing per-run `sequence`, `occurred_at`,
`type`, and a type-specific safe payload. Event types include `accepted`, `started`, `phase`,
`evidence`, `answer_delta`, `completed`, `refused`, `failed`, `cancel_requested`, `cancelled`,
`timed_out`, and `heartbeat`.

Exactly one terminal event is derived from the persisted terminal transition. `Last-Event-ID` (or an
equivalent sequence cursor) replays durable lifecycle events and then a current-state snapshot.
Repeated transport delivery is allowed; clients deduplicate by event ID. Event payloads refer to
business object IDs and safe counts/statuses. Final content is fetched or included only from the
persisted, validated result under the caller's authorization.

`answer_delta` is optional provisional display data. The current non-streaming `ModelGateway.chat()`
adapter emits no deltas. A future streaming adapter must keep citation numbering stable, label every
delta provisional, make the client discard it after validation failure, and define bounded local
retention before enabling delta replay. Deltas never become queryable completed messages.

### Privacy and versioning

Question text, answer text, feedback, prompt input, model output, and resolved excerpts inherit the
local-first policy and are excluded from logs, traces, queue messages, OpenAPI examples, and committed
reports. Audit records contain IDs, hashes, versions, safe codes, counts, timings, and token totals.

Grounded-answer, QA profile, prompt, SSE event, and evaluation report contracts are independently
versioned. A run pins all of those versions plus RetrievalProfile and model identity when it starts.
Unknown event or result versions are rejected or safely ignored; they are never reinterpreted as v1.

## Alternatives

- Synchronous request-bound generation was rejected because disconnect, timeout, cancellation, and
  recovery would have no durable authority.
- A second queue or a dedicated QA service was rejected because the existing Worker boundary is
  sufficient and no isolation or capacity evidence justifies another deployment unit.
- Storing only rendered answer text was rejected because claim support and citation identity would
  be unverifiable.
- Allowing citations to follow `Document.current_version_id` was rejected because historical answers
  would silently change meaning after updates.
- Treating SSE deltas or Redis state as completion authority was rejected because neither provides
  atomic publication with the grounded result.
- Reusing refusal for model, retrieval, storage, cancellation, or timeout failures was rejected
  because it corrupts product behavior and evaluation denominators.

## Consequences

Stage 4 requires new repository ports, one reviewed migration after the data gate closes, idempotent
Worker dispatch, monotonic event sequencing, atomic result publication, and authorization-aware
citation resolution. API and Web implementations must display provisional and terminal content as
different states. Evaluation can score business outcomes without counting infrastructure failures as
correct refusals.

Accepting this ADR closes the architecture decision, not the Stage 4 startup or quality gates. Until
those gates close, only pure contracts, schemas, in-memory adapters, deterministic fakes, and allowed
repository fixtures may implement it.

## Reassessment Triggers

Reassess if measured synchronous latency makes Worker execution unnecessary, a streaming provider
requires durable delta replay, PostgreSQL event sequencing cannot meet measured fan-out or retention
needs, citation retention law requires content copies or erasure semantics beyond tombstones, shared
runtime persistence cannot represent QA attempts, or load/isolation evidence justifies a separate
service or queue.
