# Agent Loop Step 9: Quality, Safety, And Gradual Rollout

## Implemented Scope

- `scripts/evaluate_agent_loop.py` validates the hash-pinned, synthetic-only
  `agent-loop-v1` development fixture. It never executes a Provider, reads controlled corpus content,
  accepts request/prompt/answer/Tool payload fields, or enables formal evaluation.
- Prediction metadata is constrained to safe actions, counters, coverage values, bounded stop reasons,
  token/latency, recovery booleans, and the fixture's fixed security assertions. The
  `agent-loop-metrics-report-v1` report contains no case IDs or content and has explicit denominators
  for goal/subquestion/evidence/citation coverage, unsupported claims, correct refusals, Tool
  selection/repetition, approval/security checks, recovery, tokens, latency, and stop reasons.
- `knowledge_agent 0.5.0` with `AGENT_LOOP_V5_ENABLED=true` is the default fake/local provisional
  path. Set the flag to `false` to roll new Runs back to `0.3.0`; rollback is configuration-only and
  preserves Run pins, durable event history, and v1/v2 projections.
- Existing Domain state machine, Tool safety, Loop recovery, v3 event order/redaction, Web timeline,
  and isolated PostgreSQL/Redis integration coverage remain part of the regression boundary. The new
  evaluator extends those checks with a deterministic synthetic development report.

## Verification Boundary

Use `uv run --frozen python scripts/evaluate_agent_loop.py --validate-only` to verify the fixture.
The command prints `formal_run_eligible=false`; prediction reports are always `development` and
`provisional`. Do not create a prediction file containing user text, prompts, answers, citations,
document content, raw Tool output, credentials, or internal resource IDs.

This step does not run or authorize the current formal holdout. Under ADR-010 and ADR-011, retrieval,
answer, and Skill quality remain provisional until a new representative dataset/config completes the
independent formal process.
