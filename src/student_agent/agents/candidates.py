from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from student_agent.domain import CandidateSet, PrimaryIssue, SpecialistReport

from .coordinator import NormalizedCase


def _facts(reports: tuple[SpecialistReport, ...]) -> dict[str, list[tuple[str, object]]]:
    by_code: dict[str, list[tuple[str, object]]] = {}
    for report in reports:
        for fact in report.facts:
            by_code.setdefault(fact.fact_code, []).append((fact.fact_id, fact.value))
    return by_code


def generate_candidates(
    case: NormalizedCase, reports: tuple[SpecialistReport, ...]
) -> CandidateSet:
    by_code = _facts(reports)
    issue_support: dict[PrimaryIssue, list[str]] = {}

    for code in ("REFUND_ISSUE", "PAYMENT_ISSUE", "SHIPMENT_ISSUE"):
        for fact_id, value in by_code.get(code, []):
            try:
                issue = PrimaryIssue(value)
            except (TypeError, ValueError):
                continue
            issue_support.setdefault(issue, []).append(fact_id)

    status_entry = next(iter(by_code.get("ORDER_STATUS", [])), None)
    paid_entry = next(iter(by_code.get("PAYMENT_TOTAL_BRL", [])), None)
    if status_entry is not None and paid_entry is not None:
        status = status_entry[1]
        paid_total = paid_entry[1]
        if isinstance(status, str) and isinstance(paid_total, Decimal) and paid_total > 0:
            normalized_status = status.lower()
            if normalized_status in {"canceled", "cancelled"}:
                issue_support.setdefault(PrimaryIssue.CANCELED_ORDER_PAID, []).extend(
                    (status_entry[0], paid_entry[0])
                )
            elif normalized_status in {"unavailable", "unavailable_order"}:
                issue_support.setdefault(PrimaryIssue.UNAVAILABLE_ORDER_PAID, []).extend(
                    (status_entry[0], paid_entry[0])
                )

    claimed_topics = tuple(claim.topic for claim in case.claims)
    counter_ids: list[str] = []
    if status_entry is not None and isinstance(status_entry[1], str):
        status = status_entry[1].lower()
        if "canceled_order_paid" in claimed_topics and status not in {"canceled", "cancelled"}:
            counter_ids.append(status_entry[0])
        if "unavailable_order_paid" in claimed_topics and status not in {
            "unavailable",
            "unavailable_order",
        }:
            counter_ids.append(status_entry[0])
    if paid_entry is not None and isinstance(paid_entry[1], Decimal):
        payment_claims = {
            "canceled_order_paid",
            "unavailable_order_paid",
            "duplicate_charge",
            "payment_mismatch",
            "valid_split_payment",
        }
        if payment_claims.intersection(claimed_topics) and paid_entry[1] <= 0:
            counter_ids.append(paid_entry[0])
    count_entry = next(iter(by_code.get("PAYMENT_COUNT", [])), None)
    if (
        "valid_split_payment" in claimed_topics
        and count_entry is not None
        and isinstance(count_entry[1], int)
        and count_entry[1] <= 1
    ):
        counter_ids.append(count_entry[0])
    refund_entry = next(iter(by_code.get("REFUND_STATUS", [])), None)
    if refund_entry is not None and isinstance(refund_entry[1], str):
        state = refund_entry[1].lower()
        if "refund_pending" in claimed_topics and state not in {
            "initiated",
            "pending",
            "processing",
            "requested",
        }:
            counter_ids.append(refund_entry[0])
        if "refund_failed" in claimed_topics and state not in {"canceled", "failed", "rejected"}:
            counter_ids.append(refund_entry[0])
    timeline_entry = next(iter(by_code.get("SHIPMENT_TIMELINE", [])), None)
    if timeline_entry is not None and isinstance(timeline_entry[1], Mapping):
        delivered = timeline_entry[1].get("delivered_at")
        estimated = timeline_entry[1].get("estimated_delivery_at")
        if (
            {"late_delivery_seller", "late_delivery_logistics"}.intersection(claimed_topics)
            and isinstance(delivered, datetime)
            and isinstance(estimated, datetime)
            and delivered <= estimated
        ):
            counter_ids.append(timeline_entry[0])
    ordered = sorted(
        issue_support,
        key=lambda issue: (
            claimed_topics.index(issue.value)
            if issue.value in claimed_topics
            else len(claimed_topics),
            issue.value,
        ),
    )
    selected = tuple(ordered[:3]) or (PrimaryIssue.INSUFFICIENT_EVIDENCE,)
    supporting_ids = tuple(
        dict.fromkeys(fact_id for issue in selected for fact_id in issue_support.get(issue, ()))
    )
    return CandidateSet(
        case_id=case.case_id,
        issues=selected,
        supporting_fact_ids=supporting_ids,
        counter_fact_ids=tuple(dict.fromkeys(counter_ids)),
    )
