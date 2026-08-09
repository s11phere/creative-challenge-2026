# knowledge_agent 0.5.0

This default provisional package runs the generic Agent Loop over five server-registered knowledge
Tools: `knowledge_search`, `knowledge_inspect`, `grounded_answer`, `verify_answer`, and
`finalize_answer`. New Runs use `knowledge_agent 0.5.0`; versions 0.1 through 0.4 remain installed
for recovery and rollback. Setting `AGENT_LOOP_V5_ENABLED=false` fails closed to `0.3.0` for future
Runs. Recreate API/Worker after the change; existing Run pins and v1/v2 projections remain unchanged.

Search returns only untrusted counts and identifiers. Source text stays inside the existing
Grounded QA context-builder and generator path. The Grounded QA Application Port owns Evidence,
citations, refusal semantics, persistence, and the single user-visible Assistant publication. The
Loop finalizer only emits a safe routing projection after verification; it does not publish a second
message.

Use `uv run --frozen python scripts/evaluate_agent_loop.py --validate-only` before a fake/local
rollout to verify the hash-pinned synthetic fixture. This package is development/provisional
engineering work. It does not close the retrieval, answer, or Skill quality gates and must not be
used to claim a formal quality result.
