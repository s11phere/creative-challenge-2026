# Agent Loop Step 6: Effort And Capability Mapping

## Implemented Scope

- `ConversationRecord` persists the default provider-neutral effort preference:
  `auto`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`.
- `/effort` reads that preference; `/effort <value>` updates it without changing historical Runs.
- Each newly created Assistant or context-compaction `ConversationRun` captures a
  `reasoning-profile-v1`: requested/effective effort, Provider, model, mapping version, mode, and
  downgrade reason. The v2 Run response exposes only this safe metadata.
- `ModelCapabilityRegistry` maps a Provider/model to native, coarse boolean-thinking, or disabled
  reasoning. Explicit intensity requests fail closed when unsupported. Only `auto` can map to
  disabled reasoning with a stable downgrade reason.
- `deepseek-v4-flash` is an exact `OpenAICompatibleGateway` capability: it receives the native
  Chat Completion `reasoning_effort` field with `thinking` enabled and records
  `reasoning-mapping-v2`/`native`. Its documented `xhigh -> high` provider mapping is recorded in
  `effective_effort`; the configured endpoint has also been verified to accept `medium` and return
  non-empty `reasoning_content`, although `medium` is not listed in the current public Chat
  Completion parameter table. Other OpenAI-compatible models retain boolean `thinking`/`coarse`
  mapping.
- Provider `reasoning_content` is transient and is not persisted, logged, included in SSE, or
  exposed to the Web client.
- The existing `fast_chat_reasoning_enabled` setting is retained. When it is false, an `auto`
  preference remains disabled for the current OpenAI-compatible Chat path.

## Persistence And Migration

`0a1b2c3d4e5f_add_conversation_reasoning_profiles` adds `conversations.reasoning_effort` and
`conversation_runs.reasoning_profile`. Upgrade backfills legacy Runs with an explicit, disabled
profile. Downgrade refuses to discard a changed preference or mapped profile.

## Quality Boundary

This is provisional orchestration and auditability work only. It does not run or close any formal
retrieval, answer, or Skill quality gate, and it does not authorize a formal holdout evaluation.
