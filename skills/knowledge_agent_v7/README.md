# knowledge_agent 0.7.0

This immutable provisional package replaces the rigid v0.6 retrieval state machine with a
model-directed Agent Loop. The model receives the trusted Skill prompt, registered Tool schemas,
and safe observations, then selects each next Tool call. `knowledge_search` and
`knowledge_inspect` return deterministic local `recommended_next` guidance; server-side checks
still prevent uninspected retrieval from reaching Grounded QA and prevent terminal publication
before verification.

The package retains the existing QA Application Port, fixed Space/version boundaries, durable
runtime checkpoints, cancellation, permissions, and QA-owned citation/publication semantics.
`0.5.0` and `0.6.0` remain unchanged for historical Run recovery and rollback.

The package is provisional. Run `uv run --frozen python scripts/evaluate_agent_loop.py
--validate-only` before local rollout; this does not close any formal retrieval, answer, or Skill
quality gate.
