# ADR-005: Ingestion Identity, Versioning, Publication, And Deletion

- Status: Accepted
- Date: 2026-07-18

## Context

Stage 2 must ingest a source repeatedly without creating duplicate documents, versions, chunks, or
tasks. It must also distinguish exact source bytes from normalized textual content, preserve a
logical Document across moves, rebuild derived artifacts when processing configuration changes,
publish a complete version atomically, and withdraw deleted content before asynchronous cleanup.

The stage 2 Step 1 schema established the six core tables, but it did not yet encode all identities
needed by the ingestion pipeline. In particular, `stable_key` was globally queried without a scoped
unique constraint, raw and normalized hashes were conflated, Chunk writes had no retry-safe key,
processing versions were incomplete, and task stage also represented terminal status. PostgreSQL is
the source of truth under ADR-002 and ADR-009, while Redis/Dramatiq provides at-least-once delivery.

## Decision

### Document identity and moves

`Document` is the stable logical identity. `stable_key` is derived from the normalized source URI
and is unique within a Source through `(source_id, stable_key)`. Repository lookups always include
the Source ID; a stable key is never treated as globally unique.

A move may retain the Document ID only when a hash match has exactly one candidate in the same
Space and Source. No candidate creates a new Document. Multiple candidates produce an explicit
conflict and are never merged automatically. Space isolation applies before identity matching.

### Raw bytes and normalized content

`blob_hash` is the lowercase SHA-256 of the exact source bytes. It verifies manifest entries and
BlobStore integrity and detects byte-for-byte repeats.

`content_hash` is the lowercase SHA-256 of the versioned NORMALIZE output. It is the Content
Fingerprint defined by the stage 0 glossary. `normalizer_version` identifies the normalization
algorithm. The two hashes are stored separately and are not interchangeable.

### Immutable processing versions

A `DocumentVersion` is an immutable candidate build. It records `parser_version`,
`normalizer_version`, `chunker_version`, `embedding_version`, a canonical processing configuration,
and its SHA-256 `processing_config_hash`. The retry-safe version identity is:

```text
(document_id, content_hash, parser_version, normalizer_version,
 chunker_version, embedding_version, processing_config_hash)
```

The canonical configuration contains every option that can change parsing, normalization, chunking,
or embedding output. Configuration serialization uses sorted keys and stable UTF-8 JSON before
hashing. A processing change creates a new candidate version even when source content is unchanged.

The initial pgvector column is fixed at 768 dimensions. This is a schema contract, not a runtime
setting. Changing it requires a new ADR or ADR update, an Alembic migration, provider compatibility
validation, and a complete embedding rebuild. Runtime configuration cannot change the column size.

### Chunk and embedding identity

`(version_id, ordinal)` is the unique Chunk row identity used for retry-safe upsert. `chunk_hash` is
the lowercase SHA-256 of the normalized embedding input only; it excludes version ID, ordinal, and
locator metadata. Repeated text in different locations may therefore share a hash while remaining
different Chunk rows.

Embedding reuse is keyed by the chunk hash plus embedding version and embedding-specific canonical
configuration. Cross-Space reuse is denied by default because derived data inherits source
sensitivity. A future policy may permit it only through an explicit ADR and privacy tests.

### Atomic publication and rollback

Chunks belonging to a candidate version are not part of the published candidate set. After
VALIDATE succeeds, the application verifies that the candidate belongs to the target Document and
Space, then changes `documents.current_version_id` in one PostgreSQL transaction. Failure,
cancellation, or Worker loss leaves the previous published version unchanged. Rollback republishes a
previous complete version through the same validation and transaction boundary.

`current_version_id` has a foreign key to `document_versions.id`; the application additionally
enforces that the referenced version belongs to the same Document because a simple foreign key
cannot express that cross-column invariant.

### Withdrawal and deletion

Deletion first sets a Document tombstone and withdraws `current_version_id` transactionally. The
Document then cannot enter the published candidate set even if asynchronous cleanup fails. A durable
task with operation `delete` removes Chunks, embeddings, and source blobs after the configured
recovery window. Minimal tombstone metadata remains so later citation handling can report that the
source no longer exists.

### Durable task identity and delivery

`IngestionTask` separates:

- `operation`: `ingest`, `rebuild`, or `delete`;
- `status`: queued/running/success/failure/cancellation/dead-letter state;
- `stage`: the current deterministic pipeline stage.

Each task stores a Source-scoped idempotency key, optional target version, retry limit, cancellation
time, enqueue time, heartbeat, lease expiry, safe error code, and redacted error summary. The unique
task identity is `(source_id, idempotency_key)`.

The database record is created before broker dispatch. Dispatch is replayable, duplicate deliveries
are expected, and a reconciler may redeliver queued tasks or running tasks whose lease expired. Queue
messages contain IDs and control metadata only. PostgreSQL task state, not Redis or log presence, is
the completion authority.

## Alternatives

- A globally unique `stable_key` was rejected because identical relative paths can exist in separate
  Sources and Spaces.
- A single hash was rejected because exact-byte integrity and normalized-content identity have
  different inputs and failure semantics.
- Including version ID or ordinal in `chunk_hash` was rejected because it prevents legitimate
  embedding reuse and conflates row identity with content identity.
- Runtime-configurable vector dimensions were rejected because one pgvector column cannot safely
  contain mixed dimensions and the existing setting did not control the schema.
- Publishing chunks incrementally was rejected because retrieval could observe partial versions.
- Hard deletion in the request transaction was rejected because cleanup is long-running and later
  citation handling needs a stable tombstone.
- Treating successful Redis enqueue as durable task creation was rejected because PostgreSQL and
  Redis do not share a transaction.

## Consequences

The existing six-table model remains, but a new migration adds fields, foreign keys, and unique
constraints. Existing stage 2 Step 1 rows receive deterministic compatibility values during
migration. Inconsistent duplicate rows cause the constraint migration to fail and require explicit
repair rather than silent merging.

Application services must canonicalize hashes and configuration before repository writes, handle
unique-conflict rereads, validate ownership before publication, and implement idempotent task
consumers. The fixed vector dimension simplifies the first retrieval baseline but constrains model
selection to compatible embeddings until a schema migration is approved.

The stage 1 diagnostic actor's started/completed log lines remain an accepted entry risk: persistent
task state, Redis queue inspection, trace IDs, and dead-letter evidence are the required alternatives.
Stage 2 business actor acceptance must verify database state transitions and recovery behavior and
must not infer task failure solely from missing container log lines.

## Reassessment Triggers

Reassess when a Source cannot provide stable URI identity, content-addressed Blob storage needs a
different deduplication scope, vector dimensions must change, cross-Space embedding reuse has an
approved privacy benefit, publication requires more than one storage transaction, or retention and
citation requirements no longer fit tombstone-based deletion.
