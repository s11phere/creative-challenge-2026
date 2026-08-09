# Agent Loop Tools Effort: Step 5

> Status: provisional engineering implementation
> Date: 2026-08-09

Step 5 adds an opt-in `knowledge_agent 0.5.0` package for the generic Agent Loop. The existing
`knowledge_agent 0.3.0` default and the installed 0.1-0.4 packages remain unchanged for new-run
compatibility, rollback, and historical recovery.

## Tool Boundary

The Application-layer `KnowledgeLoopTools` adapter registers five model-visible, read-only Tools:

- `knowledge_search` calls only `SearchService.search(SearchRequest, RetrievalProfileV1)` with the
  server-fixed Space and `QARetrievalScope`. It returns counts, identifiers, source/version
  metadata, and the retrieval profile version; it never returns `SearchHit.text`.
- `knowledge_inspect` aggregates only safe observations collected by earlier searches and emits gap
  signals such as no matched evidence or single-source coverage.
- `grounded_answer` delegates to the current QA Run through `GroundedQAApplicationPort.execute`.
  Query hints are bounded by the trusted QA planning profile; QA remains responsible for context,
  Evidence, claims, citations, and publication.
- `verify_answer` checks only the current Run's structured result metadata. It accepts a grounded
  answer, insufficient-evidence refusal, or conflict as terminal business outcomes; runtime QA
  failures remain dependency failures.
- `finalize_answer` is a gate signal. The generic Loop finalizer checks that verification completed
  and that the model's terminal action matches the QA outcome. It emits no answer body and publishes
  no second Assistant message.

The Worker uses `AgentLoopExecutor` only when the fixed Skill version is `0.5.0`; the active default
continues to use the existing deterministic adapter. A deterministic fake-provider wrapper drives
the five-step synthetic sequence for local development only.

## Verification

Focused tests cover SearchService-only access, fixed Space and retrieval filters, source-text
redaction, current-Run verification, one QA delegation, finalization-only publication, insufficient
evidence refusal, and conflict terminal behavior.

```text
uv run pytest -q tests/unit/test_knowledge_loop_tools.py \
  tests/unit/test_agent_loop_executor.py tests/contract/test_tool_registry_contract.py
uv run ruff format --check packages/application/src/application/skills/knowledge_loop.py \
  packages/infrastructure/src/infrastructure/qa_execution.py tests/unit/test_knowledge_loop_tools.py
uv run ruff check packages/application/src/application/skills/knowledge_loop.py \
  packages/infrastructure/src/infrastructure/qa_execution.py tests/unit/test_knowledge_loop_tools.py
uv run mypy packages/application/src/application/skills/knowledge_loop.py \
  packages/infrastructure/src/infrastructure/qa_execution.py tests/unit/test_knowledge_loop_tools.py
```

These checks are provisional contract evidence. No formal retrieval, answer, or Skill holdout was
run, and no quality acceptance is claimed.
