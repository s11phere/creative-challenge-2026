# Assistant Agent Contracts v1

These files are the canonical, versioned contracts for the product-level Assistant Agent. They
are intentionally separate from individual Skill manifests and from the bounded Runtime
`LLMDecision` contract.

| Artifact | Purpose |
| --- | --- |
| `base-system-prompt-v1.txt` | Product-level routing and response boundary. |
| `router-decision-v1.schema.json` | Strict model intent for `respond`, `clarify`, or `invoke_skill`. |
| `command-catalog-v1.schema.json` | Safe metadata returned by the versioned command catalog. |
| `clarification-v1.schema.json` | Server-authored clarification and safe resource candidates. |
| `error-codes-v1.json` | Stable errors introduced by this evolution. |

The router is never authorized by these files to execute a Skill, use a Tool, select a resource
identity, expand a Space, or bypass policy. The Application layer validates every decision,
resolves resources in the current Space, pins an active Skill version, and dispatches any long
work through the existing Worker boundary.

Changing an artifact changes its SHA-256 in `manifest.json` and requires a new contract version.
The v1 contracts are frozen for the staged implementation described in
`docs/agent-conversation-evolution-plan.md`.

