from typing import Any

import pytest

from student_agent.domain import (
    AgentTask,
    EvidenceDomain,
    EvidenceRecord,
    EvidenceSource,
    Fact,
    SpecialistReport,
    TaskStatus,
    VerificationReport,
)
from student_agent.evidence import EvidenceRegistry
from student_agent.orchestration import CaseTrace, LifecycleTraceError

EVIDENCE_REF = "ev_abcdefghijklmnopqrstuvwxyz"


class FakeTraceSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def build_trace() -> tuple[CaseTrace, FakeTraceSink, EvidenceRegistry]:
    sink = FakeTraceSink()
    registry = EvidenceRegistry("CASE_001")
    registry.register(
        EvidenceRecord(
            case_id="CASE_001",
            evidence_ref=EVIDENCE_REF,
            result_hash="sha256:" + "a" * 64,
            domain=EvidenceDomain.ORDER,
            tool_name="get_order",
            data={"order_id": "order-1"},
        )
    )
    return CaseTrace("CASE_001", sink, registry), sink, registry


def task(*, case_id: str = "CASE_001") -> AgentTask:
    return AgentTask(
        task_id="task-order",
        case_id=case_id,
        actor="order-item-agent",
        objective="Inspect order state",
    )


def report() -> SpecialistReport:
    return SpecialistReport(
        case_id="CASE_001",
        task_id="task-order",
        actor="order-item-agent",
        status=TaskStatus.COMPLETED,
        facts=(
            Fact(
                fact_id="fact-order",
                fact_code="ORDER_STATUS",
                value="canceled",
                source=EvidenceSource.MCP,
                evidence_refs=(EVIDENCE_REF,),
            ),
        ),
        evidence_refs=(EVIDENCE_REF,),
    )


def test_full_lifecycle_preserves_order_and_evidence_linkage() -> None:
    trace, sink, registry = build_trace()
    trace.case_received()
    trace.task_assigned(task())
    trace.tool_result_consumed(
        actor="order-item-agent", tool_name="get_order", evidence_ref=EVIDENCE_REF
    )
    trace.handoff(report())
    trace.verification_completed(VerificationReport(case_id="CASE_001", passed=True))
    trace.case_finalized(evidence_refs=(EVIDENCE_REF,))

    assert [event["event_type"] for event in sink.events] == [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "verification_completed",
        "case_finalized",
    ]
    assert registry.get(EVIDENCE_REF).consumed_by == ("order-item-agent",)
    assert registry.refs_for_fact("fact-order") == (EVIDENCE_REF,)


def test_assignment_requires_case_received() -> None:
    trace, _, _ = build_trace()
    with pytest.raises(LifecycleTraceError, match="case_received"):
        trace.task_assigned(task())


def test_assignment_rejects_cross_case_task() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    with pytest.raises(LifecycleTraceError, match="another case"):
        trace.task_assigned(task(case_id="CASE_002"))


def test_consumption_requires_an_assigned_actor() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    with pytest.raises(LifecycleTraceError, match="no assigned task"):
        trace.tool_result_consumed(
            actor="order-item-agent", tool_name="get_order", evidence_ref=EVIDENCE_REF
        )


def test_handoff_requires_consumed_evidence() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    trace.task_assigned(task())
    with pytest.raises(ValueError, match="unconsumed"):
        trace.handoff(report())


def test_verification_requires_handoff() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    with pytest.raises(LifecycleTraceError, match="handoff"):
        trace.verification_completed(VerificationReport(case_id="CASE_001", passed=True))


def test_finalize_requires_verification() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    with pytest.raises(LifecycleTraceError, match="verification_completed"):
        trace.case_finalized(evidence_refs=())


def test_failed_verification_allows_exactly_one_retry() -> None:
    trace, sink, _ = build_trace()
    trace.case_received()
    trace.task_assigned(task())
    trace.tool_result_consumed(
        actor="order-item-agent", tool_name="get_order", evidence_ref=EVIDENCE_REF
    )
    trace.handoff(report())
    trace.verification_completed(
        VerificationReport(
            case_id="CASE_001",
            passed=False,
            error_codes=("REVISION_REQUIRED",),
        )
    )
    trace.verification_completed(VerificationReport(case_id="CASE_001", passed=True))
    with pytest.raises(LifecycleTraceError, match="passing verification"):
        trace.verification_completed(VerificationReport(case_id="CASE_001", passed=True))
    assert [event["decision_code"] for event in sink.events[-2:]] == [
        "REVISION_REQUIRED",
        "VERIFICATION_PASSED",
    ]


def test_no_event_after_finalization() -> None:
    trace, _, _ = build_trace()
    trace.case_received()
    trace.task_assigned(task())
    trace.tool_result_consumed(
        actor="order-item-agent", tool_name="get_order", evidence_ref=EVIDENCE_REF
    )
    trace.handoff(report())
    trace.verification_completed(VerificationReport(case_id="CASE_001", passed=True))
    trace.case_finalized(evidence_refs=(EVIDENCE_REF,))
    with pytest.raises(LifecycleTraceError, match="after case_finalized"):
        trace.task_assigned(
            AgentTask(
                task_id="task-late",
                case_id="CASE_001",
                actor="payment-agent",
                objective="Too late",
            )
        )
