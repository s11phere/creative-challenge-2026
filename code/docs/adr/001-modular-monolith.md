# ADR-001: Modular Monolith With Independent Worker

- Status: Accepted
- Date: 2026-07-15

## Context

The first release must connect ingestion, retrieval, grounded answering, Web, HTTP API, and Skill
execution without introducing distributed ownership or operational overhead. Parsing, embedding,
and indexing are long-running and cannot live only in request-process memory.

## Decision

Use a modular monolith for API and application behavior, plus an independently runnable Worker for
durable long tasks. Keep Domain, Application, Knowledge, Agent Runtime, Model Gateway, and
Infrastructure boundaries explicit. Cross-module access uses ports or application services rather
than private tables.

## Alternatives

- Microservices were rejected because no capacity, isolation, deployment, or team boundary justifies them.
- A single request process was rejected because long work must be retryable, cancellable, and observable.

## Consequences

One deployment can host most modules during development, while the Worker scales independently.
Module boundaries require contract tests and disciplined dependency direction even though code shares
one repository and database.

## Reassessment Triggers

Reassess only when measured load, fault isolation, independent release cadence, or team ownership
cannot be handled by the modular monolith and Worker boundary.
