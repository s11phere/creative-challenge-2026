"""PostgreSQL persistence for Exam Preparation Sessions."""

from __future__ import annotations

from uuid import UUID

from domain.exam_preparation import ExamAction, ExamPaper, ExamPhase, ExamSession, ExamSubmission
from sqlalchemy import select, update

from .database import Database
from .orm import ExamActionModel, ExamPaperModel, ExamSessionModel, ExamSubmissionModel


class PostgresExamSessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, v: ExamSession) -> ExamSession:
        async with self._database.transaction() as s:
            s.add(
                ExamSessionModel(
                    id=v.session_id,
                    conversation_id=v.conversation_id,
                    space_id=v.space_id,
                    owner_id=v.owner_id,
                    skill_version=v.skill_version,
                    skill_content_sha256=v.skill_content_sha256,
                    phase=v.phase.value,
                    revision=v.revision,
                    state=v.state,
                    current_interaction=v.current_interaction,
                    created_at=v.created_at,
                    updated_at=v.updated_at,
                )
            )
        return v

    async def get(self, session_id: UUID) -> ExamSession | None:
        async with self._database.session() as s:
            m = await s.get(ExamSessionModel, session_id)
            return _session(m) if m else None

    async def list_for_conversation(self, conversation_id: UUID) -> tuple[ExamSession, ...]:
        async with self._database.session() as s:
            rows = (
                await s.execute(
                    select(ExamSessionModel)
                    .where(ExamSessionModel.conversation_id == conversation_id)
                    .order_by(ExamSessionModel.created_at)
                )
            ).scalars()
            return tuple(_session(m) for m in rows)

    async def update(self, v: ExamSession, *, expected_revision: int) -> ExamSession | None:
        async with self._database.transaction() as s:
            result = await s.execute(
                update(ExamSessionModel)
                .where(
                    ExamSessionModel.id == v.session_id,
                    ExamSessionModel.revision == expected_revision,
                )
                .values(
                    phase=v.phase.value,
                    revision=v.revision,
                    state=v.state,
                    current_interaction=v.current_interaction,
                    updated_at=v.updated_at,
                )
            )
            return v if getattr(result, "rowcount", 0) == 1 else None

    async def create_paper(self, v: ExamPaper) -> ExamPaper:
        async with self._database.transaction() as s:
            s.add(
                ExamPaperModel(
                    id=v.paper_id,
                    session_id=v.session_id,
                    kind=v.kind,
                    version=v.version,
                    locked=v.locked,
                    public_payload=v.public_payload,
                    private_answer_key=v.private_answer_key,
                )
            )
        return v

    async def get_paper(self, paper_id: UUID) -> ExamPaper | None:
        async with self._database.session() as s:
            m = await s.get(ExamPaperModel, paper_id)
            return (
                ExamPaper(
                    session_id=m.session_id,
                    kind=m.kind,
                    public_payload=m.public_payload,
                    private_answer_key=m.private_answer_key,
                    paper_id=m.id,
                    version=m.version,
                    locked=m.locked,
                )
                if m
                else None
            )

    async def create_submission(self, v: ExamSubmission) -> ExamSubmission:
        async with self._database.transaction() as s:
            s.add(
                ExamSubmissionModel(
                    session_id=v.session_id,
                    paper_id=v.paper_id,
                    paper_version=v.paper_version,
                    submission_id=v.submission_id,
                    answers=v.answers,
                    result=v.result,
                    created_at=v.created_at,
                )
            )
        return v

    async def get_submission(self, session_id: UUID, submission_id: str) -> ExamSubmission | None:
        async with self._database.session() as s:
            m = (
                await s.execute(
                    select(ExamSubmissionModel).where(
                        ExamSubmissionModel.session_id == session_id,
                        ExamSubmissionModel.submission_id == submission_id,
                    )
                )
            ).scalar_one_or_none()
            return (
                ExamSubmission(
                    session_id=m.session_id,
                    paper_id=m.paper_id,
                    paper_version=m.paper_version,
                    submission_id=m.submission_id,
                    answers=m.answers,
                    result=m.result,
                    created_at=m.created_at,
                )
                if m
                else None
            )

    async def create_action(self, v: ExamAction) -> ExamAction:
        async with self._database.transaction() as s:
            s.add(
                ExamActionModel(
                    id=v.action_id,
                    session_id=v.session_id,
                    run_id=v.run_id,
                    action=v.action,
                    interaction_id=v.interaction_id,
                    idempotency_key=v.idempotency_key,
                    safe_result=v.safe_result,
                    created_at=v.created_at,
                )
            )
        return v

    async def get_action(self, session_id: UUID, idempotency_key: str) -> ExamAction | None:
        async with self._database.session() as s:
            m = (
                await s.execute(
                    select(ExamActionModel).where(
                        ExamActionModel.session_id == session_id,
                        ExamActionModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            return _action(m) if m else None

    async def list_actions(self, session_id: UUID) -> tuple[ExamAction, ...]:
        async with self._database.session() as s:
            rows = (
                await s.execute(
                    select(ExamActionModel)
                    .where(ExamActionModel.session_id == session_id)
                    .order_by(ExamActionModel.created_at)
                )
            ).scalars()
            return tuple(_action(m) for m in rows)


def _session(m: ExamSessionModel) -> ExamSession:
    return ExamSession(
        session_id=m.id,
        conversation_id=m.conversation_id,
        space_id=m.space_id,
        owner_id=m.owner_id,
        skill_version=m.skill_version,
        skill_content_sha256=m.skill_content_sha256,
        phase=ExamPhase(m.phase),
        revision=m.revision,
        state=m.state,
        current_interaction=m.current_interaction,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _action(m: ExamActionModel) -> ExamAction:
    return ExamAction(
        action_id=m.id,
        session_id=m.session_id,
        run_id=m.run_id,
        action=m.action,
        interaction_id=m.interaction_id,
        idempotency_key=m.idempotency_key,
        safe_result=m.safe_result,
        created_at=m.created_at,
    )
