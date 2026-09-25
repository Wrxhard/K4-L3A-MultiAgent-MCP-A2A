from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from student_agent.domain import (
    CaseStatus,
    Claim,
    ClaimAssessment,
    ClaimVerdict,
    CriticReport,
    CriticVerdict,
    DraftAssessment,
    EntityIndex,
    EvidenceDomain,
    EvidenceRecord,
    EvidenceSource,
    Fact,
    PartyType,
    PrimaryIssue,
    RankedCause,
    ResponsibleParty,
    SpecialistReport,
    TaskStatus,
    VerificationReport,
)


def evidence_record(*, case_id: str = "CASE_001", consumed: bool = True) -> EvidenceRecord:
    return EvidenceRecord(
        case_id=case_id,
        evidence_ref="ev_abcdefghijklmnopqrstuvwxyz",
        result_hash="sha256:" + "a" * 64,
        domain=EvidenceDomain.ORDER,
        tool_name="get_order",
        data={"order_status": "canceled"},
        consumed_by=("order-agent",) if consumed else (),
    )


def test_claim_rejects_empty_identifier() -> None:
    with pytest.raises(ValueError, match="claim_id"):
        Claim(claim_id="", topic="duplicate_charge")


def test_mcp_fact_requires_evidence() -> None:
    with pytest.raises(ValueError, match="MCP fact"):
        Fact(
            fact_id="fact-1",
            fact_code="ORDER_STATUS",
            value="canceled",
            source=EvidenceSource.MCP,
        )


def test_case_input_fact_cannot_claim_mcp_evidence() -> None:
    with pytest.raises(ValueError, match="case-input"):
        Fact(
            fact_id="fact-1",
            fact_code="CLAIMED_ORDER_ID",
            value="order-1",
            source=EvidenceSource.CASE_INPUT,
            evidence_refs=("ev_abcdefghijklmnopqrstuvwxyz",),
        )


def test_fact_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Fact(
            fact_id="fact-1",
            fact_code="ORDER_OPENED_AT",
            value="2018-01-01",
            source=EvidenceSource.CASE_INPUT,
            observed_at=datetime(2018, 1, 1),
        )


def test_specialist_report_requires_fact_refs_at_report_level() -> None:
    fact = Fact(
        fact_id="fact-1",
        fact_code="ORDER_STATUS",
        value="canceled",
        source=EvidenceSource.MCP,
        evidence_refs=("ev_abcdefghijklmnopqrstuvwxyz",),
    )
    with pytest.raises(ValueError, match="report evidence_refs"):
        SpecialistReport(
            case_id="CASE_001",
            task_id="task-1",
            actor="order-agent",
            status=TaskStatus.COMPLETED,
            facts=(fact,),
        )


def test_draft_rejects_negative_money() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        DraftAssessment(
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
            recommended_refund_brl=Decimal("-1"),
            refund_lines=(),
            resolution_actions=("issue_refund",),
        )


def test_critic_report_enforces_verdict_consistency() -> None:
    with pytest.raises(ValueError, match="passing critic"):
        CriticReport(
            case_id="CASE_001",
            verdict=CriticVerdict.PASS,
            error_codes=("ISSUE_NOT_SUPPORTED",),
        )


def test_verification_report_enforces_failure_errors() -> None:
    with pytest.raises(ValueError, match="failed verification"):
        VerificationReport(case_id="CASE_001", passed=False)


def test_valid_evidence_and_timezone_aware_fact() -> None:
    record = evidence_record()
    fact = Fact(
        fact_id="fact-1",
        fact_code="ORDER_STATUS",
        value="canceled",
        source=EvidenceSource.MCP,
        evidence_refs=(record.evidence_ref,),
        observed_at=datetime(2018, 1, 1, tzinfo=UTC),
    )
    assert fact.evidence_refs == (record.evidence_ref,)


def test_domain_records_are_frozen() -> None:
    claim = Claim(claim_id="claim-1", topic="duplicate_charge")
    with pytest.raises(FrozenInstanceError):
        claim.topic = "payment_mismatch"  # type: ignore[misc]


def test_evidence_record_validates_public_reference_format() -> None:
    with pytest.raises(ValueError, match="evidence_ref"):
        EvidenceRecord(
            case_id="CASE_001",
            evidence_ref="invented",
            result_hash="sha256:" + "a" * 64,
            domain=EvidenceDomain.ORDER,
            tool_name="get_order",
            data={},
        )


def test_specialist_report_requires_terminal_status() -> None:
    with pytest.raises(ValueError, match="terminal"):
        SpecialistReport(
            case_id="CASE_001",
            task_id="task-1",
            actor="order-agent",
            status=TaskStatus.RUNNING,
        )
