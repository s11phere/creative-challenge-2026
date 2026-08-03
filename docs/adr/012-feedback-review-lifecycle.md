# ADR-012: Feedback Review Lifecycle and Candidate Export

## Status

Accepted for the Stage 4/5 engineering implementation. Formal evaluation remains provisional.

## Context

QA feedback is collected against a published Run/Attempt/answer message. A submitted vote may
contain private notes and must not be promoted directly into an evaluation dataset. The repository
needs a durable review decision, reviewer identity, authorization and redaction attestations,
approved Evidence identities, and a gold-answer digest without exposing question, answer, note,
prompt, provider output, or source excerpts through the review API.

## Decision

- Keep feedback submission and review on the existing `qa_feedback` identity and lifecycle.
- Persist review metadata in the same row with a forward-only Alembic migration.
- Permit exactly one transition from `pending_review` to `accepted` or `rejected`; replaying the
  same decision is idempotent, while a conflicting second decision returns a contract conflict.
- An accepted review requires reviewer identity, authorization confirmation, redaction completion,
  expected behavior, and a gold digest for answer candidates. A rejected review requires a reason.
- Scope every read and write by Space. Review responses are metadata-only and never include user or
  source text.
- Candidate export is a separate, explicit command. It only writes a new development JSONL file
  after the exporter validates accepted review metadata and `public_demo`/`repository_fixture`
  Evidence policy. Frozen datasets, manifests, and holdout files are never modified.

## Consequences

Review state survives API/Worker restarts and can be audited by reviewer identity. The additional
columns preserve compatibility with existing pending feedback. The export command is intentionally
conservative: missing or withdrawn Evidence causes the candidate to be rejected rather than copied.
Formal answer quality and holdout acceptance still require a separately versioned, authorized run.

## Re-evaluation Triggers

Revisit this ADR if authentication/roles are introduced, feedback becomes cross-Space, review
decisions need amendment history, candidate export includes non-metadata content, or the evaluation
dataset publication workflow changes.
