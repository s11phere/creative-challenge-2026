# Stage 5 Cross-Stage Contract Audit

Status: audited 2026-07-19; refreshed 2026-07-31. Authoritative decisions are in
[ADR-006](adr/006-skill-manifest-versioning-and-trust.md) and
[ADR-007](adr/007-grounded-qa-persistence-and-sse.md).

Stage 5 is allowed to define generic Runtime, Tool, and Skill contracts and to consume the provisional
Grounded QA Application Port. By explicit product direction on 2026-07-31, the existing QA Web/API may
also expose a usable provisional in-process execution path before the formal gates close. This does not
activate or claim formal availability of the `knowledge_qa` Skill.

| Upstream capability | Expected reusable contract | Current checkout | Stage 5 action |
| --- | --- | --- | --- |
| Stage 2 ingestion | Published `DocumentVersion` identity, locator metadata, Space ownership, withdrawal state | Domain, PostgreSQL adapters, Worker pipeline and formal Stage 2 acceptance exist | Reuse Application/Domain boundaries; never read ingestion ORM from a Skill |
| Stage 3 retrieval | `SearchService.search(SearchRequest, RetrievalProfileV1)` returning versioned, Space-scoped results | SearchService, PostgreSQL FTS/pgvector and four retrieval modes exist; formal model/profile/holdout gate remains open | Grounded QA owns retrieval; Skill must not call RetrievalStore or duplicate ranking logic |
| Stage 4 grounded answer | Structured answer/refusal with claims, citations, evidence, versions, and stable errors | Domain contracts, generator and provisional `GroundedQAApplicationPort` exist; the QA API now executes it against real PostgreSQL retrieval | Reuse the QA Port; block formal `knowledge_qa` activation until Stage 4 exits |
| Stage 4 citation resolver | Current/history/withdrawn source resolution and original location | Resolver/target Port and PostgreSQL target validation adapter exist; terminal excerpt API and approved-corpus golden validation are absent | Reuse the adapter from QA; do not synthesize citation logic in Skill |
| Stage 4 conversations/runs | Conversation, shared AgentRun, Evidence ownership and persistence semantics | Domain/Repository Port and in-memory transaction double exist; Stage 5 now adds a shared-identity in-memory checkpoint transaction double; reviewed PostgreSQL implementation is absent | Reuse identities and Ports; do not apply the current QA ORM/Alembic draft or create a parallel run model |
| Stage 4 SSE/cancellation | Event names, payloads, cancellation, recovery, and `event_version` | `qa-sse-v1`, explicit cancellation and provisional in-memory API exist; durable events, Worker recovery and API restart recovery are absent | Reuse event schema in fakes; public Runtime API waits for durable upstream protocol |
| Model gateway | Capability aliases and bounded usage/errors | `ModelGateway` package with `fast_chat`/`embedding_zh` fake exists | Reuse port; no provider SDK in Skill |
| Queue delivery | PostgreSQL fact source; Redis/Dramatiq ID-only delivery | ADR-009 and ingestion actor exist; no reviewed QA AgentRun actor | Defer QA background/recovery actor until durable run model exists |

## Verification boundary

The refreshed audit permits Stage 5 schema, workflow and handler contract work against the provisional
QA Port. The user-authorized usable slice additionally permits the existing QA API/Web to execute that
Port with real PostgreSQL retrieval and citation target validation. QA persistence and execution remain
process-local; this is not a PostgreSQL/Worker Runtime integration or a `knowledge_qa` availability claim.
Formal Stage 5 business completion remains blocked until Stage 3 formally exits and Stage 4 supplies the
reviewed persistence, Worker, Citation excerpt API and quality evidence.

The permitted contract work is now represented by `skills/_provisional/knowledge_qa` and
`application.skills.KnowledgeQASkillAdapter`. Trusted-root bulk reload skips the package, and no startup
path registers it. Tests load it explicitly with a fake QA Port; this does not change the formal gate.

Step 7 was re-audited on 2026-07-31. ADR-007 is accepted, but its durable PostgreSQL/Worker/restart
preconditions are not implemented. The usable slice therefore extends the existing `/api/v1/qa/runs`
and `qa-sse-v1` transport rather than adding `/api/v1/skills`, a parallel `/api/v1/runs` family, or a
second SSE schema. The Web QA entry is usable; a Web Skill entry remains intentionally absent.
