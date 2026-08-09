# Agent Loop Tools Effort: Step 4

> Status: provisional engineering implementation
> Date: 2026-08-09

Step 4 adds opt-in `fs_write 1.0.0` and `shell_exec 1.0.0` Runtime Tool definitions. Neither Tool
is registered by the default read-only registry, exposed through a default Skill, or wired to a
user-facing command. This is a safety boundary implementation only; it does not grant write,
installation, network, or process-control access to a client or model.

## Approval and Idempotency

`WRITE_KNOWLEDGE` and the new explicit `EXECUTE_PROCESS` permission both require a durable
approval. The Registry validates the approval again immediately before an invocation. When the
durable PostgreSQL adapter is used, approval is bound to Run, Space, caller, Tool name/version,
idempotency key, and the digested input identity. Rejected, revoked, expired, mismatched, or absent
approvals fail before the handler starts.

Successful results are cached by `(run_id, idempotency_key)`. An async key lock prevents two
concurrent successful deliveries from executing the same side effect twice in one Runtime process;
a conflicting reuse fails closed. The pending Tool remains in the existing Loop checkpoint and both
write and process permissions move the Run to `waiting_approval` before execution.

## Filesystem Writes

`FileWritePolicy` accepts only configured absolute non-link roots plus an explicit per-Space list
of `WritableFile` targets. A write rejects traversal, device paths, cross-Space targets,
symlinks/junctions, missing trusted parent directories, non-regular targets, oversized UTF-8
content, and unsafe target changes. It always rejects `.env`, credential/secret material, private
key/certificate files, system-prompt targets, and Skill-package paths, including when a policy
mistakenly allowlists one.

Writes use a same-directory temporary file, flush and fsync it, revalidate the target and optional
expected SHA-256, then use `os.replace` for an atomic replacement. Temporary files are removed on
failure. The model-visible result contains only an untrusted marker, logical path, byte count, and
content digest; Registry audit records retain digests only.

## Command Execution

`ShellExecutionPolicy` maps a simple executable alias to a fixed absolute regular-file path and
maps a Space to explicit trusted working directories. `shell_exec` accepts only an argv array; it
never invokes a shell, accepts no caller-supplied environment, and starts with the small
policy-owned environment. Its definition does not carry `EXTERNAL_NETWORK`; therefore a network
grant is not available through this Tool. Deployment-level sandboxing/firewall remains responsible
for enforcing that capability at the operating-system boundary.

Standard output and error are streamed with bounded capture, decoded as untrusted text, and
line-ending-normalized for the contract. The process receives no stdin. Registry timeout or runtime
cancellation terminates a running process, and logs/checkpoints retain output digests rather than
complete command output.

## Verification

Focused tests cover approval denial and exact invocation identity, protected write targets, target
digest races, atomic idempotent concurrent retries, executable/cwd allowlists, shell-free argv
execution, output truncation, timeout cleanup, cancellation, and the `waiting_approval` Runtime
transition for `EXECUTE_PROCESS`.

```text
UV_CACHE_DIR=tmp/uv-cache uv run pytest -q tests/unit/test_side_effect_tools.py \
  tests/unit/test_agent_loop_executor.py tests/contract/test_tool_registry_contract.py \
  tests/unit/test_read_only_file_tools.py
UV_CACHE_DIR=tmp/uv-cache uv run ruff format --check .
UV_CACHE_DIR=tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=tmp/uv-cache uv run mypy apps packages
```

No formal retrieval, answer, or Skill holdout was run. The managed Windows environment may emit a
`.pytest_cache` permission warning; it does not affect test results.
