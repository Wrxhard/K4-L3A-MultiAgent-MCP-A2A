from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.runnables import RunnableLambda

from student_agent.agent_contracts import (
    AgentRegistry,
    AgentResult,
    AgentTask,
    EntitySet,
    OrderItemPayload,
    PaymentPayload,
    PolicyPayload,
    ShipmentPayload,
    VerificationPackage,
    VerificationReport,
)

AgentHandler = Callable[[AgentTask], Coroutine[Any, Any, AgentResult]]
VerifierHandler = Callable[
    [VerificationPackage], Coroutine[Any, Any, VerificationReport]
]


class RecordingTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, **event: object) -> dict[str, object]:
        self.events.append(event)
        return event


def completed_order_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="order_item",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=OrderItemPayload(
            EntitySet(
                order_ids=("ORDER_1",),
                item_ids=("ITEM_1",),
                seller_ids=("SELLER_1",),
                payment_references=("PAYMENT_1",),
                shipment_ids=("SHIPMENT_1",),
            ),
            (),
        ),
        evidence_refs=("ev_order000000000000000001",),
    )


def completed_payment_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="payment",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=PaymentPayload(
            EntitySet(payment_references=("PAYMENT_1",)),
            (),
        ),
        evidence_refs=("ev_payment0000000000000001",),
    )


def completed_shipment_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="shipment",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=ShipmentPayload(EntitySet(shipment_ids=("SHIPMENT_1",)), ()),
        evidence_refs=("ev_shipment000000000000001",),
    )


def completed_policy_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="policy",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=PolicyPayload(
            assessment={
                "primary_issue": "refund_pending",
                "case_status": "action_required",
                "confidence": 0.9,
            },
            claim_assessments=(),
            root_cause_analysis={
                "ranked_causes": [{"cause_code": "REFUND_DELAY", "rank": 1}],
                "responsible_parties": [
                    {"party_type": "payment_provider", "party_id": None}
                ],
            },
            data_conflicts=(),
            financial_resolution={
                "currency": "BRL",
                "recommended_refund_brl": 10.0,
                "refund_lines": [
                    {
                        "reason_code": "REFUND_PENDING",
                        "amount_brl": 10.0,
                        "entity_id": "ITEM_1",
                    }
                ],
            },
            resolution_actions=("FOLLOW_UP_REFUND",),
        ),
        evidence_refs=("ev_policy00000000000000001",),
    )


async def passing_order_item(task: AgentTask) -> AgentResult:
    return completed_order_result(task)


async def passing_payment(task: AgentTask) -> AgentResult:
    return completed_payment_result(task)


async def passing_shipment(task: AgentTask) -> AgentResult:
    return completed_shipment_result(task)


async def passing_policy(task: AgentTask) -> AgentResult:
    return completed_policy_result(task)


async def passing_verifier(package: VerificationPackage) -> VerificationReport:
    del package
    return VerificationReport.passed()


def make_registry(
    order_item: AgentHandler,
    payment: AgentHandler,
    shipment: AgentHandler,
    policy: AgentHandler,
    verifier: VerifierHandler,
) -> AgentRegistry:
    return AgentRegistry(
        order_item=RunnableLambda(order_item),
        payment=RunnableLambda(payment),
        shipment=RunnableLambda(shipment),
        policy=RunnableLambda(policy),
        verifier=RunnableLambda(verifier),
    )


def make_passing_registry() -> AgentRegistry:
    return make_registry(
        passing_order_item,
        passing_payment,
        passing_shipment,
        passing_policy,
        passing_verifier,
    )
