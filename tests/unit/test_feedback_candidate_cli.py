from pathlib import Path

import pytest

from scripts.export_feedback_candidates import _assert_output_path, _manifest_policies


def test_candidate_export_rejects_frozen_or_existing_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="frozen"):
        _assert_output_path(tmp_path / "holdout" / "candidates.jsonl")
    existing = tmp_path / "development.jsonl"
    existing.write_text("metadata-only\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _assert_output_path(existing)


def test_manifest_policy_loader_reads_only_policy_metadata() -> None:
    policies = _manifest_policies(Path("cases/evals/corpus/v0/manifest.yaml"))
    assert policies["omnistudio/readme"] == (
        "public_demo",
        frozenset({"local_development", "local_evaluation", "repository_fixture"}),
    )
