from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pytest import MonkeyPatch

from scripts import evaluate_ingestion


def _manifest(source_hash: str, *, status: str = "frozen") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "corpus_version": "test-v0",
        "status": status,
        "distribution_scope": "internal_team_only",
        "spaces": [
            {
                "id": "test",
                "sources": [
                    {
                        "source_key": "test/note",
                        "path": "fixtures/note.md",
                        "format": "markdown",
                        "allowed_uses": ["local_evaluation"],
                        "content_sha256": source_hash,
                    },
                    {
                        "source_key": "test/code",
                        "path": "fixtures/code.py",
                        "format": "code_python",
                        "allowed_uses": ["local_evaluation"],
                        "content_sha256": "0" * 64,
                    },
                ],
            }
        ],
    }


def _write_fixture(root: Path, monkeypatch: MonkeyPatch) -> tuple[Path, str]:
    source = b"# Heading\n\nA manifest-approved fixture.\n"
    source_hash = hashlib.sha256(source).hexdigest()
    source_path = root / "cases" / "fixtures" / "note.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest(source_hash)), encoding="utf-8")
    monkeypatch.setattr(evaluate_ingestion, "REPOSITORY_ROOT", root)
    return manifest_path, evaluate_ingestion._sha256_path(manifest_path)


@pytest.mark.asyncio
async def test_evaluation_parses_only_p0_sources(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manifest_path, manifest_hash = _write_fixture(tmp_path, monkeypatch)
    manifest = evaluate_ingestion._load_manifest(manifest_path, manifest_hash)

    report = await evaluate_ingestion.evaluate_manifest(
        manifest,
        manifest_sha256=manifest_hash,
        minimum_success_rate=0.95,
    )

    assert report["gate"]["passed"] is True
    assert report["totals"] == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert report["formats"]["markdown"]["success_rate"] == 1.0
    assert "source_key" not in str(report)
    assert "fixture" not in str(report)


def test_cli_rejects_manifest_hash_mismatch(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: Any
) -> None:
    manifest_path, _ = _write_fixture(tmp_path, monkeypatch)

    result = evaluate_ingestion.main(
        ["--manifest", manifest_path.name, "--expected-manifest-sha256", "0" * 64]
    )

    assert result == 2
    assert "Manifest SHA-256 mismatch" in capsys.readouterr().err


def test_formal_evaluation_requires_frozen_manifest(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    source_hash = hashlib.sha256(b"content").hexdigest()
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(_manifest(source_hash, status="draft")), encoding="utf-8"
    )
    monkeypatch.setattr(evaluate_ingestion, "REPOSITORY_ROOT", tmp_path)

    with pytest.raises(evaluate_ingestion.IngestionEvaluationError, match="frozen manifest"):
        evaluate_ingestion._load_manifest(
            manifest_path, evaluate_ingestion._sha256_path(manifest_path)
        )


def test_repository_path_cannot_escape_workspace(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(evaluate_ingestion, "REPOSITORY_ROOT", tmp_path)
    with pytest.raises(evaluate_ingestion.IngestionEvaluationError, match="escapes repository"):
        evaluate_ingestion._resolve_repository_file("../outside")
