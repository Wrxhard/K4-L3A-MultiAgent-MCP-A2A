import asyncio
from typing import Any

import pytest

from student_agent.agents import analyze_shipment, investigate_shipment
from student_agent.domain import AgentTask, PrimaryIssue
from student_agent.evidence import EvidenceAdapterError, EvidenceRegistry, ToolCatalog
from student_agent.orchestration import CaseTrace


def summary(
    *,
    handoff: str | None = "2026-01-09T10:00:00Z",
    limit: str | None = "2026-01-10T10:00:00Z",
    delivered: str | None = "2026-01-18T10:00:00Z",
    estimated: str | None = "2026-01-15T10:00:00Z",
) -> dict[str, Any]:
    return {
        "order_id": "order-1",
        "shipment_id": "shipment-1",
        "carrier_handoff_at": handoff,
        "shipping_limit_at": limit,
        "delivered_at": delivered,
        "estimated_delivery_at": estimated,
    }


def test_late_after_on_time_handoff_is_logistics_responsibility() -> None:
    result = analyze_shipment(summary())
    assert result.issue is PrimaryIssue.LATE_DELIVERY_LOGISTICS
    assert result.late_days == 3


def test_handoff_after_shipping_limit_is_seller_responsibility() -> None:
    result = analyze_shipment(summary(handoff="2026-01-12T10:00:00Z"))
    assert result.issue is PrimaryIssue.LATE_DELIVERY_SELLER


def test_on_time_delivery_has_no_late_issue() -> None:
    result = analyze_shipment(summary(delivered="2026-01-14T10:00:00Z"))
    assert result.issue is None
    assert result.late_days is None


def test_missing_handoff_does_not_guess_responsibility() -> None:
    result = analyze_shipment(summary(handoff=None))
    assert result.late_days == 3
    assert result.issue is None


def test_invalid_shipment_timestamp_is_rejected() -> None:
    with pytest.raises(EvidenceAdapterError, match="ISO-8601"):
        analyze_shipment(summary(delivered="not-a-date"))


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_shipment_abcdefghijklmnopqrstuvwxyz",
            "result_hash": "sha256:" + "d" * 64,
            "domain": "shipment",
            "data": self.data,
            "warnings": [],
        }


class FakeTraceSink:
    def emit(self, **event: Any) -> dict[str, Any]:
        return event


def test_shipment_specialist_registers_traceable_issue_fact() -> None:
    registry = EvidenceRegistry("CASE_001")
    trace = CaseTrace("CASE_001", FakeTraceSink(), registry)
    trace.case_received()
    task = AgentTask(
        task_id="CASE_001-shipment-1",
        case_id="CASE_001",
        actor="shipment-agent",
        objective="Verify delivery timing",
    )
    trace.task_assigned(task)
    gateway = FakeGateway(summary())
    report = asyncio.run(
        investigate_shipment(
            task,
            order_id="order-1",
            gateway=gateway,
            catalog=ToolCatalog(),
            registry=registry,
            trace=trace,
        )
    )

    issue = next(fact for fact in report.facts if fact.fact_code == "SHIPMENT_ISSUE")
    assert issue.value == "late_delivery_logistics"
    assert report.evidence_refs == ("ev_shipment_abcdefghijklmnopqrstuvwxyz",)
    assert registry.get(report.evidence_refs[0]).consumed_by == ("shipment-agent",)
    assert gateway.calls == [
        ("get_shipment_summary", "CASE_001", {"order_id": "order-1"})
    ]
