# MVP Scope

> Stage 0 baseline. Thresholds are provisional until the first reproducible baseline run.

## P0: Required For The First Usable Loop

| Capability | Acceptance boundary |
| --- | --- |
| Markdown, TXT, and text-based PDF ingestion | Parse real corpus sources, retain headings plus line/page locators, and report unsupported or damaged files |
| Idempotent incremental indexing | A repeated import creates no duplicate versions or chunks; content changes create a new immutable version |
| Space isolation | Retrieval is scoped before candidate recall and verified again before citations are returned |
| Hybrid retrieval | Keyword and vector paths can run independently; RRF fusion and top-k are versioned |
| Traceable answering | Structured claim-evidence output; every citation resolves to a source version and original location |
| Evidence-aware refusal | Insufficient evidence is distinct from model, queue, parser, or database failure |
| Web workbench | Data sources, conversation/task workspace, evidence viewer, retry, cancel, and failure states |
| `knowledge_qa` Skill | One version-pinned workflow callable from Web and `/api/v1`; test invocation uses the same application service |
| Offline evaluation | Versioned corpus, cases, configuration, and machine-readable report |

## P1: Significant Enhancements

| Capability | Boundary |
| --- | --- |
| DOCX, HTML, notebook, and general code parsing | Add parser adapters only after Markdown/TXT/PDF contracts pass |
| Complex PDF and OCR | Triggered by measured loss on tables, scans, formulas, or multi-column layouts |
| Knowledge organization Skills | Summaries, review cards, and source comparison after grounded QA is stable |
| Checkpoint recovery and Skill hot loading | Version compatibility and trust checks required before enabling |
| Feedback workflow and Eval Dashboard | Feedback enters a review queue before becoming a regression case |
| Multiple model providers | Added through ModelGateway capability aliases and contract tests |

## P2: Deferred

Multi-agent orchestration, knowledge graphs, multimodal retrieval, team collaboration, external
connectors, and model fine-tuning remain deferred until a P0 metric or real user workflow proves
the need. Interfaces may preserve replacement boundaries, but no unused infrastructure is built.

## Provisional Quality Gates

- Parser success rate on the approved corpus: at least 95%.
- Citation target resolution: 100%.
- Retrieval evidence Recall@5: at least 85% on the frozen holdout.
- Supported-claim rate: at least 90%.
- Refusal accuracy: at least 90%.
- Cross-space and withdrawn-source recall violations: 0.
- Repeated runs record corpus, dataset, parser, chunker, embedding, retrieval, prompt, model, and
  Skill versions.

## Stage 0 Exit

The stage exits only when the team can independently reach the same pass/fail decision for every
case, evidence can be opened at the declared source version and locator, data rights are recorded,
and P0/P1/P2 boundaries no longer contradict the implementation plan.
