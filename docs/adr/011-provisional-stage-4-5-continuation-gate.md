# ADR-011: Permit Provisional Stage 4/5 Continuation on a Non-Passing Retrieval Baseline

- Status: Accepted
- Date: 2026-08-03

## Context

ADR-010 terminated the current Stage 3 quality effort because the evaluation boundary was not
representative enough for a formal conclusion. The new `knowledge-qa-v1` development run repaired
some development annotations and was reproduced with the local GPU Qwen3 Embedding and BGE
Reranker stack, but it did not materially expand corpus coverage or change the formal result:

- Hybrid + Reranker Claim Recall@10: `69.7548%`;
- Evidence Recall@10: `62.3431%`;
- MRR: `0.6839`;
- P95: `382.6 ms`;
- infrastructure failure rate: `0%`;
- must-exclude violations: `0`.

The project still needs to complete Stage 4/5 engineering work. Blocking all provisional QA, Runtime,
Web, feedback and Skill work on the formal retrieval quality gate would leave the already implemented
interfaces unverified in the intended end-to-end workflow. At the same time, treating the result as a
formal quality pass would contradict ADR-010 and the Stage 0 quality policy.

## Decision

Keep the formal Stage 3/4 quality gates unchanged. In addition, accept an explicitly provisional
continuation gate for engineering work:

| Metric | Provisional continuation floor |
| --- | ---: |
| Claim Recall@10 on the registered development P0 slice | >= 65% |
| Evidence Recall@10 on the registered development P0 slice | >= 60% |
| MRR | >= 0.60 |
| Retrieval P95 | <= 500 ms |
| Infrastructure failure rate | 0% |
| `must-exclude` violations | 0 |
| Space/version/tombstone/Citation security violations | 0 |

The current v1 GPU development run satisfies this continuation gate. Therefore:

1. Stage 4/5 may continue with the pinned v1 retrieval configuration for provisional development,
   contract, E2E, Web, Runtime, feedback and Skill work.
2. All reports, API/UI status and documentation must continue to label the retrieval and QA baseline
   `provisional`; `formal_runs_enabled` remains `false` and no current holdout may be run or read.
3. The continuation gate is not a user-facing quality claim and cannot close Stage 3, Stage 4 or
   Stage 5. Formal release still requires the original quality thresholds, a representative versioned
   dataset, a frozen configuration and the normal one-time holdout process.
4. Security, privacy, source authorization, Space/version isolation, citation target resolution and
   no-secret/no-document-body logging rules are not lowered by this decision.
5. Any regression below the continuation floor pauses provisional work until a new development report
   and versioned configuration are recorded.

This decision does not change the current config to `frozen`, does not enable formal execution, and
does not permit modifying an existing holdout in place.

## Alternatives

- Keep all Stage 4/5 work blocked until a representative retrieval dataset is available. Rejected for
  the current delivery objective because the engineering contracts can be tested without claiming
  formal quality.
- Declare the current retrieval result a formal pass by lowering the Stage 0 threshold. Rejected because
  it would erase the distinction between engineering evidence and quality acceptance.
- Run the existing holdout to justify continuation. Rejected because ADR-010 explicitly prohibits it.

## Consequences

Stage 4/5 can proceed and produce useful provisional evidence using a known, reproducible retrieval
baseline. The repository must carry two visible states: formal quality remains open, while the
provisional continuation gate is satisfied. Any release or statement of supported answer quality must
still wait for the formal process; the lower floor cannot be used to select or tune against holdout data.

## Reassessment Triggers

Reassess this decision when a representative dataset/config version is available, when formal Stage 3
is intentionally reopened, when the continuation floor is missed, or when Stage 4/5 moves from internal
provisional engineering to a user-facing quality claim. Any change to the formal thresholds requires a
separate ADR and updated acceptance records.
