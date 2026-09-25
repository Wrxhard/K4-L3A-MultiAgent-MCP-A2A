from __future__ import annotations

import pytest
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
    validate_agent_result,
    validate_verification_report,
)


def payment_task() -> AgentTask:
    return AgentTask(
        case_id="CASE_001",
        actor="payment",
        invocation=1,
        context_version=1,
        retry_count_for_context=0,
        case={"case_id": "CASE_001"},
        context={},
        feedback=None,
    )


def test_validate_agent_result_rejects_payload_owned_by_another_actor() -> None:
    task = payment_task()
    result = AgentResult.completed(
        actor="payment",
        invocation=1,
        context_version=1,
        retry_count_for_context=0,
        payload=ShipmentPayload(EntitySet(), ()),
    )

    with pytest.raises(ValueError, match="payload"):
        validate_agent_result(task, result)


def test_validate_agent_result_rejects_counter_mismatch() -> None:
    task = payment_task()
    result = AgentResult.completed(
        actor="payment",
        invocation=2,
        context_version=1,
        retry_count_for_context=0,
        payload=PaymentPayload(EntitySet(), ()),
    )

    with pytest.raises(ValueError, match="invocation"):
        validate_agent_result(task, result)


def test_failed_result_rejects_secret_in_safe_detail() -> None:
    with pytest.raises(ValueError, match="safe_detail"):
        AgentResult.failed(
            actor="order_item",
            invocation=1,
            context_version=1,
            retry_count_for_context=0,
            error_code="AGENT_CRASHED",
            failed_stage="agent_task",
            retryable=True,
            safe_detail="sk-team-1234567890abcdef leaked",
        )


def test_retry_report_requires_a_known_actor() -> None:
    report = VerificationReport(
        verdict="retry_required",
        target_actor="unknown-agent",
        error_code="BAD_TARGET",
        feedback="Unknown retry target",
        retryable=True,
    )

    with pytest.raises(ValueError, match="target_actor"):
        validate_verification_report(report)


def test_completed_result_rejects_duplicate_evidence_refs() -> None:
    with pytest.raises(ValueError, match="evidence_refs"):
        AgentResult.completed(
            actor="payment",
            invocation=1,
            context_version=1,
            retry_count_for_context=0,
            payload=PaymentPayload(EntitySet(), ()),
            evidence_refs=("ev_12345678901234567890", "ev_12345678901234567890"),
        )


def test_registry_returns_the_runnable_for_each_actor() -> None:
    async def agent(task: AgentTask) -> AgentResult:
        return AgentResult.failed(
            actor=task.actor,
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code="TEST_FAILURE",
            failed_stage="test",
            retryable=False,
            safe_detail="test failure",
        )

    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return VerificationReport.failed("TEST_FAILURE", "test failure")

    runnable = RunnableLambda(agent)
    registry = AgentRegistry(
        order_item=runnable,
        payment=runnable,
        shipment=runnable,
        policy=runnable,
        verifier=RunnableLambda(verifier),
    )

    assert registry.for_actor("order_item") is runnable
    assert registry.for_actor("payment") is runnable
    assert registry.for_actor("shipment") is runnable
    assert registry.for_actor("policy") is runnable


def test_policy_payload_owns_final_semantic_fields() -> None:
    payload = PolicyPayload(
        assessment={"case_status": "needs_investigation"},
        claim_assessments=(),
        root_cause_analysis={"ranked_causes": [], "responsible_parties": []},
        data_conflicts=(),
        financial_resolution={
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        resolution_actions=(),
    )
    order_payload = OrderItemPayload(EntitySet(), ())

    assert payload.financial_resolution["currency"] == "BRL"
    assert order_payload.entities == EntitySet()
