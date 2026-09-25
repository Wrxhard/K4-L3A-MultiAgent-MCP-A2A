from __future__ import annotations

import asyncio

import pytest

from student_agent.agent_contracts import (
    AgentResult,
    AgentTask,
    EntitySet,
    Observation,
    PaymentPayload,
    ShipmentPayload,
    VerificationPackage,
    VerificationReport,
)
from student_agent.coordinator import Coordinator, CoordinatorError
from tests.coordinator_fakes import (
    RecordingTrace,
    completed_payment_result,
    completed_policy_result,
    completed_shipment_result,
    make_registry,
    passing_order_item,
)


def test_coordinator_runs_staged_graph_and_builds_candidate() -> None:
    events: list[str] = []
    payment_started = asyncio.Event()
    shipment_started = asyncio.Event()

    async def order_item(task: AgentTask) -> AgentResult:
        events.append("order_item")
        return await passing_order_item(task)

    async def payment(task: AgentTask) -> AgentResult:
        assert "order_item" in task.context["active_results"]
        payment_started.set()
        await shipment_started.wait()
        events.append("payment")
        return completed_payment_result(task)

    async def shipment(task: AgentTask) -> AgentResult:
        assert "order_item" in task.context["active_results"]
        shipment_started.set()
        await payment_started.wait()
        events.append("shipment")
        return completed_shipment_result(task)

    async def policy(task: AgentTask) -> AgentResult:
        assert set(task.context["active_results"]) == {
            "order_item",
            "payment",
            "shipment",
        }
        events.append("policy")
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        assert package.candidate_output is not None
        events.append("verifier")
        return VerificationReport.passed()

    output = asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(order_item, payment, shipment, policy, verifier),
            RecordingTrace(),
        )
    )

    assert events[0] == "order_item"
    assert set(events[1:3]) == {"payment", "shipment"}
    assert events[-2:] == ["policy", "verifier"]
    assert output["schema_version"] == "day09-l3a-output-v2"
    assert output["case_id"] == "CASE_001"
    assert output["affected_entities"]["order_ids"] == ["ORDER_1"]
    assert output["evidence_refs"] == [
        "ev_order000000000000000001",
        "ev_payment0000000000000001",
        "ev_shipment000000000000001",
        "ev_policy00000000000000001",
    ]
    assert output["assessment"]["case_status"] == "action_required"


def test_invalid_case_fails_before_invoking_agents() -> None:
    events: list[str] = []

    async def unexpected_agent(task: AgentTask) -> AgentResult:
        events.append(task.actor)
        raise AssertionError("agent must not run")

    async def unexpected_verifier(package: VerificationPackage) -> VerificationReport:
        del package
        raise AssertionError("verifier must not run")

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": ""},
                make_registry(
                    unexpected_agent,
                    unexpected_agent,
                    unexpected_agent,
                    unexpected_agent,
                    unexpected_verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "INVALID_CASE"
    assert events == []


def test_conflicting_specialist_observations_are_preserved() -> None:
    async def payment(task: AgentTask) -> AgentResult:
        return AgentResult.completed(
            actor="payment",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=PaymentPayload(
                EntitySet(payment_references=("PAYMENT_1",)),
                (Observation("PAYMENT_STATUS", "PAYMENT_1", {"status": "paid"}),),
            ),
        )

    async def shipment(task: AgentTask) -> AgentResult:
        return AgentResult.completed(
            actor="shipment",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=ShipmentPayload(
                EntitySet(shipment_ids=("SHIPMENT_1",)),
                (Observation("PAYMENT_STATUS", "PAYMENT_1", {"status": "unknown"}),),
            ),
        )

    async def policy(task: AgentTask) -> AgentResult:
        active = task.context["active_results"]
        payment_payload = active["payment"].payload
        shipment_payload = active["shipment"].payload
        assert isinstance(payment_payload, PaymentPayload)
        assert isinstance(shipment_payload, ShipmentPayload)
        assert payment_payload.observations[0].facts["status"] == "paid"
        assert shipment_payload.observations[0].facts["status"] == "unknown"
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        payment_payload = package.active_results["payment"].payload
        shipment_payload = package.active_results["shipment"].payload
        assert isinstance(payment_payload, PaymentPayload)
        assert isinstance(shipment_payload, ShipmentPayload)
        assert payment_payload.observations
        assert shipment_payload.observations
        return VerificationReport.passed()

    registry = make_registry(passing_order_item, payment, shipment, policy, verifier)
    output = asyncio.run(
        Coordinator().solve({"case_id": "CASE_001"}, registry, RecordingTrace())
    )

    assert isinstance(output["financial_resolution"], dict)
