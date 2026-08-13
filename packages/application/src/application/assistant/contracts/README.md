# Assistant Agent Contracts v2

These files are the canonical, versioned contracts for the product-level Assistant Agent. They
are intentionally separate from individual Skill manifests and from the bounded Runtime
native Tool-use v2 contract.

| Artifact | Purpose |
| --- | --- |
| `base-system-prompt-v8.txt` | Current native Tool-use prompt and bounded Assistant context. |
| `command-catalog-v1.schema.json` | Safe metadata returned by the versioned command catalog. |
| `clarification-v1.schema.json` | Server-authored clarification and safe resource candidates. |
| `error-codes-v1.json` | Stable errors introduced by this evolution. |

Native Tool-use is never authorized by these files to select a resource identity, expand a Space,
or bypass policy. The Application layer validates every Tool call, pins an active Skill version,
and dispatches long work through the existing Worker boundary.

The frozen current artifacts remain pinned in `manifest.json`. The current prompt identity is
`assistant-base-prompt-v8`; older prompt revisions are intentionally unsupported.
`docs/agent-conversation-evolution-plan.md`.
