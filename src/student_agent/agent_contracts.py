from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypeAlias


JSONValue: TypeAlias = str | int | float | bool | None | list[Any] | dict[str, Any]


@dataclass(frozen=True)
class AgentFeedback:
    error_code: str
    message: str
    required_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    actor: Literal["policy"]
    invocation: int
    context_version: int
    retry_count_for_context: int
    case: Mapping[str, JSONValue]
    context: Mapping[str, Any]
    feedback: AgentFeedback | None = None


@dataclass(frozen=True)
class ToolCallSummary:
    tool_name: str
    attempts: int
    status: Literal["completed", "failed", "skipped"]
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyPayload:
    assessment: Mapping[str, Any]
    claim_assessments: tuple[Mapping[str, Any], ...]
    root_cause_analysis: Mapping[str, Any]
    data_conflicts: tuple[Mapping[str, Any], ...]
    financial_resolution: Mapping[str, Any]
    resolution_actions: tuple[str, ...]


@dataclass(frozen=True)
class AgentResult:
    actor: str
    invocation: int
    context_version: int
    retry_count_for_context: int
    status: Literal["completed", "failed"]
    payload: PolicyPayload | None = None
    evidence_refs: tuple[str, ...] = ()
    tool_calls: tuple[ToolCallSummary, ...] = ()
    error_code: str | None = None
    failed_stage: str | None = None
    retryable: bool = False
    safe_detail: str | None = None

    @classmethod
    def completed(
        cls,
        *,
        actor: str,
        invocation: int,
        context_version: int,
        retry_count_for_context: int,
        payload: PolicyPayload,
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
        )

    @classmethod
    def failed(
        cls,
        *,
        actor: str,
        invocation: int,
        context_version: int,
        retry_count_for_context: int,
        error_code: str,
        failed_stage: str,
        retryable: bool,
        safe_detail: str,
        tool_calls: tuple[ToolCallSummary, ...] = (),
    ) -> AgentResult:
        return cls(
            actor=actor,
            invocation=invocation,
            context_version=context_version,
            retry_count_for_context=retry_count_for_context,
            status="failed",
            error_code=error_code,
            failed_stage=failed_stage,
            retryable=retryable,
            safe_detail=safe_detail[:160],
            tool_calls=tool_calls,
        )


@dataclass(frozen=True)
class AgentRegistry:
    """Small integration seam for the coordinator's agent runnables."""

    policy: Any
