"""Durable domain contracts for interactive exam preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4


class ExamPhase(StrEnum):
    SETUP = "setup"
    BROAD_QUIZ = "broad_quiz"
    ADAPTIVE_QUIZ = "adaptive_quiz"
    DIAGNOSIS = "diagnosis"
    REVIEW_PLAN = "review_plan"
    STUDY_GUIDE = "study_guide"
    REVIEW_CARDS = "review_cards"
    MOCK_EXAM = "mock_exam"
    MOCK_REVIEW = "mock_review"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ExamSession:
    conversation_id: UUID
    space_id: UUID
    owner_id: str
    skill_version: str
    skill_content_sha256: str
    session_id: UUID = field(default_factory=uuid4)
    phase: ExamPhase = ExamPhase.SETUP
    revision: int = 1
    state: dict[str, object] = field(default_factory=dict)
    current_interaction: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.owner_id.strip() or not self.skill_version.strip():
            raise ValueError("Exam Session owner and Skill version are required")
        if len(self.skill_content_sha256) != 64 or self.revision < 1:
            raise ValueError("Exam Session identity or revision is invalid")


@dataclass(frozen=True)
class ExamPaper:
    session_id: UUID
    kind: str
    public_payload: dict[str, object]
    private_answer_key: dict[str, object]
    paper_id: UUID = field(default_factory=uuid4)
    version: int = 1
    locked: bool = True


@dataclass(frozen=True)
class ExamSubmission:
    session_id: UUID
    paper_id: UUID
    paper_version: int
    submission_id: str
    answers: list[dict[str, object]]
    result: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ExamAction:
    session_id: UUID
    action: str
    idempotency_key: str
    interaction_id: str
    run_id: UUID | None = None
    action_id: UUID = field(default_factory=uuid4)
    safe_result: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ExamSessionRepository(Protocol):
    async def create(self, session: ExamSession) -> ExamSession: ...
    async def get(self, session_id: UUID) -> ExamSession | None: ...
    async def list_for_conversation(self, conversation_id: UUID) -> tuple[ExamSession, ...]: ...
    async def update(
        self, session: ExamSession, *, expected_revision: int
    ) -> ExamSession | None: ...
    async def create_paper(self, paper: ExamPaper) -> ExamPaper: ...
    async def get_paper(self, paper_id: UUID) -> ExamPaper | None: ...
    async def create_submission(self, submission: ExamSubmission) -> ExamSubmission: ...
    async def get_submission(
        self, session_id: UUID, submission_id: str
    ) -> ExamSubmission | None: ...
    async def create_action(self, action: ExamAction) -> ExamAction: ...
    async def get_action(self, session_id: UUID, idempotency_key: str) -> ExamAction | None: ...
    async def list_actions(self, session_id: UUID) -> tuple[ExamAction, ...]: ...


__all__ = [
    "ExamAction",
    "ExamPaper",
    "ExamPhase",
    "ExamSession",
    "ExamSessionRepository",
    "ExamSubmission",
]
