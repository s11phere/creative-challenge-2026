# Recall optimization experiment audit

Date: 2026-07-30

Status: original claim invalidated; repaired development rerun completed. This document does not
provide a formal Stage 3 quality result.

## Original claim

PR #2 reported development Dense Recall@5 of 90.48% against an 85% gate. That result cannot be
used as evidence that retrieval performance improved because the evaluated implementation had
two correctness defects:

1. Leading `raw_text` nodes were treated as a table of contents and removed. The PDF parser emits
   pages as `raw_text`, so ordinary PDF content could be merged into one oversized chunk or
   omitted from the intended chunk boundaries.
2. Result de-duplication used source URI and locator overlap. Multiple documents under one
   directory source share the source URI, so valid results from different documents could be
   discarded.

The PR also removed the reranker runtime while the active API profile still supports and defaults
to `hybrid_rerank`. This made the claimed deployment configuration incompatible with the reported
evaluation configuration.

## Repaired experiment boundary

The repaired implementation preserves all source text, marks conservatively detected leading TOC
chunks as `table_of_contents`, and excludes them only from direct retrieval candidates. Boundary
text de-duplication is limited to the same immutable `(document_id, version_id)` and runs before
the per-document quota. The existing weighted-RRF and reranker contracts remain intact.

The chunker version is `1.3`; all documents must be rebuilt before comparing metrics. Evaluation
must use only the frozen development split, approved manifest sources, pinned local model
revisions, and an isolated database. Holdout remains disabled.

## Repaired development result

The repaired implementation was evaluated on 2026-07-30 using only the frozen development split
in a fresh isolated PostgreSQL/Redis project. Several idempotent continuations were needed because
the local CPU embedding service processed large PDFs slowly. All 74 published document versions
report chunker `1.3`. The 16 parser failures are code formats
excluded by this retrieval protocol, which includes Markdown, text, and PDF.

| Metric | Repaired result |
| --- | ---: |
| Dense exact evidence Recall@5 | 59.52% (125/210) |
| MRR | 0.5886 |
| evidence nDCG@5 | 0.4566 |
| full evidence coverage | 41.58% |
| latency P50 / P95 | 558.3 ms / 720.6 ms |
| retrieval failure rate | 0% |
| must-exclude violations | 0 |

Evaluation identity:

- Config hash: `1e93bd6f1b29a7fca128fbe7ef152ca43c31cb4b28055c5bd989a26f882470bb`
- Model revision: `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`
- Embedding identity: `embedding-v1-86074bd3d75f10aa8f254843`
- Query/document instructions: `qwen3-web-search-v1` / `none-v1`
- Report: `tmp/retrieval-eval-pr2-toc-v0-fixed.json` (private local evaluation artifact,
  not version-control eligible)

Against the previously confirmed 51.90% development Dense baseline, the repaired result is an
7.62 percentage-point improvement. The improvement is therefore observable on development, but
the original 90.48% claim remains invalid and the repaired result is still 25 percentage points
below the 85% gate. The config remains provisional, `formal_runs_enabled` remains false, and no
holdout was read or executed.
