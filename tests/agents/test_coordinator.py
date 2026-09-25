import pytest

from student_agent.agents import CaseInputError, make_order_item_task, normalize_case


def case_input() -> dict:
    return {
        "case_id": "CASE_001",
        "opened_at": "2026-01-02T03:04:05Z",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "language": "pt-BR",
            "message": "Minha compra foi cancelada.",
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "canceled_order_paid"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
    }


def test_normalize_case_keeps_claims_as_unverified_routing_input() -> None:
    normalized = normalize_case(case_input())
    assert normalized.claimed_order_id == "order-1"
    assert [claim.topic for claim in normalized.claims] == [
        "canceled_order_paid",
        "requested_full_refund",
    ]
    assert normalized.entities.order_ids == ("order-1",)


def test_make_order_task_is_case_scoped() -> None:
    normalized = normalize_case(case_input())
    task = make_order_item_task(normalized)
    assert task.case_id == "CASE_001"
    assert task.entity_ids == ("order-1",)
    assert task.actor == "order-item-agent"


def test_normalize_case_rejects_duplicate_claim_ids() -> None:
    value = case_input()
    value["customer_request"]["claims"][1]["claim_id"] = "claim-1"
    with pytest.raises(CaseInputError, match="unique"):
        normalize_case(value)


def test_normalize_case_rejects_naive_opened_at() -> None:
    value = case_input()
    value["opened_at"] = "2026-01-02T03:04:05"
    with pytest.raises(CaseInputError, match="timezone-aware"):
        normalize_case(value)
