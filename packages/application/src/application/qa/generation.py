"""Versioned structured QA generation with deterministic grounding checks."""

from __future__ import annotations

import asyncio
import json
import logging
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

from .answer_mode import GroundedAnswerMode
from .context_builder import ContextBundle
from .evidence import EvidenceVerifier
from .profile import QAGenerationProfileV1

logger = logging.getLogger(__name__)


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
class StructuredResearchAnswerDraft:
    """Research-v2 output flattened into the existing grounded publication contract."""

    text: str
    claims: tuple[StructuredClaimDraft, ...]
    limitations: tuple[str, ...]
    mode: GroundedAnswerMode
    paper_brief_claim_ids: tuple[str, ...] = ()
    cross_document_claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredRefusalDraft:
    message: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class StructuredConflictDraft:
    message: str
    evidence_ids: tuple[UUID, ...]
    limitations: tuple[str, ...]


type StructuredQADraft = (
    StructuredAnswerDraft
    | StructuredResearchAnswerDraft
    | StructuredRefusalDraft
    | StructuredConflictDraft
)


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

    def __init__(
        self,
        schema: dict[str, Any],
        *,
        research_schema: dict[str, Any] | None = None,
    ) -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise QAContractError("Grounded answer schema is invalid") from exc
        self._validator = Draft202012Validator(schema)
        self._format_instruction = (
            "Return exactly one JSON object and no Markdown or explanatory text. "
            "For an answer, put every supported statement in claims; the server derives the "
            "published answer from the ordered claim texts. The JSON object must conform to "
            "this schema:\n"
            + json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        self._research_validator: Draft202012Validator | None = None
        self._research_format_instruction: str | None = None
        if research_schema is not None:
            try:
                Draft202012Validator.check_schema(research_schema)
            except SchemaError as exc:
                raise QAContractError("Research answer schema is invalid") from exc
            self._research_validator = Draft202012Validator(research_schema)
            self._research_format_instruction = (
                "Return exactly one JSON object and no Markdown or explanatory text. "
                "Use the Research schema below; evidence_ids must exactly match supplied "
                "evidence blocks. The server renders the validated object as Markdown and owns "
                "all published Citations. The JSON object must conform to this schema:\n"
                + json.dumps(
                    research_schema,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    @property
    def format_instruction(self) -> str:
        return self._format_instruction

    def format_instruction_for(self, answer_mode: GroundedAnswerMode) -> str:
        if answer_mode is GroundedAnswerMode.DEFAULT:
            return self._format_instruction
        if self._research_format_instruction is None:
            raise QAContractError("Research answer mode requires its structured schema")
        return self._research_format_instruction

    def parse(
        self,
        text: str,
        *,
        answer_mode: GroundedAnswerMode = GroundedAnswerMode.DEFAULT,
    ) -> StructuredQADraft:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StructuredOutputError("Model output is not exact JSON") from exc
        if not isinstance(value, dict):
            raise StructuredOutputError("Model output must be a JSON object")
        validator = self._validator
        if answer_mode is not GroundedAnswerMode.DEFAULT:
            if self._research_validator is None:
                raise QAContractError("Research answer mode requires its structured schema")
            validator = self._research_validator
        try:
            validator.validate(value)
        except ValidationError as exc:
            contract = (
                "grounded-answer-v1"
                if answer_mode is GroundedAnswerMode.DEFAULT
                else "research-grounded-answer-v2"
            )
            raise StructuredOutputError(f"Model output does not match {contract}") from exc
        if answer_mode is not GroundedAnswerMode.DEFAULT:
            return self._decode_research(value, expected_mode=answer_mode)
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
        # The top-level answer is a model convenience field. Claims are the cited,
        # independently verified source of truth for the published answer.
        answer = "\n".join(claim.text for claim in claims)
        return StructuredAnswerDraft(text=answer, claims=claims, limitations=limitations)

    def _decode_research(
        self,
        value: dict[str, Any],
        *,
        expected_mode: GroundedAnswerMode,
    ) -> StructuredQADraft:
        if value["result_type"] == "refuse":
            return StructuredRefusalDraft(
                message=str(value["message"]),
                limitations=tuple(_string_list(value["limitations"])),
            )
        mode = GroundedAnswerMode(str(value["mode"]))
        if mode is not expected_mode:
            raise StructuredOutputError("Research answer mode does not match the requested mode")
        if mode is GroundedAnswerMode.RESEARCH_DEEP_READ:
            return _decode_research_deep_read(value)
        return _decode_research_literature_review(value)


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
        answer_mode: GroundedAnswerMode = GroundedAnswerMode.DEFAULT,
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
        if (
            answer_mode is GroundedAnswerMode.RESEARCH_LITERATURE_REVIEW
            and len({item.document_id for item in evidence}) < 2
        ):
            return GenerationResult(
                result=_insufficient_evidence_result(
                    "A literature review requires evidence from at least two selected papers."
                ),
                identity=self._identity,
                verification=VerificationMetrics(0.0, 0.0, GroundedConfidence.LIMITED),
                usage=GenerationUsage(0, 0, 0, 0, 0.0),
            )
        await self._verify_for_generation(space_id=question.space_id, evidence=evidence)
        await self._check_cancelled()

        responses: list[ChatResponse] = []
        request = self._initial_request(context, answer_mode=answer_mode)
        while len(responses) < self._profile.max_model_calls:
            await self._check_cancelled()
            response = await self._chat(request)
            responses.append(response)
            await self._check_cancelled()
            try:
                if response.finish_reason not in {None, "stop"}:
                    raise StructuredOutputError("Model output did not finish normally")
                draft = self._parser.parse(response.text, answer_mode=answer_mode)
            except StructuredOutputError:
                if len(responses) == self._profile.max_model_calls:
                    raise QAError(
                        QAErrorCode.STRUCTURED_RESPONSE_INVALID,
                        "The model returned an invalid structured response.",
                    ) from None
                request = self._repair_request(
                    response.text,
                    context=context,
                    answer_mode=answer_mode,
                )
                continue

            await self._check_cancelled()
            try:
                result, verification = await self._materialize(
                    draft=draft,
                    space_id=question.space_id,
                    evidence=evidence,
                )
            except StructuredOutputError:
                if len(responses) == self._profile.max_model_calls:
                    raise QAError(
                        QAErrorCode.STRUCTURED_RESPONSE_INVALID,
                        "The model returned an invalid Research structure.",
                    ) from None
                request = self._citation_repair_request(context, answer_mode=answer_mode)
                continue
            except QAError as exc:
                if (
                    exc.code is QAErrorCode.CITATION_INVALID
                    and _references_unknown_evidence(draft, evidence)
                    and len(responses) < self._profile.max_model_calls
                ):
                    request = self._citation_repair_request(context, answer_mode=answer_mode)
                    continue
                raise
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
        raise QAError(
            QAErrorCode.STRUCTURED_RESPONSE_INVALID, "QA generation exhausted its budget."
        )

    async def _chat(self, request: ChatRequest) -> ChatResponse:
        try:
            async with asyncio.timeout(self._profile.timeout_seconds):
                response = await self._gateway.chat(
                    request,
                    capability=CapabilityAlias.FAST_CHAT,
                )
        except TimeoutError as exc:
            logger.warning(
                "qa_model_call_timed_out",
                extra={
                    "capability": CapabilityAlias.FAST_CHAT.value,
                    "error_code": QAErrorCode.TIMED_OUT.value,
                    "retryable": True,
                    "error_type": type(exc).__name__,
                },
            )
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
            logger.warning(
                "qa_model_call_failed",
                extra={
                    "capability": exc.capability.value,
                    "error_code": exc.code.value,
                    "retryable": exc.retryable,
                    "error_type": type(exc).__name__,
                },
            )
            raise QAError(
                code,
                "The configured QA model is unavailable.",
                retryable=exc.retryable,
            ) from exc
        if response.capability is not CapabilityAlias.FAST_CHAT:
            logger.error(
                "qa_model_wrong_capability",
                extra={
                    "capability": response.capability.value,
                    "error_code": ModelErrorCode.UNSUPPORTED_CAPABILITY.value,
                    "retryable": False,
                },
            )
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

        if isinstance(draft, StructuredResearchAnswerDraft):
            _validate_research_document_grounding(draft, evidence_by_id)

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

        publishable_claims = tuple(
            claim
            for claim in draft.claims
            if any(evidence_by_id[evidence_id].matched for evidence_id in claim.evidence_ids)
        )
        claim_support_rate = len(publishable_claims) / len(draft.claims)
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
        if not publishable_claims or (
            citation_completeness_rate < self._profile.min_citation_completeness_rate
        ):
            return (
                _insufficient_evidence_result(
                    "The available evidence does not support a complete answer."
                ),
                verification,
            )

        published_ids = tuple(
            evidence_id for claim in publishable_claims for evidence_id in claim.evidence_ids
        )
        ordered_ids = tuple(dict.fromkeys(published_ids))
        selected = tuple(evidence_by_id[evidence_id] for evidence_id in ordered_ids)
        claims = tuple(
            Claim(claim.claim_id, claim.text, claim.evidence_ids) for claim in publishable_claims
        )
        citations = tuple(_citation_from_evidence(candidate) for candidate in selected)
        limitations = draft.limitations
        if len(publishable_claims) < len(draft.claims):
            limitations = tuple(
                dict.fromkeys(
                    (
                        *limitations,
                        "Some candidate claims were omitted because they were supported only by "
                        "retrieval context expansion.",
                    )
                )
            )
        answer = GroundedAnswer(
            text="\n".join(claim.text for claim in publishable_claims),
            claims=claims,
            citations=citations,
            limitations=limitations,
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

    def _initial_request(
        self, context: ContextBundle, *, answer_mode: GroundedAnswerMode
    ) -> ChatRequest:
        system = "\n".join(
            (
                *context.system_rules,
                self._prompt_contract,
                _answer_mode_instruction(answer_mode),
                self._parser.format_instruction_for(answer_mode),
            )
        )
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

    def _repair_request(
        self,
        candidate_text: str,
        *,
        context: ContextBundle,
        answer_mode: GroundedAnswerMode,
    ) -> ChatRequest:
        system = "\n".join(
            (
                *context.system_rules,
                self._prompt_contract,
                _answer_mode_instruction(answer_mode),
                self._parser.format_instruction_for(answer_mode),
                "Repair the candidate into the exact requested JSON contract. Do not add facts "
                "or Evidence IDs that are absent from the supplied evidence blocks.",
            )
        )
        user_sections = [f"<invalid_candidate>\n{candidate_text}\n</invalid_candidate>"]
        user_sections.extend(item.rendered_block for item in context.evidence)
        return ChatRequest(
            messages=(
                ChatMessage(ChatRole.SYSTEM, system),
                ChatMessage(ChatRole.USER, "\n".join(user_sections)),
            ),
            temperature=self._profile.temperature,
            max_tokens=self._profile.max_output_tokens,
        )

    def _citation_repair_request(
        self, context: ContextBundle, *, answer_mode: GroundedAnswerMode
    ) -> ChatRequest:
        system = "\n".join(
            (
                *context.system_rules,
                self._prompt_contract,
                _answer_mode_instruction(answer_mode),
                self._parser.format_instruction_for(answer_mode),
                "Regenerate the complete answer. Every cited evidence_id must exactly match an "
                "evidence block supplied below. Do not invent, transform, or retain any other ID.",
            )
        )
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


def _decode_claim(value: Any) -> StructuredClaimDraft:
    assert isinstance(value, dict)
    return StructuredClaimDraft(
        claim_id=str(value["claim_id"]),
        text=str(value["text"]),
        evidence_ids=tuple(_uuid_list(value["evidence_ids"])),
    )


def _decode_research_deep_read(value: dict[str, Any]) -> StructuredResearchAnswerDraft:
    claims: list[StructuredClaimDraft] = []

    def add(claim_id: str, heading: str, item: Any) -> None:
        research_item = _research_item(item)
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=f"## {heading}\n\n{research_item[0]}",
                evidence_ids=research_item[1],
            )
        )

    def add_many(prefix: str, heading: str, items: Any) -> None:
        decoded = _research_items(items)
        for index, (text, evidence_ids) in enumerate(decoded, 1):
            claims.append(
                StructuredClaimDraft(
                    claim_id=f"{prefix}-{index}",
                    text=(f"## {heading}\n\n" if index == 1 else "") + f"- {text}",
                    evidence_ids=evidence_ids,
                )
            )

    add("research-question", "Research question", value["research_question"])
    add_many("contribution", "Contributions", value["contributions"])
    add_many("method", "Method and undergraduate explanation", value["method_explanation"])
    add_many("data-metric", "Data and metrics", value["data_and_metrics"])
    add_many("result", "Main results", value["results"])
    add_many("paper-limitation", "Paper limitations", value["paper_limitations"])
    add_many("misconception", "Common misconceptions", value["misconceptions"])
    follow_ups = "\n".join(f"- {item}" for item in _string_list(value["follow_up_questions"]))
    claims.append(
        StructuredClaimDraft(
            claim_id="follow-up-questions",
            text=f"## Follow-up questions\n\n{follow_ups}",
            evidence_ids=claims[0].evidence_ids,
        )
    )
    return StructuredResearchAnswerDraft(
        text="\n\n".join(claim.text for claim in claims),
        claims=tuple(claims),
        limitations=tuple(_string_list(value["limitations"])),
        mode=GroundedAnswerMode.RESEARCH_DEEP_READ,
    )


def _decode_research_literature_review(
    value: dict[str, Any],
) -> StructuredResearchAnswerDraft:
    claims: list[StructuredClaimDraft] = []
    brief_ids: list[str] = []
    cross_document_ids: list[str] = []
    labels: set[str] = set()
    for index, raw in enumerate(_object_list(value["paper_briefs"]), 1):
        label = str(raw["paper_label"]).strip()
        if label.casefold() in labels:
            raise StructuredOutputError("Research paper labels must be unique")
        labels.add(label.casefold())
        claim_id = f"paper-brief-{index}"
        brief_ids.append(claim_id)
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=("## Per-paper briefs\n\n" if index == 1 else "")
                + f"### {label}\n\n{str(raw['text'])}",
                evidence_ids=tuple(_uuid_list(raw["evidence_ids"])),
            )
        )

    for index, raw in enumerate(_object_list(value["evidence_matrix"]), 1):
        observations = _object_list(raw["observations"])
        observation_text = "<br>".join(
            f"**{item['paper_label']}**: {item['text']}" for item in observations
        )
        claim_id = f"matrix-{index}"
        cross_document_ids.append(claim_id)
        header = (
            "## Evidence matrix\n\n"
            "| Dimension | Per-paper evidence | Synthesis | Comparability |\n"
            "|---|---|---|---|\n"
            if index == 1
            else ""
        )
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=(
                    header
                    + f"| {_escape_table(raw['dimension'])} | {_escape_table(observation_text)} "
                    f"| {_escape_table(raw['synthesis'])} | {raw['comparability']} |"
                ),
                evidence_ids=tuple(_uuid_list(raw["evidence_ids"])),
            )
        )

    for index, raw in enumerate(_object_list(value["thematic_review"]), 1):
        claim_id = f"theme-{index}"
        cross_document_ids.append(claim_id)
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=("## Thematic review\n\n" if index == 1 else "")
                + f"### {raw['theme']}\n\n{raw['text']}",
                evidence_ids=tuple(_uuid_list(raw["evidence_ids"])),
            )
        )

    _append_research_section(
        claims,
        cross_document_ids,
        value["consensus"],
        prefix="consensus",
        heading="Consensus",
    )
    _append_research_section(
        claims,
        cross_document_ids,
        value["apparent_differences"],
        prefix="apparent-difference",
        heading="Condition-dependent apparent differences",
    )
    conflicts = _research_items(value["genuine_conflicts"])
    if conflicts:
        _append_research_section(
            claims,
            cross_document_ids,
            value["genuine_conflicts"],
            prefix="genuine-conflict",
            heading="Genuine conflicts",
        )
    else:
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for claim in claims
                if claim.claim_id in brief_ids
                for evidence_id in claim.evidence_ids
            )
        )
        claim_id = "genuine-conflict-none"
        cross_document_ids.append(claim_id)
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=(
                    "## Genuine conflicts\n\n"
                    "No genuine conflict is established by the selected evidence; reported "
                    "differences must be interpreted under their respective conditions."
                ),
                evidence_ids=evidence_ids,
            )
        )
    all_brief_evidence = tuple(
        dict.fromkeys(
            evidence_id
            for claim in claims
            if claim.claim_id in brief_ids
            for evidence_id in claim.evidence_ids
        )
    )
    gaps = "\n".join(f"- {item}" for item in _string_list(value["evidence_gaps"]))
    gap_id = "evidence-gaps"
    cross_document_ids.append(gap_id)
    claims.append(
        StructuredClaimDraft(
            claim_id=gap_id,
            text=f"## Evidence gaps\n\n{gaps}",
            evidence_ids=all_brief_evidence,
        )
    )
    limitations = tuple(_string_list(value["limitations"]))
    claims.append(
        StructuredClaimDraft(
            claim_id="review-limitations",
            text="## Review limitations\n\n" + "\n".join(f"- {item}" for item in limitations),
            evidence_ids=all_brief_evidence,
        )
    )
    return StructuredResearchAnswerDraft(
        text="\n\n".join(claim.text for claim in claims),
        claims=tuple(claims),
        limitations=limitations,
        mode=GroundedAnswerMode.RESEARCH_LITERATURE_REVIEW,
        paper_brief_claim_ids=tuple(brief_ids),
        cross_document_claim_ids=tuple(cross_document_ids),
    )


def _append_research_section(
    claims: list[StructuredClaimDraft],
    cross_document_ids: list[str],
    raw_items: Any,
    *,
    prefix: str,
    heading: str,
) -> None:
    for index, (text, evidence_ids) in enumerate(_research_items(raw_items), 1):
        claim_id = f"{prefix}-{index}"
        cross_document_ids.append(claim_id)
        claims.append(
            StructuredClaimDraft(
                claim_id=claim_id,
                text=(f"## {heading}\n\n" if index == 1 else "") + f"- {text}",
                evidence_ids=evidence_ids,
            )
        )


def _research_item(value: Any) -> tuple[str, tuple[UUID, ...]]:
    if not isinstance(value, dict):
        raise StructuredOutputError("Research evidence item must be an object")
    return str(value["text"]), tuple(_uuid_list(value["evidence_ids"]))


def _research_items(value: Any) -> tuple[tuple[str, tuple[UUID, ...]], ...]:
    return tuple(_research_item(item) for item in _object_list(value))


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise StructuredOutputError("Research section must be an array of objects")
    return value


def _escape_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _answer_mode_instruction(mode: GroundedAnswerMode) -> str:
    if mode is GroundedAnswerMode.RESEARCH_DEEP_READ:
        return (
            "Use concise Markdown-style headings inside claim text, in this order: research "
            "question, contributions, method and formula explanation for an undergraduate, "
            "data and metrics, results, limitations, common misconceptions, and follow-up "
            "questions. Every factual section must cite supplied evidence. Do not invent "
            "missing formulas, experiments, or conclusions."
        )
    if mode is GroundedAnswerMode.RESEARCH_LITERATURE_REVIEW:
        return (
            "Produce every required Research-v2 field: one brief per selected paper; at least "
            "three evidence-matrix rows; at least two thematic sections; consensus; "
            "condition-dependent apparent differences; genuine conflicts (an empty array when "
            "none are established); evidence gaps; and limitations. Each paper brief must use "
            "evidence from exactly one distinct paper. Every matrix synthesis, thematic section, "
            "consensus, apparent difference, and genuine conflict must cite evidence spanning at "
            "least two papers. Matrix observations must contain one entry per paper being "
            "synthesized. Never rank results across incompatible datasets, metrics, budgets, or "
            "experimental settings; mark those rows conditionally_comparable or not_comparable."
        )
    return "Use the default grounded-answer structure."


def _references_unknown_evidence(
    draft: StructuredQADraft, evidence: tuple[EvidenceCandidate, ...]
) -> bool:
    if isinstance(draft, StructuredRefusalDraft):
        return False
    referenced = (
        draft.evidence_ids
        if isinstance(draft, StructuredConflictDraft)
        else tuple(evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids)
    )
    allowed = {candidate.evidence_id for candidate in evidence}
    return bool(set(referenced) - allowed)


def _validate_research_document_grounding(
    draft: StructuredResearchAnswerDraft,
    evidence_by_id: dict[UUID, EvidenceCandidate],
) -> None:
    if draft.mode is not GroundedAnswerMode.RESEARCH_LITERATURE_REVIEW:
        return
    claims = {claim.claim_id: claim for claim in draft.claims}
    brief_documents: set[UUID] = set()
    for claim_id in draft.paper_brief_claim_ids:
        documents = {evidence_by_id[item].document_id for item in claims[claim_id].evidence_ids}
        if len(documents) != 1 or documents & brief_documents:
            raise StructuredOutputError(
                "Each Research paper brief must cite one distinct selected paper"
            )
        brief_documents.update(documents)
    if len(brief_documents) < 2:
        raise StructuredOutputError("A Research literature review must cover at least two papers")
    for claim_id in draft.cross_document_claim_ids:
        documents = {evidence_by_id[item].document_id for item in claims[claim_id].evidence_ids}
        if len(documents) < 2:
            raise StructuredOutputError(
                "Research synthesis sections must cite evidence from at least two papers"
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
