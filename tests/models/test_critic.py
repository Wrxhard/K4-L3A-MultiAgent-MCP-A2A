import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from student_agent.agents import critique, validate_critic_response
from student_agent.domain import (
    CaseStatus,
    ClaimAssessment,
    ClaimVerdict,
    CriticVerdict,
    DraftAssessment,
    EntityIndex,
    PartyType,
    PrimaryIssue,
    RankedCause,
    ResponsibleParty,
)
from student_agent.models import AdjudicationContext, build_critic_context


def draft() -> DraftAssessment:
    return DraftAssessment(
        case_id="CASE_001",
        primary_issue=PrimaryIssue.CANCELED_ORDER_PAID,
        case_status=CaseStatus.NEEDS_INVESTIGATION,
        confidence=Decimal("0.72"),
        claim_assessments=(
            ClaimAssessment(
                claim_id="claim-1",
                verdict=ClaimVerdict.SUPPORTED,
                confidence=Decimal("0.72"),
                evidence_refs=("ev_order_abcdefghijklmnopqrstuvwxyz",),
            ),
        ),
        entities=EntityIndex(order_ids=("order-secret",)),
        ranked_causes=(RankedCause("CANCELED_ORDER_PAID", 1),),
        responsible_parties=(ResponsibleParty(PartyType.PLATFORM, "platform-secret"),),
        evidence_refs=("ev_order_abcdefghijklmnopqrstuvwxyz",),
        conflicts=(),
        recommended_refund_brl=Decimal("123.45"),
        refund_lines=(),
        resolution_actions=("manual_review", "contact_order-secret"),
    )


def adjudication_context() -> AdjudicationContext:
    return AdjudicationContext(
        payload={
            "candidate_issues": ["canceled_order_paid"],
            "facts": [
                {
                    "fact_code": "ORDER_STATUS",
                    "value": "canceled",
                    "evidence_aliases": ["E1"],
                    "role": "supporting",
                }
            ],
        },
        alias_to_evidence_ref={"E1": "ev_order_abcdefghijklmnopqrstuvwxyz"},
        allowed_fact_codes=frozenset({"ORDER_STATUS"}),
        claim_ids=("claim-1",),
        fact_id_to_code={"fact-1": "ORDER_STATUS"},
    )


def pass_response() -> dict[str, Any]:
    return {
        "verdict": "pass",
        "error_codes": [],
        "challenged_fields": [],
        "recommended_confidence_cap": None,
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


def test_critic_context_excludes_money_entity_ids_and_evidence_refs() -> None:
    context = build_critic_context(draft(), adjudication_context())
    serialized = json.dumps(context.payload)
    assert "123.45" not in serialized
    assert "order-secret" not in serialized
    assert "platform-secret" not in serialized
    assert "ev_order_" not in serialized
    assert context.payload["draft"]["has_refund"] is True
    assert context.payload["draft"]["resolution_actions"] == ["manual_review"]


def test_valid_pass_response_is_accepted() -> None:
    report = validate_critic_response(pass_response(), case_id="CASE_001")
    assert report.verdict is CriticVerdict.PASS


def test_valid_reject_response_is_bounded() -> None:
    report = validate_critic_response(
        {
            "verdict": "reject",
            "error_codes": ["CONFIDENCE_TOO_HIGH"],
            "challenged_fields": ["assessment.confidence"],
            "recommended_confidence_cap": 0.5,
        },
        case_id="CASE_001",
    )
    assert report.verdict is CriticVerdict.REJECT
    assert report.recommended_confidence_cap == Decimal("0.5")


def test_unknown_error_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        validate_critic_response(
            {
                "verdict": "reject",
                "error_codes": ["REWRITE_EVERYTHING"],
                "challenged_fields": [],
                "recommended_confidence_cap": None,
            },
            case_id="CASE_001",
        )


def test_invalid_confidence_cap_is_rejected() -> None:
    value = pass_response()
    value["recommended_confidence_cap"] = "NaN"
    with pytest.raises(ValueError, match="within"):
        validate_critic_response(value, case_id="CASE_001")


def test_invalid_then_valid_response_uses_one_repair_attempt() -> None:
    context = build_critic_context(draft(), adjudication_context())
    client = FakeModelClient([{"verdict": "maybe"}, pass_response()])
    result = asyncio.run(
        critique(case_id="CASE_001", context=context, client=client)
    )
    assert result.report.verdict is CriticVerdict.PASS
    assert result.attempts == 2
    assert "repair_instruction" in client.calls[1]["payload"]


def test_two_transport_failures_use_deterministic_fallback() -> None:
    context = build_critic_context(draft(), adjudication_context())
    client = FakeModelClient([TimeoutError(), TimeoutError()])
    result = asyncio.run(
        critique(case_id="CASE_001", context=context, client=client)
    )
    assert result.used_fallback
    assert result.report.verdict is CriticVerdict.PASS
    assert result.decision_code == "CRITIC_FALLBACK"


def test_deterministic_precheck_failure_rejects_without_model() -> None:
    context = build_critic_context(
        draft(),
        adjudication_context(),
        deterministic_error_codes=("MISSING_REQUIRED_EVIDENCE",),
    )
    result = asyncio.run(
        critique(case_id="CASE_001", context=context, client=None)
    )
    assert result.report.verdict is CriticVerdict.REJECT
    assert result.report.error_codes == ("MISSING_REQUIRED_EVIDENCE",)
