from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agent_runtime import FileSystemSkillRegistry
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_catalog import FileSystemSkillCatalog

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills"
WORKFLOW_SKILLS = {
    "research_reading_workflow": ("research_reading_workflow", "1.1.0"),
    "exam_preparation_workflow": ("exam_preparation_workflow", "1.2.1"),
    "course_project_workflow": ("course_project_workflow", "1.0.0"),
}


def _schema(package_directory: str, name: str) -> dict[str, object]:
    path = SKILL_ROOT / package_directory / "schemas" / name
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_student_workflow_packages_load_pin_and_remain_inactive() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()

    for name, (_directory, version) in WORKFLOW_SKILLS.items():
        package = registry.get(name, version)
        pin = registry.pin(name, version)

        assert package.manifest.manifest_version == "2"
        assert package.manifest.permissions
        assert pin.content_sha256 == package.content_sha256
        assert registry.validate_pin(pin) is package

    active_catalog = FileSystemSkillCatalog(assistant_skill_registry(), include_manifest_v2=True)
    active_names = {item.name for item in active_catalog.list_active_invocations()}
    assert "research_reading_workflow" in active_names
    assert "exam_preparation_workflow" in active_names
    assert "course_project_workflow" not in active_names


def test_student_workflow_commands_and_aliases_are_unique() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()
    command_owners: dict[str, str] = {}

    for name, (_directory, version) in WORKFLOW_SKILLS.items():
        invocation = registry.get(name, version).manifest.invocation
        assert invocation is not None
        for command in invocation.commands:
            assert command not in command_owners, (
                f"command {command!r} is shared by {name!r} and {command_owners[command]!r}"
            )
            command_owners[command] = name


def test_research_contract_is_rag_only_and_uses_the_qa_projection() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()
    package = registry.get("research_reading_workflow", "1.1.0")
    invocation = package.manifest.invocation
    assert invocation is not None
    assert invocation.execution_mode == "projected"

    input_schema = _schema("research_reading_workflow", "input.json")
    input_properties = input_schema["properties"]
    assert isinstance(input_properties, dict)
    assert {
        "reproduction_constraints",
        "experiment_observations",
        "candidate_hypotheses",
    }.isdisjoint(input_properties)

    output_schema = _schema("research_reading_workflow", "output.json")
    output_properties = output_schema["properties"]
    assert isinstance(output_properties, dict)
    assert {
        "reproduction_plan",
        "candidate_hypotheses",
        "experiment_observations",
    }.isdisjoint(output_properties)
    assert output_properties["schema_version"] == {"const": "research-reading-workflow-output-v2"}


def test_exam_question_papers_cannot_contain_answers_or_explanations() -> None:
    schema = _schema("exam_preparation_workflow", "output.json")
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    question = definitions["question"]
    assert isinstance(question, dict)
    properties = question["properties"]
    assert isinstance(properties, dict)
    assert {"answer", "correct_options", "explanation"}.isdisjoint(properties)

    review = definitions["review"]
    assert isinstance(review, dict)
    review_properties = review["properties"]
    assert isinstance(review_properties, dict)
    assert "items" in review_properties


def test_defense_questions_cannot_contain_hidden_reference_points() -> None:
    schema = _schema("course_project_workflow", "output.json")
    properties = schema["properties"]
    assert isinstance(properties, dict)
    defense_quiz = properties["defense_quiz"]
    assert isinstance(defense_quiz, dict)
    quiz_properties = defense_quiz["properties"]
    assert isinstance(quiz_properties, dict)
    questions = quiz_properties["questions"]
    assert isinstance(questions, dict)
    question = questions["items"]
    assert isinstance(question, dict)
    question_properties = question["properties"]
    assert isinstance(question_properties, dict)
    assert {"reference_points", "suggested_answer"}.isdisjoint(question_properties)
    assert "defense_review" in properties


def test_eval_cases_are_synthetic_only() -> None:
    for package_directory, _version in WORKFLOW_SKILLS.values():
        cases_path = SKILL_ROOT / package_directory / "evals" / "cases.jsonl"
        cases = [
            json.loads(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert cases
        assert all(case.get("fixture") == "synthetic_only" for case in cases)
