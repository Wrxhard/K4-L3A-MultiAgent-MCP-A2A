from decimal import Decimal

import pytest

from student_agent.domain import (
    CaseStatus,
    ClaimAssessment,
    ClaimVerdict,
    DraftAssessment,
    EntityIndex,
    EvidenceDomain,
    EvidenceRecord,
    InvariantError,
    PartyType,
    PrimaryIssue,
    RankedCause,
    RefundLine,
    ResponsibleParty,
    WorkflowState,
    parse_decimal,
    require_case_scope,
    require_consumed_evidence_refs,
    require_known_evidence_refs,
    require_non_negative,
    require_refund_total,
    require_state_transition,
    require_subset,
)


def record(
    evidence_ref: str = "ev_abcdefghijklmnopqrstuvwxyz",
    *,
    case_id: str = "CASE_001",
    consumed: bool = True,
) -> EvidenceRecord:
    return EvidenceRecord(
        case_id=case_id,
        evidence_ref=evidence_ref,
        result_hash="sha256:" + "a" * 64,
        domain=EvidenceDomain.PAYMENT,
        tool_name="get_payment_timeline",
        data={},
        consumed_by=("payment-agent",) if consumed else (),
    )


def draft(*, refund: Decimal, line_amounts: tuple[Decimal, ...]) -> DraftAssessment:
    return DraftAssessment(
        case_id="CASE_001",
        primary_issue=PrimaryIssue.CANCELED_ORDER_PAID,
        case_status=CaseStatus.ACTION_REQUIRED,
        confidence=Decimal("0.9"),
        claim_assessments=(
            ClaimAssessment(
                claim_id="claim-1",
                verdict=ClaimVerdict.SUPPORTED,
                confidence=Decimal("0.9"),
                evidence_refs=("ev_abcdefghijklmnopqrstuvwxyz",),
            ),
        ),
        entities=EntityIndex(order_ids=("order-1",)),
        ranked_causes=(RankedCause("ORDER_CANCELED", 1),),
        responsible_parties=(ResponsibleParty(PartyType.PLATFORM, None),),
        evidence_refs=("ev_abcdefghijklmnopqrstuvwxyz",),
        conflicts=(),
        recommended_refund_brl=refund,
        refund_lines=tuple(
            RefundLine("refund_paid_order", amount, "order-1") for amount in line_amounts
        ),
        resolution_actions=("issue_refund",),
    )


def test_parse_decimal_uses_string_conversion_for_float() -> None:
    assert parse_decimal(0.1, field_name="amount") == Decimal("0.1")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True])
def test_parse_decimal_rejects_non_finite_or_boolean(value: object) -> None:
    with pytest.raises(InvariantError, match="decimal|finite"):
        parse_decimal(value, field_name="amount")  # type: ignore[arg-type]


def test_non_negative_invariant() -> None:
    with pytest.raises(InvariantError, match="negative"):
        require_non_negative(Decimal("-0.01"), field_name="amount")


def test_case_scope_rejects_cross_case_record() -> None:
    with pytest.raises(InvariantError, match="mismatched"):
        require_case_scope("CASE_001", [record(case_id="CASE_002")])


def test_known_evidence_rejects_unknown_ref() -> None:
    with pytest.raises(InvariantError, match="unknown evidence"):
        require_known_evidence_refs(["ev_unknown_reference_12345"], [record()])


def test_consumed_evidence_rejects_unconsumed_ref() -> None:
    evidence = record(consumed=False)
    with pytest.raises(InvariantError, match="unconsumed"):
        require_consumed_evidence_refs([evidence.evidence_ref], [evidence])


def test_subset_rejects_values_outside_parent() -> None:
    with pytest.raises(InvariantError, match="outside"):
        require_subset(["E1", "E2"], ["E1"], label="claim evidence")


def test_refund_total_accepts_exact_decimal_sum() -> None:
    require_refund_total(
        draft(refund=Decimal("97.00"), line_amounts=(Decimal("79.00"), Decimal("18.00")))
    )


def test_refund_total_rejects_mismatch() -> None:
    with pytest.raises(InvariantError, match="does not equal"):
        require_refund_total(
            draft(refund=Decimal("96.00"), line_amounts=(Decimal("79.00"), Decimal("18.00")))
        )


def test_state_transition_accepts_declared_edge() -> None:
    require_state_transition(WorkflowState.RECEIVED, WorkflowState.PLANNING)


def test_state_transition_rejects_skip() -> None:
    with pytest.raises(InvariantError, match="invalid workflow transition"):
        require_state_transition(WorkflowState.RECEIVED, WorkflowState.FINALIZED)
