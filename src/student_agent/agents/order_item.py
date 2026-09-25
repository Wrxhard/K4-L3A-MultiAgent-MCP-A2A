from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

from student_agent.domain import (
    AgentTask,
    EvidenceSource,
    Fact,
    InvariantError,
    SpecialistReport,
    TaskStatus,
    parse_decimal,
)
from student_agent.evidence import (
    ORDER_ITEM_AGENT,
    EvidenceAdapterError,
    EvidenceRegistry,
    ToolCatalog,
    adapt_evidence,
)
from student_agent.orchestration import CaseTrace


class Gateway(Protocol):
    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]: ...


def _unique_text(rows: list[Any], key: str) -> tuple[str, ...]:
    values: list[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            value = row.get(key)
            if isinstance(value, str) and value and value not in values:
                values.append(value)
    return tuple(values)


def _expected_total(rows: list[Any]) -> Decimal | None:
    total = Decimal("0")
    found = False
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        for key in ("price", "freight_value"):
            value = row.get(key)
            if value is not None:
                try:
                    total += parse_decimal(value, field_name=f"items[{index}].{key}")
                except InvariantError as exc:
                    raise EvidenceAdapterError(str(exc)) from exc
                found = True
    return total if found else None


def _fact(
    *, task: AgentTask, code: str, value: object, evidence_ref: str, entity_id: str | None
) -> Fact:
    return Fact(
        fact_id=f"{task.task_id}-{code.lower()}",
        fact_code=code,
        value=value,
        source=EvidenceSource.MCP,
        entity_id=entity_id,
        evidence_refs=(evidence_ref,),
    )


async def investigate_order_items(
    task: AgentTask,
    *,
    order_id: str,
    gateway: Gateway,
    catalog: ToolCatalog,
    registry: EvidenceRegistry,
    trace: CaseTrace,
) -> SpecialistReport:
    if task.actor != ORDER_ITEM_AGENT:
        raise ValueError("order/item specialist received a task owned by another actor")
    facts: list[Fact] = []
    evidence_refs: list[str] = []
    warnings: list[str] = []

    for tool_name in ("get_order", "get_order_items"):
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
        registry.register(record)
        trace.tool_result_consumed(
            actor=task.actor,
            tool_name=tool_name,
            evidence_ref=record.evidence_ref,
        )
        evidence_refs.append(record.evidence_ref)
        warnings.extend(record.warnings)

        if tool_name == "get_order":
            if not isinstance(record.data, Mapping):
                raise EvidenceAdapterError("get_order data must be an object")
            status = record.data.get("order_status")
            if isinstance(status, str) and status:
                facts.append(
                    _fact(
                        task=task,
                        code="ORDER_STATUS",
                        value=status,
                        evidence_ref=record.evidence_ref,
                        entity_id=order_id,
                    )
                )
        else:
            if not isinstance(record.data, list):
                raise EvidenceAdapterError("get_order_items data must be an array")
            item_ids = _unique_text(record.data, "order_item_id")
            seller_ids = _unique_text(record.data, "seller_id")
            expected_total = _expected_total(record.data)
            if item_ids:
                facts.append(
                    _fact(
                        task=task,
                        code="ORDER_ITEM_IDS",
                        value=item_ids,
                        evidence_ref=record.evidence_ref,
                        entity_id=order_id,
                    )
                )
            if seller_ids:
                facts.append(
                    _fact(
                        task=task,
                        code="SELLER_IDS",
                        value=seller_ids,
                        evidence_ref=record.evidence_ref,
                        entity_id=order_id,
                    )
                )
            if expected_total is not None:
                facts.append(
                    _fact(
                        task=task,
                        code="ORDER_EXPECTED_TOTAL_BRL",
                        value=expected_total,
                        evidence_ref=record.evidence_ref,
                        entity_id=order_id,
                    )
                )

    status = TaskStatus.COMPLETED if facts else TaskStatus.PARTIAL
    return SpecialistReport(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        status=status,
        facts=tuple(facts),
        evidence_refs=tuple(evidence_refs),
        warnings=tuple(dict.fromkeys(warnings)),
    )
