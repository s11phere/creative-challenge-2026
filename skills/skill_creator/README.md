# skill_creator

Agent-guided creation and iteration of personal Skills (Phase 4, Path A).

This is a **prompts-only** Skill: its workflow is a nominal single node and its
instructions tell the assistant loop how to drive the Skill Creator Tools
(`skill_scaffold` / `skill_write` / `skill_validate` / `skill_run_eval` /
`skill_activate` / `skill_draft`). Personal Skills it produces are low-trust and
only compose existing handlers/tools (ADR-018).
