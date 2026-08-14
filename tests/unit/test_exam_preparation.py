from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from application.exam_preparation import (
    ExamPreparationError,
    ExamPreparationService,
    parse_objective_answers,
)
from domain.exam_preparation import ExamAction, ExamPaper, ExamSession, ExamSubmission


class MemoryExamRepository:
    def __init__(self) -> None:
        self.sessions: dict[UUID, ExamSession] = {}
        self.papers: dict[UUID, ExamPaper] = {}
        self.submissions: dict[tuple[UUID, str], ExamSubmission] = {}
        self.actions: dict[tuple[UUID, str], ExamAction] = {}

    async def create(self, value: ExamSession) -> ExamSession:
        self.sessions[value.session_id] = value
        return value

    async def get(self, value: UUID) -> ExamSession | None:
        return self.sessions.get(value)

    async def list_for_conversation(self, value: UUID) -> tuple[ExamSession, ...]:
        return tuple(s for s in self.sessions.values() if s.conversation_id == value)

    async def update(self, value: ExamSession, *, expected_revision: int) -> ExamSession | None:
        if self.sessions[value.session_id].revision != expected_revision:
            return None
        self.sessions[value.session_id] = value
        return value

    async def create_paper(self, value: ExamPaper) -> ExamPaper:
        self.papers[value.paper_id] = value
        return value

    async def get_paper(self, value: UUID) -> ExamPaper | None:
        return self.papers.get(value)

    async def create_submission(self, value: ExamSubmission) -> ExamSubmission:
        self.submissions[(value.session_id, value.submission_id)] = value
        return value

    async def get_submission(self, session_id: UUID, submission_id: str) -> ExamSubmission | None:
        return self.submissions.get((session_id, submission_id))

    async def create_action(self, value: ExamAction) -> ExamAction:
        self.actions[(value.session_id, value.idempotency_key)] = value
        return value

    async def get_action(self, session_id: UUID, idempotency_key: str) -> ExamAction | None:
        return self.actions.get((session_id, idempotency_key))

    async def list_actions(self, session_id: UUID) -> tuple[ExamAction, ...]:
        return tuple(a for a in self.actions.values() if a.session_id == session_id)


@pytest.mark.asyncio
async def test_exam_loop_keeps_answers_private_and_is_idempotent() -> None:
    repo = MemoryExamRepository()
    service = ExamPreparationService(repo)
    session = await service.create_session(
        conversation_id=uuid4(),
        space_id=uuid4(),
        owner_id="local",
        skill_version="1.1.0",
        skill_content_sha256="a" * 64,
    )
    broad = await service.apply_action(
        session.session_id,
        owner_id="local",
        action="configure",
        interaction_id=str(session.current_interaction["interaction_id"]),
        idempotency_key="configure",
    )
    paper = broad.current_interaction["paper"]
    assert isinstance(paper, dict)
    assert 6 <= len(paper["sections"][0]["questions"]) <= 10  # type: ignore[index]
    assert "answer" not in str(paper).lower() and "rubric" not in str(paper).lower()
    repeated = await service.apply_action(
        session.session_id,
        owner_id="local",
        action="configure",
        interaction_id=str(session.current_interaction["interaction_id"]),
        idempotency_key="configure",
    )
    assert repeated.revision == broad.revision


@pytest.mark.asyncio
async def test_exam_rejects_stale_interaction() -> None:
    repo = MemoryExamRepository()
    service = ExamPreparationService(repo)
    session = await service.create_session(
        conversation_id=uuid4(),
        space_id=uuid4(),
        owner_id="local",
        skill_version="1.1.0",
        skill_content_sha256="b" * 64,
    )
    with pytest.raises(ExamPreparationError, match="stale"):
        await service.apply_action(
            session.session_id,
            owner_id="local",
            action="configure",
            interaction_id="old",
            idempotency_key="bad",
        )


def test_numbered_chat_answers_are_parsed_deterministically() -> None:
    assert parse_objective_answers("Q1:B, Q2:AC, Q10:D") == [
        {"question_id": "Q1", "selected_options": ["B"]},
        {"question_id": "Q2", "selected_options": ["A", "C"]},
        {"question_id": "Q10", "selected_options": ["D"]},
    ]


@pytest.mark.asyncio
async def test_capabilities_can_jump_directly_to_mock_exam() -> None:
    repo = MemoryExamRepository()
    service = ExamPreparationService(repo)
    session = await service.create_session(
        conversation_id=uuid4(),
        space_id=uuid4(),
        owner_id="local",
        skill_version="1.2.0",
        skill_content_sha256="c" * 64,
    )
    updated = await service.perform_capability(
        session.session_id,
        owner_id="local",
        capability="mock_exam",
        idempotency_key="mock-now",
        run_id=uuid4(),
    )
    assert updated.phase.value == "mock_exam"
    assert updated.state["completed_capabilities"] == ["mock_exam"]
    assert updated.current_interaction["kind"] == "mock_exam"


@pytest.mark.asyncio
async def test_submitting_diagnostic_does_not_force_adaptive_check() -> None:
    repo = MemoryExamRepository()
    service = ExamPreparationService(repo)
    session = await service.create_session(
        conversation_id=uuid4(),
        space_id=uuid4(),
        owner_id="local",
        skill_version="1.2.0",
        skill_content_sha256="d" * 64,
    )
    quiz = await service.perform_capability(
        session.session_id,
        owner_id="local",
        capability="diagnose",
        idempotency_key="diagnose",
        run_id=uuid4(),
    )
    paper = quiz.current_interaction["paper"]
    assert isinstance(paper, dict)
    reviewed = await service.submit_current_paper(
        quiz.session_id,
        owner_id="local",
        idempotency_key="submit",
        interaction_id=str(quiz.current_interaction["interaction_id"]),
        paper_id=UUID(str(paper["paper_id"])),
        paper_version=int(paper["paper_version"]),
        answers=[{"question_id": "Q1", "selected_options": ["C"]}],
        submission_id="submission-1",
        run_id=uuid4(),
    )
    assert reviewed.state["active_capability"] == "review"
    assert reviewed.current_interaction["kind"] == "exam_review"
    assert reviewed.current_interaction.get("paper") is None


@pytest.mark.asyncio
async def test_generate_reuses_active_capability_and_regenerate_versions_paper() -> None:
    repo = MemoryExamRepository()
    service = ExamPreparationService(repo)
    session = await service.create_session(
        conversation_id=uuid4(),
        space_id=uuid4(),
        owner_id="local",
        skill_version="1.2.1",
        skill_content_sha256="e" * 64,
    )
    first = await service.perform_capability(
        session.session_id,
        owner_id="local",
        capability="diagnose",
        idempotency_key="first",
        run_id=uuid4(),
    )
    reused = await service.perform_capability(
        session.session_id,
        owner_id="local",
        capability="diagnose",
        idempotency_key="second",
        run_id=uuid4(),
    )
    assert reused.revision == first.revision
    assert reused.current_interaction == first.current_interaction
    regenerated = await service.perform_capability(
        session.session_id,
        owner_id="local",
        capability="diagnose",
        idempotency_key="third",
        run_id=uuid4(),
        regenerate=True,
    )
    first_paper = first.current_interaction["paper"]
    next_paper = regenerated.current_interaction["paper"]
    assert isinstance(first_paper, dict) and isinstance(next_paper, dict)
    assert regenerated.revision == first.revision + 1
    assert next_paper["paper_id"] != first_paper["paper_id"]
    assert next_paper["paper_version"] == 2
