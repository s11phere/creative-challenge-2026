# Assistant Agent Contracts v1

These files are the canonical, versioned contracts for the product-level Assistant Agent. They
are intentionally separate from individual Skill manifests and from the bounded Runtime
`LLMDecision` contract.

| Artifact | Purpose |
| --- | --- |
| `base-system-prompt-v1.txt` | Product-level routing and response boundary. |
| `base-system-prompt-v2.txt` | Historical router prompt retained for persisted v2 Runs. |
| `base-system-prompt-v3.txt` | Historical router prompt retained for persisted v3 Runs. |
| `base-system-prompt-v4.txt` | Historical router prompt; defaults to ordinary conversation and requires an explicit current-Space knowledge dependency for `knowledge_agent`. |
| `base-system-prompt-v5.txt` | Historical autonomous-loop prompt retained for fixed Runs. |
| `base-system-prompt-v6.txt` | Historical autonomous-loop prompt retained for fixed Runs. |
| `base-system-prompt-v7.txt` | Current autonomous-loop prompt; models every compound deliverable, requires a final postcondition check, treats workspace artifacts as part of the request, and explains one accidental duplicate Tool recovery without prescribing a fixed Tool sequence. |
| `router-decision-v1.schema.json` | Strict model intent for `respond`, `clarify`, or `invoke_skill`. |
| `command-catalog-v1.schema.json` | Safe metadata returned by the versioned command catalog. |
| `clarification-v1.schema.json` | Server-authored clarification and safe resource candidates. |
| `error-codes-v1.json` | Stable errors introduced by this evolution. |

The router is never authorized by these files to execute a Skill, use a Tool, select a resource
identity, expand a Space, or bypass policy. The Application layer validates every decision,
resolves resources in the current Space, pins an active Skill version, and dispatches any long
work through the existing Worker boundary.

The frozen v1 artifacts remain pinned in `manifest.json`. Router prompt revisions are separately
versioned in each ConversationRun's `core_prompt_version`; historical prompt files remain readable.
The v1 contracts are frozen for the staged implementation described in
`docs/agent-conversation-evolution-plan.md`.
