# Stage 4 Grounded QA Persistence Design Review

- Status: Provisional PostgreSQL and Worker execution subset implemented
- Date: 2026-07-23
- Governing decision: ADR-007

## Gate

Stage 0 and Stage 2 are closed; the formal Stage 3 exit remains open. The relational design below
remains the target formal shape. A user-authorized provisional subset now has ORM models, a forward
Alembic revision, PostgreSQL QA Repository/Event Store, and API restart recovery. It does not include
excerpt navigation, formal QA configuration, or a claim that Stage 4 is complete.

The implemented subset uses `qa_runs` as the stable shared run projection and append-only
`qa_run_attempts`; Evidence, Citation, Feedback, and events are durable. Structured nested values
remain versioned JSON while the formal normalized claim/locator/timing tables described below are
still deferred. Restart recovery preserves terminal runs, requeues safe non-terminal attempts, and
removes unpublished Evidence before rerun. The Worker receives identifiers only, claims an attempt
under a renewable PostgreSQL lease, and safely ignores duplicate delivery after terminal publication.

## Proposed Ownership

The migration should add the following normalized records after the gate closes:

| Record | Identity and ownership | Required data |
| --- | --- | --- |
| `conversations` | `conversation_id`; immutable `space_id` and `owner_id` | created, updated, archived timestamps |
| `messages` | `message_id`; composite reference to Conversation/Space | append-only role/content, optional run identity, scoped idempotency key |
| `agent_runs` | shared `run_id`; Space/caller ownership | generic AgentRun status and fixed Skill identity; this is not a QA-only duplicate |
| `qa_run_attempts` | `attempt_id`; unique `(run_id, attempt_number)` | predecessor, question, status, cancel/error, all pinned versions, Token/model counts and timestamps |
| `qa_run_phase_timings` | unique `(attempt_id, phase)` | finite non-negative duration; no question, prompt, or source text |
| `qa_evidence` | unique `(attempt_id, evidence_id)` | source/document/version/chunk identity, source key, digest, matched/context-only and resolution status; no excerpt text |
| `qa_evidence_locators` | unique `(attempt_id, evidence_id, ordinal)` | locator kind/start/end for every immutable Evidence locator |
| `qa_claims` | unique `(attempt_id, claim_id)` | ordered validated claim text belonging to the terminal answer |
| `qa_claim_evidence` | unique `(attempt_id, claim_id, evidence_id)` | claim support join; at least one matched Evidence is enforced before publication |
| `qa_citations` | unique `(attempt_id, message_id, evidence_id)` | fixed Citation identity, selected locator, digest and lifecycle status |
| `qa_feedback` | `feedback_id`; reference to Conversation/Message/Run/Attempt/Space | caller, decision, optional local note, `pending_review`, scoped idempotency key |

The existing Source, Document, DocumentVersion and Chunk tables remain authoritative. Evidence stores
their immutable IDs and digest, not a second document snapshot. Checkpoint and Skill Registry tables
are out of scope; AgentRun must be shared with the accepted runtime identity rather than copied into
a parallel QA runtime.

## Constraints And Indexes

The migration review must prove the following before implementation is accepted:

1. Parent records expose composite unique keys containing identity plus `space_id`; child composite
   foreign keys prevent a Message, Run, Attempt, Evidence, Citation or Feedback from changing Space.
2. The Source -> Document -> DocumentVersion -> Chunk chain uses ordinary foreign keys. Application
   validation repeats the full chain, current-version and locator checks immediately before publish.
3. Conversation Space/owner are immutable. Messages, Evidence, Claims and Citations are append-only.
4. Partial unique indexes scope non-null message/run/feedback idempotency keys to their owner boundary.
5. Attempt number and predecessor form a linear chain per run. A retry inserts a new attempt and
   cannot update a terminal predecessor back to a runnable state.
6. A terminal attempt references exactly one assistant Message. Claim/Citation rows can exist only
   for that attempt and message; duplicate Evidence or Citation IDs are rejected.
7. Indexes support Conversation listing by owner/Space/update time, Run lookup by status/update time,
   Evidence/Citation lookup by attempt, and pending Feedback review. No index contains content text.

## Transactions

### Create And Dispatch

One transaction inserts or reuses the user Message by idempotency key, inserts the shared AgentRun
and its first QA attempt, and commits. Queue dispatch happens only after that commit and carries IDs
plus safe control metadata. A dispatch failure leaves a recoverable committed run rather than an
untraceable queue message.

### Evidence And Usage

Evidence writes are immutable upserts scoped to one attempt; a duplicate identity with different
ownership, locator or digest fails. Usage snapshots are monotonic and contain only counts, version
IDs and timings. Neither operation writes prompt input, model output or excerpts to logs.

### Terminal Publication

The transaction locks the latest attempt, verifies it is `verifying` and not cancel-requested,
rechecks Evidence availability, then inserts the assistant Message, Claims, claim/Evidence joins and
Citations. It updates the attempt to exactly one business terminal status in the same commit. Any
validation or write failure rolls back every terminal row. Replayed delivery returns the existing
terminal result only when the business payload matches; conflicting replay fails.

Runtime `failed`, `cancelled` and `timed_out` transitions save only a safe error code and usage. They
cannot insert an assistant answer. Feedback is a separate idempotent transaction and always starts
at `pending_review`.

## Migration Acceptance After Gate Closure

Implementation must use a new Alembic revision without changing migration history. It must verify:

- upgrade from the current single head and downgrade back to it;
- exactly one Alembic head;
- empty isolated PostgreSQL startup and migration;
- composite ownership, uniqueness and check constraints with negative integration tests;
- atomic terminal rollback, duplicate delivery, concurrent cancel/publish and retry serialization;
- retained-volume restart without loss or duplicate terminal facts.

No migration command should be run against a business or shared developer database for this review.
