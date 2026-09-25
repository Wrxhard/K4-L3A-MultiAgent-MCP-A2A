import asyncio
from decimal import Decimal
from typing import Any

import pytest

from student_agent.agents import evaluate_policy, investigate_policy
from student_agent.domain import AgentTask, PartyType, PrimaryIssue
from student_agent.evidence import EvidenceAdapterError, EvidenceRegistry, ToolCatalog
from student_agent.orchestration import CaseTrace


def policy_data(rule: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "currency": "BRL",
        "policy_version": "EC_POLICY_V1",
        "rules": {"canceled_order_paid": rule} if rule is not None else {},
    }


def test_full_refund_uses_verified_paid_total() -> None:
    decision = evaluate_policy(
        policy_data(
            {
                "refund_eligible": True,
                "refund_type": "full",
                "resolution_actions": ["issue_policy_refund"],
                "responsible_party": "platform",
            }
        ),
        expected_policy_version="EC_POLICY_V1",
        issue=PrimaryIssue.CANCELED_ORDER_PAID,
        paid_total_brl=Decimal("110.00"),
    )
    assert decision.refund_brl == Decimal("110.00")
    assert decision.responsible_party is PartyType.PLATFORM
    assert decision.decision_code == "POLICY_REFUND_ELIGIBLE"


def test_explicit_ineligible_rule_is_not_treated_as_missing() -> None:
    decision = evaluate_policy(
        policy_data({"refund_eligible": False, "resolution_actions": []}),
        expected_policy_version="EC_POLICY_V1",
        issue=PrimaryIssue.CANCELED_ORDER_PAID,
        paid_total_brl=Decimal("110.00"),
    )
    assert decision.rule_found
    assert decision.refund_eligible is False
    assert decision.decision_code == "POLICY_REFUND_INELIGIBLE"


def test_missing_issue_rule_requests_more_information() -> None:
    decision = evaluate_policy(
        policy_data(),
        expected_policy_version="EC_POLICY_V1",
        issue=PrimaryIssue.CANCELED_ORDER_PAID,
        paid_total_brl=Decimal("110.00"),
    )
    assert not decision.rule_found
    assert decision.decision_code == "POLICY_NEEDS_FACT"


def test_policy_refund_cannot_exceed_verified_loss() -> None:
    with pytest.raises(EvidenceAdapterError, match="exceeds"):
        evaluate_policy(
            policy_data({"refund_eligible": True, "refund_brl": "120.00"}),
            expected_policy_version="EC_POLICY_V1",
            issue=PrimaryIssue.CANCELED_ORDER_PAID,
            paid_total_brl=Decimal("110.00"),
        )


def test_policy_rejects_non_brl_currency() -> None:
    value = policy_data({"refund_eligible": True})
    value["currency"] = "USD"
    with pytest.raises(EvidenceAdapterError, match="BRL"):
        evaluate_policy(
            value,
            expected_policy_version="EC_POLICY_V1",
            issue=PrimaryIssue.CANCELED_ORDER_PAID,
            paid_total_brl=Decimal("110.00"),
        )


class FakeGateway:
    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        assert tool_name == "get_policy"
        assert case_id == "CASE_001"
        assert arguments == {"policy_version": "EC_POLICY_V1"}
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_policy_abcdefghijklmnopqrstuvwxyz",
            "result_hash": "sha256:" + "e" * 64,
            "domain": "policy",
            "data": policy_data(
                {
                    "refund_eligible": True,
                    "refund_type": "full",
                    "resolution_actions": ["issue_policy_refund"],
                }
            ),
            "warnings": [],
        }


class FakeTraceSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_policy_specialist_emits_decision_before_handoff() -> None:
    registry = EvidenceRegistry("CASE_001")
    sink = FakeTraceSink()
    trace = CaseTrace("CASE_001", sink, registry)
    trace.case_received()
    task = AgentTask(
        task_id="CASE_001-policy-1",
        case_id="CASE_001",
        actor="policy-agent",
        objective="Apply policy",
    )
    trace.task_assigned(task)
    report = asyncio.run(
        investigate_policy(
            task,
            policy_version="EC_POLICY_V1",
            issue=PrimaryIssue.CANCELED_ORDER_PAID,
            paid_total_brl=Decimal("110.00"),
            gateway=FakeGateway(),
            catalog=ToolCatalog(),
            registry=registry,
            trace=trace,
        )
    )
    trace.handoff(report)

    assert [event["event_type"] for event in sink.events][-3:] == [
        "tool_result_consumed",
        "policy_decided",
        "handoff",
    ]
    assert any(fact.fact_code == "POLICY_REFUND_BRL" for fact in report.facts)
