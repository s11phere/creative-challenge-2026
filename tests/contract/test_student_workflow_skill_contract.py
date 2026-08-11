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
    "research_reading_workflow": "research_reading_workflow",
    "exam_preparation_workflow": "exam_preparation_workflow",
    "course_project_workflow": "course_project_workflow",
}


def _schema(package_directory: str, name: str) -> dict[str, object]:
    path = SKILL_ROOT / package_directory / "schemas" / name
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_student_workflow_packages_load_pin_and_remain_inactive() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()

    for name in WORKFLOW_SKILLS:
        package = registry.get(name, "1.0.0")
        pin = registry.pin(name, "1.0.0")

        assert package.manifest.manifest_version == "2"
        assert package.manifest.permissions
        assert pin.content_sha256 == package.content_sha256
        assert registry.validate_pin(pin) is package

    active_catalog = FileSystemSkillCatalog(assistant_skill_registry(), include_manifest_v2=True)
    active_names = {item.name for item in active_catalog.list_active_invocations()}
    assert active_names.isdisjoint(WORKFLOW_SKILLS)


def test_student_workflow_commands_and_aliases_are_unique() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()
    command_owners: dict[str, str] = {}

    for name in WORKFLOW_SKILLS:
        invocation = registry.get(name, "1.0.0").manifest.invocation
        assert invocation is not None
        for command in invocation.commands:
            assert command not in command_owners, (
                f"command {command!r} is shared by {name!r} and {command_owners[command]!r}"
            )
            command_owners[command] = name


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
    for package_directory in WORKFLOW_SKILLS.values():
        cases_path = SKILL_ROOT / package_directory / "evals" / "cases.jsonl"
        cases = [
            json.loads(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert cases
        assert all(case.get("fixture") == "synthetic_only" for case in cases)
