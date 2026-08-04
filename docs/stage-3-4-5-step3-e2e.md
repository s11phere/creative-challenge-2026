# Step 3 Backend E2E Evidence

Date: 2026-08-03
Status: provisional execution complete; formal Stage 4 acceptance remains open.

## Scope

- Isolated Compose project: `stage5-provisional-e2e`.
- API: `http://localhost:58010`.
- Model policy: fake chat/embedding/reranker, external providers disabled.
- Data: existing isolated sources in the zero UUID space; no shared business volume or private corpus was used.
- No Stage 3 holdout or formal answer holdout was run.

## HTTP journey

The following path was executed without logging question, answer, excerpt, prompt, or provider output:

1. `GET /api/v1/health/ready` returned `ready`; PostgreSQL, Redis, and fake model checks were healthy.
2. Conversation creation returned HTTP 201.
3. Question submission returned HTTP 202 and a queued Run.
4. Worker execution reached `completed`, with one answer and one Citation.
5. SSE replay returned `accepted`, `started`, and `completed` events.
6. Citation resolution returned a bounded `lines` locator, but status was `invalid` and excerpt length was zero.
7. Feedback submission returned `pending_review`; repeating the same idempotency command returned the same feedback identity.

The isolated backend integration suite was also run against a separate PostgreSQL/Redis project with
fake model providers:

```text
47 passed, 1 warning
```

## Fix and verification

The API previously emitted an opaque UUID as the SSE `id` while parsing `Last-Event-ID` as an integer sequence. The endpoint now emits `QAStreamEvent.sequence` as the SSE id. The affected API and SSE unit tests pass:

```text
12 passed, 1 warning
```

The running API container could not be rebuilt in the restricted Docker buildx environment, so the live HTTP instance still needs a normal image rebuild before this fix can be rechecked over the network.

## Open findings

- Citation resolution for the fake end-to-end answer is currently invalid. This is recorded as a provisional quality failure, not converted into a formal pass.
- Live SSE sequence recheck is pending the image rebuild described above; the contract and ASGI tests cover the corrected behavior.
- Full import-to-feedback journey, Playwright coverage, and formal answer quality gates remain open for later steps.
