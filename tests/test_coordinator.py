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
    completed_order_result,
    completed_payment_result,
    completed_policy_result,
    completed_shipment_result,
    make_registry,
    passing_order_item,
    passing_payment,
    passing_policy,
    passing_shipment,
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


def test_agent_crash_is_recovered_once_without_extra_verifier_round() -> None:
    calls = 0
    packages: list[VerificationPackage] = []

    async def order_item(task: AgentTask) -> AgentResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret backend detail")
        assert task.invocation == 2
        assert task.retry_count_for_context == 1
        return completed_order_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        packages.append(package)
        return VerificationReport.passed()

    output = asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(
                order_item,
                passing_payment,
                passing_shipment,
                passing_policy,
                verifier,
            ),
            RecordingTrace(),
        )
    )

    assert output["case_id"] == "CASE_001"
    assert calls == 2
    assert len(packages) == 1
    assert len(packages[0].attempt_history["order_item"]) == 2
    assert "secret backend detail" not in repr(packages)


def test_exhausted_local_mcp_failure_is_not_recovered_by_coordinator() -> None:
    calls = 0
    packages: list[VerificationPackage] = []

    async def order_item(task: AgentTask) -> AgentResult:
        nonlocal calls
        calls += 1
        return AgentResult.failed(
            actor="order_item",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code="MCP_TIMEOUT_EXHAUSTED",
            failed_stage="get_order",
            retryable=False,
            safe_detail="Order evidence timed out",
        )

    async def verifier(package: VerificationPackage) -> VerificationReport:
        packages.append(package)
        return VerificationReport.failed(
            "AGENT_FAILURE_NOT_RECOVERABLE", "Order evidence is unavailable"
        )

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "AGENT_FAILURE_NOT_RECOVERABLE"
    assert calls == 1
    assert packages[0].candidate_output is None
    assert packages[0].unresolved_failures[0].error_code == "MCP_TIMEOUT_EXHAUSTED"


def test_verifier_retry_payment_recomputes_policy_with_new_context() -> None:
    payment_tasks: list[AgentTask] = []
    policy_tasks: list[AgentTask] = []
    reports = iter(
        (
            VerificationReport.retry(
                "payment",
                "MISSING_PAYMENT_EVIDENCE",
                "Recheck payment status and charged amount",
            ),
            VerificationReport.passed(),
        )
    )

    async def payment(task: AgentTask) -> AgentResult:
        payment_tasks.append(task)
        return completed_payment_result(task)

    async def policy(task: AgentTask) -> AgentResult:
        policy_tasks.append(task)
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return next(reports)

    asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(
                passing_order_item,
                payment,
                passing_shipment,
                policy,
                verifier,
            ),
            RecordingTrace(),
        )
    )

    assert [task.invocation for task in payment_tasks] == [1, 2]
    assert payment_tasks[1].retry_count_for_context == 1
    assert payment_tasks[1].feedback is not None
    assert payment_tasks[1].feedback.error_code == "MISSING_PAYMENT_EVIDENCE"
    assert [task.context_version for task in policy_tasks] == [1, 2]
    assert [task.retry_count_for_context for task in policy_tasks] == [0, 0]


def test_verifier_retry_order_item_recomputes_all_dependants() -> None:
    calls = {actor: 0 for actor in ("order_item", "payment", "shipment", "policy")}
    downstream_versions: dict[str, list[int]] = {
        "payment": [],
        "shipment": [],
        "policy": [],
    }
    reports = iter(
        (
            VerificationReport.retry(
                "order_item", "WRONG_ENTITY_SCOPE", "Resolve the correct order"
            ),
            VerificationReport.passed(),
        )
    )

    async def order_item(task: AgentTask) -> AgentResult:
        calls["order_item"] += 1
        return completed_order_result(task)

    async def payment(task: AgentTask) -> AgentResult:
        calls["payment"] += 1
        downstream_versions["payment"].append(task.context_version)
        return completed_payment_result(task)

    async def shipment(task: AgentTask) -> AgentResult:
        calls["shipment"] += 1
        downstream_versions["shipment"].append(task.context_version)
        return completed_shipment_result(task)

    async def policy(task: AgentTask) -> AgentResult:
        calls["policy"] += 1
        downstream_versions["policy"].append(task.context_version)
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return next(reports)

    asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(order_item, payment, shipment, policy, verifier),
            RecordingTrace(),
        )
    )

    assert calls == {"order_item": 2, "payment": 2, "shipment": 2, "policy": 2}
    assert downstream_versions == {
        "payment": [1, 2],
        "shipment": [1, 2],
        "policy": [1, 2],
    }


def test_verifier_retry_policy_does_not_rerun_specialists() -> None:
    policy_tasks: list[AgentTask] = []
    reports = iter(
        (
            VerificationReport.retry(
                "policy", "REFUND_TOTAL_MISMATCH", "Recalculate refund"
            ),
            VerificationReport.passed(),
        )
    )

    async def policy(task: AgentTask) -> AgentResult:
        policy_tasks.append(task)
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return next(reports)

    output = asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(
                passing_order_item,
                passing_payment,
                passing_shipment,
                policy,
                verifier,
            ),
            RecordingTrace(),
        )
    )

    assert output["case_id"] == "CASE_001"
    assert [task.invocation for task in policy_tasks] == [1, 2]
    assert policy_tasks[1].retry_count_for_context == 1


def test_second_same_context_policy_retry_is_rejected() -> None:
    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return VerificationReport.retry(
            "policy", "REFUND_TOTAL_MISMATCH", "Recalculate refund"
        )

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    passing_order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "RETRY_LIMIT_EXCEEDED"


def test_passed_report_without_candidate_is_rejected() -> None:
    async def order_item(task: AgentTask) -> AgentResult:
        return AgentResult.failed(
            actor="order_item",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code="ORDER_NOT_FOUND",
            failed_stage="get_order",
            retryable=False,
            safe_detail="Order not found",
        )

    async def verifier(package: VerificationPackage) -> VerificationReport:
        assert package.candidate_output is None
        return VerificationReport.passed()

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "INVALID_VERIFIER_REPORT"


def test_non_retryable_retry_report_is_rejected() -> None:
    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return VerificationReport(
            verdict="retry_required",
            target_actor="policy",
            error_code="POLICY_ERROR",
            feedback="Retry was marked non-retryable",
            retryable=False,
        )

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    passing_order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "RETRY_NOT_ALLOWED"


def test_invalid_retry_target_is_rejected() -> None:
    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return VerificationReport(
            verdict="retry_required",
            target_actor="verifier",
            error_code="INVALID_TARGET",
            feedback="Retry verifier",
            retryable=True,
        )

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    passing_order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "INVALID_RETRY_TARGET"


def test_verification_stops_before_round_six() -> None:
    targets = iter(("policy", "payment", "policy", "shipment", "policy"))
    calls = 0

    async def verifier(package: VerificationPackage) -> VerificationReport:
        nonlocal calls
        calls += 1
        return VerificationReport.retry(
            next(targets),
            "SEMANTIC_MISMATCH",
            "Recheck the target result",
        )

    with pytest.raises(CoordinatorError) as caught:
        asyncio.run(
            Coordinator().solve(
                {"case_id": "CASE_001"},
                make_registry(
                    passing_order_item,
                    passing_payment,
                    passing_shipment,
                    passing_policy,
                    verifier,
                ),
                RecordingTrace(),
            )
        )

    assert caught.value.code == "VERIFICATION_LIMIT_EXCEEDED"
    assert calls == 5
