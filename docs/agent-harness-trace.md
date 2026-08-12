# Agent Harness Trace

`QA_DEBUG_TRACE_ENABLED=true` enables a development-local trace for one Assistant Run. It remains
disabled outside `APP_ENV=development`; no trace content is added to logs, SSE, HTTP responses, or
PostgreSQL.

Each top-level Agent Loop iteration records an `agent_round` event containing the complete Chat
request messages and the complete model response. Existing `tool_call` and `tool_result` events
retain the corresponding full Tool arguments and outputs.

Export a Run as a readable Markdown report:

```powershell
.venv\Scripts\python.exe scripts/export_agent_harness_trace.py --run-id <run-uuid>
```

The command reads only `QA_DEBUG_TRACE_PATH/<run-uuid>.jsonl` and writes by default to:

```text
QA_DEBUG_TRACE_PATH/exports/agent-harness-<run-uuid>.md
```

For the default local configuration this is `tmp/qa-debug/exports/`. Use `--output <path>` to
choose another file, and add `--force` only when intentionally replacing an existing export:

```powershell
.venv\Scripts\python.exe scripts/export_agent_harness_trace.py `
  --run-id <run-uuid> `
  --output tmp/qa-debug/exports/my-run.md
```

The report separates each Runtime round, model call phase, model input/output, and Tool input,
output, or error. Chat messages are shown by role and wrapped for normal-width screens; structured
Tool payloads remain indented JSON. The command prints only the final output path to standard output.
Each report is sensitive local diagnostic data: do not commit, upload, or paste it into tickets.
Delete traces and exports when the investigation is complete.

For a body-free Harness migration baseline, use the local trace only to calculate aggregate counts
and UTF-8 byte estimates. The command prints no prompt, answer, Tool argument, Tool result, or
identifier and never writes a report:

```powershell
.venv\Scripts\python.exe scripts/summarize_agent_harness_baseline.py --run-id <run-uuid>
```

The output is development-only and provisional. It is not a retrieval, answer, or Skill quality
evaluation, and must not be committed or added to normal logs, SSE, or PostgreSQL.

For native Harness v2, the executor records the same `agent_round` event key with a body-free
shape: `phase=native_tool_use`, `context_digest`, `visible_observation_bytes`, `cache_mode`, and
safe usage counters. The event contains no Chat messages, Tool arguments, Tool output, prompt,
answer, or provider text. The same rule applies to the separate `tool_call` events emitted by the
v2 executor; only stable Tool identity and digest summaries are retained.
