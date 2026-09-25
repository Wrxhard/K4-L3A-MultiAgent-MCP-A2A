from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from langchain_core.runnables import Runnable

Actor: TypeAlias = Literal["order_item", "payment", "shipment", "policy"]
AgentStatus: TypeAlias = Literal["completed", "failed"]
VerificationVerdict: TypeAlias = Literal["passed", "retry_required", "failed"]
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

ACTORS: tuple[Actor, ...] = ("order_item", "payment", "shipment", "policy")
EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
TEAM_KEY_PATTERN = re.compile(r"sk-team-[A-Za-z0-9_-]{16,128}")


def _unique_strings(values: tuple[str, ...], label: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must not contain duplicates")


def _evidence_refs(values: tuple[str, ...], label: str = "evidence_refs") -> None:
    _unique_strings(values, label)
    if any(not EVIDENCE_REF_PATTERN.fullmatch(value) for value in values):
        raise ValueError(f"{label} contains an invalid evidence reference")


@dataclass(frozen=True)
class EntitySet:
    order_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    payment_references: tuple[str, ...] = ()
    shipment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "order_ids",
            "item_ids",
            "seller_ids",
            "payment_references",
            "shipment_ids",
        ):
            _unique_strings(getattr(self, name), name)


@dataclass(frozen=True)
class Observation:
    code: str
    subject_id: str | None
    facts: Mapping[str, JSONValue]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("observation code must be non-empty")
        _evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class ToolCallSummary:
    tool_name: str
    attempts: int
    status: Literal["completed", "failed"]
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool_name must be non-empty")
        if self.attempts < 1:
            raise ValueError("tool attempts must be positive")
        if self.status not in ("completed", "failed"):
            raise ValueError("invalid tool status")
        _evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class OrderItemPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class PaymentPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class ShipmentPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class PolicyPayload:
    assessment: Mapping[str, JSONValue]
    claim_assessments: tuple[Mapping[str, JSONValue], ...]
    root_cause_analysis: Mapping[str, JSONValue]
    data_conflicts: tuple[Mapping[str, JSONValue], ...]
    financial_resolution: Mapping[str, JSONValue]
    resolution_actions: tuple[str, ...]


AgentPayload: TypeAlias = OrderItemPayload | PaymentPayload | ShipmentPayload | PolicyPayload


@dataclass(frozen=True)
class AgentFeedback:
    error_code: str
    message: str
    required_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.error_code or not self.message:
            raise ValueError("feedback error_code and message must be non-empty")
        _unique_strings(self.required_checks, "required_checks")


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    actor: Actor
    invocation: int
    context_version: int
    retry_count_for_context: int
    case: Mapping[str, JSONValue]
    context: Mapping[str, Any]
    feedback: AgentFeedback | None = None

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id must be non-empty")
        if self.actor not in ACTORS:
            raise ValueError("invalid actor")
        if self.invocation < 1:
            raise ValueError("invocation must be positive")
        if self.context_version < 1:
            raise ValueError("context_version must be positive")
        if self.retry_count_for_context < 0:
            raise ValueError("retry_count_for_context must not be negative")


@dataclass(frozen=True)
class AgentResult:
    actor: Actor
    invocation: int
    context_version: int
    retry_count_for_context: int
    status: AgentStatus
    payload: AgentPayload | None
    evidence_refs: tuple[str, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    failed_stage: str | None
    error_code: str | None
    retryable: bool
    safe_detail: str | None

    def __post_init__(self) -> None:
        if self.actor not in ACTORS:
            raise ValueError("invalid actor")
        if self.invocation < 1:
            raise ValueError("invocation must be positive")
        if self.context_version < 1:
            raise ValueError("context_version must be positive")
        if self.retry_count_for_context < 0:
            raise ValueError("retry_count_for_context must not be negative")
        _evidence_refs(self.evidence_refs)
        if self.safe_detail is not None and (
            len(self.safe_detail) > 160 or TEAM_KEY_PATTERN.search(self.safe_detail)
        ):
            raise ValueError("safe_detail contains unsafe content")
        if self.status == "completed":
            if self.payload is None or self.error_code is not None or self.failed_stage is not None:
                raise ValueError("completed result has invalid failure fields")
        elif self.status == "failed":
            if self.payload is not None or not self.error_code or not self.failed_stage:
                raise ValueError("failed result has invalid fields")
        else:
            raise ValueError("invalid agent status")

    @classmethod
    def completed(
        cls,
        *,
        actor: Actor,
        invocation: int,
        context_version: int,
        retry_count_for_context: int,
        payload: AgentPayload,
        evidence_refs: tuple[str, ...] = (),
        tool_calls: tuple[ToolCallSummary, ...] = (),
    ) -> AgentResult:
        return cls(
            actor=actor,
            invocation=invocation,
            context_version=context_version,
            retry_count_for_context=retry_count_for_context,
            status="completed",
            payload=payload,
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            tool_calls=tool_calls,
            failed_stage=None,
            error_code=None,
            retryable=False,
            safe_detail=None,
        )

    @classmethod
    def failed(
        cls,
        *,
        actor: Actor,
        invocation: int,
        context_version: int,
        retry_count_for_context: int,
        error_code: str,
        failed_stage: str,
        retryable: bool,
        safe_detail: str,
        evidence_refs: tuple[str, ...] = (),
        tool_calls: tuple[ToolCallSummary, ...] = (),
    ) -> AgentResult:
        return cls(
            actor=actor,
            invocation=invocation,
            context_version=context_version,
            retry_count_for_context=retry_count_for_context,
            status="failed",
            payload=None,
            evidence_refs=evidence_refs,
            tool_calls=tool_calls,
            failed_stage=failed_stage,
            error_code=error_code,
            retryable=retryable,
            safe_detail=safe_detail[:160],
        )


@dataclass(frozen=True)
class VerificationPackage:
    case_id: str
    verification_round: int
    candidate_output: Mapping[str, Any] | None
    attempt_history: Mapping[Actor, tuple[AgentResult, ...]]
    active_results: Mapping[Actor, AgentResult]
    unresolved_failures: tuple[AgentResult, ...]


@dataclass(frozen=True)
class VerificationReport:
    verdict: VerificationVerdict
    target_actor: Actor | str | None
    error_code: str | None
    feedback: str | None
    retryable: bool

    @classmethod
    def passed(cls) -> VerificationReport:
        return cls("passed", None, None, None, False)

    @classmethod
    def retry(cls, target_actor: Actor, error_code: str, feedback: str) -> VerificationReport:
        return cls("retry_required", target_actor, error_code, feedback, True)

    @classmethod
    def failed(cls, error_code: str, feedback: str) -> VerificationReport:
        return cls("failed", None, error_code, feedback, False)


@dataclass(frozen=True)
class AgentRegistry:
    order_item: Runnable[AgentTask, AgentResult]
    payment: Runnable[AgentTask, AgentResult]
    shipment: Runnable[AgentTask, AgentResult]
    policy: Runnable[AgentTask, AgentResult]
    verifier: Runnable[VerificationPackage, VerificationReport]

    def for_actor(self, actor: Actor) -> Runnable[AgentTask, AgentResult]:
        if actor not in ACTORS:
            raise ValueError(f"invalid actor: {actor}")
        return getattr(self, actor)


PAYLOAD_TYPE_BY_ACTOR: dict[Actor, type[AgentPayload]] = {
    "order_item": OrderItemPayload,
    "payment": PaymentPayload,
    "shipment": ShipmentPayload,
    "policy": PolicyPayload,
}


def validate_agent_result(task: AgentTask, result: AgentResult) -> None:
    for field in ("actor", "invocation", "context_version", "retry_count_for_context"):
        if getattr(task, field) != getattr(result, field):
            raise ValueError(f"agent result {field} does not match task")
    if result.status == "completed" and not isinstance(
        result.payload, PAYLOAD_TYPE_BY_ACTOR[result.actor]
    ):
        raise ValueError(f"{result.actor} returned an invalid payload type")


def validate_verification_report(report: VerificationReport) -> None:
    if report.verdict == "passed":
        if any((report.target_actor, report.error_code, report.feedback, report.retryable)):
            raise ValueError("passed verification report has invalid fields")
        return
    if report.verdict == "retry_required":
        if report.target_actor not in ACTORS:
            raise ValueError("retry report has an invalid target_actor")
        if not report.error_code or not report.feedback or not report.retryable:
            raise ValueError("retry report has invalid fields")
        return
    if report.verdict == "failed":
        if report.target_actor is not None or not report.error_code or not report.feedback:
            raise ValueError("failed verification report has invalid fields")
        if report.retryable:
            raise ValueError("failed verification report cannot be retryable")
        return
    raise ValueError("invalid verification verdict")
