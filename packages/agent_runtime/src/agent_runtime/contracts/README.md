# Agent Runtime Contracts

These JSON Schemas are the frozen provisional contracts for the generic Agent Loop. Model output
is untrusted intent; server-side policy, Space resolution, approval, checkpoint, lease, and finalizer
logic remain authoritative. Event payloads are summaries only and must not contain prompts, answer
bodies, document excerpts, credentials, or complete command output.

The contracts are independently versioned so the existing Assistant v1/v2 and QA projections can
remain readable during staged rollout. Incompatible changes require a new schema version; compatible
additions update the frozen manifest hash.
