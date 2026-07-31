# Stage 5 Cross-Stage Contract Audit

Status: audited 2026-07-19; refreshed 2026-07-31. Authoritative decisions are in
[ADR-006](adr/006-skill-manifest-versioning-and-trust.md) and
[ADR-007](adr/007-grounded-qa-persistence-and-sse.md).

Stage 5 is allowed to define generic Runtime, Tool, and Skill contracts and to consume the provisional
Grounded QA Application Port. By explicit product direction on 2026-07-31, the existing persistent QA
Web/API/Worker path also executes the active provisional `knowledge_qa 0.1.0` before the formal quality
gates close. This does not claim formal Stage 3/4/5 completion.

| Upstream capability | Expected reusable contract | Current checkout | Stage 5 action |
| --- | --- | --- | --- |
| Stage 2 ingestion | Published `DocumentVersion` identity, locator metadata, Space ownership, withdrawal state | Domain, PostgreSQL adapters, Worker pipeline and formal Stage 2 acceptance exist | Reuse Application/Domain boundaries; never read ingestion ORM from a Skill |
| Stage 3 retrieval | `SearchService.search(SearchRequest, RetrievalProfileV1)` returning versioned, Space-scoped results | SearchService, PostgreSQL FTS/pgvector and four retrieval modes exist; formal model/profile/holdout gate remains open | Grounded QA owns retrieval; Skill must not call RetrievalStore or duplicate ranking logic |
| Stage 4 grounded answer | Structured answer/refusal with claims, citations, evidence, versions, and stable errors | Generator and unique provisional `GroundedQAApplicationPort` execute against real PostgreSQL retrieval from the Worker | Reuse the QA Port; Skill must execute the existing QA Run and never submit a second one |
| Stage 4 citation resolver | Current/history/withdrawn source resolution and original location | Published Citation resolver, PostgreSQL target validation and terminal excerpt API exist; formal quality validation remains open | Reuse the adapter from QA; do not synthesize citation logic in Skill |
| Stage 4 conversations/runs | Conversation, shared AgentRun, Evidence ownership and persistence semantics | PostgreSQL Conversation/Message/QA Run/Attempt/Evidence/Citation/Feedback repositories and lease recovery exist; generic Runtime Checkpoint remains in memory | Reuse the QA Run identity as the Skill run identity; do not create a parallel Runtime persistence model |
| Stage 4 SSE/cancellation | Event names, payloads, cancellation, recovery, and `event_version` | Durable `qa-sse-v1`, explicit cancellation, Worker recovery and API restart replay exist | Reuse the existing transport; public generic Runtime API still waits for its own justified use cases |
| Model gateway | Capability aliases and bounded usage/errors | `ModelGateway` package with `fast_chat`/`embedding_zh` fake exists | Reuse port; no provider SDK in Skill |
| Queue delivery | PostgreSQL fact source; Redis/Dramatiq ID-only delivery | QA actor carries only Run/trace/event identities and uses PostgreSQL attempt lease/heartbeat recovery | Reuse the actor; do not introduce another queue or message body containing question/evidence |

## Verification boundary

The refreshed audit permits Stage 5 schema, workflow and handler contract work against the provisional
QA Port. The user-authorized usable slice additionally permits the existing QA API/Web to execute that
Port with real PostgreSQL retrieval and citation target validation. QA Run/Attempt/Event are now the durable
recovery authority, and the Worker executes the fixed Skill package against that same Run. Generic Runtime
Checkpoint is still process-local. Registry active state now uses a PostgreSQL pointer with digest and
revision CAS. Formal Stage 5 business
completion remains blocked on Stage 3/4 quality gates, generic Runtime persistence, derived-knowledge
writes, and formal business quality gates.

The implementation is represented by `skills/knowledge_qa` and
`application.skills.KnowledgeQASkillAdapter`. Trusted-root reload installs the declaration-only package;
API/Worker assembly explicitly activates the configured semver, new QA Runs fix its content digest, and
Worker recovery pins the Run's version rather than choosing the current active version. Contract tests retain
the fake QA Port path and verify that Worker mode never submits a second Run.

Step 7 was re-audited on 2026-07-31. ADR-007's durable PostgreSQL/Worker/restart subset is implemented.
The usable slice therefore continues to use the existing `/api/v1/qa/runs` and `qa-sse-v1` transport.
The `/api/v1/skills` catalog exposes installed/fixed identity, manifest budget, and active revision. Its
activate/rollback write paths accept only an installed semver plus the expected revision; they cannot set a
package path, entrypoint, permissions, or budget. PostgreSQL is the pointer authority across API restarts,
while queued runs retain their own fixed identity. There is no parallel `/api/v1/runs` family or second SSE
schema. The Web QA entry displays the catalog identity; a separate Skill management UI remains absent.

Step 8 was re-audited on 2026-07-31. `summarize_document`, `compare_sources`, and
`create_review_cards 0.1.0` use the same QA Run identity, Worker, `qa-sse-v1`, Runtime handler, and
Grounded QA Port. Submission resolves an exact Space-owned Source/Document/DocumentVersion scope and
persists it on the Run; SearchService rejects stale, withdrawn, deleted, or mismatched fixed versions
instead of following a new publication. Comparison publication requires citations from at least two
sources. Review cards are preview-only and expose `SKILL_WRITE_PORT_UNAVAILABLE` with zero side effects;
there is no derived-knowledge write adapter or durable approval flow.
