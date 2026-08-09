# knowledge_agent 0.5.0

This opt-in provisional package runs the generic Agent Loop over five server-registered knowledge
Tools: `knowledge_search`, `knowledge_inspect`, `grounded_answer`, `verify_answer`, and
`finalize_answer`. It is not the default active version; `knowledge_agent 0.3.0` remains active,
and versions 0.1 through 0.4 remain installed for recovery and rollback.

Search returns only untrusted counts and identifiers. Source text stays inside the existing
Grounded QA context-builder and generator path. The Grounded QA Application Port owns Evidence,
citations, refusal semantics, persistence, and the single user-visible Assistant publication. The
Loop finalizer only emits a safe routing projection after verification; it does not publish a second
message.

This package is development/provisional engineering work. It does not close the retrieval, answer,
or Skill quality gates and must not be used to claim a formal quality result.
