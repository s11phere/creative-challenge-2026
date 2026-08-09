# Agent Loop Tools Effort: Step 2

> Status: provisional engineering implementation
> Date: 2026-08-09

Step 2 extends the existing bounded conversation context without changing the append-only message
or rolling-summary persistence model. It remains provisional engineering work and does not run or
claim any formal retrieval, answer, or Skill quality acceptance.

## Unified Snapshot

`ConversationContextService.snapshot()` remains the shared entry point for Assistant routing,
Loop callers, resource/Skill invocation callers, and standalone Skill rendering. In addition to the
rolling summary, recent visible messages, current request, sensitivity, and token estimate, it now
models:

- the current Loop goal and server-authored subquestions;
- privacy-safe summaries of observed Tool calls;
- evidence candidate/coverage counts and safe identifiers;
- unresolved items plus cancellation and durable approval state; and
- provider-neutral continuation metadata.

The `router_input()` representation keeps conversation and Tool content explicitly marked as
untrusted data. `standalone_request()` intentionally carries task state and the current request,
but not raw prior assistant answers or Tool output. Grounded QA continues to use its evidence-only
`ContextBuilder` and its current-question retrieval query.

## Isolation and Continuation

Snapshot construction validates Conversation, caller, Space, every loaded message, and every loaded
summary. A malformed cross-Space record fails closed before any model request. Original messages
remain append-only; rolling summaries remain versioned, idempotent, digest-pinned, and sensitivity
inheriting.

`ChatContinuation` is a Provider-neutral `ModelGateway` contract. A Provider is eligible to send a
native Responses continuation ID only when its capability is explicitly enabled by a future
capability registry; its necessary structured replay items and context digest remain attached. All
other Providers use the bounded structured transcript replay. The current default adapters do not
declare native continuation support, so they use the replay path.

## Verification

The targeted suite covers Loop task/Tool/approval state in snapshots, evidence coverage, standalone
request isolation, native-vs-fallback continuation metadata, and Conversation/Space summary
isolation. It was checked with:

```text
UV_CACHE_DIR=tmp/uv-cache uv run pytest -q tests/unit/test_agent_loop_context.py \
  tests/unit/test_assistant_context.py tests/unit/test_assistant_agent.py \
  tests/unit/test_assistant_commands.py tests/unit/test_agent_loop_executor.py
UV_CACHE_DIR=tmp/uv-cache uv run ruff check ...
UV_CACHE_DIR=tmp/uv-cache uv run ruff format --check ...
UV_CACHE_DIR=tmp/uv-cache uv run mypy packages/domain/src/domain \
  packages/model_gateway/src/model_gateway packages/application/src/application/assistant \
  tests/unit/test_agent_loop_context.py
```

The test run passed 24 tests. The managed environment emitted a `.pytest_cache` permission warning;
it did not affect test execution. No formal holdout was run.
