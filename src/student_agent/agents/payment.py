from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from student_agent.domain import (
    AgentTask,
    EvidenceSource,
    Fact,
    InvariantError,
    PrimaryIssue,
    SpecialistReport,
    TaskStatus,
    parse_decimal,
)
from student_agent.evidence import (
    PAYMENT_AGENT,
    EvidenceAdapterError,
    EvidenceRegistry,
    EvidenceToolError,
    ToolCatalog,
    adapt_evidence,
)
from student_agent.orchestration import CaseTrace

from .order_item import Gateway

REFUND_TOPICS = frozenset({"refund_pending", "refund_failed"})
PENDING_REFUND_STATES = frozenset({"initiated", "pending", "processing", "requested"})
FAILED_REFUND_STATES = frozenset({"canceled", "failed", "rejected"})


@dataclass(frozen=True, slots=True)
class PaymentAnalysis:
    expected_total_brl: Decimal | None
    paid_total_brl: Decimal
    payment_count: int
    payment_references: tuple[str, ...]
    issue: PrimaryIssue | None


def _payment_value(row: Mapping[str, Any], index: int) -> Decimal:
    if "payment_value" not in row:
        raise EvidenceAdapterError(f"payments[{index}].payment_value is required")
    try:
        value = parse_decimal(
            row["payment_value"], field_name=f"payments[{index}].payment_value"
        )
    except InvariantError as exc:
        raise EvidenceAdapterError(str(exc)) from exc
    if value < 0:
        raise EvidenceAdapterError(f"payments[{index}].payment_value cannot be negative")
    return value


def _payment_reference(row: Mapping[str, Any]) -> str | None:
    for key in ("payment_reference", "payment_id", "transaction_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _fingerprint(row: Mapping[str, Any], value: Decimal) -> tuple[str, Decimal, int]:
    payment_type = row.get("payment_type")
    installments = row.get("payment_installments", 1)
    return (
        payment_type if isinstance(payment_type, str) else "unknown",
        value,
        installments if isinstance(installments, int) else 1,
    )


def analyze_payments(
    payments: list[Any], *, expected_total_brl: Decimal | None
) -> PaymentAnalysis:
    values: list[Decimal] = []
    references: list[str] = []
    fingerprints: list[tuple[str, Decimal, int]] = []
    for index, raw_row in enumerate(payments):
        if not isinstance(raw_row, Mapping):
            raise EvidenceAdapterError(f"payments[{index}] must be an object")
        value = _payment_value(raw_row, index)
        values.append(value)
        fingerprints.append(_fingerprint(raw_row, value))
        reference = _payment_reference(raw_row)
        if reference is not None and reference not in references:
            references.append(reference)

    paid_total = sum(values, Decimal("0"))
    issue: PrimaryIssue | None = None
    if expected_total_brl is not None and payments:
        if paid_total == expected_total_brl and len(payments) > 1:
            issue = PrimaryIssue.VALID_SPLIT_PAYMENT
        elif paid_total != expected_total_brl:
            repeated = any(count > 1 for count in Counter(fingerprints).values())
            issue = (
                PrimaryIssue.DUPLICATE_CHARGE
                if paid_total > expected_total_brl and repeated
                else PrimaryIssue.PAYMENT_MISMATCH
            )
    return PaymentAnalysis(
        expected_total_brl=expected_total_brl,
        paid_total_brl=paid_total,
        payment_count=len(payments),
        payment_references=tuple(references),
        issue=issue,
    )


def _fact(
    *, task: AgentTask, code: str, value: object, evidence_ref: str, order_id: str
) -> Fact:
    return Fact(
        fact_id=f"{task.task_id}-{code.lower()}",
        fact_code=code,
        value=value,
        source=EvidenceSource.MCP,
        entity_id=order_id,
        evidence_refs=(evidence_ref,),
    )


def _latest_refund_state(data: Mapping[str, Any]) -> str | None:
    events = data.get("events")
    if not isinstance(events, list):
        raise EvidenceAdapterError("refund timeline events must be an array")
    states: list[str] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        value = event.get("refund_status", event.get("status"))
        if isinstance(value, str) and value:
            states.append(value.strip().lower())
    return states[-1] if states else None


async def _collect(
    *,
    task: AgentTask,
    tool_name: str,
    order_id: str,
    gateway: Gateway,
    catalog: ToolCatalog,
    registry: EvidenceRegistry,
    trace: CaseTrace,
) -> tuple[Any, str, tuple[str, ...]]:
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
    return record.data, record.evidence_ref, record.warnings


async def investigate_payments(
    task: AgentTask,
    *,
    order_id: str,
    claim_topics: tuple[str, ...],
    expected_total_brl: Decimal | None,
    gateway: Gateway,
    catalog: ToolCatalog,
    registry: EvidenceRegistry,
    trace: CaseTrace,
) -> SpecialistReport:
    if task.actor != PAYMENT_AGENT:
        raise ValueError("payment specialist received a task owned by another actor")
    payment_warnings: tuple[str, ...] = ()
    try:
        data, payment_ref, payment_warnings = await _collect(
            task=task,
            tool_name="get_payment_timeline",
            order_id=order_id,
            gateway=gateway,
            catalog=catalog,
            registry=registry,
            trace=trace,
        )
    except EvidenceToolError as exc:
        if not exc.not_found:
            raise
        data = None

    payments = data.get("payments") if isinstance(data, Mapping) else None
    if not isinstance(payments, list):
        fallback_data, payment_ref, fallback_warnings = await _collect(
            task=task,
            tool_name="get_order_payments",
            order_id=order_id,
            gateway=gateway,
            catalog=catalog,
            registry=registry,
            trace=trace,
        )
        if not isinstance(fallback_data, list):
            raise EvidenceAdapterError("base payment data must be an array")
        payments = fallback_data
        payment_warnings = (*payment_warnings, *fallback_warnings)

    analysis = analyze_payments(payments, expected_total_brl=expected_total_brl)
    facts = [
        _fact(
            task=task,
            code="PAYMENT_TOTAL_BRL",
            value=analysis.paid_total_brl,
            evidence_ref=payment_ref,
            order_id=order_id,
        ),
        _fact(
            task=task,
            code="PAYMENT_COUNT",
            value=analysis.payment_count,
            evidence_ref=payment_ref,
            order_id=order_id,
        ),
    ]
    if analysis.payment_references:
        facts.append(
            _fact(
                task=task,
                code="PAYMENT_REFERENCES",
                value=analysis.payment_references,
                evidence_ref=payment_ref,
                order_id=order_id,
            )
        )
    if analysis.issue is not None:
        facts.append(
            _fact(
                task=task,
                code="PAYMENT_ISSUE",
                value=analysis.issue.value,
                evidence_ref=payment_ref,
                order_id=order_id,
            )
        )

    evidence_refs = [payment_ref]
    warnings = list(payment_warnings)
    if REFUND_TOPICS.intersection(claim_topics):
        try:
            refund_data, refund_ref, refund_warnings = await _collect(
                task=task,
                tool_name="get_refund_timeline",
                order_id=order_id,
                gateway=gateway,
                catalog=catalog,
                registry=registry,
                trace=trace,
            )
        except EvidenceToolError as exc:
            if not exc.not_found:
                raise
            warnings.append("refund timeline not found; refund state remains unknown")
            refund_data = None
        if refund_data is None:
            return SpecialistReport(
                case_id=task.case_id,
                task_id=task.task_id,
                actor=task.actor,
                status=TaskStatus.PARTIAL,
                facts=tuple(facts),
                evidence_refs=tuple(evidence_refs),
                warnings=tuple(dict.fromkeys(warnings)),
            )
        if not isinstance(refund_data, Mapping):
            raise EvidenceAdapterError("refund timeline data must be an object")
        evidence_refs.append(refund_ref)
        warnings.extend(refund_warnings)
        refund_state = _latest_refund_state(refund_data)
        if refund_state is not None:
            facts.append(
                _fact(
                    task=task,
                    code="REFUND_STATUS",
                    value=refund_state,
                    evidence_ref=refund_ref,
                    order_id=order_id,
                )
            )
            refund_issue = None
            if refund_state in PENDING_REFUND_STATES:
                refund_issue = PrimaryIssue.REFUND_PENDING
            elif refund_state in FAILED_REFUND_STATES:
                refund_issue = PrimaryIssue.REFUND_FAILED
            if refund_issue is not None:
                facts.append(
                    _fact(
                        task=task,
                        code="REFUND_ISSUE",
                        value=refund_issue.value,
                        evidence_ref=refund_ref,
                        order_id=order_id,
                    )
                )

    return SpecialistReport(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        status=TaskStatus.COMPLETED,
        facts=tuple(facts),
        evidence_refs=tuple(evidence_refs),
        warnings=tuple(dict.fromkeys(warnings)),
    )
