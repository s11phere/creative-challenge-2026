# Assistant Agent Contracts v1

These files are the canonical, versioned contracts for the product-level Assistant Agent. They
are intentionally separate from individual Skill manifests and from the bounded Runtime
`LLMDecision` contract.

| Artifact | Purpose |
| --- | --- |
| `base-system-prompt-v7.txt` | Current autonomous-loop prompt; models every compound deliverable, requires a final postcondition check, treats workspace artifacts as part of the request, and explains one accidental duplicate Tool recovery without prescribing a fixed Tool sequence. |
| `router-decision-v1.schema.json` | Strict model intent for `respond`, `clarify`, or `invoke_skill`. |
| `command-catalog-v1.schema.json` | Safe metadata returned by the versioned command catalog. |
| `clarification-v1.schema.json` | Server-authored clarification and safe resource candidates. |
| `error-codes-v1.json` | Stable errors introduced by this evolution. |

The router is never authorized by these files to execute a Skill, use a Tool, select a resource
identity, expand a Space, or bypass policy. The Application layer validates every decision,
resolves resources in the current Space, pins an active Skill version, and dispatches any long
work through the existing Worker boundary.

The frozen current artifacts remain pinned in `manifest.json`. The current prompt identity is
`assistant-base-prompt-v7`; older prompt revisions are intentionally unsupported.
`docs/agent-conversation-evolution-plan.md`.
