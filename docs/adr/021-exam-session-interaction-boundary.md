# ADR-021: Exam Session interaction and answer-isolation boundary

Status: Accepted (development/provisional)
Date: 2026-08-13

## Context

Interactive preparation pauses for several user submissions, while Harness v2 Runs are short and
have no generic waiting-user-input state. Questions, student answers and rubrics are too sensitive
and too large for the redacted v4 timeline. Model-generated JSON also cannot own authorization or
scoring decisions.

## Decision

Persist an `ExamSession` across multiple `ConversationRun` actions. Store public papers and private
answer keys separately, store answers only in private submissions, and keep a body-free action
history for recovery. Use a versioned interaction/action API and render cards in the existing
conversation page. Require interaction ID, locked paper version, owner/Space and idempotency checks.
Generate artifacts only through a schema-bound native model Tool after `SearchService.search`;
allow one repair, then fail closed. Score objective answers on the server. Subjective results remain
suggestions with rubric, confidence and human-review flags; code is never executed.

## Alternatives

Keeping one Run open would require a new generic runtime state and complicate leases and recovery.
Putting forms in assistant Markdown would not provide reliable submissions or answer isolation.
Letting the model return final public JSON would make authorization and leakage model-controlled.

## Consequences

The Web and API gain an exam-specific projection, but QA retrieval, citations, Worker execution and
durable approval remain shared. The slice is locally demonstrable and restart-safe after database
migration. It does not change ADR-010/011 quality gates and remains development/provisional.

## Re-evaluate when

Revisit if Harness gains a first-class durable user-interaction primitive, or after formal retrieval
and Skill quality gates close.
