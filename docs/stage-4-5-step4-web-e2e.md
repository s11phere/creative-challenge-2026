# Step 4 Web E2E Evidence

Date: 2026-08-03
Status: provisional Web verification complete; Playwright core-journey gate closed on 2026-08-13.

## 2026-08-13 Playwright browser E2E (core journey)

The browser-level E2E gate is now closed for the **core journey** via Playwright, running against
the **real Compose stack** (`MODEL_PROVIDER=fake` for deterministic assertions) at
`http://127.0.0.1:5173`:

- `apps/web/e2e/health.spec.ts` — status dashboard renders with all four services ready and the
  model gateway reported as the fake stub.
- `apps/web/e2e/conversation.spec.ts` — greeting produces the terminal answer
  `fake-response-autonomous` with an Agent run card; empty-conversation placeholder with disabled
  send button; deterministic submit-failure alert via route interception. A deterministic
  empty-Space knowledge-refusal assertion was **intentionally omitted**: the fake provider's
  knowledge path does not terminate on an empty Space (see known-defect note below).
- `apps/web/e2e/composer.spec.ts` — `/` command panel with keyboard selection; Enter submits a
  plain turn (HTTP 202); Enter on an empty composer does not submit.
- `apps/web/e2e/mobile.spec.ts` — 390×844 mobile viewport smoke for the conversation workspace and
  status dashboard.

CI runs these in the extended `compose-smoke` job (Compose smoke + web E2E) on ubuntu-latest with
headless Chromium; `pnpm typecheck:e2e` type-checks `e2e/` and `playwright.config.ts` via the
dedicated `tsconfig.e2e.json`, kept out of the app `tsc -b` build graph.

Screenshots/video are disabled (privacy); only synthetic content is used and on-failure traces are
privacy-scanned before upload. Desktop/mobile screenshots, real-browser keyboard traversal, and
responsive-viewport rendering are now covered for the core journey. The full
import→QA→citation→feedback journey with an ingested fixture remains open and is out of the core
scope.

## Known defect surfaced by the E2E run

Running the suite against the real fake-provider stack surfaced a genuine product defect in the
native knowledge loop that is **out of scope for this browser gate** and needs a dedicated fix:

- On an empty Space, a knowledge question makes the fake provider's assistant loop re-issue
  `knowledge_retrieve` forever (the runtime model observation carries only
  `iteration/tool_name/status/summary`, so the fake never saw the coverage gap). A first fix
  (bounded retrieval up to the server's `max_search_observations`, then route to
  `knowledge_answer`) is in `packages/infrastructure/src/infrastructure/qa_execution.py` and covered
  by `tests/unit/test_qa_runtime.py`. With it the loop now reaches Grounded QA, where a second,
  pre-existing issue appears: the `knowledge_answer` terminal observation fails harness schema
  validation (`RUN_NATIVE_TOOL_RESULT_INVALID`). Both should be fixed and verified before claiming
  any browser evidence for the empty-Space knowledge path.

## Verification performed

From `apps/web` with the locked pnpm toolchain:

```text
corepack pnpm@10.20.0 lint       PASS
corepack pnpm@10.20.0 typecheck PASS
corepack pnpm@10.20.0 test      PASS: 4 files, 27 tests
corepack pnpm@10.20.0 build     PASS
```

Existing component tests cover navigation, health loading/degraded/error states, source upload,
cancel/retry, keyboard submission, QA queued/cancelled/recovery paths, refusal without invented
citations, verified citation loading, citation failure/retry, and review-card approval.

The isolated Web service on `http://localhost:55180` also passed a non-browser smoke check:

- `/` returned HTTP 200 with the application root mount.
- The production CSS asset returned HTTP 200.
- Same-origin `/api/v1/health/ready` returned HTTP 200 with `ready`.

## Open gate

The repository has no Playwright dependency or configuration, and no Chromium/Chrome/Edge or
Playwright executable is available in this environment. Therefore desktop/mobile screenshots,
keyboard traversal in a real browser, responsive overflow inspection, refresh/reopen behavior, and
browser-level SSE/retry evidence were not claimed as passed.

No private content, answer body, citation excerpt, or provider response was written to this record.
