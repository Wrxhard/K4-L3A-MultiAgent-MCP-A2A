from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from student_agent.domain import (
    AgentTask,
    EvidenceSource,
    Fact,
    PrimaryIssue,
    SpecialistReport,
    TaskStatus,
)
from student_agent.evidence import (
    SHIPMENT_AGENT,
    EvidenceAdapterError,
    EvidenceRegistry,
    ToolCatalog,
    adapt_evidence,
)
from student_agent.orchestration import CaseTrace

from .order_item import Gateway

SHIPMENT_TOPICS = frozenset({"late_delivery_seller", "late_delivery_logistics"})


@dataclass(frozen=True, slots=True)
class ShipmentAnalysis:
    shipping_limit_at: datetime | None
    carrier_handoff_at: datetime | None
    delivered_at: datetime | None
    estimated_delivery_at: datetime | None
    issue: PrimaryIssue | None
    late_days: int | None


def _timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvidenceAdapterError(f"{field} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceAdapterError(f"{field} must be an ISO-8601 datetime") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _first(data: Mapping[str, Any], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def analyze_shipment(data: Mapping[str, Any]) -> ShipmentAnalysis:
    shipping_limit_at = _timestamp(
        _first(data, ("shipping_limit_date", "shipping_limit_at")),
        "shipping_limit_at",
    )
    carrier_handoff_at = _timestamp(
        _first(
            data,
            ("order_delivered_carrier_date", "carrier_handoff_at", "shipped_at"),
        ),
        "carrier_handoff_at",
    )
    delivered_at = _timestamp(
        _first(data, ("order_delivered_customer_date", "delivered_at")),
        "delivered_at",
    )
    estimated_delivery_at = _timestamp(
        _first(data, ("order_estimated_delivery_date", "estimated_delivery_at")),
        "estimated_delivery_at",
    )

    issue: PrimaryIssue | None = None
    late_days: int | None = None
    if delivered_at is not None and estimated_delivery_at is not None:
        delay = delivered_at - estimated_delivery_at
        if delay.total_seconds() > 0:
            late_days = max(1, (delay.days if delay.seconds == 0 else delay.days + 1))
            if carrier_handoff_at is not None and shipping_limit_at is not None:
                issue = (
                    PrimaryIssue.LATE_DELIVERY_SELLER
                    if carrier_handoff_at > shipping_limit_at
                    else PrimaryIssue.LATE_DELIVERY_LOGISTICS
                )

    return ShipmentAnalysis(
        shipping_limit_at=shipping_limit_at,
        carrier_handoff_at=carrier_handoff_at,
        delivered_at=delivered_at,
        estimated_delivery_at=estimated_delivery_at,
        issue=issue,
        late_days=late_days,
    )


def _fact(
    *, task: AgentTask, code: str, value: object, evidence_ref: str, entity_id: str
) -> Fact:
    return Fact(
        fact_id=f"{task.task_id}-{code.lower()}",
        fact_code=code,
        value=value,
        source=EvidenceSource.MCP,
        entity_id=entity_id,
        evidence_refs=(evidence_ref,),
    )


async def investigate_shipment(
    task: AgentTask,
    *,
    order_id: str,
    gateway: Gateway,
    catalog: ToolCatalog,
    registry: EvidenceRegistry,
    trace: CaseTrace,
) -> SpecialistReport:
    if task.actor != SHIPMENT_AGENT:
        raise ValueError("shipment specialist received a task owned by another actor")
    tool_name = "get_shipment_summary"
    catalog.authorize(task.actor, tool_name)
    arguments = {"case_id": task.case_id, "order_id": order_id}
    catalog.validate_arguments(tool_name, arguments)
    envelope = await gateway.call(tool_name, case_id=task.case_id, order_id=order_id)
    record = adapt_evidence(
        envelope,
        case_id=task.case_id,
        tool_name=tool_name,
        catalog=catalog,
        expected_order_id=order_id,
    )
    if not isinstance(record.data, Mapping):
        raise EvidenceAdapterError("shipment summary data must be an object")
    registry.register(record)
    trace.tool_result_consumed(
        actor=task.actor,
        tool_name=tool_name,
        evidence_ref=record.evidence_ref,
    )

    analysis = analyze_shipment(record.data)
    timeline = {
        "shipping_limit_at": analysis.shipping_limit_at,
        "carrier_handoff_at": analysis.carrier_handoff_at,
        "delivered_at": analysis.delivered_at,
        "estimated_delivery_at": analysis.estimated_delivery_at,
    }
    facts = [
        _fact(
            task=task,
            code="SHIPMENT_TIMELINE",
            value=timeline,
            evidence_ref=record.evidence_ref,
            entity_id=order_id,
        )
    ]
    shipment_id = record.data.get("shipment_id")
    if isinstance(shipment_id, str) and shipment_id:
        facts.append(
            _fact(
                task=task,
                code="SHIPMENT_IDS",
                value=(shipment_id,),
                evidence_ref=record.evidence_ref,
                entity_id=order_id,
            )
        )
    if analysis.late_days is not None:
        facts.append(
            _fact(
                task=task,
                code="DELIVERY_LATE_DAYS",
                value=analysis.late_days,
                evidence_ref=record.evidence_ref,
                entity_id=order_id,
            )
        )
    if analysis.issue is not None:
        facts.append(
            _fact(
                task=task,
                code="SHIPMENT_ISSUE",
                value=analysis.issue.value,
                evidence_ref=record.evidence_ref,
                entity_id=order_id,
            )
        )

    return SpecialistReport(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        status=TaskStatus.COMPLETED if analysis.issue else TaskStatus.PARTIAL,
        facts=tuple(facts),
        evidence_refs=(record.evidence_ref,),
        warnings=record.warnings,
    )
