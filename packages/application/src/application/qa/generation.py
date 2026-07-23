"""Versioned structured QA generation with deterministic grounding checks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from domain.grounded_qa import (
    Citation,
    CitationStatus,
    Claim,
    ConflictNotice,
    EvidenceCandidate,
    GroundedAnswer,
    QACancellationProbe,
    QAContractError,
    QAError,
    QAErrorCode,
    QAOutcome,
    QAResult,
    QuestionInput,
    Refusal,
    RefusalReason,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    ModelErrorCode,
    ModelGateway,
    ModelGatewayError,
)

from .context_builder import ContextBundle
from .evidence import EvidenceVerifier
from .profile import QAGenerationProfileV1


class StructuredOutputError(ValueError):
    """A safe structural error that never contains raw model output."""


class SuggestedResultType(StrEnum):
    ANSWER = "answer"
    REFUSE = "refuse"
    CONFLICT = "conflict"


class GroundedConfidence(StrEnum):
    HIGH = "high"
    LIMITED = "limited"


@dataclass(frozen=True)
class StructuredClaimDraft:
    claim_id: str
    text: str
    evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class StructuredAnswerDraft:
    text: str
    claims: tuple[StructuredClaimDraft, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class StructuredRefusalDraft:
    message: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class StructuredConflictDraft:
    message: str
    evidence_ids: tuple[UUID, ...]
    limitations: tuple[str, ...]


type StructuredQADraft = StructuredAnswerDraft | StructuredRefusalDraft | StructuredConflictDraft


@dataclass(frozen=True)
class GenerationIdentity:
    profile_id: str
    retrieval_profile_reference: str
    model_identity: str
    capability_alias: str
    prompt_template_id: str
    structured_output_schema: str
    corpus_version: str
    dataset_version: str

    def __post_init__(self) -> None:
        if any(not value for value in self.__dict__.values()):
            raise QAContractError("Generation identity values must not be blank")


@dataclass(frozen=True)
class VerificationMetrics:
    claim_support_rate: float | None
    citation_completeness_rate: float | None
    confidence: GroundedConfidence | None


@dataclass(frozen=True)
class GenerationUsage:
    model_calls: int
    repair_attempts: int
    input_tokens: int
    output_tokens: int
    model_latency_ms: float


@dataclass(frozen=True)
class GenerationResult:
    result: QAResult
    identity: GenerationIdentity
    verification: VerificationMetrics
    usage: GenerationUsage


class StructuredAnswerParser:
    """Parse exact JSON against the versioned schema; markdown and regex recovery are forbidden."""

    def __init__(self, schema: dict[str, Any]) -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise QAContractError("Grounded answer schema is invalid") from exc
        self._validator = Draft202012Validator(schema)

    def parse(self, text: str) -> StructuredQADraft:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StructuredOutputError("Model output is not exact JSON") from exc
        if not isinstance(value, dict):
            raise StructuredOutputError("Model output must be a JSON object")
        try:
            self._validator.validate(value)
        except ValidationError as exc:
            raise StructuredOutputError("Model output does not match grounded-answer-v1") from exc
        return self._decode(value)

    def _decode(self, value: dict[str, Any]) -> StructuredQADraft:
        result_type = SuggestedResultType(value["result_type"])
        limitations = tuple(_string_list(value["limitations"]))
        if result_type is SuggestedResultType.REFUSE:
            return StructuredRefusalDraft(message=str(value["message"]), limitations=limitations)
        if result_type is SuggestedResultType.CONFLICT:
            return StructuredConflictDraft(
                message=str(value["message"]),
                evidence_ids=tuple(_uuid_list(value["evidence_ids"])),
                limitations=limitations,
            )

        raw_claims = value["claims"]
        assert isinstance(raw_claims, list)
        claims = tuple(_decode_claim(claim) for claim in raw_claims)
        claim_ids = tuple(claim.claim_id for claim in claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise StructuredOutputError("Model output claim IDs must be unique")
        answer = str(value["answer"])
        if answer != "\n".join(claim.text for claim in claims):
            raise StructuredOutputError("Answer text must be the ordered concatenation of claims")
        return StructuredAnswerDraft(text=answer, claims=claims, limitations=limitations)


class GroundedAnswerGenerator:
    """Call fast_chat and publish only fully verified, server-grounded results."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        parser: StructuredAnswerParser,
        verifier: EvidenceVerifier,
        profile: QAGenerationProfileV1,
        prompt_contract: str,
        corpus_version: str,
        dataset_version: str,
        cancellation: QACancellationProbe | None = None,
    ) -> None:
        if not prompt_contract.strip():
            raise QAContractError("QA prompt contract must not be blank")
        if not corpus_version or not dataset_version:
            raise QAContractError("QA corpus and dataset versions must not be blank")
        self._gateway = gateway
        self._parser = parser
        self._verifier = verifier
        self._profile = profile
        self._prompt_contract = prompt_contract
        self._cancellation = cancellation
        self._identity = GenerationIdentity(
            profile_id=profile.profile_id,
            retrieval_profile_reference=profile.retrieval_profile_reference,
            model_identity=profile.model_identity,
            capability_alias=profile.capability_alias,
            prompt_template_id=profile.prompt_template_id,
            structured_output_schema=profile.structured_output_schema,
            corpus_version=corpus_version,
            dataset_version=dataset_version,
        )

    async def generate(
        self,
        *,
        question: QuestionInput,
        context: ContextBundle,
    ) -> GenerationResult:
        if question.question != context.question:
            raise QAError(QAErrorCode.INVALID_INPUT, "Question and context do not match.")
        await self._check_cancelled()
        evidence = tuple(item.candidate for item in context.evidence)
        if not evidence:
            return GenerationResult(
                result=_insufficient_evidence_result("No evidence is available for this question."),
                identity=self._identity,
                verification=VerificationMetrics(0.0, 0.0, GroundedConfidence.LIMITED),
                usage=GenerationUsage(0, 0, 0, 0, 0.0),
            )
        await self._verify_for_generation(space_id=question.space_id, evidence=evidence)
        await self._check_cancelled()

        responses: list[ChatResponse] = []
        draft: StructuredQADraft | None = None
        candidate_text: str | None = None
        for attempt in range(self._profile.max_repair_attempts + 1):
            request = (
                self._initial_request(context)
                if attempt == 0
                else self._repair_request(candidate_text or "")
            )
            await self._check_cancelled()
            response = await self._chat(request)
            responses.append(response)
            await self._check_cancelled()
            candidate_text = response.text
            try:
                if response.finish_reason not in {None, "stop"}:
                    raise StructuredOutputError("Model output did not finish normally")
                draft = self._parser.parse(response.text)
                break
            except StructuredOutputError:
                if attempt == self._profile.max_repair_attempts:
                    raise QAError(
                        QAErrorCode.STRUCTURED_RESPONSE_INVALID,
                        "The model returned an invalid structured response.",
                    ) from None
        assert draft is not None

        await self._check_cancelled()
        result, verification = await self._materialize(
            draft=draft,
            space_id=question.space_id,
            evidence=evidence,
        )
        return GenerationResult(
            result=result,
            identity=self._identity,
            verification=verification,
            usage=GenerationUsage(
                model_calls=len(responses),
                repair_attempts=len(responses) - 1,
                input_tokens=sum(response.usage.input_tokens for response in responses),
                output_tokens=sum(response.usage.output_tokens for response in responses),
                model_latency_ms=sum(response.latency_ms for response in responses),
            ),
        )

    async def _chat(self, request: ChatRequest) -> ChatResponse:
        try:
            async with asyncio.timeout(self._profile.timeout_seconds):
                response = await self._gateway.chat(
                    request,
                    capability=CapabilityAlias.FAST_CHAT,
                )
        except TimeoutError as exc:
            raise QAError(
                QAErrorCode.TIMED_OUT,
                "QA generation exceeded the active timeout.",
                retryable=True,
            ) from exc
        except ModelGatewayError as exc:
            code = {
                ModelErrorCode.TIMEOUT: QAErrorCode.TIMED_OUT,
                ModelErrorCode.RATE_LIMITED: QAErrorCode.MODEL_RATE_LIMITED,
                ModelErrorCode.AUTHENTICATION: QAErrorCode.MODEL_AUTHENTICATION_FAILED,
                ModelErrorCode.POLICY_DENIED: QAErrorCode.POLICY_DENIED,
            }.get(exc.code, QAErrorCode.MODEL_FAILED)
            raise QAError(
                code,
                "The configured QA model is unavailable.",
                retryable=exc.retryable,
            ) from exc
        if response.capability is not CapabilityAlias.FAST_CHAT:
            raise QAError(QAErrorCode.MODEL_FAILED, "The QA model returned a wrong capability.")
        return response

    async def _materialize(
        self,
        *,
        draft: StructuredQADraft,
        space_id: UUID,
        evidence: tuple[EvidenceCandidate, ...],
    ) -> tuple[QAResult, VerificationMetrics]:
        if isinstance(draft, StructuredRefusalDraft):
            return (
                _insufficient_evidence_result(draft.message),
                VerificationMetrics(None, None, None),
            )

        evidence_by_id = {candidate.evidence_id: candidate for candidate in evidence}
        selected_ids = (
            draft.evidence_ids
            if isinstance(draft, StructuredConflictDraft)
            else tuple(evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids)
        )
        unknown_ids = set(selected_ids) - evidence_by_id.keys()
        if unknown_ids:
            raise QAError(
                QAErrorCode.CITATION_INVALID,
                "The model referenced Evidence outside the current run.",
            )

        if isinstance(draft, StructuredConflictDraft):
            selected = tuple(evidence_by_id[evidence_id] for evidence_id in draft.evidence_ids)
            if len({candidate.source_id for candidate in selected}) < 2:
                return (
                    _insufficient_evidence_result(
                        "The available evidence does not establish a multi-source conflict."
                    ),
                    VerificationMetrics(0.0, 1.0, GroundedConfidence.LIMITED),
                )
            await self._verify_for_generation(space_id=space_id, evidence=selected)
            await self._check_cancelled()
            return (
                QAResult(
                    outcome=QAOutcome.CONFLICT,
                    conflict=ConflictNotice(draft.evidence_ids, draft.message),
                ),
                VerificationMetrics(1.0, 1.0, GroundedConfidence.HIGH),
            )

        supported_claims = sum(
            any(evidence_by_id[evidence_id].matched for evidence_id in claim.evidence_ids)
            for claim in draft.claims
        )
        claim_support_rate = supported_claims / len(draft.claims)
        citation_completeness_rate = sum(bool(claim.evidence_ids) for claim in draft.claims) / len(
            draft.claims
        )
        confidence = (
            GroundedConfidence.HIGH
            if claim_support_rate == 1.0 and citation_completeness_rate == 1.0
            else GroundedConfidence.LIMITED
        )
        verification = VerificationMetrics(
            claim_support_rate,
            citation_completeness_rate,
            confidence,
        )
        if (
            claim_support_rate < self._profile.min_claim_support_rate
            or citation_completeness_rate < self._profile.min_citation_completeness_rate
        ):
            return (
                _insufficient_evidence_result(
                    "The available evidence does not support a complete answer."
                ),
                verification,
            )

        ordered_ids = tuple(dict.fromkeys(selected_ids))
        selected = tuple(evidence_by_id[evidence_id] for evidence_id in ordered_ids)
        claims = tuple(
            Claim(claim.claim_id, claim.text, claim.evidence_ids) for claim in draft.claims
        )
        citations = tuple(_citation_from_evidence(candidate) for candidate in selected)
        answer = GroundedAnswer(
            text=draft.text,
            claims=claims,
            citations=citations,
            limitations=draft.limitations,
        )
        await self._check_cancelled()
        try:
            await self._verifier.validate_for_publication(
                space_id=space_id,
                answer=answer,
                evidence=selected,
            )
        except QAContractError as exc:
            raise QAError(
                QAErrorCode.CITATION_INVALID,
                "Evidence failed the publication integrity check.",
            ) from exc
        return QAResult(outcome=QAOutcome.ANSWER, answer=answer), verification

    async def _verify_for_generation(
        self, *, space_id: UUID, evidence: tuple[EvidenceCandidate, ...]
    ) -> None:
        try:
            await self._verifier.validate_for_generation(space_id=space_id, evidence=evidence)
        except QAContractError as exc:
            raise QAError(
                QAErrorCode.CITATION_INVALID,
                "Evidence failed the generation integrity check.",
            ) from exc

    async def _check_cancelled(self) -> None:
        if self._cancellation is not None and await self._cancellation.is_cancel_requested():
            raise QAError(QAErrorCode.CANCELLED, "QA generation was cancelled.")

    def _initial_request(self, context: ContextBundle) -> ChatRequest:
        system = "\n".join((*context.system_rules, self._prompt_contract))
        user_sections = [f"<question>\n{context.question}\n</question>"]
        user_sections.extend(
            f'<history role="{turn.role.value}">\n{turn.content}\n</history>'
            for turn in context.history
        )
        user_sections.extend(item.rendered_block for item in context.evidence)
        return ChatRequest(
            messages=(
                ChatMessage(ChatRole.SYSTEM, system),
                ChatMessage(ChatRole.USER, "\n".join(user_sections)),
            ),
            temperature=self._profile.temperature,
            max_tokens=self._profile.max_output_tokens,
        )

    def _repair_request(self, candidate_text: str) -> ChatRequest:
        system = "\n".join(
            (
                self._prompt_contract,
                "Repair the candidate into exact grounded-answer-v1 JSON. Do not add facts or IDs.",
            )
        )
        return ChatRequest(
            messages=(
                ChatMessage(ChatRole.SYSTEM, system),
                ChatMessage(
                    ChatRole.USER,
                    f"<invalid_candidate>\n{candidate_text}\n</invalid_candidate>",
                ),
            ),
            temperature=self._profile.temperature,
            max_tokens=self._profile.max_output_tokens,
        )


def _decode_claim(value: Any) -> StructuredClaimDraft:
    assert isinstance(value, dict)
    return StructuredClaimDraft(
        claim_id=str(value["claim_id"]),
        text=str(value["text"]),
        evidence_ids=tuple(_uuid_list(value["evidence_ids"])),
    )


def _string_list(value: Any) -> list[str]:
    assert isinstance(value, list)
    return [str(item) for item in value]


def _uuid_list(value: Any) -> list[UUID]:
    assert isinstance(value, list)
    return [UUID(str(item)) for item in value]


def _citation_from_evidence(candidate: EvidenceCandidate) -> Citation:
    return Citation(
        evidence_id=candidate.evidence_id,
        space_id=candidate.space_id,
        source_id=candidate.source_id,
        document_id=candidate.document_id,
        version_id=candidate.version_id,
        chunk_id=candidate.chunk_id,
        locator=candidate.locators[0],
        excerpt_sha256=candidate.excerpt_sha256,
        status=CitationStatus.VALID,
    )


def _insufficient_evidence_result(message: str) -> QAResult:
    return QAResult(
        outcome=QAOutcome.REFUSE,
        refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, message),
    )


__all__ = [
    "GenerationIdentity",
    "GenerationResult",
    "GenerationUsage",
    "GroundedAnswerGenerator",
    "GroundedConfidence",
    "StructuredAnswerDraft",
    "StructuredAnswerParser",
    "StructuredClaimDraft",
    "StructuredConflictDraft",
    "StructuredOutputError",
    "StructuredQADraft",
    "StructuredRefusalDraft",
    "SuggestedResultType",
    "VerificationMetrics",
]
