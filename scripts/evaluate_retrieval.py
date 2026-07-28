"""Run the versioned retrieval evaluation protocol.

Validation is always available. Execution is deliberately guarded by an
explicit isolated-database environment variable and never runs holdout while
the provisional Stage 0 gate is open.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4, uuid5

import yaml
from application.ingestion import IngestionConfig, IngestionOrchestrator, SourceRegistrationService
from application.retrieval import (
    GatewayQueryTextEmbedder,
    GatewayReranker,
    QueryEmbeddingService,
    RetrievalEvaluationCase,
    RetrievalEvaluationObservation,
    RetrievalEvaluationResult,
    SearchService,
    query_embedding_config,
    run_retrieval_evaluation,
)
from application.retrieval.evaluation import (
    EvidenceUnit,
    Locator,
    RetrievedChunk,
)
from domain.embedding import EmbeddingIdentity
from domain.models import (
    DocumentStatus,
    IngestionTask,
    SourceType,
    Space,
    TaskOperation,
)
from domain.retrieval import (
    HybridEmbeddingFailurePolicy,
    RerankerFailurePolicy,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    RetrievalProfileV1,
    SearchExecutionContext,
    SearchRequest,
)
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.chunkers import StructureChunker
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.parsers import ParserFactory, get_parser
from infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
    SpaceRepository,
)
from infrastructure.retrieval import DenseSearchMode, PostgresRetrievalStore
from jsonschema import Draft202012Validator
from model_gateway import (
    GatewayConfig,
    ModelGateway,
    ModelProvider,
    create_model_gateway,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_SCHEMA_PATH = (
    REPOSITORY_ROOT / "cases" / "evals" / "configs" / "retrieval-eval-config-v1.schema.json"
)
EVALUATION_NAMESPACE = UUID("6f9948a4-9a4a-4b1d-8e58-4de3a3e2a0d2")
_FORBIDDEN_REPORT_KEYS = {
    "query",
    "question",
    "text",
    "quote",
    "excerpt",
    "embedding",
    "vector",
    "api_key",
    "secret",
    "prompt",
    "response_body",
}


class EvaluationConfigError(ValueError):
    """Raised when an evaluation input violates the frozen protocol."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_repository_path(raw_path: str) -> Path:
    candidate = (REPOSITORY_ROOT / raw_path).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise EvaluationConfigError(f"Path escapes repository root: {raw_path}") from exc
    if not candidate.is_file():
        raise EvaluationConfigError(f"Required file does not exist: {raw_path}")
    return candidate


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvaluationConfigError(f"Expected a YAML object: {path}")
    return value


def _validate_sha(path: Path, expected: str, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise EvaluationConfigError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _split_content_hash(raw_lines: Sequence[str]) -> str:
    content = "\n".join(raw_lines)
    if raw_lines:
        content += "\n"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_evaluation_config(
    config: Mapping[str, Any],
    *,
    config_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate config, allowlist, source bytes, dataset schema, and splits."""
    schema_errors = sorted(
        Draft202012Validator(config_schema).iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if schema_errors:
        first = schema_errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise EvaluationConfigError(f"Config schema error at {location}: {first.message}")

    corpus_config = config["corpus"]
    dataset_config = config["dataset"]
    gates = config["gates"]
    manifest_path = _resolve_repository_path(corpus_config["manifest_path"])
    _validate_sha(manifest_path, corpus_config["manifest_sha256"], "Corpus manifest")
    manifest = _load_yaml_mapping(manifest_path)
    if manifest.get("corpus_version") != corpus_config["version"]:
        raise EvaluationConfigError("Corpus version does not match evaluation config")

    source_by_key: dict[str, tuple[str, Mapping[str, Any]]] = {}
    sensitivity_counts: Counter[str] = Counter()
    repository_fixture_count = 0
    source_count = 0
    for space in manifest.get("spaces", []):
        if not isinstance(space, dict) or not isinstance(space.get("id"), str):
            raise EvaluationConfigError("Manifest contains an invalid Space entry")
        for source in space.get("sources", []):
            if not isinstance(source, dict):
                raise EvaluationConfigError("Manifest contains an invalid source entry")
            source_key = source.get("source_key")
            if not isinstance(source_key, str) or source_key in source_by_key:
                raise EvaluationConfigError(
                    f"Manifest source_key is invalid or duplicate: {source_key}"
                )
            allowed_uses = source.get("allowed_uses")
            if (
                not isinstance(allowed_uses, list)
                or corpus_config["required_allowed_use"] not in allowed_uses
            ):
                raise EvaluationConfigError(f"Source {source_key} does not allow local_evaluation")
            sensitivity = source.get("sensitivity")
            if not isinstance(sensitivity, str) or not sensitivity:
                raise EvaluationConfigError(
                    f"Source {source_key} has no sensitivity classification"
                )
            source_path = _resolve_repository_path(f"cases/{source.get('path', '')}")
            content_sha256 = source.get("content_sha256")
            if not isinstance(content_sha256, str):
                raise EvaluationConfigError(f"Source {source_key} has no content_sha256")
            _validate_sha(source_path, content_sha256, f"Source {source_key}")
            source_by_key[source_key] = (space["id"], source)
            sensitivity_counts[sensitivity] += 1
            repository_fixture_count += int("repository_fixture" in allowed_uses)
            source_count += 1

    cases_path = _resolve_repository_path(dataset_config["cases_path"])
    dataset_schema_path = _resolve_repository_path(dataset_config["schema_path"])
    _validate_sha(cases_path, dataset_config["cases_sha256"], "Dataset")
    _validate_sha(dataset_schema_path, dataset_config["schema_sha256"], "Dataset schema")
    dataset_schema = json.loads(dataset_schema_path.read_text(encoding="utf-8"))
    case_validator = Draft202012Validator(dataset_schema)
    raw_lines = [line for line in cases_path.read_text(encoding="utf-8").splitlines() if line]
    split_lines: dict[str, list[str]] = {"development": [], "holdout": []}
    split_ids: dict[str, set[str]] = {"development": set(), "holdout": set()}
    split_questions: dict[str, set[str]] = {"development": set(), "holdout": set()}
    category_counts: Counter[str] = Counter()
    included_split_counts: Counter[str] = Counter()
    excluded_format_split_counts: Counter[str] = Counter()
    included_source_formats = frozenset(config["protocol"]["included_source_formats"])
    evidenced_case_count = 0
    no_evidence_case_count = 0
    for line_number, raw_line in enumerate(raw_lines, start=1):
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise EvaluationConfigError(f"Invalid dataset JSON on line {line_number}") from exc
        errors = sorted(case_validator.iter_errors(case), key=lambda error: list(error.path))
        if errors:
            raise EvaluationConfigError(
                f"Dataset schema error on line {line_number}: {errors[0].message}"
            )
        split = case["split"]
        case_id = case["id"]
        if case_id in split_ids[split]:
            raise EvaluationConfigError(f"Duplicate case ID: {case_id}")
        split_lines[split].append(raw_line)
        split_ids[split].add(case_id)
        split_questions[split].add(" ".join(case["question"].split()).casefold())
        category_counts[case["category"]] += 1
        evidenced_case_count += int(bool(case["evidence"]))
        no_evidence_case_count += int(not case["evidence"])
        evidence_formats: set[str] = set()
        for evidence in case["evidence"]:
            source_entry = source_by_key.get(evidence["source_key"])
            if source_entry is None:
                raise EvaluationConfigError(f"Case {case_id} references source outside manifest")
            source_space, source = source_entry
            if source_space != case["space_id"]:
                raise EvaluationConfigError(f"Case {case_id} evidence crosses Space boundary")
            if source["content_sha256"] != evidence["source_version"]:
                raise EvaluationConfigError(
                    f"Case {case_id} evidence version differs from manifest"
                )
            evidence_formats.add(str(source["format"]))
        if evidence_formats.issubset(included_source_formats):
            included_split_counts[split] += 1
        else:
            excluded_format_split_counts[split] += 1

    if split_ids["development"] & split_ids["holdout"]:
        raise EvaluationConfigError("Development and holdout case IDs overlap")
    if split_questions["development"] & split_questions["holdout"]:
        raise EvaluationConfigError("Development and holdout questions overlap")
    for split, expected in dataset_config["splits"].items():
        if len(split_lines[split]) != expected["count"]:
            raise EvaluationConfigError(f"Unexpected {split} case count")
        if _split_content_hash(split_lines[split]) != expected["content_sha256"]:
            raise EvaluationConfigError(f"Unexpected {split} split content hash")

    blockers: list[str] = []
    if manifest.get("status") != gates["formal_manifest_status"]:
        blockers.append(
            f"manifest_status={manifest.get('status')} (requires {gates['formal_manifest_status']})"
        )
    stage_2_acceptance_path = (REPOSITORY_ROOT / gates["stage_2_step_9_acceptance_path"]).resolve()
    if not stage_2_acceptance_path.is_file():
        blockers.append("stage_2_step_9_acceptance_missing")
    if not gates["formal_runs_enabled"]:
        blockers.append("formal_runs_disabled_by_config")

    return {
        "schema_version": "retrieval-eval-validation-v1",
        "config_hash": _canonical_config_hash(config),
        "config_status": config["status"],
        "formal_run_eligible": not blockers,
        "formal_run_blockers": blockers,
        "corpus": {
            "version": manifest["corpus_version"],
            "manifest_status": manifest["status"],
            "manifest_sha256": corpus_config["manifest_sha256"],
            "source_count": source_count,
            "repository_fixture_count": repository_fixture_count,
            "sensitivity_counts": dict(sorted(sensitivity_counts.items())),
        },
        "dataset": {
            "version": dataset_config["version"],
            "cases_sha256": dataset_config["cases_sha256"],
            "case_count": len(raw_lines),
            "split_counts": {split: len(ids) for split, ids in sorted(split_ids.items())},
            "included_split_counts": {
                split: included_split_counts[split] for split in sorted(split_ids)
            },
            "excluded_format_split_counts": {
                split: excluded_format_split_counts[split] for split in sorted(split_ids)
            },
            "category_counts": dict(sorted(category_counts.items())),
            "evidenced_case_count": evidenced_case_count,
            "no_evidence_case_count": no_evidence_case_count,
        },
        "protocol": config["protocol"],
        "runtime": config["runtime"],
    }


def _canonical_config_hash(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _manifest_sources(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    manifest = _load_yaml_mapping(_resolve_repository_path(config["corpus"]["manifest_path"]))
    sources: list[dict[str, Any]] = []
    for space in manifest["spaces"]:
        for source in space["sources"]:
            item = dict(source)
            item["space_id"] = space["id"]
            item["path"] = f"cases/{source['path']}"
            sources.append(item)
    return sources


def _load_cases(config: Mapping[str, Any], split: str) -> tuple[RetrievalEvaluationCase, ...]:
    path = _resolve_repository_path(config["dataset"]["cases_path"])
    included_source_formats = frozenset(config["protocol"]["included_source_formats"])
    source_formats = {
        str(item["source_key"]): str(item["format"]) for item in _manifest_sources(config)
    }
    cases: list[RetrievalEvaluationCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        if raw["split"] != split:
            continue
        if any(
            source_formats[item["source_key"]] not in included_source_formats
            for item in raw["evidence"]
        ):
            continue
        evidence = tuple(
            EvidenceUnit(
                source_key=item["source_key"],
                source_version=item["source_version"],
                locator=Locator.from_mapping(item["locator"]),
            )
            for item in raw["evidence"]
        )
        expectations = raw.get("retrieval_expectations") or {}
        must_exclude = tuple(expectations.get("must_exclude", ()))
        cases.append(
            RetrievalEvaluationCase(
                case_id=raw["id"],
                category=raw["category"],
                space_id=str(raw["space_id"]),
                query=raw["question"],
                gold_evidence=evidence,
                must_exclude=must_exclude,
            )
        )
    return tuple(cases)


def _space_uuid(space_key: str) -> UUID:
    return uuid5(EVALUATION_NAMESPACE, f"space:{space_key}")


def _source_uuid(source_key: str) -> UUID:
    return uuid5(EVALUATION_NAMESPACE, f"source:{source_key}")


def _create_gateway() -> ModelGateway:
    def secret(value: Any) -> str | None:
        return value.get_secret_value() if value else None

    return create_model_gateway(
        GatewayConfig(
            provider=ModelProvider(settings.model_provider),
            endpoint=settings.model_endpoint,
            api_key=secret(settings.model_api_key),
            fast_chat_endpoint=settings.fast_chat_endpoint,
            fast_chat_api_key=secret(settings.fast_chat_api_key),
            fast_chat_model=settings.fast_chat_model,
            embedding_endpoint=settings.embedding_endpoint,
            embedding_api_key=secret(settings.embedding_api_key),
            embedding_model=settings.embedding_model,
            reranker_endpoint=settings.reranker_endpoint,
            reranker_api_key=secret(settings.reranker_api_key),
            reranker_model=settings.reranker_model,
            embedding_protocol=settings.embedding_protocol,
            allow_external=settings.model_allow_external,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
        )
    )


class _ParserAdapter:
    def __init__(self) -> None:
        self._factory = ParserFactory()

    async def parse(self, raw: bytes, metadata: Any) -> Any:
        return await self._factory.parse(raw, metadata.file_name, metadata.mime_type)


async def _prepare_corpus(
    sources: Sequence[Mapping[str, Any]],
    *,
    database: Database,
    gateway: ModelGateway,
    identity: EmbeddingIdentity,
    blob_root: Path,
    source_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    if source_keys is not None:
        sources = tuple(item for item in sources if str(item["source_key"]) in source_keys)
    failures = _unsupported_source_failures(sources)
    succeeded = 0
    skipped = 0
    blob_store = LocalFileBlobStore(blob_root)
    async with database.session() as session:
        spaces = SpaceRepository(session)
        source_repo = SourceRepository(session)
        document_repo = DocumentRepository(session)
        version_repo = DocumentVersionRepository(session)
        task_repo = IngestionTaskRepository(session)
        for item in sources:
            source_key = str(item["source_key"])
            if get_parser(str(item["path"])) is None:
                continue
            space_id = _space_uuid(str(item["space_id"]))
            space = await spaces.get(space_id)
            if space is None:
                await spaces.create(
                    Space(id=space_id, name=str(item["space_id"]), owner_id="evaluation")
                )
            registered_source = await SourceRegistrationService(
                source_repo=source_repo,
                document_repo=document_repo,
                version_repo=version_repo,
            ).create_source(
                space_id,
                source_type=SourceType.UPLOAD,
                uri=source_key,
                source_id=_source_uuid(source_key),
            )
            raw = _resolve_repository_path(item["path"]).read_bytes()
            registration = await SourceRegistrationService(
                source_repo=source_repo,
                document_repo=document_repo,
                version_repo=version_repo,
            ).register_file(
                registered_source.source,
                raw,
                blob_store,
                file_stable_key=str(item["path"]),
                file_path=str(item["path"]),
            )
            document = await document_repo.get(registration.document.id)
            existing = (
                await version_repo.get(registration.version_id) if registration.version_id else None
            )
            if (
                existing is not None
                and document is not None
                and existing.status is DocumentStatus.PUBLISHED
                and document.current_version_id == existing.id
            ):
                skipped += 1
                continue
            task = await task_repo.create(
                IngestionTask(
                    source_id=registered_source.source.id,
                    operation=TaskOperation.INGEST,
                    target_version_id=registration.version_id,
                    max_retries=0,
                )
            )
            await session.commit()
            orchestrator = IngestionOrchestrator(
                source_repo=source_repo,
                document_repo=document_repo,
                version_repo=version_repo,
                chunk_repo=ChunkRepository(session),
                task_repo=task_repo,
                parser=_ParserAdapter(),
                chunker=StructureChunker(),
                text_embedder=GatewayQueryTextEmbedder(gateway),
                blob_store=blob_store,
            )
            try:
                await orchestrator.run_pipeline(
                    task,
                    config=IngestionConfig(
                        embedding_batch_size=settings.embedding_batch_size,
                        embedding_identity=identity,
                    ),
                )
                await session.commit()
                succeeded += 1
            except Exception as exc:
                diagnostic = type(exc).__name__
                if isinstance(exc, RetrievalError):
                    diagnostic = f"{diagnostic}:{exc.code.value}"
                print(
                    f"evaluation preparation failure source={source_key} diagnostic={diagnostic}",
                    file=sys.stderr,
                )
                try:
                    # A database/provider exception can leave the session in
                    # a failed transaction. Roll back before recording the
                    # task failure so one source cannot poison later sources.
                    await session.rollback()
                    await orchestrator.handle_pipeline_error(
                        task, ValueError("evaluation preparation failed")
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                failures.append(
                    {
                        "source_key": source_key,
                        "category": _preparation_failure_category(exc),
                    }
                )
    return {"succeeded": succeeded, "skipped": skipped, "failures": failures}


def _unsupported_source_failures(
    sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {"source_key": str(item["source_key"]), "category": "parser"}
        for item in sources
        if get_parser(str(item["path"])) is None
    ]


def _preparation_failure_category(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("Parse failed:"):
        return "parser"
    if isinstance(exc, RetrievalError):
        return "provider"
    return "infrastructure"


def _profile(experiment: Mapping[str, Any], identity: EmbeddingIdentity) -> RetrievalProfileV1:
    values = dict(experiment["profile"])
    values["hybrid_embedding_failure_policy"] = HybridEmbeddingFailurePolicy(
        values["hybrid_embedding_failure_policy"]
    )
    values["reranker_failure_policy"] = RerankerFailurePolicy(values["reranker_failure_policy"])
    return RetrievalProfileV1(
        embedding_version=identity.version,
        expected_embedding_dimensions=identity.dimensions,
        **values,
    )


async def _execute_case(
    case: RetrievalEvaluationCase,
    experiment: Mapping[str, Any],
    *,
    database: Database,
    gateway: ModelGateway,
    identity: EmbeddingIdentity,
) -> RetrievalEvaluationObservation:
    started = perf_counter()
    profile = _profile(experiment, identity)
    mode = RetrievalMode(experiment["mode"])
    try:
        async with database.session() as session:
            space_repo = SpaceRepository(session)
            service = SearchService(
                space_repo=space_repo,
                source_repo=SourceRepository(session),
                document_repo=DocumentRepository(session),
                retrieval_store=PostgresRetrievalStore(
                    session,
                    dense_mode=DenseSearchMode(experiment["dense_mode"]),
                    ivfflat_probes=int(experiment.get("ivfflat_probes", 10)),
                ),
                query_embedder=QueryEmbeddingService(
                    GatewayQueryTextEmbedder(gateway),
                    config=query_embedding_config(
                        identity,
                        timeout_seconds=settings.retrieval_timeout_seconds,
                    ),
                ),
                reranker=GatewayReranker(gateway),
            )
            result = await service.search(
                SearchRequest(
                    query=case.query,
                    space_id=_space_uuid(case.space_id),
                    mode=mode,
                    execution_context=SearchExecutionContext.OFFLINE_EVALUATION,
                ),
                profile,
            )
            version_repo = DocumentVersionRepository(session)
            converted: list[RetrievedChunk] = []
            for hit in result.hits:
                version = await version_repo.get(hit.version_id)
                if version is None:
                    raise RetrievalError(
                        code=RetrievalErrorCode.PROVIDER_POLICY_DENIED,
                        message="Evaluation hit version is unavailable.",
                    )
                converted.append(
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
            diagnostics = result.diagnostics
            return RetrievalEvaluationObservation(
                requested_mode=diagnostics.requested_mode.value,
                executed_mode=diagnostics.executed_mode.value,
                hits=tuple(converted),
                latency_ms=(perf_counter() - started) * 1000,
                profile_version=diagnostics.profile_version,
                embedding_version=diagnostics.embedding_version,
                reranker_version=diagnostics.reranker_version,
                keyword_index_version=diagnostics.keyword_index_version,
                dense_index_version=diagnostics.dense_index_version,
                candidate_counts=(
                    ("keyword", diagnostics.candidate_counts.keyword),
                    ("dense", diagnostics.candidate_counts.dense),
                    ("fused", diagnostics.candidate_counts.fused),
                    ("reranked", diagnostics.candidate_counts.reranked),
                    ("final", diagnostics.candidate_counts.final),
                ),
                degraded=diagnostics.degraded,
                degradation_reasons=tuple(
                    reason.value for reason in diagnostics.degradation_reasons
                ),
            )
    except RetrievalError as exc:
        return RetrievalEvaluationObservation(
            requested_mode=mode.value,
            executed_mode=mode.value,
            hits=(),
            latency_ms=(perf_counter() - started) * 1000,
            profile_version=profile.profile_version,
            error_code=str(exc.code),
        )
    except Exception:
        return RetrievalEvaluationObservation(
            requested_mode=mode.value,
            executed_mode=mode.value,
            hits=(),
            latency_ms=(perf_counter() - started) * 1000,
            profile_version=profile.profile_version,
            error_code="EVALUATION_INFRASTRUCTURE_ERROR",
        )


def _metric_dict(value: Any) -> dict[str, Any]:
    return {key: getattr(value, key) for key in value.__dataclass_fields__}


def _observation_dict(observation: RetrievalEvaluationObservation) -> dict[str, Any]:
    return {
        "requested_mode": observation.requested_mode,
        "executed_mode": observation.executed_mode,
        "latency_ms": round(observation.latency_ms, 3),
        "profile_version": observation.profile_version,
        "embedding_version": observation.embedding_version,
        "reranker_version": observation.reranker_version,
        "keyword_index_version": observation.keyword_index_version,
        "dense_index_version": observation.dense_index_version,
        "candidate_counts": dict(observation.candidate_counts),
        "degraded": observation.degraded,
        "degradation_reasons": list(observation.degradation_reasons),
        "error_code": observation.error_code,
        "hits": [
            {
                "chunk_id": hit.chunk_id,
                "source_key": hit.source_key,
                "source_version": hit.source_version,
                "rank": hit.rank,
                "locators": [
                    {"kind": loc.kind, "start": loc.start, "end": loc.end} for loc in hit.locators
                ],
                "keyword_rank": hit.keyword_rank,
                "keyword_score": hit.keyword_score,
                "dense_rank": hit.dense_rank,
                "dense_score": hit.dense_score,
                "fused_rank": hit.fused_rank,
                "fused_score": hit.fused_score,
                "rerank_rank": hit.rerank_rank,
                "rerank_score": hit.rerank_score,
                "context_only": hit.context_only,
            }
            for hit in observation.hits
        ],
    }


def _build_report(
    validation: Mapping[str, Any],
    config: Mapping[str, Any],
    split: str,
    experiment: Mapping[str, Any],
    result: RetrievalEvaluationResult,
    *,
    preparation: Mapping[str, Any],
    identity: EmbeddingIdentity,
) -> dict[str, Any]:
    return {
        "schema_version": "retrieval-report-v1",
        "run_id": uuid4().hex,
        "run_kind": "provisional_engineering",
        "selected_split": split,
        "git_commit": _git_commit(),
        "git_dirty": bool(_git_status()),
        "corpus_hash": validation["corpus"]["manifest_sha256"],
        "dataset_hash": validation["dataset"]["cases_sha256"],
        "config_hash": validation["config_hash"],
        "formal_run_eligible": validation["formal_run_eligible"],
        "formal_run_blockers": validation["formal_run_blockers"],
        "embedding_identity": {
            "version": identity.version,
            "model_revision": identity.model_revision,
            "dimensions": identity.dimensions,
            "query_instruction_version": identity.query_instruction_version,
            "document_instruction_version": identity.document_instruction_version,
            "normalization": identity.normalization,
            "precision": identity.precision,
        },
        "retrieval_experiment": {
            "id": experiment["id"],
            "mode": experiment["mode"],
            "dense_mode": experiment["dense_mode"],
            "profile": experiment["profile"],
            "profile_hash": _canonical_config_hash(experiment["profile"]),
        },
        "runtime": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "concurrency": config["runtime"]["concurrency"],
            "warmup_queries": config["runtime"]["warmup_queries"],
            "sampling": config["runtime"]["sampling"],
            "retrieval_p95_budget_ms": config["runtime"]["retrieval_p95_budget_ms"],
            "packages": {
                name: _package_version(name)
                for name in ("application", "domain", "infrastructure", "model-gateway", "pgvector")
            },
        },
        "preparation": preparation,
        "metrics": _metric_dict(result.metrics),
        "slices": {category: _metric_dict(metrics) for category, metrics in result.slices},
        "cases": [
            {
                "case_id": item.case_id,
                "category": item.category,
                "status": item.status,
                "metrics": _metric_dict(item.metrics),
                "failure_categories": [category.value for category in item.failure_categories],
                "observation": _observation_dict(item.observation),
            }
            for item in result.cases
        ],
    }


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_status() -> str:
    try:
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _privacy_scan(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_REPORT_KEYS:
                raise EvaluationConfigError(f"Report contains forbidden field: {key}")
            _privacy_scan(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _privacy_scan(child)


def _write_report(path: Path, bundle: Mapping[str, Any]) -> None:
    _privacy_scan(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reports = bundle["reports"]
    summary = [
        "# Retrieval Evaluation Summary",
        "",
        "This is an engineering report. It is not a Stage 0 or holdout acceptance record.",
        "",
    ]
    for report in reports:
        summary.extend(
            [
                f"## {report['retrieval_experiment']['id']}",
                "",
                f"- Run kind: `{report['run_kind']}`",
                f"- Split: `{report['selected_split']}`",
                f"- Formal eligible: `{report['formal_run_eligible']}`",
                f"- Evidence Recall@5: `{report['metrics']['evidence_recall_at_k']}`",
                f"- MRR: `{report['metrics']['mrr']}`",
                f"- P95 latency (ms): `{report['metrics']['latency_p95_ms']}`",
                f"- Failure rate: `{report['metrics']['failure_rate']}`",
                "",
            ]
        )
    path.with_suffix(".md").write_text("\n".join(summary) + "\n", encoding="utf-8")


async def _run_experiment(
    config: Mapping[str, Any],
    validation: Mapping[str, Any],
    split: str,
    experiment: Mapping[str, Any],
    *,
    prepare: bool,
    blob_root: Path,
    prepare_source_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    identity = settings.active_embedding_identity()
    database = Database(settings.database_url)
    gateway = _create_gateway()
    try:
        sources = _manifest_sources(config)
        preparation: dict[str, Any] = {
            "succeeded": 0,
            "skipped": 0,
            "failures": _unsupported_source_failures(sources),
        }
        if prepare:
            preparation = await _prepare_corpus(
                sources,
                database=database,
                gateway=gateway,
                identity=identity,
                blob_root=blob_root,
                source_keys=prepare_source_keys,
            )
        cases = list(_load_cases(config, split))
        for case in cases[: int(config["runtime"]["warmup_queries"])]:
            await _execute_case(
                case,
                experiment,
                database=database,
                gateway=gateway,
                identity=identity,
            )
        failed_sources = frozenset(item["source_key"] for item in preparation["failures"])
        result = await run_retrieval_evaluation(
            cases,
            lambda case: _execute_case(
                case, experiment, database=database, gateway=gateway, identity=identity
            ),
            k=int(config["protocol"]["recall_k"]),
            failed_source_keys=failed_sources,
        )
        return _build_report(
            validation,
            config,
            split,
            experiment,
            result,
            preparation=preparation,
            identity=identity,
        )
    finally:
        await gateway.aclose()
        await database.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Repository-relative evaluation YAML")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument(
        "--output", help="Optional JSON report path; a Markdown summary is written beside it"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate frozen inputs without executing retrieval",
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
        "--experiment", action="append", help="Run only the named experiment; repeatable"
    )
    parser.add_argument("--confirm-holdout", help="Required for an eligible formal holdout run")
    parser.add_argument("--blob-root", default="tmp/retrieval-eval-blobs")
    parser.add_argument("--quiet", action="store_true", help="Do not print the report bundle")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config_path = _resolve_repository_path(args.config)
        config = _load_yaml_mapping(config_path)
        config_schema = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
        validation = validate_evaluation_config(config, config_schema=config_schema)
    except (EvaluationConfigError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
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

    experiments = list(config["experiments"])
    selected = set(args.experiment or (item["id"] for item in experiments))
    unknown = selected - {item["id"] for item in experiments}
    if unknown:
        print(f"unknown experiment: {sorted(unknown)}", file=sys.stderr)
        return 2
    reports: list[dict[str, Any]] = []
    try:
        for experiment in experiments:
            if experiment["id"] not in selected:
                continue
            reports.append(
                asyncio.run(
                    _run_experiment(
                        config,
                        validation,
                        args.split,
                        experiment,
                        prepare=args.prepare_corpus,
                        blob_root=(REPOSITORY_ROOT / args.blob_root).resolve(),
                        prepare_source_keys=(
                            frozenset(args.prepare_source) if args.prepare_source else None
                        ),
                    )
                )
            )
    except (EvaluationConfigError, OSError, ValueError) as exc:
        print(f"evaluation execution rejected: {exc}", file=sys.stderr)
        return 2
    output = {"schema_version": "retrieval-report-bundle-v1", "reports": reports}
    try:
        _privacy_scan(output)
        if args.output:
            _write_report(Path(args.output).resolve(), output)
    except (EvaluationConfigError, OSError) as exc:
        print(f"evaluation report rejected: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
