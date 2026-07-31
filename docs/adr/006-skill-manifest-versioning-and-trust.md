# ADR-006: Skill Manifest Versioning And Trust Model

- Status: Accepted
- Date: 2026-07-19

## Context

Stage 5 packages bounded Agent Runtime workflows as versioned Skills. A Skill is a
workflow package, not a prompt file, and must be callable through Web, HTTP API, and
tests without creating separate semantics. The repository currently has the runtime
boundary decision in ADR-003, but has no authoritative rules for Skill package shape,
content identity, activation, permissions, or recovery.

The stage 0 data gate is still open and the stage 2-4 ingestion, retrieval, citation,
and grounded-answer application ports are not implemented in this checkout. Stage 5
therefore needs contracts that can be validated with deterministic fakes without
claiming that `knowledge_qa` is production-ready.

## Decision

### Package and manifest

Each Skill is an immutable directory under a configured trusted root. The minimum
package contains `skill.yaml`, workflow definition, prompts, input/output schemas,
evaluation fixtures, and a README. The manifest is declarative and must include:

`manifest_version`, `name`, `version`, `description`, `input_schema`, `output_schema`,
`required_tools`, `required_capabilities`, `permissions`, `budgets`, `entrypoint`,
`compatibility`, `prompts`, and `evals`.

Unknown fields are rejected unless a future manifest version explicitly defines an
extension rule. JSON Schema references may resolve only to files inside the same Skill
package. Remote references, absolute paths, parent traversal, symbolic links, and
junctions are rejected.

Production loading reads only the configured trusted root and only handlers explicitly
registered by application startup. A manifest cannot import arbitrary Python, execute
shell/SQL, fetch URLs, or select an unregistered handler. The initial implementation
may use a declaration-only workflow or a repository-owned handler allowlist.

An LLM decision node, when enabled by an application-owned handler, may call only the
provider-neutral `ModelGateway.fast_chat` capability and must return the versioned
`LLMDecision` schema. The allowed actions are `call_tool`, `complete`, and `refuse`.
The node validates JSON and the server-side Tool allowlist; it never invokes a Tool
itself. Tool Registry permission, Space, budget, approval, idempotency, and output
validation remain mandatory before any side effect. User and document content is
untrusted prompt input and cannot change the system instruction or allowlist.

### Identity, compatibility, and activation

The immutable registration identity is `(name, version, content_sha256)`, where the
digest is computed over a canonical, sorted file listing and normalized UTF-8 file
bytes, excluding transient files. Registering the same identity is idempotent.
Registering the same name and version with a different digest is a hard conflict and
must never overwrite the existing package.

Installed versions and the active version are separate records. Activating a version is
an atomic pointer change and affects only runs created afterwards. An AgentRun captures
the complete Skill identity, manifest version, content digest, workflow/prompt/schema
digests, and compatibility metadata at creation time. Updating the active pointer
never changes an existing run.

An incompatible change requires a new semantic version and an explicit compatibility
range. A compatible patch may be activated only when its manifest and handler contract
pass validation. Removing an installed version is refused while any run, checkpoint, or
audit record references it; an implementation may archive it according to retention
policy instead.

### Trust, permissions, and writes

Permissions come from the server-side Skill registration and caller/Space context;
neither model output nor document content can grant permissions. Tools are selected
from a server-side allowlist and are invoked through Application/Domain ports. Every
write-capable tool requires a durable approval request, an idempotency key, and an
ownership check before side effects. A rejected or expired approval has no side effect.
External network and model capabilities are denied unless explicitly declared by the
Skill and allowed by deployment policy (including the existing model gateway policy).

### Errors, events, and recovery

Stable error codes use the prefixes `SKILL_`, `TOOL_`, `RUN_`, `AUTH_`, and `DEPENDENCY_`.
Validation, compatibility, permission, and schema errors are non-retryable. Bounded
provider, queue, or transient dependency failures may be retried under the run budget;
evidence insufficiency is a normal grounded-answer refusal, not an infrastructure
failure. API and internal runtime events carry an explicit `event_version` (initially
`1`) and unknown future event versions are rejected or safely ignored according to the
transport contract; they are never silently reinterpreted.

Recovery resumes only from the latest verified checkpoint. Recovery revalidates caller,
Space, Skill digest, checkpoint schema, approval context, lease, and remaining budget.
Completed tool effects are identified by their idempotency key and are not repeated.
State transitions, tool calls, approvals, checkpoints, and terminal errors record only
redacted identifiers, summaries, counts, timing, and stable codes; full documents,
prompts, model responses, credentials, and embeddings are not written to audit logs.

## Cross-stage contract boundary

Stage 5 may depend on formal Application ports for published document versions,
retrieval results, grounded answers, citations/evidence, conversations, runs, and SSE
events once stages 2-4 provide them. It must not read their ORM tables directly or
reimplement retrieval, context construction, generation, refusal, or citation
validation. In the current checkout those stage 3/4 ports and entities are absent;
contract tests must use deterministic fakes until the upstream stages land. This
limitation is recorded in `docs/stage-5-contract-audit.md`.

## Alternatives

- Treating a Skill as a prompt plus arbitrary code was rejected because it permits
  supply-chain execution and makes permissions and recovery unverifiable.
- Overwriting an active package in place was rejected because running work would become
  irreproducible.
- Allowing remote schema or handler references was rejected because they bypass the
  trusted-root and offline-first boundary.
- Defining a second answer or event protocol in Stage 5 was rejected because Web, API,
  and tests must share the upstream Application contracts.

## Consequences

The first registry and runtime can be implemented and tested without PostgreSQL,
Redis, or upstream knowledge services. They must carry immutable Skill identity and
redacted audit data through every run. Stage 5 business Skills remain blocked until
stages 2-4 publish the referenced Application ports and the stage 0 data gate closes.
Future manifest extensions, signature requirements, or a different execution sandbox
require an ADR update and compatibility policy.

## Reassessment Triggers

Reassess when untrusted third-party Skills, remote distribution, signature verification,
parallel agents, cross-Space collaboration, or a runtime sandbox becomes a real P0
requirement; when event versioning cannot remain backward-compatible; or when upstream
Application contracts require a different run/checkpoint identity.
