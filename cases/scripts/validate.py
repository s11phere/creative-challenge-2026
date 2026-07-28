#!/usr/bin/env python3
"""Validate the Stage 0 corpus and evidence-backed evaluation dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
import yaml
from jsonschema import Draft202012Validator

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "evals" / "corpus" / "v0" / "manifest.yaml"
CASES_PATH = ROOT / "evals" / "datasets" / "knowledge-qa-v0" / "cases.jsonl"
SCHEMA_PATH = ROOT / "evals" / "datasets" / "knowledge-qa-v0" / "schema.json"
PDF_VISUAL_REVIEW_PATH = ROOT / "evals" / "corpus" / "v0" / "PDF-VISUAL-REVIEW.yaml"

SOURCE_FIELDS = {
    "source_key",
    "path",
    "format",
    "language",
    "license",
    "sensitivity",
    "allowed_uses",
    "redistribution",
    "content_sha256",
}
SENSITIVITIES = {"private_local", "public_demo", "restricted"}
FORMAT_EXTENSIONS = {
    "markdown": {".md", ".markdown"},
    "text": {".txt"},
    "pdf": {".pdf"},
    "code_python": {".py"},
    "code_cpp": {".cpp", ".cc", ".cxx"},
    "code_c": {".c"},
    "code_sql": {".sql"},
    "notebook": {".ipynb"},
}
PDF_EXTRACTION_PYTHON = (3, 12)
PDF_EXTRACTION_PYMUPDF_VERSION = "1.28.0"
PDF_EXTRACTION_PROTOCOL = "python3.12-pymupdf-1.28.0-Page.get_text-text-nfkc-whitespace-v1"


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def excerpt_sha256(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_lines(path: Path) -> list[str]:
    """Return locator lines; notebooks use their ordered cell source lines."""
    if path.suffix.lower() == ".ipynb":
        notebook = json.loads(path.read_text(encoding="utf-8"))
        lines: list[str] = []
        for cell in notebook.get("cells", []):
            source = cell.get("source", [])
            if isinstance(source, str):
                lines.extend(source.splitlines())
            elif isinstance(source, list):
                for value in source:
                    lines.extend(str(value).splitlines())
        return lines
    return path.read_text(encoding="utf-8").splitlines()


class Audit:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []
        self.stats: Counter[str] = Counter()

    def error(self, category: str, message: str) -> None:
        self.issues.append((category, message))
        self.stats[category] += 1


def load_inputs(
    audit: Audit,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[tuple[str, int], dict[str, Any]],
]:
    try:
        manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cases = [
            json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line
        ]
        visual_document = yaml.safe_load(PDF_VISUAL_REVIEW_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        audit.error("input", str(exc))
        return {}, {}, [], {}
    if not isinstance(manifest, dict) or not isinstance(schema, dict):
        audit.error("input", "manifest and schema must be mappings")
        return {}, {}, [], {}
    if not isinstance(visual_document, dict):
        audit.error("visual_review", "PDF-VISUAL-REVIEW.yaml must be a mapping")
        return manifest, schema, cases, {}
    if visual_document.get("protocol") != PDF_EXTRACTION_PROTOCOL:
        audit.error(
            "visual_review", "visual review protocol does not match PDF extraction protocol"
        )
    visual_reviews: dict[tuple[str, int], dict[str, Any]] = {}
    entries = visual_document.get("entries")
    if not isinstance(entries, list):
        audit.error("visual_review", "visual review entries must be a list")
    else:
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                audit.error("visual_review", f"entry {index}: expected a mapping")
                continue
            case_id = entry.get("case_id")
            evidence_index = entry.get("evidence_index")
            if not isinstance(case_id, str) or not isinstance(evidence_index, int):
                audit.error("visual_review", f"entry {index}: invalid case_id/evidence_index")
                continue
            key = (case_id, evidence_index)
            if key in visual_reviews:
                audit.error(
                    "visual_review", f"duplicate entry {case_id} evidence[{evidence_index}]"
                )
                continue
            if entry.get("status") != "verified":
                audit.error(
                    "visual_review", f"{case_id} evidence[{evidence_index}]: status is not verified"
                )
            transcription = entry.get("transcription")
            if not isinstance(transcription, str) or not transcription.strip():
                audit.error(
                    "visual_review", f"{case_id} evidence[{evidence_index}]: missing transcription"
                )
            elif any(
                unicodedata.category(char) in {"Cc", "Cf", "Co", "Cs"} and char not in "\n\r\t"
                for char in transcription
            ):
                audit.error(
                    "visual_review",
                    f"{case_id} evidence[{evidence_index}]: transcription contains control/private-use characters",
                )
            visual_reviews[key] = entry
    return manifest, schema, cases, visual_reviews


def validate_sources(
    audit: Audit, manifest: dict[str, Any]
) -> dict[str, tuple[str, dict[str, Any], Path]]:
    sources: dict[str, tuple[str, dict[str, Any], Path]] = {}
    source_paths: set[str] = set()
    for space in manifest.get("spaces", []):
        if not isinstance(space, dict) or not isinstance(space.get("id"), str):
            audit.error("manifest", "invalid Space entry")
            continue
        space_id = space["id"]
        for source in space.get("sources", []):
            if not isinstance(source, dict):
                audit.error("manifest", f"{space_id}: invalid source entry")
                continue
            missing = sorted(SOURCE_FIELDS - source.keys())
            key = source.get("source_key")
            if missing:
                audit.error("manifest", f"{key or space_id}: missing fields {missing}")
                continue
            if not isinstance(key, str) or key in sources:
                audit.error("manifest", f"invalid or duplicate source_key: {key}")
                continue
            if not key.startswith(f"{space_id}/"):
                audit.error("manifest", f"{key}: source_key does not belong to {space_id}")
            relative_path = source["path"]
            if not isinstance(relative_path, str) or relative_path in source_paths:
                audit.error("manifest", f"{key}: invalid or duplicate path {relative_path}")
                continue
            source_path = (ROOT / relative_path).resolve()
            if ROOT not in source_path.parents:
                audit.error("manifest", f"{key}: path escapes cases root")
                continue
            source_paths.add(relative_path)
            sources[key] = (space_id, source, source_path)

            sensitivity = source.get("sensitivity")
            if sensitivity not in SENSITIVITIES:
                audit.error("policy", f"{key}: invalid sensitivity {sensitivity}")
            allowed_uses = source.get("allowed_uses")
            if not isinstance(allowed_uses, list) or not allowed_uses:
                audit.error("policy", f"{key}: allowed_uses must be a non-empty list")
            elif "repository_fixture" in allowed_uses and sensitivity != "public_demo":
                audit.error("policy", f"{key}: repository_fixture must be public_demo")
            source_format = source.get("format")
            suffix = source_path.suffix.lower()
            if source_format not in FORMAT_EXTENSIONS:
                audit.error("manifest", f"{key}: unsupported format {source_format}")
            elif suffix not in FORMAT_EXTENSIONS[source_format]:
                audit.error("manifest", f"{key}: {source_format} does not match {suffix}")

            expected_hash = source.get("content_sha256")
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                audit.error("source_hash", f"{key}: invalid content_sha256")
            elif not source_path.is_file():
                audit.error("source_hash", f"{key}: missing file {relative_path}")
            elif file_sha256(source_path) != expected_hash:
                audit.error("source_hash", f"{key}: content_sha256 mismatch")

    control_directories = {"docs", "evals", "scripts"}
    for directory in ROOT.iterdir():
        if not directory.is_dir() or directory.name in control_directories:
            continue
        for candidate in directory.rglob("*"):
            if candidate.is_file() and candidate.relative_to(ROOT).as_posix() not in source_paths:
                audit.error(
                    "manifest",
                    f"unlisted corpus file: {candidate.relative_to(ROOT).as_posix()}",
                )

    internal_only = manifest.get("distribution_scope") == "internal_team_only"
    if manifest.get("status") == "frozen":
        for key, (_, source, _) in sources.items():
            if (
                source.get("license") == "undetermined"
                or source.get("redistribution") == "review_required"
            ) and not internal_only:
                audit.error("policy", f"{key}: unresolved rights in frozen corpus")
            if internal_only and "repository_fixture" in source.get("allowed_uses", []):
                if source.get("sensitivity") != "public_demo":
                    audit.error(
                        "policy",
                        f"{key}: internal-only unresolved source cannot be a repository_fixture",
                    )
    return sources


def validate_cases(
    audit: Audit,
    schema: dict[str, Any],
    cases: list[dict[str, Any]],
    sources: dict[str, tuple[str, dict[str, Any], Path]],
    visual_reviews: dict[tuple[str, int], dict[str, Any]],
) -> Counter[str]:
    validator = Draft202012Validator(schema)
    seen_ids: set[str] = set()
    questions: dict[str, set[str]] = {"development": set(), "holdout": set()}
    split_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    pdf_readers: dict[Path, fitz.Document] = {}

    for line_number, case in enumerate(cases, start=1):
        case_id = case.get("id", f"line-{line_number}")
        for issue in validator.iter_errors(case):
            location = ".".join(str(part) for part in issue.absolute_path) or "<root>"
            audit.error("schema", f"{case_id} {location}: {issue.message}")
        if case_id in seen_ids:
            audit.error("dataset", f"duplicate case ID: {case_id}")
        seen_ids.add(case_id)
        split = case.get("split")
        if isinstance(split, str):
            split_counts[split] += 1
        category = case.get("category")
        if isinstance(category, str):
            category_counts[category] += 1
        question_key = normalize_text(str(case.get("question", ""))).casefold()
        if split in questions:
            if question_key in questions[split]:
                audit.error("dataset", f"{case_id}: duplicate question in {split}")
            questions[split].add(question_key)

        claim_ids = {claim.get("id") for claim in case.get("answer_claims", [])}
        supported_claims: set[str] = set()
        for index, evidence in enumerate(case.get("evidence", []), start=1):
            prefix = f"{case_id} evidence[{index}]"
            key = evidence.get("source_key")
            source_entry = sources.get(key)
            if source_entry is None:
                audit.error("evidence_link", f"{prefix}: source outside manifest: {key}")
                continue
            source_space, source, source_path = source_entry
            if source_space != case.get("space_id"):
                audit.error("evidence_link", f"{prefix}: crosses Space boundary")
            if evidence.get("source_version") != source.get("content_sha256"):
                audit.error("evidence_link", f"{prefix}: source_version mismatch")
            quote = str(evidence.get("quote", ""))
            if evidence.get("excerpt_sha256") != excerpt_sha256(quote):
                audit.error("evidence_hash", f"{prefix}: excerpt_sha256 mismatch")
            supported_claims.update(evidence.get("supports_claims", []))
            validate_locator(
                audit, prefix, evidence.get("locator", {}), quote, source_path, pdf_readers
            )
            visual_review = visual_reviews.get((case_id, index))
            if visual_review is not None:
                if source.get("format") != "pdf":
                    audit.error(
                        "visual_review", f"{prefix}: visual review is only valid for PDF evidence"
                    )
                locator = evidence.get("locator", {})
                if visual_review.get("source_key") != key:
                    audit.error(
                        "visual_review", f"{prefix}: source_key does not match visual review"
                    )
                if visual_review.get("page") != locator.get("page"):
                    audit.error("visual_review", f"{prefix}: page does not match visual review")

        missing_support = claim_ids - supported_claims
        if missing_support:
            audit.error("evidence_link", f"{case_id}: unsupported claims {sorted(missing_support)}")

    overlap = questions["development"] & questions["holdout"]
    if overlap:
        audit.error("dataset", f"development/holdout share {len(overlap)} questions")
    seen_visual_keys = {
        (case.get("id"), index)
        for case in cases
        for index, evidence in enumerate(case.get("evidence", []), start=1)
        if (case.get("id"), index) in visual_reviews
    }
    for case_id, evidence_index in sorted(set(visual_reviews) - seen_visual_keys):
        audit.error("visual_review", f"orphan visual review {case_id} evidence[{evidence_index}]")
    print(f"  splits: {dict(split_counts)}")
    print(f"  categories: {dict(category_counts)}")
    print(f"  visual reviews: {len(visual_reviews)}")
    return split_counts


def validate_locator(
    audit: Audit,
    prefix: str,
    locator: dict[str, Any],
    quote: str,
    source_path: Path,
    pdf_readers: dict[Path, fitz.Document],
) -> None:
    locator_type = locator.get("type")
    if locator_type == "lines":
        try:
            lines = source_lines(source_path)
        except (OSError, UnicodeDecodeError) as exc:
            audit.error("text_locator", f"{prefix}: cannot read source: {exc}")
            return
        start, end = locator.get("start"), locator.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            audit.error("text_locator", f"{prefix}: invalid range {start}-{end}")
        elif end > len(lines):
            audit.error("text_locator", f"{prefix}: line {end} exceeds {len(lines)}")
        elif normalize_text(quote) not in normalize_text("\n".join(lines[start - 1 : end])):
            audit.error("text_locator", f"{prefix}: quote not found in lines {start}-{end}")
    elif locator_type == "pdf_page":
        try:
            reader = pdf_readers.get(source_path)
            if reader is None:
                reader = fitz.open(source_path)
                pdf_readers[source_path] = reader
            page = locator.get("page")
            if not isinstance(page, int) or not 1 <= page <= reader.page_count:
                audit.error("pdf_locator", f"{prefix}: page {page} outside 1-{reader.page_count}")
            else:
                extracted = normalize_text(reader[page - 1].get_text("text") or "")
                if normalize_text(quote) not in extracted:
                    audit.error("pdf_locator", f"{prefix}: quote not found on PDF page {page}")
        except Exception as exc:
            audit.error("pdf_locator", f"{prefix}: PDF inspection failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", "-q", action="store_true", help="print only the summary")
    parser.add_argument("--max-issues", type=int, default=50, help="maximum issue details to print")
    args = parser.parse_args()

    audit = Audit()
    if sys.version_info[:2] != PDF_EXTRACTION_PYTHON:
        audit.error(
            "environment",
            f"PDF extraction requires Python 3.12.x, found {sys.version_info.major}.{sys.version_info.minor}",
        )
    if fitz.VersionBind != PDF_EXTRACTION_PYMUPDF_VERSION:
        audit.error(
            "environment",
            f"PDF extraction requires PyMuPDF {PDF_EXTRACTION_PYMUPDF_VERSION}, found {fitz.VersionBind}",
        )
    manifest, schema, cases, visual_reviews = load_inputs(audit)
    sources = validate_sources(audit, manifest) if manifest else {}
    if schema and cases:
        validate_cases(audit, schema, cases, sources, visual_reviews)

    print(f"  root: {ROOT}")
    print(f"  spaces: {len(manifest.get('spaces', [])) if manifest else 0}")
    print(f"  sources: {len(sources)}")
    print(f"  cases: {len(cases)}")
    print(f"  pdf extraction: {PDF_EXTRACTION_PROTOCOL}")
    print(f"  issue categories: {dict(audit.stats)}")
    if audit.issues and not args.quiet:
        for category, message in audit.issues[: max(args.max_issues, 0)]:
            print(f"  [ERROR:{category}] {message}")
        remaining = len(audit.issues) - max(args.max_issues, 0)
        if remaining > 0:
            print(f"  ... {remaining} additional issues omitted")
    if audit.issues:
        print(f"[FAIL] {len(audit.issues)} validation issues")
        return 1
    print("[OK] all validation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
