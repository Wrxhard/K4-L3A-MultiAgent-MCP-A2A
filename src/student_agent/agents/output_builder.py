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
