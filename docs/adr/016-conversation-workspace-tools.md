# ADR-016: Conversation Workspace Tools

- Status: Accepted
- Date: 2026-08-11
- Scope: Provisional local-development capability. ADR-010 and ADR-011 continue to govern formal
  retrieval, answer, and Skill quality acceptance.

## Context

The Agent Runtime already provided bounded `fs_list`, `fs_read`, `fs_write`, and `shell_exec`
Tools, but the normal Assistant Loop did not register them. Exposing host paths directly would make
conversation data non-portable, allow path confusion, and risk sending local file contents to an
external model. A write or command decision must also remain bound to the existing durable approval
and checkpoint contracts.

## Decision

1. A Conversation stores an optional logical workspace path below one configured
   `AGENT_WORKSPACE_ROOT_PATH`. The API resolves a user selection before storing it; the Worker
   resolves it again for every Run. Absolute client paths are never persisted.
2. A selected workspace must already exist below the configured root. Root containment, traversal,
   symlinks, and Windows junctions are rejected. The workspace cannot change while the Conversation
   has an active Run.
3. The `workspace` field in the model input explicitly states whether Tools are available, the
   logical path, the relative-path convention, and the allowlisted command aliases. `fs_list`,
   `fs_read`, `fs_write`, and `shell_exec` interpret file paths and command cwd values relative to
   that root; the legacy `workspace/...` form remains accepted for Tool compatibility.
4. The Worker registers these Tools for `MODEL_PROVIDER=fake` without additional consent. Any
   non-fake Chat Provider requires the explicit `AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`
   setting. `start-local.ps1 -AllowExternalWorkspaceTools` is a one-process opt-in and leaves the
   default false. The opt-in means selected workspace content may be supplied to that Provider.
5. Read Tools are bounded, return untrusted output, and reject links. Writes remain atomic and
   reject protected configuration, credential, prompt, and Skill-package paths. Commands use only
   configured executable aliases, argv execution without a shell, bounded output, cancellation,
   and a workspace-scoped cwd.
6. Writes and commands enter `WAITING_APPROVAL`. The Runtime records the exact invocation digest in
   a durable approval request and checkpoint. The v2 approval endpoints decide a request only after
   confirming it belongs to the specified Run; approval resumes the same Run, while rejection
   cancels the pending Run without executing the Tool. The Web timeline can make that decision
   directly. An approved decision with `always_allow=true` additionally records the Tool name in
   the current Conversation. Later side-effect invocations of that Tool name in the same
   Conversation bypass the approval wait, but retain all workspace, protected-path, command-alias,
   schema, cancellation, idempotency, and Runtime permission checks. A new Conversation does not
   inherit this allowance.
7. API and Worker deployments must mount the same workspace root. The Compose configuration binds
   `AGENT_WORKSPACE_HOST_PATH` to `/data/agent-workspaces` in both containers.
8. The Agent timeline displays a filesystem Tool's target path and a command Tool's argv-rendered
   command plus relative cwd. It stores only bounded, control-character-sanitized previews of
   `shell_exec` stdout/stderr and `fs_list` entries in the v3 event stream. Previews are limited to
   4,000 characters, signal truncation, and are expandable in the UI; `fs_read` and `fs_write` do
   not expose file content in this display channel.
9. The local workspace and the fixed current-Space knowledge corpus remain separate data scopes.
   A workspace listing never determines whether uploaded knowledge is present. Prompted Agent
   planning, rather than a server-side intent matcher, determines whether retrieval, listing, and
   writing are needed for a compound request. When a user asks to save the verified QA result, the
   Agent chooses the target path and explicitly calls the existing approval-gated `fs_write` Tool;
   the runtime resolves a dedicated result marker to the server-owned answer text only for that
   selected write. Before terminal completion, the Assistant harness checks the successful QA and
   write observations for that explicit request; if QA is complete but the artifact is absent, it
   may inspect `.` and then call the normal approval-gated write Tool. This is a completion
   postcondition, not a fixed retrieval plan or an authorization path. No default output filename
   is imposed. The display-only workspace name is not a Tool cwd; `.` is the selected workspace
   root.

## Alternatives

- Expose arbitrary host paths to the model: rejected because it cannot enforce a stable local-data
  boundary or make relative paths meaningful across API and Worker processes.
- Register the Tools for all providers without consent: rejected because external-provider requests
  could contain local workspace content without the required policy and visible consent checks.
- Add a parallel workspace execution service: rejected because it would duplicate existing Run,
  SSE, cancellation, approval, and recovery semantics.

## Consequences

Local fake-model sessions can select a folder through `/workspace <folder>` or the conversation
workspace API, then let the Agent inspect files and request approved changes or commands. A
non-fake session needs the explicit visibility consent above. The API and Worker require a shared
mount, and an unavailable persisted workspace fails closed by omitting the Tools. The capability is
an engineering workflow feature, not a formal quality acceptance. A Conversation-level allowance
improves repeated local workflow actions but is not a blanket shell grant or a cross-Conversation
permission. The bounded output preview makes execution inspectable in the UI without creating an
unbounded command log or exposing file-read content through events.

## Reassessment Triggers

Reassess before changing the consent policy, widening a Conversation-level allowance beyond a Tool
name, adding a command alias with a broader data or network boundary, allowing a workspace outside
the configured root, increasing preview retention, or changing the approval identity, cancellation,
or checkpoint protocol.
