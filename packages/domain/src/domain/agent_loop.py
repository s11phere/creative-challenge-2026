"""Provider-neutral state machine for a recoverable Agent Loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast

type ModelVisibleJSON = (
    None | bool | int | float | str | list["ModelVisibleJSON"] | dict[str, "ModelVisibleJSON"]
)

_MAX_MODEL_VISIBLE_OBSERVATION_BYTES = 16_384
_MAX_FINAL_RESPONSE_CHARS = 12_000


class AgentLoopContractError(ValueError):
    """Base error for invalid loop state or transitions."""


class AgentLoopTransitionError(AgentLoopContractError):
    """Raised when a loop action is not legal for its current phase."""


class AgentLoopNoProgressError(AgentLoopContractError):
    """Raised when a loop repeats the same Tool request without progress."""


class AgentLoopPhase(StrEnum):
    ACCEPTED = "accepted"
    PLANNING = "planning"
    TOOL_REQUESTED = "tool_requested"
    WAITING_APPROVAL = "waiting_approval"
    TOOL_RUNNING = "tool_running"
    OBSERVING = "observing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CLARIFYING = "clarifying"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentLoopStopReason(StrEnum):
    GOAL_COMPLETE = "goal_complete"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    CONFLICT = "conflict"
    USER_CLARIFICATION = "user_clarification"
    POLICY_REFUSAL = "policy_refusal"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class AgentLoopFinalizationState(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PUBLISHED = "published"


@dataclass(frozen=True)
class AgentLoopTask:
    """Server-authored bounded goal and subquestions for one loop."""

    goal: str
    subquestions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.goal.strip() or len(self.goal) > 4_000:
            raise AgentLoopContractError("Agent Loop goal must be non-empty and bounded")
        if len(self.subquestions) > 20 or any(
            not question.strip() or len(question) > 1_000 for question in self.subquestions
        ):
            raise AgentLoopContractError("Agent Loop subquestions are invalid or unbounded")
        if len(set(self.subquestions)) != len(self.subquestions):
            raise AgentLoopContractError("Agent Loop subquestions must be unique")


@dataclass(frozen=True)
class AgentLoopCompletionCheck:
    goal_complete: bool = False
    evidence_sufficient: bool = False
    has_conflict: bool = False


@dataclass(frozen=True)
class AgentLoopToolObservation:
    """A redacted Tool observation persisted in a checkpoint.

    ``model_output`` is retained only when the Tool contract explicitly allows model
    visibility.  It is bounded and checkpointed locally so recovery can continue the
    same reasoning loop; SSE and audit events still contain only digests.
    """

    iteration: int
    tool_name: str
    tool_version: str
    idempotency_key: str
    input_summary: str
    output_summary: str
    error_code: str | None = None
    retry_count: int = 0
    duration_ms: int = 0
    model_output: ModelVisibleJSON | None = None

    def __post_init__(self) -> None:
        if self.iteration < 1 or not self.tool_name or not self.tool_version:
            raise AgentLoopContractError("Tool observation identity is invalid")
        if not self.idempotency_key or not self.input_summary or not self.output_summary:
            raise AgentLoopContractError("Tool observation must contain redacted summaries")
        if self.retry_count < 0 or self.duration_ms < 0:
            raise AgentLoopContractError("Tool observation counters cannot be negative")
        try:
            encoded = json.dumps(
                self.model_output,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AgentLoopContractError("Tool observation model output is not JSON") from exc
        if len(encoded) > _MAX_MODEL_VISIBLE_OBSERVATION_BYTES:
            raise AgentLoopContractError("Tool observation model output is too large")


@dataclass(frozen=True)
class AgentLoopIteration:
    number: int
    decision_digest: str
    tool_name: str | None = None
    observation: AgentLoopToolObservation | None = None

    def __post_init__(self) -> None:
        if self.number < 1 or not self.decision_digest:
            raise AgentLoopContractError("Loop iteration identity is invalid")
        if self.observation is not None and self.observation.iteration != self.number:
            raise AgentLoopContractError("Tool observation belongs to another iteration")


@dataclass(frozen=True)
class AgentLoopState:
    """Immutable loop state that can be serialized into a Runtime checkpoint."""

    task: AgentLoopTask
    phase: AgentLoopPhase = AgentLoopPhase.ACCEPTED
    iteration: int = 0
    completion: AgentLoopCompletionCheck = AgentLoopCompletionCheck()
    observations: tuple[AgentLoopToolObservation, ...] = ()
    iterations: tuple[AgentLoopIteration, ...] = ()
    stop_reason: AgentLoopStopReason | None = None
    finalization: AgentLoopFinalizationState = AgentLoopFinalizationState.NOT_STARTED
    finalization_action: str | None = None
    finalizer_publication_id: str | None = None
    finalization_response: str | None = None
    approval_id: str | None = None
    lease_id: str | None = None
    last_idempotency_key: str | None = None
    tool_request_fingerprints: tuple[str, ...] = ()
    repeated_tool_request_fingerprints: tuple[str, ...] = ()
    pending_tool_name: str | None = None
    pending_tool_version: str | None = None
    pending_arguments: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.iteration < 0 or len(self.iterations) > self.iteration:
            raise AgentLoopContractError("Loop iteration counter is inconsistent")
        if len(self.observations) > self.iteration:
            raise AgentLoopContractError("Loop observation count is inconsistent")
        if self.phase is AgentLoopPhase.FINALIZING and (
            self.finalization is not AgentLoopFinalizationState.IN_PROGRESS
        ):
            raise AgentLoopContractError("Finalizing loop must have in-progress finalization")
        if self.phase in {AgentLoopPhase.COMPLETED, AgentLoopPhase.REFUSED} and (
            self.finalization is not AgentLoopFinalizationState.PUBLISHED
        ):
            raise AgentLoopContractError("Terminal loop must have a published finalization")
        finalization_identity = (self.finalization_action, self.finalizer_publication_id)
        if any(value is not None for value in finalization_identity) and not all(
            value is not None for value in finalization_identity
        ):
            raise AgentLoopContractError("Finalization publication identity is incomplete")
        if self.finalization_action is not None and self.finalization_action not in {
            "complete",
            "clarify",
            "refuse",
        }:
            raise AgentLoopContractError("Finalization action is invalid")
        if self.finalization_response is not None and (
            not self.finalization_response.strip()
            or len(self.finalization_response) > _MAX_FINAL_RESPONSE_CHARS
        ):
            raise AgentLoopContractError("Finalization response is invalid")
        if (
            self.finalizer_publication_id is not None
            and not self.finalizer_publication_id.startswith("assistant-publication:")
        ):
            raise AgentLoopContractError("Finalizer publication identity is invalid")
        if (
            self.phase
            in {
                AgentLoopPhase.CLARIFYING,
                AgentLoopPhase.FAILED,
                AgentLoopPhase.CANCELLED,
                AgentLoopPhase.TIMED_OUT,
            }
            and self.stop_reason is None
        ):
            raise AgentLoopContractError("Stopped loop must record a stop reason")
        pending = (self.pending_tool_name, self.pending_tool_version, self.pending_arguments)
        if any(value is not None for value in pending) and not all(
            value is not None for value in pending
        ):
            raise AgentLoopContractError("Pending Tool request is incomplete")

    @classmethod
    def accepted(cls, task: AgentLoopTask, *, lease_id: str | None = None) -> AgentLoopState:
        return cls(task=task, lease_id=lease_id)

    def start(self) -> AgentLoopState:
        self._require_phase(AgentLoopPhase.ACCEPTED)
        return replace(self, phase=AgentLoopPhase.PLANNING)

    def begin_iteration(self, decision: Any) -> AgentLoopState:
        if self.phase not in {AgentLoopPhase.PLANNING, AgentLoopPhase.OBSERVING}:
            raise AgentLoopTransitionError("Loop can plan only from planning or observing")
        number = self.iteration + 1
        digest = _digest(decision)
        return replace(
            self,
            phase=AgentLoopPhase.PLANNING,
            iteration=number,
            iterations=self.iterations + (AgentLoopIteration(number, digest),),
        )

    def request_tool(
        self,
        *,
        name: str,
        version: str,
        arguments: dict[str, object],
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AgentLoopState:
        self._require_phase(AgentLoopPhase.PLANNING)
        if not idempotency_key:
            raise AgentLoopContractError("Tool request requires an idempotency key")
        if not request_fingerprint:
            raise AgentLoopContractError("Tool request requires a request fingerprint")
        if request_fingerprint in self.tool_request_fingerprints:
            raise AgentLoopNoProgressError("Tool request repeats an existing request fingerprint")
        return replace(
            self,
            phase=AgentLoopPhase.TOOL_REQUESTED,
            iterations=self.iterations[:-1] + (replace(self.iterations[-1], tool_name=name),),
            last_idempotency_key=idempotency_key,
            pending_tool_name=name,
            pending_tool_version=version,
            pending_arguments=dict(arguments),
            tool_request_fingerprints=self.tool_request_fingerprints + (request_fingerprint,),
        )

    def observe_repeated_tool_request(
        self,
        *,
        name: str,
        version: str,
        idempotency_key: str,
        request_fingerprint: str,
        input_summary: str,
        output_summary: str,
        model_output: ModelVisibleJSON,
    ) -> AgentLoopState:
        """Return one model-visible recovery observation for a duplicate Tool request."""
        self._require_phase(AgentLoopPhase.PLANNING)
        if (
            request_fingerprint not in self.tool_request_fingerprints
            or request_fingerprint in self.repeated_tool_request_fingerprints
        ):
            raise AgentLoopNoProgressError("Tool request has no recoverable duplicate observation")
        observation = AgentLoopToolObservation(
            iteration=self.iteration,
            tool_name=name,
            tool_version=version,
            idempotency_key=idempotency_key,
            input_summary=input_summary,
            output_summary=output_summary,
            error_code="RUN_LLM_DUPLICATE_TOOL_REQUEST",
            model_output=model_output,
        )
        return replace(
            self,
            phase=AgentLoopPhase.OBSERVING,
            iterations=self.iterations[:-1]
            + (replace(self.iterations[-1], tool_name=name, observation=observation),),
            observations=self.observations + (observation,),
            repeated_tool_request_fingerprints=self.repeated_tool_request_fingerprints
            + (request_fingerprint,),
        )

    def wait_for_approval(self, approval_id: str | None = None) -> AgentLoopState:
        self._require_phase(AgentLoopPhase.TOOL_REQUESTED)
        return replace(self, phase=AgentLoopPhase.WAITING_APPROVAL, approval_id=approval_id)

    def start_tool(self) -> AgentLoopState:
        if self.phase not in {AgentLoopPhase.TOOL_REQUESTED, AgentLoopPhase.WAITING_APPROVAL}:
            raise AgentLoopTransitionError("Tool can start only after a Tool request")
        return replace(self, phase=AgentLoopPhase.TOOL_RUNNING)

    def observe(self, observation: AgentLoopToolObservation) -> AgentLoopState:
        self._require_phase(AgentLoopPhase.TOOL_RUNNING)
        if observation.iteration != self.iteration:
            raise AgentLoopContractError("Observation iteration does not match the loop")
        updated_iterations = self.iterations[:-1] + (
            replace(self.iterations[-1], observation=observation),
        )
        return replace(
            self,
            phase=AgentLoopPhase.OBSERVING,
            observations=self.observations + (observation,),
            iterations=updated_iterations,
            pending_tool_name=None,
            pending_tool_version=None,
            pending_arguments=None,
        )

    def begin_finalization(
        self,
        *,
        completion: AgentLoopCompletionCheck,
        stop_reason: AgentLoopStopReason,
        finalization_action: str | None = None,
        finalizer_publication_id: str | None = None,
        finalization_response: str | None = None,
    ) -> AgentLoopState:
        if self.phase not in {AgentLoopPhase.PLANNING, AgentLoopPhase.OBSERVING}:
            raise AgentLoopTransitionError("Loop can finalize only after planning or observation")
        return replace(
            self,
            phase=AgentLoopPhase.FINALIZING,
            completion=completion,
            stop_reason=stop_reason,
            finalization=AgentLoopFinalizationState.IN_PROGRESS,
            finalization_action=finalization_action,
            finalizer_publication_id=finalizer_publication_id,
            finalization_response=finalization_response,
        )

    def publish(self, *, refused: bool = False, clarified: bool = False) -> AgentLoopState:
        self._require_phase(AgentLoopPhase.FINALIZING)
        if refused and clarified:
            raise AgentLoopContractError("Loop finalization cannot refuse and clarify together")
        return replace(
            self,
            phase=(
                AgentLoopPhase.REFUSED
                if refused
                else AgentLoopPhase.CLARIFYING
                if clarified
                else AgentLoopPhase.COMPLETED
            ),
            finalization=AgentLoopFinalizationState.PUBLISHED,
        )

    def stop(self, phase: AgentLoopPhase, reason: AgentLoopStopReason) -> AgentLoopState:
        if phase not in {
            AgentLoopPhase.CLARIFYING,
            AgentLoopPhase.FAILED,
            AgentLoopPhase.CANCELLED,
            AgentLoopPhase.TIMED_OUT,
        }:
            raise AgentLoopContractError("Invalid non-publication stop phase")
        if self.phase in {
            AgentLoopPhase.COMPLETED,
            AgentLoopPhase.REFUSED,
            AgentLoopPhase.CLARIFYING,
            AgentLoopPhase.FAILED,
            AgentLoopPhase.CANCELLED,
            AgentLoopPhase.TIMED_OUT,
        }:
            raise AgentLoopTransitionError("Terminal loop cannot be stopped again")
        return replace(self, phase=phase, stop_reason=reason)

    def as_checkpoint(self) -> dict[str, object]:
        """Return only redacted JSON state suitable for a Runtime checkpoint."""
        return {
            "schema_version": "agent-loop-v2",
            "goal": self.task.goal,
            "subquestions": list(self.task.subquestions),
            "phase": self.phase.value,
            "iteration": self.iteration,
            "completion": {
                "goal_complete": self.completion.goal_complete,
                "evidence_sufficient": self.completion.evidence_sufficient,
                "has_conflict": self.completion.has_conflict,
            },
            "observations": [
                {
                    "iteration": item.iteration,
                    "tool_name": item.tool_name,
                    "tool_version": item.tool_version,
                    "idempotency_key": item.idempotency_key,
                    "input_summary": item.input_summary,
                    "output_summary": item.output_summary,
                    "error_code": item.error_code,
                    "retry_count": item.retry_count,
                    "duration_ms": item.duration_ms,
                    "model_output": item.model_output,
                }
                for item in self.observations
            ],
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "finalization": self.finalization.value,
            "finalization_action": self.finalization_action,
            "finalizer_publication_id": self.finalizer_publication_id,
            "finalization_response": self.finalization_response,
            "approval_id": self.approval_id,
            "lease_id": self.lease_id,
            "last_idempotency_key": self.last_idempotency_key,
            "tool_request_fingerprints": list(self.tool_request_fingerprints),
            "repeated_tool_request_fingerprints": list(self.repeated_tool_request_fingerprints),
            "pending_tool_name": self.pending_tool_name,
            "pending_tool_version": self.pending_tool_version,
            "pending_arguments": self.pending_arguments,
        }

    @classmethod
    def from_checkpoint(cls, value: dict[str, object]) -> AgentLoopState:
        schema_version = value.get("schema_version")
        if schema_version not in {"agent-loop-v1", "agent-loop-v2"}:
            raise AgentLoopContractError("unsupported Agent Loop checkpoint schema")
        raw_completion = value.get("completion", {})
        if not isinstance(raw_completion, dict):
            raise AgentLoopContractError("checkpoint completion is invalid")
        raw_observations = value.get("observations", [])
        if not isinstance(raw_observations, list):
            raise AgentLoopContractError("checkpoint observations are invalid")
        raw_subquestions = value.get("subquestions", [])
        if not isinstance(raw_subquestions, list):
            raise AgentLoopContractError("checkpoint subquestions are invalid")
        raw_fingerprints = value.get("tool_request_fingerprints", [])
        if not isinstance(raw_fingerprints, list) or not all(
            isinstance(item, str) and item for item in raw_fingerprints
        ):
            raise AgentLoopContractError("checkpoint Tool request fingerprints are invalid")
        raw_repeated_fingerprints = value.get("repeated_tool_request_fingerprints", [])
        if not isinstance(raw_repeated_fingerprints, list) or not all(
            isinstance(item, str) and item for item in raw_repeated_fingerprints
        ):
            raise AgentLoopContractError(
                "checkpoint repeated Tool request fingerprints are invalid"
            )
        observations = tuple(
            AgentLoopToolObservation(
                iteration=int(item["iteration"]),
                tool_name=str(item["tool_name"]),
                tool_version=str(item["tool_version"]),
                idempotency_key=str(item["idempotency_key"]),
                input_summary=str(item["input_summary"]),
                output_summary=str(item["output_summary"]),
                error_code=str(item["error_code"]) if item.get("error_code") else None,
                retry_count=int(item.get("retry_count", 0)),
                duration_ms=int(item.get("duration_ms", 0)),
                model_output=(
                    cast(ModelVisibleJSON, item["model_output"])
                    if schema_version == "agent-loop-v2" and "model_output" in item
                    else None
                ),
            )
            for item in cast(list[object], raw_observations)
            if isinstance(item, dict)
        )
        state = cls(
            task=AgentLoopTask(
                goal=str(value["goal"]),
                subquestions=tuple(str(item) for item in cast(list[object], raw_subquestions)),
            ),
            phase=AgentLoopPhase(str(value["phase"])),
            iteration=int(cast(str | int, value.get("iteration", 0))),
            completion=AgentLoopCompletionCheck(
                goal_complete=bool(raw_completion.get("goal_complete", False)),
                evidence_sufficient=bool(raw_completion.get("evidence_sufficient", False)),
                has_conflict=bool(raw_completion.get("has_conflict", False)),
            ),
            observations=observations,
            stop_reason=(
                AgentLoopStopReason(str(value["stop_reason"])) if value.get("stop_reason") else None
            ),
            finalization=AgentLoopFinalizationState(str(value.get("finalization", "not_started"))),
            finalization_action=(
                str(value["finalization_action"]) if value.get("finalization_action") else None
            ),
            finalizer_publication_id=(
                str(value["finalizer_publication_id"])
                if value.get("finalizer_publication_id")
                else None
            ),
            finalization_response=(
                str(value["finalization_response"]) if value.get("finalization_response") else None
            ),
            approval_id=str(value["approval_id"]) if value.get("approval_id") else None,
            lease_id=str(value["lease_id"]) if value.get("lease_id") else None,
            last_idempotency_key=(
                str(value["last_idempotency_key"]) if value.get("last_idempotency_key") else None
            ),
            tool_request_fingerprints=tuple(cast(list[str], raw_fingerprints)),
            repeated_tool_request_fingerprints=tuple(cast(list[str], raw_repeated_fingerprints)),
            pending_tool_name=(
                str(value["pending_tool_name"]) if value.get("pending_tool_name") else None
            ),
            pending_tool_version=(
                str(value["pending_tool_version"]) if value.get("pending_tool_version") else None
            ),
            pending_arguments=(
                dict(cast(dict[str, object], value["pending_arguments"]))
                if isinstance(value.get("pending_arguments"), dict)
                else None
            ),
        )
        return _restore_iterations(state)

    def _require_phase(self, expected: AgentLoopPhase) -> None:
        if self.phase is not expected:
            raise AgentLoopTransitionError(
                f"expected loop phase {expected.value}, got {self.phase.value}"
            )


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _restore_iterations(state: AgentLoopState) -> AgentLoopState:
    by_iteration = {observation.iteration: observation for observation in state.observations}
    iterations = tuple(
        AgentLoopIteration(
            number=number,
            decision_digest="sha256:checkpoint-restored",
            tool_name=(
                by_iteration[number].tool_name
                if number in by_iteration
                else state.pending_tool_name
            ),
            observation=by_iteration.get(number),
        )
        for number in range(1, state.iteration + 1)
    )
    return replace(state, iterations=iterations)


__all__ = [
    "AgentLoopCompletionCheck",
    "AgentLoopContractError",
    "AgentLoopFinalizationState",
    "AgentLoopIteration",
    "AgentLoopNoProgressError",
    "AgentLoopPhase",
    "AgentLoopState",
    "AgentLoopStopReason",
    "AgentLoopTask",
    "AgentLoopToolObservation",
    "AgentLoopTransitionError",
]
