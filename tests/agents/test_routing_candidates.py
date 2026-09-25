from datetime import UTC, datetime
from decimal import Decimal

from student_agent.agents import ROUTING_MATRIX, build_route_plan, generate_candidates
from student_agent.agents.coordinator import NormalizedCase
from student_agent.domain import (
    Claim,
    EntityIndex,
    EvidenceSource,
    Fact,
    PrimaryIssue,
    SpecialistReport,
    TaskStatus,
)


def normalized(*topics: str) -> NormalizedCase:
    return NormalizedCase(
        case_id="CASE_001",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        policy_version="EC_POLICY_V1",
        language="pt-BR",
        claimed_order_id="order-1",
        claims=tuple(Claim(f"claim-{index}", topic) for index, topic in enumerate(topics)),
        entities=EntityIndex(order_ids=("order-1",)),
    )


def report(*facts: Fact) -> SpecialistReport:
    return SpecialistReport(
        case_id="CASE_001",
        task_id="task-1",
        actor="test-agent",
        status=TaskStatus.COMPLETED,
        facts=facts,
        evidence_refs=tuple(
            dict.fromkeys(ref for fact in facts for ref in fact.evidence_refs)
        ),
    )


def fact(fact_id: str, code: str, value: object, ref_suffix: str) -> Fact:
    return Fact(
        fact_id=fact_id,
        fact_code=code,
        value=value,
        source=EvidenceSource.MCP,
        evidence_refs=(f"ev_{ref_suffix}_abcdefghijklmnopqrst",),
    )


def test_late_delivery_route_does_not_call_payment_without_refund_claim() -> None:
    plan = build_route_plan(normalized("late_delivery_logistics").claims)
    assert plan.actors == ("order-item-agent", "shipment-agent", "policy-agent")


def test_routing_matrix_covers_every_known_claim_topic() -> None:
    assert set(ROUTING_MATRIX) == {
        *(issue.value for issue in PrimaryIssue),
        "requested_full_refund",
    }


def test_route_union_adds_payment_for_independent_refund_claim() -> None:
    plan = build_route_plan(
        normalized("late_delivery_logistics", "requested_full_refund").claims
    )
    assert plan.actors == (
        "order-item-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
    )


def test_unknown_topic_is_recorded_and_routes_to_safe_order_baseline() -> None:
    plan = build_route_plan(normalized("future_topic").claims)
    assert plan.actors == ("order-item-agent",)
    assert plan.unknown_topics == ("future_topic",)


def test_claim_alone_cannot_create_candidate() -> None:
    candidates = generate_candidates(normalized("duplicate_charge"), ())
    assert candidates.issues == (PrimaryIssue.INSUFFICIENT_EVIDENCE,)
    assert candidates.supporting_fact_ids == ()


def test_composite_canceled_paid_candidate_requires_both_verified_facts() -> None:
    evidence = report(
        fact("fact-status", "ORDER_STATUS", "canceled", "order"),
        fact("fact-paid", "PAYMENT_TOTAL_BRL", Decimal("110.00"), "payment"),
    )
    candidates = generate_candidates(normalized("canceled_order_paid"), (evidence,))
    assert candidates.issues[0] is PrimaryIssue.CANCELED_ORDER_PAID
    assert candidates.supporting_fact_ids == ("fact-status", "fact-paid")


def test_candidate_order_prefers_claimed_topic_only_among_verified_issues() -> None:
    evidence = report(
        fact("fact-payment", "PAYMENT_ISSUE", "payment_mismatch", "payment"),
        fact("fact-shipment", "SHIPMENT_ISSUE", "late_delivery_logistics", "shipment"),
    )
    candidates = generate_candidates(normalized("late_delivery_logistics"), (evidence,))
    assert candidates.issues == (
        PrimaryIssue.LATE_DELIVERY_LOGISTICS,
        PrimaryIssue.PAYMENT_MISMATCH,
    )


def test_candidate_set_is_bounded_to_three_issues() -> None:
    evidence = report(
        fact("fact-refund", "REFUND_ISSUE", "refund_failed", "refund"),
        fact("fact-payment", "PAYMENT_ISSUE", "payment_mismatch", "payment"),
        fact("fact-shipment", "SHIPMENT_ISSUE", "late_delivery_logistics", "shipment"),
        fact("fact-status", "ORDER_STATUS", "canceled", "order"),
        fact("fact-paid", "PAYMENT_TOTAL_BRL", Decimal("110.00"), "paid"),
    )
    candidates = generate_candidates(normalized("refund_failed"), (evidence,))
    assert len(candidates.issues) == 3
    assert candidates.issues[0] is PrimaryIssue.REFUND_FAILED


def test_directly_contradicting_facts_are_preserved_for_adjudication() -> None:
    evidence = report(
        fact("fact-status", "ORDER_STATUS", "delivered", "order"),
        fact("fact-paid", "PAYMENT_TOTAL_BRL", Decimal("0"), "payment"),
    )
    candidates = generate_candidates(normalized("canceled_order_paid"), (evidence,))
    assert candidates.issues == (PrimaryIssue.INSUFFICIENT_EVIDENCE,)
    assert candidates.counter_fact_ids == ("fact-status", "fact-paid")
