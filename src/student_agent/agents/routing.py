from __future__ import annotations

from dataclasses import dataclass

from student_agent.domain import Claim
from student_agent.evidence import (
    ORDER_ITEM_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
)

ROUTING_MATRIX: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "unavailable_order_paid": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "late_delivery_seller": (ORDER_ITEM_AGENT, SHIPMENT_AGENT, POLICY_AGENT),
    "late_delivery_logistics": (ORDER_ITEM_AGENT, SHIPMENT_AGENT, POLICY_AGENT),
    "valid_split_payment": (ORDER_ITEM_AGENT, PAYMENT_AGENT),
    "payment_mismatch": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "duplicate_charge": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "refund_pending": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "refund_failed": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "requested_full_refund": (ORDER_ITEM_AGENT, PAYMENT_AGENT, POLICY_AGENT),
    "unsupported_claim": (ORDER_ITEM_AGENT,),
    "insufficient_evidence": (ORDER_ITEM_AGENT,),
}
ACTOR_ORDER = (ORDER_ITEM_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT, POLICY_AGENT)


@dataclass(frozen=True, slots=True)
class RoutePlan:
    actors: tuple[str, ...]
    unknown_topics: tuple[str, ...] = ()

    def includes(self, actor: str) -> bool:
        return actor in self.actors


def build_route_plan(claims: tuple[Claim, ...]) -> RoutePlan:
    selected = {ORDER_ITEM_AGENT}
    unknown: list[str] = []
    for claim in claims:
        actors = ROUTING_MATRIX.get(claim.topic)
        if actors is None:
            unknown.append(claim.topic)
            continue
        selected.update(actors)
    return RoutePlan(
        actors=tuple(actor for actor in ACTOR_ORDER if actor in selected),
        unknown_topics=tuple(dict.fromkeys(unknown)),
    )
