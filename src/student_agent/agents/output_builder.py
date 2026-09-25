from __future__ import annotations

from decimal import Decimal
from typing import Any

from student_agent.domain import (
    CaseStatus,
    ClaimAssessment,
    ClaimVerdict,
    DraftAssessment,
    EntityIndex,
    PartyType,
    PrimaryIssue,
    RankedCause,
    ResponsibleParty,
    SpecialistReport,
)

from .coordinator import NormalizedCase


def _tuple_fact(report: SpecialistReport, code: str) -> tuple[str, ...]:
    for fact in report.facts:
        if fact.fact_code == code and isinstance(fact.value, tuple):
            return tuple(value for value in fact.value if isinstance(value, str))
    return ()


def _fact_value(reports: tuple[SpecialistReport, ...], code: str) -> object | None:
    for report in reports:
        for fact in report.facts:
            if fact.fact_code == code:
                return fact.value
    return None


def _all_refs(reports: tuple[SpecialistReport, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for report in reports for ref in report.evidence_refs))


def _first_tuple_fact(
    reports: tuple[SpecialistReport, ...], code: str
) -> tuple[str, ...]:
    for report in reports:
        value = _tuple_fact(report, code)
        if value:
            return value
    return ()


def build_order_only_draft(
    case: NormalizedCase, report: SpecialistReport
) -> DraftAssessment:
    refs = report.evidence_refs
    entities = EntityIndex(
        order_ids=(case.claimed_order_id,),
        item_ids=_tuple_fact(report, "ORDER_ITEM_IDS"),
        seller_ids=_tuple_fact(report, "SELLER_IDS"),
    )
    claim_assessments = tuple(
        ClaimAssessment(
            claim_id=claim.claim_id,
            verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
            confidence=Decimal("0.35"),
            evidence_refs=refs,
        )
        for claim in case.claims
    )
    return DraftAssessment(
        case_id=case.case_id,
        primary_issue=PrimaryIssue.INSUFFICIENT_EVIDENCE,
        case_status=CaseStatus.NEEDS_INVESTIGATION,
        confidence=Decimal("0.35"),
        claim_assessments=claim_assessments,
        entities=entities,
        ranked_causes=(RankedCause("INSUFFICIENT_PAYMENT_POLICY_EVIDENCE", 1),),
        responsible_parties=(ResponsibleParty(PartyType.UNKNOWN, None),),
        evidence_refs=refs,
        conflicts=report.conflicts,
        recommended_refund_brl=Decimal("0"),
        refund_lines=(),
        resolution_actions=("collect_payment_and_policy_evidence",),
    )


def build_rules_draft(
    case: NormalizedCase, reports: tuple[SpecialistReport, ...]
) -> DraftAssessment:
    refs = _all_refs(reports)
    item_ids = _first_tuple_fact(reports, "ORDER_ITEM_IDS")
    seller_ids = _first_tuple_fact(reports, "SELLER_IDS")
    payment_references = _first_tuple_fact(reports, "PAYMENT_REFERENCES")
    issue_value = _fact_value(reports, "REFUND_ISSUE") or _fact_value(
        reports, "PAYMENT_ISSUE"
    )
    order_status = _fact_value(reports, "ORDER_STATUS")
    paid_total = _fact_value(reports, "PAYMENT_TOTAL_BRL")
    if issue_value is None and isinstance(order_status, str) and isinstance(paid_total, Decimal):
        normalized_status = order_status.lower()
        if paid_total > 0 and normalized_status in {"canceled", "cancelled"}:
            issue_value = PrimaryIssue.CANCELED_ORDER_PAID.value
        elif paid_total > 0 and normalized_status in {"unavailable", "unavailable_order"}:
            issue_value = PrimaryIssue.UNAVAILABLE_ORDER_PAID.value

    issue = (
        PrimaryIssue(issue_value)
        if isinstance(issue_value, str)
        else PrimaryIssue.INSUFFICIENT_EVIDENCE
    )
    sufficient = issue is not PrimaryIssue.INSUFFICIENT_EVIDENCE
    no_action = issue is PrimaryIssue.VALID_SPLIT_PAYMENT
    confidence = (
        Decimal("0.85")
        if no_action
        else Decimal("0.72") if sufficient else Decimal("0.35")
    )
    assessments = tuple(
        ClaimAssessment(
            claim_id=claim.claim_id,
            verdict=(
                ClaimVerdict.SUPPORTED
                if claim.topic == issue.value
                else ClaimVerdict.INSUFFICIENT_EVIDENCE
            ),
            confidence=confidence if claim.topic == issue.value else Decimal("0.35"),
            evidence_refs=refs,
        )
        for claim in case.claims
    )
    cause_code = issue.value.upper() if sufficient else "INSUFFICIENT_PAYMENT_POLICY_EVIDENCE"
    actions = () if no_action else ("collect_policy_evidence",)
    conflicts = tuple(conflict for report in reports for conflict in report.conflicts)
    return DraftAssessment(
        case_id=case.case_id,
        primary_issue=issue,
        case_status=CaseStatus.NO_ACTION if no_action else CaseStatus.NEEDS_INVESTIGATION,
        confidence=confidence,
        claim_assessments=assessments,
        entities=EntityIndex(
            order_ids=(case.claimed_order_id,),
            item_ids=item_ids,
            seller_ids=seller_ids,
            payment_references=payment_references,
        ),
        ranked_causes=(RankedCause(cause_code, 1),),
        responsible_parties=(ResponsibleParty(PartyType.UNKNOWN, None),),
        evidence_refs=refs,
        conflicts=conflicts,
        recommended_refund_brl=Decimal("0"),
        refund_lines=(),
        resolution_actions=actions,
    )


def draft_to_output(draft: DraftAssessment) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": draft.case_id,
        "assessment": {
            "primary_issue": draft.primary_issue.value,
            "case_status": draft.case_status.value,
            "confidence": float(draft.confidence),
        },
        "affected_entities": {
            "order_ids": list(draft.entities.order_ids),
            "item_ids": list(draft.entities.item_ids),
            "seller_ids": list(draft.entities.seller_ids),
            "payment_references": list(draft.entities.payment_references),
            "shipment_ids": list(draft.entities.shipment_ids),
        },
        "claim_assessments": [
            {
                "claim_id": claim.claim_id,
                "verdict": claim.verdict.value,
                "confidence": float(claim.confidence),
                "evidence_refs": list(claim.evidence_refs),
            }
            for claim in draft.claim_assessments
        ],
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": cause.cause_code, "rank": cause.rank}
                for cause in draft.ranked_causes
            ],
            "responsible_parties": [
                {"party_type": party.party_type.value, "party_id": party.party_id}
                for party in draft.responsible_parties
            ],
        },
        "evidence_refs": list(draft.evidence_refs),
        "data_conflicts": [
            {
                "field": conflict.field,
                "sources": list(conflict.sources),
                "selected_source": conflict.selected_source,
                "resolution_code": conflict.resolution_code,
            }
            for conflict in draft.conflicts
        ],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(draft.recommended_refund_brl),
            "refund_lines": [
                {
                    "reason_code": line.reason_code,
                    "amount_brl": float(line.amount_brl),
                    "entity_id": line.entity_id,
                }
                for line in draft.refund_lines
            ],
        },
        "resolution_actions": list(draft.resolution_actions),
    }
