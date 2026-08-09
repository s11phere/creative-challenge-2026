# Agent Loop Tools Effort: Step 1

> Status: provisional engineering implementation
> Date: 2026-08-09

Step 1 adds a recoverable generic Loop without replacing the existing deterministic Skill
workflow. The implementation is intentionally behind the current v1/v2 default so historical Run
identities and the active `knowledge_agent 0.3.0` package remain unchanged.

## Domain and ports

- `domain.agent_loop.AgentLoopState` models `accepted`, `planning`, `tool_requested`,
  `waiting_approval`, `tool_running`, `observing`, `finalizing`, and terminal phases.
- The state records the server-authored goal/subquestions, iteration records, completion check,
  stop reason, redacted Tool observations, pending approval request, lease identity, and request
  fingerprints.
- `agent_runtime.AgentLoopExecutor` reuses `AgentRun`, `ToolRegistry`, `RuntimeStateStore`,
  `ModelGateway`, and the existing finalizer boundary. Model output remains untrusted intent.

## Safety and recovery

- The server emergency ceiling is independent of Skill prompt text; total Run budget, timeout,
  cancellation, Tool retry policy, and idempotency remain enforced.
- Repeated Tool request fingerprints fail with `RUN_LLM_NO_PROGRESS`.
- Every successful observation and approval wait writes a verified checkpoint. Recovery validates
  caller, Space, fixed Skill identity, state digest, approval identity, lease identity, and
  idempotency metadata, then resumes a pending Tool request rather than issuing a new one.
- Finalization transitions through `RunEvent.FINALIZE`; after the finalizer returns, the state is
  published and the Run reaches the existing `completed` state. No Tool is called after that gate.
- `knowledge_agent_v4` contains the dynamic goal/coverage prompt but is opt-in; `0.3.0` remains the
  active default until a later rollout decision.

## Verification

The Step 1 tests cover:

- two Tool observations followed by one finalizer publication;
- duplicate arguments and no-progress termination;
- durable approval pause, checkpoint persistence, and pending Tool recovery;
- existing Runtime domain/checkpoint/LLM behavior and Skill/API compatibility.

The local targeted suite passed 36 tests, `mypy apps packages` passed, and the modified files pass
Ruff checks. Results remain provisional and no formal retrieval or answer holdout was run.
