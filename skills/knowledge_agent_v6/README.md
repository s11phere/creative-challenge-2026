# knowledge_agent 0.6.0

This provisional package keeps the existing generic Agent Loop and its five server-registered
knowledge Tools: `knowledge_search`, `knowledge_inspect`, `grounded_answer`, `verify_answer`, and
`finalize_answer`. It adds prompt and server-side retrieval gates: a search and coverage inspection
must precede Grounded QA; weak coverage permits one follow-up search and inspection; verification
and finalization remain mandatory before the Loop terminates. The seven-call budget covers that
bounded sequence.

`knowledge_agent 0.6.0` is the default for new Runs. `0.5.0` remains installed unchanged for
historical Run recovery and rollback. Source text stays within Grounded QA; the Loop receives only
untrusted metadata and never publishes a second user-visible answer.

Use `uv run --frozen python scripts/evaluate_agent_loop.py --validate-only` before a fake/local
rollout. This package is provisional engineering work and does not close the retrieval, answer, or
Skill formal quality gates.
