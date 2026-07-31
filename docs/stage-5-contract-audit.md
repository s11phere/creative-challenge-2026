# Stage 5 Cross-Stage Contract Audit

Status: audited 2026-07-19; refreshed 2026-07-31. Authoritative decisions are in
[ADR-006](adr/006-skill-manifest-versioning-and-trust.md) and
[ADR-007](adr/007-grounded-qa-persistence-and-sse.md).

Stage 5 is allowed to define generic Runtime, Tool, and Skill contracts and to consume the provisional
Grounded QA Application Port with deterministic fakes. It is not allowed to implement or expose the
`knowledge_qa` business Skill until the upstream formal gates and production adapters are complete.

| Upstream capability | Expected reusable contract | Current checkout | Stage 5 action |
| --- | --- | --- | --- |
| Stage 2 ingestion | Published `DocumentVersion` identity, locator metadata, Space ownership, withdrawal state | Domain, PostgreSQL adapters, Worker pipeline and formal Stage 2 acceptance exist | Reuse Application/Domain boundaries; never read ingestion ORM from a Skill |
| Stage 3 retrieval | `SearchService.search(SearchRequest, RetrievalProfileV1)` returning versioned, Space-scoped results | SearchService, PostgreSQL FTS/pgvector and four retrieval modes exist; formal model/profile/holdout gate remains open | Grounded QA owns retrieval; Skill must not call RetrievalStore or duplicate ranking logic |
| Stage 4 grounded answer | Structured answer/refusal with claims, citations, evidence, versions, and stable errors | Domain contracts, generator and provisional `GroundedQAApplicationPort` exist and pass synthetic end-to-end tests | Use the QA Port in fake contract tests; block production `knowledge_qa` until Stage 4 exits |
| Stage 4 citation resolver | Current/history/withdrawn source resolution and original location | Resolver and target Port exist; PostgreSQL target adapter, terminal Citation API and approved-corpus golden validation are absent | Do not synthesize citation logic in Skill; wait for the production adapter/API |
| Stage 4 conversations/runs | Conversation, shared AgentRun, Evidence ownership and persistence semantics | Domain/Repository Port and in-memory transaction double exist; Stage 5 now adds a shared-identity in-memory checkpoint transaction double; reviewed PostgreSQL implementation is absent | Reuse identities and Ports; do not apply the current QA ORM/Alembic draft or create a parallel run model |
| Stage 4 SSE/cancellation | Event names, payloads, cancellation, recovery, and `event_version` | `qa-sse-v1`, explicit cancellation and provisional in-memory API exist; durable events, Worker recovery and API restart recovery are absent | Reuse event schema in fakes; public Runtime API waits for durable upstream protocol |
| Model gateway | Capability aliases and bounded usage/errors | `ModelGateway` package with `fast_chat`/`embedding_zh` fake exists | Reuse port; no provider SDK in Skill |
| Queue delivery | PostgreSQL fact source; Redis/Dramatiq ID-only delivery | ADR-009 and ingestion actor exist; no reviewed QA AgentRun actor | Defer QA background/recovery actor until durable run model exists |

## Verification boundary

The refreshed audit permits Stage 5 schema, workflow and handler contract work against the provisional
QA Port with local deterministic fakes. It does not permit PostgreSQL/Worker/API integration or a
`knowledge_qa` availability claim. Formal Stage 5 business work remains blocked until Stage 3 formally
exits and Stage 4 supplies the reviewed persistence, Worker, Citation API and quality evidence.

The permitted contract work is now represented by `skills/_provisional/knowledge_qa` and
`application.skills.KnowledgeQASkillAdapter`. Trusted-root bulk reload skips the package, and no startup
path registers it. Tests load it explicitly with a fake QA Port; this does not change the formal gate.
