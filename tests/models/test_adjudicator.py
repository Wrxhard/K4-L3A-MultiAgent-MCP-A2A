import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from student_agent.agents import adjudicate, validate_adjudication
from student_agent.agents.coordinator import NormalizedCase
from student_agent.domain import (
    CandidateSet,
    Claim,
    EntityIndex,
    EvidenceSource,
    Fact,
    PrimaryIssue,
    SpecialistReport,
    TaskStatus,
)
from student_agent.models import build_adjudication_context


def fixtures():
    case = NormalizedCase(
        case_id="CASE_001",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        policy_version="EC_POLICY_V1",
        language="pt-BR",
        claimed_order_id="order-secret",
        claims=(
            Claim("claim-1", "canceled_order_paid"),
            Claim("claim-2", "requested_full_refund"),
        ),
        entities=EntityIndex(order_ids=("order-secret",)),
    )
    order_ref = "ev_order_abcdefghijklmnopqrstuvwxyz"
    payment_ref = "ev_payment_abcdefghijklmnopqrstuvwxyz"
    facts = (
        Fact(
            fact_id="fact-status",
            fact_code="ORDER_STATUS",
            value="canceled",
            source=EvidenceSource.MCP,
            entity_id="order-secret",
            evidence_refs=(order_ref,),
        ),
        Fact(
            fact_id="fact-paid",
            fact_code="PAYMENT_TOTAL_BRL",
            value=Decimal("123.45"),
            source=EvidenceSource.MCP,
            entity_id="order-secret",
            evidence_refs=(payment_ref,),
        ),
    )
    report = SpecialistReport(
        case_id="CASE_001",
        task_id="task-1",
        actor="test-agent",
        status=TaskStatus.COMPLETED,
        facts=facts,
        evidence_refs=(order_ref, payment_ref),
    )
    candidates = CandidateSet(
        case_id="CASE_001",
        issues=(PrimaryIssue.CANCELED_ORDER_PAID,),
        supporting_fact_ids=("fact-status", "fact-paid"),
    )
    context = build_adjudication_context(case, candidates, (report,))
    return case, candidates, context


def valid_response() -> dict[str, Any]:
    return {
        "selected_issue": "canceled_order_paid",
        "claim_decisions": [
            {
                "claim_id": "claim-1",
                "verdict": "supported",
                "supporting_fact_codes": ["ORDER_STATUS", "PAYMENT_PRESENT"],
                "supporting_evidence_aliases": ["E1", "E2"],
            },
            {
                "claim_id": "claim-2",
                "verdict": "insufficient_evidence",
                "supporting_fact_codes": [],
                "supporting_evidence_aliases": [],
            },
        ],
        "supporting_fact_codes": ["ORDER_STATUS", "PAYMENT_PRESENT"],
        "supporting_evidence_aliases": ["E1", "E2"],
        "confidence_band": "medium",
    }


class FakeModelClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate_json(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def test_context_uses_aliases_and_excludes_raw_ids_money_and_message() -> None:
    _, _, context = fixtures()
    serialized = json.dumps(context.payload)
    assert "ev_order_" not in serialized
    assert "order-secret" not in serialized
    assert "123.45" not in serialized
    assert {fact["fact_code"] for fact in context.payload["facts"]} == {
        "ORDER_STATUS",
        "PAYMENT_PRESENT",
    }


def test_valid_bounded_response_is_accepted() -> None:
    case, candidates, context = fixtures()
    decision = validate_adjudication(
        valid_response(),
        case_id=case.case_id,
        candidates=candidates,
        context=context,
    )
    assert decision.selected_issue is PrimaryIssue.CANCELED_ORDER_PAID


def test_issue_outside_candidate_set_is_rejected() -> None:
    case, candidates, context = fixtures()
    response = valid_response()
    response["selected_issue"] = "duplicate_charge"
    with pytest.raises(ValueError, match="outside"):
        validate_adjudication(
            response,
            case_id=case.case_id,
            candidates=candidates,
            context=context,
        )


def test_unknown_alias_is_rejected() -> None:
    case, candidates, context = fixtures()
    response = valid_response()
    response["supporting_evidence_aliases"] = ["E999"]
    with pytest.raises(ValueError, match="unknown evidence alias"):
        validate_adjudication(
            response,
            case_id=case.case_id,
            candidates=candidates,
            context=context,
        )


def test_one_invalid_response_gets_one_repair_attempt() -> None:
    case, candidates, context = fixtures()
    invalid = valid_response()
    invalid["selected_issue"] = "duplicate_charge"
    client = FakeModelClient([invalid, valid_response()])
    result = asyncio.run(
        adjudicate(
            case_id=case.case_id,
            candidates=candidates,
            context=context,
            client=client,
        )
    )
    assert not result.used_fallback
    assert result.attempts == 2
    assert client.calls[0]["temperature"] == 0.0
    assert "repair_instruction" in client.calls[1]["payload"]


def test_two_invalid_responses_use_deterministic_fallback() -> None:
    case, candidates, context = fixtures()
    client = FakeModelClient([ValueError("bad JSON"), ValueError("bad JSON again")])
    result = asyncio.run(
        adjudicate(
            case_id=case.case_id,
            candidates=candidates,
            context=context,
            client=client,
        )
    )
    assert result.used_fallback
    assert result.attempts == 2
    assert result.decision.selected_issue is PrimaryIssue.CANCELED_ORDER_PAID
    assert result.decision_code == "ADJUDICATOR_FALLBACK"


def test_constraint_violations_have_specific_fallback_code() -> None:
    case, candidates, context = fixtures()
    invalid = valid_response()
    invalid["selected_issue"] = "duplicate_charge"
    client = FakeModelClient([invalid, invalid])
    result = asyncio.run(
        adjudicate(
            case_id=case.case_id,
            candidates=candidates,
            context=context,
            client=client,
        )
    )
    assert result.used_fallback
    assert result.decision_code == "ADJUDICATOR_CONSTRAINT_VIOLATION"
