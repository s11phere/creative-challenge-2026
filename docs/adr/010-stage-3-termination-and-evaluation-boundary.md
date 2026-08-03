# ADR-010: Terminate Stage 3 Without Treating the Quality Gate as Passed

- Status: Accepted
- Date: 2026-08-03

## Context

Stage 3 engineering work is complete, including Keyword/Dense/Hybrid/Hybrid+Reranker retrieval,
the Search Application Port, version and Space boundaries, and the versioned offline evaluation
tooling. PR #3's fixed GPU configuration was reproduced locally with the current code and data:
Claim Recall@10 was `69.7548%`, Evidence Recall@10 was `62.3431%`, MRR was `0.6839`, P95 was
`383.5 ms`, and the failure rate was `0%`. The result is below the PR's claimed `75.8%` and below
the Stage 3 quality gate.

The development evaluation set is not considered sufficiently representative to support another
quality conclusion for this stage. This is a limitation of the evaluation boundary, not evidence
that the quality gate was met. A formal holdout was not run, and the current result must remain
traceable as a failed provisional reproduction.

## Decision

Terminate the current Stage 3 effort by explicit project decision. Record its outcome as:

- engineering implementation: complete;
- formal quality gate: not passed;
- reason for termination: evaluation-set representativeness is insufficient for a useful stage
  conclusion;
- formal holdout: not run.

Keep `cases/evals/configs/retrieval-v1.yaml` at `status: provisional` with
`formal_runs_enabled: false`. Do not rewrite the reported metrics, mark the configuration as
`frozen`, enable the formal holdout, or describe Stage 3 as quality-accepted. The existing
Search Application Port and retrieval safety boundaries remain available to later work, with the
provisional quality boundary visible to callers and documentation.

## Alternatives

- Run the current formal holdout and use it to close the stage. Rejected because the current
  evaluation boundary is not considered representative enough to support that decision.
- Declare Stage 3 passed based on engineering completion or the provisional Recall result.
  Rejected because neither satisfies the recorded quality criteria.
- Keep Stage 3 open and continue tuning the current dataset. Rejected for the current project
  sequence; it would turn a known evaluation limitation into unbounded tuning work.

## Consequences

The repository preserves an honest, reproducible record of the PR #3 result and its limitations.
Later stages may build on the retrieval interfaces and security invariants, but must not claim a
formal retrieval quality baseline or silently use the provisional score as an acceptance result.
The current evaluation configuration is not a production-quality freeze and must not be enabled
for formal runs by changing a flag alone.

Stage 3 may only be reopened as a new evaluation effort. Reopening requires a new dataset version
with an explicit coverage/representativeness decision, a new configuration or model/profile
version as applicable, a fresh development evaluation, and then the normal formal-gate process.
Existing development or holdout data must not be modified in place or reused as if this decision
had been a quality pass.

## Reassessment Triggers

Reassess this decision when a versioned evaluation set has materially improved coverage and its
annotation, split, and evidence-locator policy has been reviewed; when representative production
or manually reviewed queries are available under the Stage 0 data boundary; or when a new retrieval
model/profile requires a fresh quality baseline. Any reassessment must update this ADR or add a
superseding ADR before enabling a formal holdout.
