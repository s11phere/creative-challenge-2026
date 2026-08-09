# Agent Loop Tools Effort: Step 3

> Status: provisional engineering implementation
> Date: 2026-08-09

Step 3 adds opt-in, read-only `fs_list 1.0.0` and `fs_read 1.0.0` Tools to the generic Runtime. It
does not add file writes, command execution, network access, a new default Skill, or any formal
retrieval/answer/Skill quality acceptance.

## Trust Boundary

`FileToolPolicy` receives only configured absolute workspace/trusted roots and an explicit manifest
allowlist keyed by runtime Space. `ManifestAllowedFile` pins a source key, root-relative path,
SHA-256, and sensitivity. The manifest loader accepts only entries with the requested allowed use;
it defaults to `public_demo` model visibility. `private_local` or `restricted` content requires an
explicit deployment-policy configuration before it can be offered to a model-visible Tool.

The policy rejects root or target symlinks/junctions, parent and device paths, unmanifested files,
cross-Space lookups, content hash drift, oversized files, malformed UTF-8, invalid paging offsets,
and cancellation. Listings contain only manifest entries and do not enumerate arbitrary root
contents. Reads return bounded UTF-8 content with source/digest/sensitivity metadata and a
`trust: untrusted` marker. Registry audit records contain only SHA-256 summaries.

## Runtime Behavior

`create_read_only_file_registry()` exposes only the two handlers, both requiring
`READ_KNOWLEDGE`, with no approval, write, shell, or external-network permission. Identical
successful invocations in one in-memory Runtime registry reuse the result for the same Run and
idempotency key; conflicting reuse fails closed. Generic Loop recovery/checkpoint behavior remains
owned by the existing Runtime and no user-facing message is published by these Tools.

## Verification

The targeted tests cover safe Agent Loop directory/read completion, manifest hash checks,
path/device/traversal isolation, root-link rejection, large file, malformed encoding, cancellation,
timeout, idempotency, conflicting idempotency keys, and cross-Space denial.

```text
UV_CACHE_DIR=tmp/uv-cache uv run pytest -q tests/unit/test_read_only_file_tools.py \
  tests/contract/test_tool_registry_contract.py tests/unit/test_agent_loop_executor.py \
  tests/unit/test_llm_decision.py
UV_CACHE_DIR=tmp/uv-cache uv run ruff check ...
UV_CACHE_DIR=tmp/uv-cache uv run ruff format --check ...
UV_CACHE_DIR=tmp/uv-cache uv run mypy packages/agent_runtime/src/agent_runtime \
  tests/unit/test_read_only_file_tools.py
```

The targeted run passed 37 tests. Where Windows does not allow test symlink creation, the same
policy branch is exercised through a junction/link detector double. The managed environment also
reports a `.pytest_cache` permission warning; it does not affect the test outcome. No formal holdout
was run.
