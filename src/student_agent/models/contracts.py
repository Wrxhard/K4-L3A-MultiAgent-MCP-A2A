from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from student_agent.agents.coordinator import NormalizedCase
from student_agent.domain import CandidateSet, SpecialistReport

SAFE_FACT_CODES = frozenset(
    {
        "ORDER_STATUS",
        "PAYMENT_COUNT",
        "PAYMENT_ISSUE",
        "REFUND_STATUS",
        "REFUND_ISSUE",
        "DELIVERY_LATE_DAYS",
        "SHIPMENT_ISSUE",
        "POLICY_RULE_FOUND",
        "POLICY_REFUND_ELIGIBLE",
        "POLICY_ACTIONS",
        "POLICY_RESPONSIBLE_PARTY",
    }
)


@dataclass(frozen=True, slots=True)
class AdjudicationContext:
    payload: dict[str, Any]
    alias_to_evidence_ref: dict[str, str]
    allowed_fact_codes: frozenset[str]
    claim_ids: tuple[str, ...]
    fact_id_to_code: dict[str, str]


def _safe_value(value: object) -> str | int | bool | list[str] | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("fact value is not safe for semantic model context")


def build_adjudication_context(
    case: NormalizedCase,
    candidates: CandidateSet,
    reports: tuple[SpecialistReport, ...],
) -> AdjudicationContext:
    fact_by_id = {fact.fact_id: fact for report in reports for fact in report.facts}
    selected_ids = tuple(
        dict.fromkeys(
            (
                *candidates.supporting_fact_ids,
                *candidates.counter_fact_ids,
                *(
                    fact.fact_id
                    for report in reports
                    for fact in report.facts
                    if fact.fact_code.startswith("POLICY_")
                ),
            )
        )
    )
    model_facts: list[tuple[Any, str, object]] = []
    for fact_id in selected_ids:
        fact = fact_by_id.get(fact_id)
        if fact is None:
            continue
        if fact.fact_code in SAFE_FACT_CODES:
            model_facts.append((fact, fact.fact_code, fact.value))
        elif fact.fact_code == "PAYMENT_TOTAL_BRL" and isinstance(fact.value, Decimal):
            model_facts.append((fact, "PAYMENT_PRESENT", fact.value > 0))
    evidence_refs = tuple(
        dict.fromkeys(ref for fact, _, _ in model_facts for ref in fact.evidence_refs)
    )
    alias_to_ref = {f"E{index}": ref for index, ref in enumerate(evidence_refs, 1)}
    ref_to_alias = {ref: alias for alias, ref in alias_to_ref.items()}
    facts = [
        {
            "fact_code": model_code,
            "value": _safe_value(model_value),
            "evidence_aliases": [ref_to_alias[ref] for ref in fact.evidence_refs],
            "role": "counter" if fact.fact_id in candidates.counter_fact_ids else "supporting",
        }
        for fact, model_code, model_value in model_facts
    ]
    payload = {
        "candidate_issues": [issue.value for issue in candidates.issues],
        "claims": [
            {"claim_id": claim.claim_id, "topic": claim.topic} for claim in case.claims
        ],
        "facts": facts,
        "allowed_evidence_aliases": list(alias_to_ref),
        "output_contract": {
            "selected_issue": {"allowed": [issue.value for issue in candidates.issues]},
            "claim_decisions": {
                "required_order": [claim.claim_id for claim in case.claims],
                "item_fields": [
                    "claim_id",
                    "verdict",
                    "supporting_fact_codes",
                    "supporting_evidence_aliases",
                ],
                "allowed_verdicts": [
                    "supported",
                    "unsupported",
                    "partially_supported",
                    "insufficient_evidence",
                ],
            },
            "supporting_fact_codes": {"allowed": sorted({fact["fact_code"] for fact in facts})},
            "supporting_evidence_aliases": {"allowed": list(alias_to_ref)},
            "confidence_band": {"allowed": ["high", "medium", "low", "insufficient"]},
        },
    }
    return AdjudicationContext(
        payload=payload,
        alias_to_evidence_ref=alias_to_ref,
        allowed_fact_codes=frozenset(model_code for _, model_code, _ in model_facts),
        claim_ids=tuple(claim.claim_id for claim in case.claims),
        fact_id_to_code={fact.fact_id: model_code for fact, model_code, _ in model_facts},
    )
