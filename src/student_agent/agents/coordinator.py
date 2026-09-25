from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from student_agent.domain import AgentTask, Claim, EntityIndex


class CaseInputError(ValueError):
    """Raised when a case cannot be normalized without guessing identifiers."""


@dataclass(frozen=True, slots=True)
class NormalizedCase:
    case_id: str
    opened_at: datetime
    policy_version: str
    language: str
    claimed_order_id: str
    claims: tuple[Claim, ...]
    entities: EntityIndex


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaseInputError(f"{field} must be a non-empty string")
    return value


def normalize_case(case: Mapping[str, Any]) -> NormalizedCase:
    case_id = _text(case.get("case_id"), "case_id")
    policy_version = _text(case.get("policy_version"), "policy_version")
    raw_opened_at = _text(case.get("opened_at"), "opened_at")
    try:
        opened_at = datetime.fromisoformat(raw_opened_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaseInputError("opened_at must be an ISO-8601 datetime") from exc
    if opened_at.tzinfo is None:
        raise CaseInputError("opened_at must be timezone-aware")

    request = case.get("customer_request")
    if not isinstance(request, Mapping):
        raise CaseInputError("customer_request must be an object")
    language = _text(request.get("language"), "customer_request.language")
    order_id = _text(request.get("claimed_order_id"), "customer_request.claimed_order_id")
    raw_claims = request.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise CaseInputError("customer_request.claims must be a non-empty array")

    claims: list[Claim] = []
    for index, raw_claim in enumerate(raw_claims):
        if not isinstance(raw_claim, Mapping):
            raise CaseInputError(f"customer_request.claims[{index}] must be an object")
        claims.append(
            Claim(
                claim_id=_text(raw_claim.get("claim_id"), f"claims[{index}].claim_id"),
                topic=_text(raw_claim.get("topic"), f"claims[{index}].topic"),
            )
        )
    if len({claim.claim_id for claim in claims}) != len(claims):
        raise CaseInputError("claim_id values must be unique")

    return NormalizedCase(
        case_id=case_id,
        opened_at=opened_at,
        policy_version=policy_version,
        language=language,
        claimed_order_id=order_id,
        claims=tuple(claims),
        entities=EntityIndex(order_ids=(order_id,)),
    )


def make_order_item_task(case: NormalizedCase) -> AgentTask:
    return AgentTask(
        task_id=f"{case.case_id}-order-item-1",
        case_id=case.case_id,
        actor="order-item-agent",
        objective="Verify the claimed order, items, sellers, and expected order value",
        entity_ids=(case.claimed_order_id,),
        required_fact_codes=("ORDER_STATUS", "ORDER_ITEM_IDS", "SELLER_IDS"),
    )


def make_payment_task(case: NormalizedCase) -> AgentTask:
    return AgentTask(
        task_id=f"{case.case_id}-payment-1",
        case_id=case.case_id,
        actor="payment-agent",
        objective="Verify payment totals, transaction structure, and refund lifecycle",
        entity_ids=(case.claimed_order_id,),
        required_fact_codes=("PAYMENT_TOTAL_BRL", "PAYMENT_COUNT", "PAYMENT_REFERENCES"),
    )


def make_shipment_task(case: NormalizedCase) -> AgentTask:
    return AgentTask(
        task_id=f"{case.case_id}-shipment-1",
        case_id=case.case_id,
        actor="shipment-agent",
        objective="Verify delivery timing and locate seller-versus-carrier delay",
        entity_ids=(case.claimed_order_id,),
        required_fact_codes=("SHIPMENT_TIMELINE", "SHIPMENT_ISSUE"),
    )


def make_policy_task(case: NormalizedCase) -> AgentTask:
    return AgentTask(
        task_id=f"{case.case_id}-policy-1",
        case_id=case.case_id,
        actor="policy-agent",
        objective="Apply the configured policy to verified facts and candidate issue",
        entity_ids=(case.claimed_order_id,),
        required_fact_codes=("POLICY_VERSION", "POLICY_REFUND_ELIGIBLE"),
    )


def make_adjudicator_task(case: NormalizedCase) -> AgentTask:
    return AgentTask(
        task_id=f"{case.case_id}-semantic-adjudicator-1",
        case_id=case.case_id,
        actor="semantic-adjudicator",
        objective="Select one bounded semantic decision from verified candidates",
        entity_ids=(),
        required_fact_codes=(),
    )
