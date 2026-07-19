# Stage 5 Cross-Stage Contract Audit

Status: audited 2026-07-19. Authoritative decisions are in [ADR-006](adr/006-skill-manifest-versioning-and-trust.md).

Stage 5 is allowed to define generic Runtime, Tool, and Skill contracts with deterministic fakes.
It is not allowed to declare the knowledge workflow usable until the upstream application ports exist.

| Upstream capability | Expected reusable contract | Current checkout | Stage 5 action |
| --- | --- | --- | --- |
| Stage 2 ingestion | Published `DocumentVersion` identity, locator metadata, Space ownership, withdrawal state | Domain/ORM ingestion entities exist; ingestion pipeline port is absent | Use identity-shaped fake; do not read ORM directly |
| Stage 3 retrieval | `RetrievalStore.search` or equivalent returning scored, versioned, Space-scoped results | No retrieval package or port | Block real retrieval Tool; use fixed `SearchResult` fake |
| Stage 4 grounded answer | Structured answer/refusal with claims, citations, evidence, versions, and stable errors | No GroundedAnswer/Application implementation | Block `knowledge_qa`; use fixed `GroundedAnswer` fixture |
| Stage 4 citation resolver | Current/history/withdrawn source resolution and original location | No citation resolver | Do not synthesize citation logic in Skill |
| Stage 4 conversations/runs | Conversation, AgentRun, Evidence ownership and persistence semantics | No such domain entities or API | Runtime persistence must remain an interface; do not create parallel business model |
| Stage 4 SSE/cancellation | Event names, payloads, cancellation, recovery, and `event_version` | No SSE or run API | Internal event contract only; public API waits for upstream protocol |
| Model gateway | Capability aliases and bounded usage/errors | `ModelGateway` package with `fast_chat`/`embedding_zh` fake exists | Reuse port; no provider SDK in Skill |
| Queue delivery | PostgreSQL fact source; Redis/Dramatiq ID-only delivery | ADR-009 accepted; no AgentRun actor | Defer background recovery actor until durable run model exists |

## Verification boundary

The audit is complete for Step 0 when ADR-006 is accepted and every missing upstream item is
explicitly marked as blocked. Contract tests added before those items land must use local,
deterministic fakes and must not be presented as end-to-end knowledge quality validation.
