# knowledge_agent 0.9.0

This immutable provisional package extends the model-directed knowledge loop with an explicit
workspace-artifact handoff and one model-visible recovery observation for an accidental duplicate
Tool request. The model still chooses whether a local workspace operation advances the user's
request; the runtime only supplies approval, path, QA-result, and duplicate-request safety checks.

The package retains the existing QA Application Port, fixed Space/version boundaries, durable
runtime checkpoints, cancellation, permissions, and QA-owned citation/publication semantics.
Versions `0.7.0` and `0.8.0` remain installed for historical Run recovery and rollback.

The package is provisional. Run `uv run --frozen python scripts/evaluate_agent_loop.py
--validate-only` before local rollout; this does not close any formal retrieval, answer, or Skill
quality gate.
