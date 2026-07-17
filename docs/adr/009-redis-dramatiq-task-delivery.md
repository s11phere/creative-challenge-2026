# ADR-009: Redis And Dramatiq For Task Delivery

- Status: Accepted
- Date: 2026-07-17

## Context

The independent Worker needs durable delivery, bounded retries, time limits, and traceable task
metadata. Long-running ingestion state must survive queue cleanup and cannot depend on a broker's
result backend. The first release does not need workflow canvases or a second queue implementation.

## Decision

Use Redis as the broker and Dramatiq as the only task delivery library. Queue messages contain
structured identifiers and control metadata, including `task_id`, `trace_id`, and `event_version`,
but never document bodies. PostgreSQL is the source of truth for persistent task state and idempotency;
Redis and Dramatiq only deliver work.

Workers use explicit timeouts and bounded retries. Cancellation is cooperative and based on persistent
task state rather than deleting broker messages. Stage 1 implements only a body-free diagnostic task;
the stage 2 data model defines durable ingestion task transitions.

## Alternatives

- Celery was deferred because its broader workflow and result-backend features are not required for the
  initial bounded Worker, while it adds configuration and operational surface.
- In-process background tasks were rejected because they do not survive API process restarts and cannot
  provide the required independent Worker boundary.
- Redis task state was rejected because broker cleanup or eviction must not erase business state.

## Consequences

The project operates one broker and one queue library. Application code must not treat a successful
enqueue as a completed state transition. Database and broker writes cannot be assumed atomic, so later
business tasks require idempotent consumers and a recoverable dispatch pattern.

## Reassessment Triggers

Reassess if measured requirements need complex workflow primitives, broker portability, scheduling,
or cancellation semantics that cannot be implemented reliably without duplicating substantial queue
framework behavior.
