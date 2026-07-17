# ADR-002: PostgreSQL And pgvector As The Initial Retrieval Store

- Status: Accepted
- Date: 2026-07-15

## Context

The product needs transactions, document/version metadata, deletion and publication semantics,
keyword retrieval, vector retrieval, filters, and reproducible local deployment. Adding separate
databases before measuring retrieval limitations would increase consistency and operational cost.

## Decision

Use PostgreSQL 16+ as the system of record, PostgreSQL full-text search for the first keyword
baseline, and pgvector for embeddings and vector candidates. Access retrieval through a
`RetrievalStore` port so keyword, vector, and hybrid paths remain independently testable.

## Alternatives

- Qdrant or OpenSearch were deferred because the initial corpus and latency targets do not yet prove a need.
- An embedded local vector database was rejected because it would split metadata transactions and retrieval publication.

## Consequences

Metadata and index publication can share transactions and space filters. Chinese keyword quality may
be limited and must be measured rather than assumed. Migrations and integration tests require a real
PostgreSQL/pgvector instance.

## Reassessment Triggers

Reassess when corpus-scale tests, Chinese retrieval evaluation, latency, index operations, or storage
isolation show a reproducible failure that cannot be addressed within PostgreSQL.
