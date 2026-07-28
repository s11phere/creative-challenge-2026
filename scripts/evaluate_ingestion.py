"""Evaluate Stage 2 parsing and chunking against the frozen corpus manifest.

The report deliberately contains aggregate metadata only. Source paths, source
keys, extracted text, chunks, and embeddings are never serialized.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

import yaml
from domain.parsing import ParseError, ParseSuccess, StructNode
from infrastructure.chunkers import StructureChunker
from infrastructure.parsers import ParserFactory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = Path("cases/evals/corpus/v0/manifest.yaml")
FROZEN_MANIFEST_SHA256 = "53d6f863060dd7d5e6affb0abda64348f0b498995b3d1cda5ef80f0e576ca738"
P0_FORMATS = frozenset({"markdown", "text", "pdf"})
PARSER_VERSION = "1.0"
NORMALIZER_VERSION = "1.0"
CHUNKER_VERSION = "1.0"


class IngestionEvaluationError(ValueError):
    """Raised when frozen inputs or parser output violate the protocol."""


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_repository_file(raw_path: str | Path) -> Path:
    candidate = (REPOSITORY_ROOT / raw_path).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise IngestionEvaluationError(f"Path escapes repository root: {raw_path}") from exc
    if not candidate.is_file():
        raise IngestionEvaluationError(f"Required file does not exist: {raw_path}")
    return candidate


def _load_manifest(path: Path, expected_sha256: str) -> Mapping[str, Any]:
    actual_sha256 = _sha256_path(path)
    if actual_sha256 != expected_sha256:
        raise IngestionEvaluationError(
            f"Manifest SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise IngestionEvaluationError("Manifest must be a YAML object")
    if manifest.get("status") != "frozen":
        raise IngestionEvaluationError("Formal ingestion evaluation requires a frozen manifest")
    if manifest.get("distribution_scope") != "internal_team_only":
        raise IngestionEvaluationError("Unexpected corpus distribution scope")
    return manifest


def _iter_nodes(nodes: Sequence[StructNode]) -> Sequence[StructNode]:
    flattened: list[StructNode] = []
    pending = list(nodes)
    while pending:
        node = pending.pop()
        flattened.append(node)
        pending.extend(node.children)
    return flattened


def _validate_locators(result: ParseSuccess, source_format: str) -> None:
    nodes = _iter_nodes(result.document.structure)
    if not nodes:
        raise IngestionEvaluationError("Parser returned no structural nodes")
    for node in nodes:
        if node.start_line < 1 or node.end_line < node.start_line:
            raise IngestionEvaluationError("Parser returned an invalid 1-based line locator")
        if source_format == "pdf" and (
            node.start_page is None
            or node.end_page is None
            or node.start_page < 1
            or node.end_page < node.start_page
        ):
            raise IngestionEvaluationError("PDF parser returned an invalid 1-based page locator")


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _timing_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "mean_ms": round(mean(values), 3) if values else 0.0,
        "p95_ms": round(_percentile(values, 0.95), 3),
        "total_ms": round(sum(values), 3),
    }


async def evaluate_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_sha256: str,
    minimum_success_rate: float,
) -> dict[str, Any]:
    """Parse and chunk every P0 source after validating its raw-byte hash."""
    parser = ParserFactory()
    chunker = StructureChunker()
    attempts: Counter[str] = Counter()
    successes: Counter[str] = Counter()
    failures: dict[str, Counter[str]] = defaultdict(Counter)
    timings: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    spaces = manifest.get("spaces")
    if not isinstance(spaces, list):
        raise IngestionEvaluationError("Manifest spaces must be a list")
    for space in spaces:
        if not isinstance(space, Mapping):
            raise IngestionEvaluationError("Manifest contains an invalid Space")
        sources = space.get("sources")
        if not isinstance(sources, list):
            raise IngestionEvaluationError("Manifest Space sources must be a list")
        for source in sources:
            if not isinstance(source, Mapping):
                raise IngestionEvaluationError("Manifest contains an invalid source")
            source_format = source.get("format")
            if source_format not in P0_FORMATS:
                continue
            allowed_uses = source.get("allowed_uses")
            if not isinstance(allowed_uses, list) or "local_evaluation" not in allowed_uses:
                raise IngestionEvaluationError("A P0 source does not allow local evaluation")
            relative_path = source.get("path")
            expected_hash = source.get("content_sha256")
            if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
                raise IngestionEvaluationError("A P0 source has invalid path or hash metadata")

            attempts[source_format] += 1
            started = perf_counter()
            source_path = _resolve_repository_file(Path("cases") / relative_path)
            raw = source_path.read_bytes()
            timings[source_format]["read"].append((perf_counter() - started) * 1000)

            started = perf_counter()
            actual_hash = _sha256_bytes(raw)
            timings[source_format]["hash"].append((perf_counter() - started) * 1000)
            if actual_hash != expected_hash:
                raise IngestionEvaluationError("A P0 source SHA-256 does not match the manifest")

            started = perf_counter()
            result = await parser.parse(raw, source_path.name)
            timings[source_format]["parse"].append((perf_counter() - started) * 1000)
            if isinstance(result, ParseError):
                failures[source_format][result.code.value] += 1
                continue
            _validate_locators(result, source_format)

            started = perf_counter()
            chunking = await chunker.chunk(result.document)
            timings[source_format]["chunk"].append((perf_counter() - started) * 1000)
            if not chunking.chunks:
                failures[source_format]["empty_chunk_set"] += 1
                continue
            successes[source_format] += 1

    total_attempts = sum(attempts.values())
    total_successes = sum(successes.values())
    if total_attempts == 0:
        raise IngestionEvaluationError("Frozen manifest contains no P0 sources")
    success_rate = total_successes / total_attempts
    formats: dict[str, Any] = {}
    for source_format in sorted(P0_FORMATS):
        count = attempts[source_format]
        succeeded = successes[source_format]
        formats[source_format] = {
            "attempted": count,
            "succeeded": succeeded,
            "failed": count - succeeded,
            "success_rate": round(succeeded / count, 6) if count else None,
            "failure_codes": dict(sorted(failures[source_format].items())),
            "timings": {
                stage: _timing_summary(values)
                for stage, values in sorted(timings[source_format].items())
            },
        }
    return {
        "schema_version": "stage2-ingestion-evaluation-v1",
        "run_kind": "formal_internal",
        "corpus": {
            "version": manifest.get("corpus_version"),
            "manifest_sha256": manifest_sha256,
            "status": manifest.get("status"),
            "distribution_scope": manifest.get("distribution_scope"),
        },
        "processing": {
            "parser_version": PARSER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "python": platform.python_version(),
            "pymupdf": importlib.metadata.version("PyMuPDF"),
        },
        "gate": {
            "minimum_success_rate": minimum_success_rate,
            "actual_success_rate": round(success_rate, 6),
            "passed": success_rate >= minimum_success_rate,
        },
        "totals": {
            "attempted": total_attempts,
            "succeeded": total_successes,
            "failed": total_attempts - total_successes,
        },
        "formats": formats,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--expected-manifest-sha256", default=FROZEN_MANIFEST_SHA256)
    parser.add_argument("--minimum-success-rate", type=float, default=0.95)
    parser.add_argument("--output")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not 0 <= args.minimum_success_rate <= 1:
        print("minimum success rate must be between 0 and 1", file=sys.stderr)
        return 2
    try:
        manifest_path = _resolve_repository_file(args.manifest)
        manifest = _load_manifest(manifest_path, args.expected_manifest_sha256)
        report = asyncio.run(
            evaluate_manifest(
                manifest,
                manifest_sha256=args.expected_manifest_sha256,
                minimum_success_rate=args.minimum_success_rate,
            )
        )
        if args.output:
            _write_report(Path(args.output).resolve(), report)
    except (IngestionEvaluationError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ingestion evaluation rejected: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate"]["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
