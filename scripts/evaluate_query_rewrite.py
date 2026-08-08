"""A/B the LLM query rewrite on the versioned retrieval protocol.

Runs every evaluation case twice through the same QA retrieval path
(``QueryPlanner`` -> ``QASearchCoordinator`` -> ``SearchService``) with query
rewriting disabled and enabled, then compares *context coverage* at the QA
context width (``limit``) instead of ``recall@10``. A top-10 truncation masks
the rewrite benefit because rewritten hits can land at ranks 11..limit.

The rewrite is question-only (R4-04): only ``case.query`` is fed to the
planner, never gold evidence or answer claims. Validation is always available;
execution is guarded by the same ``EVALUATION_DATABASE_ISOLATED`` gate and
never runs holdout while the provisional Stage 0 gate is open.

Reuses corpus preparation, case loading, and config validation from
``evaluate_retrieval``; this module only adds the paired rewrite A/B executor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import evaluate_retrieval as er
import yaml
from application.qa import LlmQueryRewriter, QASearchCoordinator, QueryPlanner
from application.qa.profile import QAPlanningProfileV1
from application.retrieval.evaluation import (
    CaseRetrievalMetrics,
    Locator,
    RetrievedChunk,
    aggregate_retrieval_metrics,
    evaluate_retrieval_case,
)
from domain.grounded_qa import QAContractError, QuestionInput
from domain.retrieval import (
    RetrievalError,
    RetrievalMode,
    RetrievalProfileV1,
    SearchExecutionContext,
    SearchRequest,
)
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa import DatabaseSearchService
from infrastructure.repositories import DocumentVersionRepository

REPOSITORY_ROOT = er.REPOSITORY_ROOT


async def _convert_hits(
    merged: Any,
    database: Database,
) -> tuple[RetrievedChunk, ...]:
    """Project merged SearchHits to locator-only chunks for scoring."""
    chunks: list[RetrievedChunk] = []
    async with database.session() as session:
        version_repo = DocumentVersionRepository(session)
        for hit in merged.hits:
            version = await version_repo.get(hit.version_id)
            if version is None:
                continue
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(hit.chunk_id),
                    source_key=hit.source_key,
                    source_version=version.blob_hash,
                    rank=hit.final_rank,
                    locators=tuple(
                        Locator(locator.kind.value, locator.start, locator.end)
                        for locator in hit.locators
                    ),
                    keyword_rank=hit.keyword_rank,
                    keyword_score=hit.keyword_score,
                    dense_rank=hit.dense_rank,
                    dense_score=hit.dense_score,
                    fused_rank=hit.fused_rank,
                    fused_score=hit.fused_score,
                    rerank_rank=hit.rerank_rank,
                    rerank_score=hit.rerank_score,
                    context_only=hit.context_only,
                )
            )
    return tuple(chunks)


def _empty_observation() -> dict[str, Any]:
    return {
        "ok": False,
        "query_count": 0,
        "rewrite_applied": False,
        "fallback_reason": None,
        "hit_count": 0,
        "duplicate_count": 0,
        "latency_ms": None,
        "error_code": None,
        "metrics": None,
        "metrics_at_10": None,
        "metrics_obj": None,
        "metrics_obj_at_10": None,
    }


async def _run_path(
    case: er.RetrievalEvaluationCase,
    *,
    planner: QueryPlanner,
    coordinator: QASearchCoordinator,
    database: Database,
    retrieval_profile: RetrievalProfileV1,
    planner_profile: QAPlanningProfileV1,
    limit: int,
    mode: RetrievalMode,
) -> dict[str, Any]:
    started = perf_counter()
    space_id = er._space_uuid(case.space_id)
    try:
        question = QuestionInput(
            question=case.query,
            space_id=space_id,
            caller_id="query-rewrite-eval",
        )
        planning = await planner.plan(question, planner_profile)
        merged = await coordinator.search(
            base_request=SearchRequest(
                query=question.question,
                space_id=space_id,
                mode=mode,
                execution_context=SearchExecutionContext.OFFLINE_EVALUATION,
            ),
            plan=planning.plan,
            profile=retrieval_profile,
            limit=limit,
        )
        chunks = await _convert_hits(merged, database)
        metrics = evaluate_retrieval_case(
            case.gold_evidence,
            chunks,
            k=limit,
            must_exclude=case.must_exclude,
            gold_claims=case.gold_claims,
        )
        metrics_at_10 = evaluate_retrieval_case(
            case.gold_evidence,
            chunks,
            k=10,
            must_exclude=case.must_exclude,
            gold_claims=case.gold_claims,
        )
        return {
            "ok": True,
            "query_count": len(planning.plan.queries),
            "rewrite_applied": planning.plan.rewrite_applied,
            "fallback_reason": (
                planning.plan.fallback_reason.value
                if planning.plan.fallback_reason is not None
                else None
            ),
            "hit_count": len(merged.hits),
            "duplicate_count": merged.duplicate_count,
            "latency_ms": round((perf_counter() - started) * 1000, 3),
            "error_code": None,
            "metrics": er._metric_dict(metrics),
            "metrics_at_10": er._metric_dict(metrics_at_10),
            "metrics_obj": metrics,
            "metrics_obj_at_10": metrics_at_10,
        }
    except (QAContractError, RetrievalError, ValueError) as exc:
        outcome = _empty_observation()
        outcome["error_code"] = getattr(exc, "code", None) or type(exc).__name__
        outcome["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        return outcome
    except Exception:  # pragma: no cover - defensive, mirrors evaluate_retrieval
        outcome = _empty_observation()
        outcome["error_code"] = "EVALUATION_INFRASTRUCTURE_ERROR"
        outcome["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        return outcome


def _aggregate(outcomes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    metrics: list[CaseRetrievalMetrics] = []
    latencies: list[float] = []
    for outcome in outcomes:
        if outcome["ok"] and outcome["metrics_obj"] is not None:
            metrics.append(outcome["metrics_obj"])
            latencies.append(outcome["latency_ms"])
    aggregated = aggregate_retrieval_metrics(
        metrics,
        latency_ms=latencies,
        failure_count=sum(not outcome["ok"] for outcome in outcomes),
        total_query_count=len(outcomes),
    )
    return er._metric_dict(aggregated)


def _coverage(value: dict[str, Any] | None) -> float | None:
    if value is None:
        return None
    return value.get("evidence_recall_at_k")


def _pairwise(
    off_outcomes: Sequence[dict[str, Any]],
    on_outcomes: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    evidenced = 0
    improved = 0
    regressed = 0
    unchanged = 0
    filled_to_full = 0
    for off, on in zip(off_outcomes, on_outcomes, strict=True):
        off_cov = _coverage(off.get("metrics"))
        on_cov = _coverage(on.get("metrics"))
        if off_cov is None or on_cov is None:
            continue
        evidenced += 1
        if on_cov > off_cov + 1e-9:
            improved += 1
        elif on_cov < off_cov - 1e-9:
            regressed += 1
        else:
            unchanged += 1
        if (
            off["metrics"].get("full_evidence_coverage_at_k") is False
            and on["metrics"].get("full_evidence_coverage_at_k") is True
        ):
            filled_to_full += 1
    return {
        "evidenced_cases": evidenced,
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "off_to_full_on": filled_to_full,
    }


def _rewrite_summary(on_outcomes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    fallbacks: Counter[str] = Counter()
    query_counts: Counter[int] = Counter()
    attempted = 0
    applied = 0
    for outcome in on_outcomes:
        attempted += 1
        if outcome["ok"]:
            query_counts[outcome["query_count"]] += 1
            if outcome["rewrite_applied"]:
                applied += 1
            if outcome["fallback_reason"] is not None:
                fallbacks[outcome["fallback_reason"]] += 1
    return {
        "attempted": attempted,
        "rewrite_applied": applied,
        "query_count_distribution": dict(sorted(query_counts.items())),
        "fallback_reasons": dict(sorted(fallbacks.items())),
    }


def _build_report(
    validation: Mapping[str, Any],
    split: str,
    *,
    mode: RetrievalMode,
    limit: int,
    off_planner_profile: QAPlanningProfileV1,
    on_planner_profile: QAPlanningProfileV1,
    retrieval_profile: RetrievalProfileV1,
    identity: Any,
    preparation: Mapping[str, Any],
    chunk_size: int,
    off_outcomes: Sequence[dict[str, Any]],
    on_outcomes: Sequence[dict[str, Any]],
    cases: Sequence[er.RetrievalEvaluationCase],
) -> dict[str, Any]:
    off_agg = _aggregate(off_outcomes)
    on_agg = _aggregate(on_outcomes)

    def _delta(key: str) -> float | None:
        left = on_agg.get(key)
        right = off_agg.get(key)
        if left is None or right is None:
            return None
        return round(left - right, 6)

    return {
        "schema_version": "query-rewrite-report-v1",
        "run_id": uuid4().hex,
        "run_kind": "provisional_engineering",
        "selected_split": split,
        "git_commit": er._git_commit(),
        "git_dirty": bool(er._git_status()),
        "corpus_hash": validation["corpus"]["manifest_sha256"],
        "dataset_hash": validation["dataset"]["cases_sha256"],
        "config_hash": validation["config_hash"],
        "formal_run_eligible": validation["formal_run_eligible"],
        "formal_run_blockers": validation["formal_run_blockers"],
        "rewrite_experiment": {
            "mode": mode.value,
            "context_limit": limit,
            "planning": {
                "max_subqueries": on_planner_profile.max_subqueries,
                "rewrite_timeout_seconds": on_planner_profile.rewrite_timeout_seconds,
                "rewrite_enabled": {
                    "off": off_planner_profile.rewrite_enabled,
                    "on": on_planner_profile.rewrite_enabled,
                },
            },
            "retrieval_profile": {
                "dense_candidate_k": retrieval_profile.dense_candidate_k,
                "rerank_k": retrieval_profile.rerank_k,
                "final_k": retrieval_profile.final_k,
                "max_chunks_per_document": retrieval_profile.max_chunks_per_document,
                "reranker_enabled": retrieval_profile.reranker_enabled,
            },
        },
        "embedding_identity": {
            "version": identity.version,
            "model_revision": identity.model_revision,
            "query_instruction_version": identity.query_instruction_version,
            "document_instruction_version": identity.document_instruction_version,
        },
        "runtime": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "packages": {
                name: er._package_version(name)
                for name in ("application", "domain", "infrastructure", "model-gateway")
            },
        },
        "preparation": {**preparation, "chunk_size": chunk_size},
        "aggregate": {
            "rewrite_off": off_agg,
            "rewrite_on": on_agg,
            "delta": {
                "evidence_recall_at_k": _delta("evidence_recall_at_k"),
                "claim_recall_at_k": _delta("claim_recall_at_k"),
                "full_evidence_coverage_rate": _delta("full_evidence_coverage_rate"),
                "mrr": _delta("mrr"),
            },
            "pairwise": _pairwise(off_outcomes, on_outcomes),
            "rewrite_summary": _rewrite_summary(on_outcomes),
        },
        "cases": [
            {
                "case_id": case.case_id,
                "category": case.category,
                "rewrite_off": {
                    "ok": off["ok"],
                    "error_code": off["error_code"],
                    "query_count": off["query_count"],
                    "hit_count": off["hit_count"],
                    "latency_ms": off["latency_ms"],
                    "metrics": off["metrics"],
                },
                "rewrite_on": {
                    "ok": on["ok"],
                    "error_code": on["error_code"],
                    "query_count": on["query_count"],
                    "rewrite_applied": on["rewrite_applied"],
                    "fallback_reason": on["fallback_reason"],
                    "hit_count": on["hit_count"],
                    "latency_ms": on["latency_ms"],
                    "metrics": on["metrics"],
                },
            }
            for case, off, on in zip(cases, off_outcomes, on_outcomes, strict=True)
        ],
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    er._privacy_scan(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    agg = report["aggregate"]
    off = agg["rewrite_off"]
    on = agg["rewrite_on"]
    delta = agg["delta"]
    pairwise = agg["pairwise"]
    rewrite = agg["rewrite_summary"]
    limit = report["rewrite_experiment"]["context_limit"]

    def _row(label: str, key: str, with_delta: bool = True) -> str:
        cells = [
            f"| {label} ",
            f"| `{off.get(key)}` ",
            f"| `{on.get(key)}` ",
        ]
        if with_delta:
            cells.append(f"| `{delta.get(key)}` |")
        else:
            cells.append("| |")
        return "".join(cells)

    lines = [
        "# Query Rewrite A/B Summary",
        "",
        "This is an engineering report. It is not a Stage 0 or holdout acceptance record.",
        "",
        f"- Split: `{report['selected_split']}`",
        f"- Mode: `{report['rewrite_experiment']['mode']}`",
        f"- Context limit: `{limit}`",
        f"- Formal eligible: `{report['formal_run_eligible']}`",
        "",
        "| metric | rewrite off | rewrite on | delta |",
        "|---|---|---|---|",
        _row(f"Evidence recall@{limit}", "evidence_recall_at_k"),
        _row(f"Claim recall@{limit}", "claim_recall_at_k"),
        _row("Full evidence coverage rate", "full_evidence_coverage_rate"),
        _row("MRR", "mrr"),
        _row("P95 latency (ms)", "latency_p95_ms", with_delta=False),
        _row("Failure rate", "failure_rate", with_delta=False),
        "",
        "Paired (evidenced cases):",
        f"- improved `{pairwise['improved']}` / regressed `{pairwise['regressed']}`",
        f"- unchanged `{pairwise['unchanged']}` / off-to-full `{pairwise['off_to_full_on']}`",
        "",
        "Rewrite summary:",
        f"- applied `{rewrite['rewrite_applied']}/{rewrite['attempted']}`",
        f"- query-count distribution `{rewrite['query_count_distribution']}`",
        f"- fallbacks `{rewrite['fallback_reasons']}`",
        "",
    ]
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Repository-relative evaluation YAML")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument(
        "--output", help="Optional JSON report path; a Markdown summary is written beside it"
    )
    parser.add_argument(
        "--validate-only", action="store_true", help="Validate frozen inputs without executing"
    )
    parser.add_argument(
        "--prepare-corpus",
        action="store_true",
        help="Ingest manifest sources into the isolated evaluation database",
    )
    parser.add_argument(
        "--prepare-source",
        action="append",
        help="Limit --prepare-corpus to one manifest source_key; repeatable",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="Override the ingestion chunk_size (default: 512)",
    )
    parser.add_argument(
        "--mode", choices=("dense_rerank", "dense", "hybrid_rerank"), default="dense_rerank"
    )
    parser.add_argument(
        "--limit", type=int, default=32, help="QA context width used for coverage scoring"
    )
    parser.add_argument("--max-subqueries", type=int, default=4)
    parser.add_argument("--rewrite-timeout", type=float, default=5.0)
    parser.add_argument(
        "--limit-cases",
        type=int,
        default=None,
        help="Cap the number of evaluated cases (smoke run)",
    )
    parser.add_argument("--confirm-holdout", help="Required for an eligible formal holdout run")
    parser.add_argument("--blob-root", default="tmp/retrieval-eval-blobs")
    parser.add_argument("--quiet", action="store_true", help="Do not print the report bundle")
    return parser


async def _run(
    config: Mapping[str, Any],
    validation: Mapping[str, Any],
    split: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    identity = settings.active_embedding_identity()
    database = Database(settings.database_url)
    gateway = er._create_gateway()
    mode = RetrievalMode(args.mode)
    limit = args.limit
    retrieval_profile = RetrievalProfileV1(embedding_version=identity.version)
    off_planner_profile = QAPlanningProfileV1(
        max_subqueries=args.max_subqueries,
        rewrite_timeout_seconds=args.rewrite_timeout,
        rewrite_enabled=False,
    )
    on_planner_profile = QAPlanningProfileV1(
        max_subqueries=args.max_subqueries,
        rewrite_timeout_seconds=args.rewrite_timeout,
        rewrite_enabled=True,
    )
    effective_chunk_size = 512 if args.chunk_size is None else args.chunk_size
    coordinator = QASearchCoordinator(DatabaseSearchService(database, gateway))
    off_planner = QueryPlanner(rewriter=None)
    on_planner = QueryPlanner(rewriter=LlmQueryRewriter(gateway))
    try:
        sources = er._manifest_sources(config)
        preparation: dict[str, Any] = {
            "succeeded": 0,
            "skipped": 0,
            "failures": er._unsupported_source_failures(sources),
        }
        if args.prepare_corpus:
            preparation = await er._prepare_corpus(
                sources,
                database=database,
                gateway=gateway,
                identity=identity,
                blob_root=(REPOSITORY_ROOT / args.blob_root).resolve(),
                source_keys=(frozenset(args.prepare_source) if args.prepare_source else None),
                chunk_size=effective_chunk_size,
            )
        cases = list(er._load_cases(config, split))
        if args.limit_cases is not None:
            cases = cases[: args.limit_cases]

        def _execute(
            case: er.RetrievalEvaluationCase,
        ) -> asyncio.Future[tuple[dict[str, Any], dict[str, Any]]]:
            return asyncio.gather(
                _run_path(
                    case,
                    planner=off_planner,
                    coordinator=coordinator,
                    database=database,
                    retrieval_profile=retrieval_profile,
                    planner_profile=off_planner_profile,
                    limit=limit,
                    mode=mode,
                ),
                _run_path(
                    case,
                    planner=on_planner,
                    coordinator=coordinator,
                    database=database,
                    retrieval_profile=retrieval_profile,
                    planner_profile=on_planner_profile,
                    limit=limit,
                    mode=mode,
                ),
            )

        warmup = cases[: int(config["runtime"]["warmup_queries"])]
        for case in warmup:
            await _execute(case)
        off_outcomes: list[dict[str, Any]] = []
        on_outcomes: list[dict[str, Any]] = []
        for case in cases:
            off, on = await _execute(case)
            off_outcomes.append(off)
            on_outcomes.append(on)
        return _build_report(
            validation,
            split,
            mode=mode,
            limit=limit,
            off_planner_profile=off_planner_profile,
            on_planner_profile=on_planner_profile,
            retrieval_profile=retrieval_profile,
            identity=identity,
            preparation=preparation,
            chunk_size=effective_chunk_size,
            off_outcomes=off_outcomes,
            on_outcomes=on_outcomes,
            cases=cases,
        )
    finally:
        await gateway.aclose()
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config_path = er._resolve_repository_path(args.config)
        config = er._load_yaml_mapping(config_path)
        config_schema = json.loads(er.CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
        validation = er.validate_evaluation_config(config, config_schema=config_schema)
    except (er.EvaluationConfigError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"evaluation config invalid: {exc}", file=sys.stderr)
        return 2

    validation["selected_split"] = args.split
    if args.validate_only:
        print(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.split == "holdout":
        if not validation["formal_run_eligible"]:
            print("holdout execution blocked by formal evaluation gates", file=sys.stderr)
            return 4
        if args.confirm_holdout != validation["config_hash"]:
            print("holdout execution requires --confirm-holdout <config_hash>", file=sys.stderr)
            return 4
    if os.getenv("EVALUATION_DATABASE_ISOLATED") != "1":
        print("evaluation execution requires EVALUATION_DATABASE_ISOLATED=1", file=sys.stderr)
        return 4

    try:
        report = asyncio.run(
            _run(
                config,
                validation,
                args.split,
                args,
            )
        )
    except (er.EvaluationConfigError, OSError, ValueError) as exc:
        print(f"evaluation execution rejected: {exc}", file=sys.stderr)
        return 2
    try:
        er._privacy_scan(report)
        if args.output:
            _write_report(Path(args.output).resolve(), report)
    except (er.EvaluationConfigError, OSError) as exc:
        print(f"evaluation report rejected: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
