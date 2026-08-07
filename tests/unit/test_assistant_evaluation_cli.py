import json

from scripts import evaluate_assistant_routing


def test_routing_evaluator_validates_synthetic_development_inputs(capsys: object) -> None:
    result = evaluate_assistant_routing.main(["--validate-only"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert result == 0
    summary = json.loads(captured.out)
    assert summary == {
        "case_count": 8,
        "dataset_split": "development",
        "formal_run_eligible": False,
        "quality_status": "provisional",
        "schema_version": "assistant-metrics-report-v1",
    }
    assert "fictional" not in captured.out


def test_routing_evaluator_blocks_execution_without_prediction_metadata(capsys: object) -> None:
    result = evaluate_assistant_routing.main([])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert result == 4
    assert "predictions metadata is required" in captured.out
