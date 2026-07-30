import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import evaluate_answers


def test_repository_qa_config_validates_without_executing_cases(capsys: object) -> None:
    dataset = (
        Path(__file__).resolve().parents[2] / "cases/evals/datasets/knowledge-qa-v0/cases.jsonl"
    )
    if not dataset.is_file():
        pytest.skip(
            "internal_team_only evaluation dataset is intentionally absent from CI checkout"
        )
    result = evaluate_answers.main(["--validate-only"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert result == 0
    summary = json.loads(captured.out)
    assert summary["report_schema_version"] == "answer-report-v1"
    assert summary["formal_run_eligible"] is False
    assert "question" not in captured.out
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "cases/evals/configs/answer-report-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(summary)


def test_execution_is_blocked_while_config_is_provisional(capsys: object) -> None:
    result = evaluate_answers.main([])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert result == 4
    assert "execution blocked" in captured.out
