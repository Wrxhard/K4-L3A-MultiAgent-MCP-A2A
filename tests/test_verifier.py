from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from student_agent.agent_contracts import (
    Actor,
    AgentResult,
    AgentTask,
    VerificationPackage,
    VerificationReport,
)
from student_agent.contracts import Contracts
from student_agent.verifier import DeterministicVerifier
from tests.coordinator_fakes import (
    completed_order_result,
    completed_payment_result,
    completed_policy_result,
    completed_shipment_result,
)


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def task(actor: Actor, *, retry_count: int = 0) -> AgentTask:
    return AgentTask(
        case_id="CASE_001",
        actor=actor,
        invocation=1,
        context_version=1,
        retry_count_for_context=retry_count,
        case={"case_id": "CASE_001"},
        context={},
        feedback=None,
    )


def active_results() -> dict[Actor, AgentResult]:
    return {
        "order_item": completed_order_result(task("order_item")),
        "payment": completed_payment_result(task("payment")),
        "shipment": completed_shipment_result(task("shipment")),
        "policy": completed_policy_result(task("policy")),
    }


def valid_candidate() -> dict[str, object]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": "CASE_001",
        "assessment": {
            "primary_issue": "refund_pending",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["ORDER_1"],
            "item_ids": ["ITEM_1"],
            "seller_ids": ["SELLER_1"],
            "payment_references": ["PAYMENT_1"],
            "shipment_ids": ["SHIPMENT_1"],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "REFUND_DELAY", "rank": 1}],
            "responsible_parties": [
                {"party_type": "payment_provider", "party_id": None}
            ],
        },
        "evidence_refs": [
            "ev_order000000000000000001",
            "ev_payment0000000000000001",
            "ev_shipment000000000000001",
            "ev_policy00000000000000001",
        ],
        "data_conflicts": [],
        "financial_resolution": {
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
        "resolution_actions": ["FOLLOW_UP_REFUND"],
    }


def package(
    candidate: dict[str, object] | None,
    *,
    active: dict[Actor, AgentResult] | None = None,
    failures: tuple[AgentResult, ...] = (),
) -> VerificationPackage:
    results = active if active is not None else active_results()
    return VerificationPackage(
        case_id="CASE_001",
        verification_round=1,
        candidate_output=candidate,
        attempt_history={actor: (result,) for actor, result in results.items()},
        active_results=results,
        unresolved_failures=failures,
    )


def verify(candidate: dict[str, object]) -> VerificationReport:
    return asyncio.run(DeterministicVerifier(contracts()).verify(package(candidate)))


def test_valid_candidate_passes_deterministic_verification() -> None:
    assert verify(valid_candidate()) == VerificationReport.passed()


def test_refund_total_mismatch_targets_policy() -> None:
    candidate = deepcopy(valid_candidate())
    candidate["financial_resolution"]["recommended_refund_brl"] = 11.0  # type: ignore[index]

    report = verify(candidate)

    assert report.verdict == "retry_required"
    assert report.target_actor == "policy"
    assert report.error_code == "REFUND_TOTAL_MISMATCH"


def test_claim_evidence_mismatch_targets_policy() -> None:
    candidate = deepcopy(valid_candidate())
    candidate["claim_assessments"] = [
        {
            "claim_id": "claim-1",
            "verdict": "supported",
            "confidence": 0.8,
            "evidence_refs": ["ev_not_in_candidate0000000001"],
        }
    ]

    report = verify(candidate)

    assert report.target_actor == "policy"
    assert report.error_code == "CLAIM_EVIDENCE_MISMATCH"


def test_invalid_conflict_selection_targets_policy() -> None:
    candidate = deepcopy(valid_candidate())
    candidate["data_conflicts"] = [
        {
            "field": "payment_status",
            "sources": ["payment", "order"],
            "selected_source": "shipment",
            "resolution_code": "SOURCE_PRIORITY",
        }
    ]

    report = verify(candidate)

    assert report.target_actor == "policy"
    assert report.error_code == "INVALID_CONFLICT_SELECTION"


def test_duplicate_root_cause_rank_targets_policy() -> None:
    candidate = deepcopy(valid_candidate())
    candidate["root_cause_analysis"]["ranked_causes"] = [  # type: ignore[index]
        {"cause_code": "REFUND_DELAY", "rank": 1},
        {"cause_code": "PAYMENT_GATEWAY", "rank": 1},
    ]

    report = verify(candidate)

    assert report.target_actor == "policy"
    assert report.error_code == "DUPLICATE_ROOT_CAUSE_RANK"


def test_coordinator_owned_evidence_merge_error_is_terminal() -> None:
    candidate = valid_candidate()
    candidate["evidence_refs"] = candidate["evidence_refs"][:-1]  # type: ignore[index]

    report = verify(candidate)

    assert report.verdict == "failed"
    assert report.target_actor is None
    assert report.error_code == "EVIDENCE_MERGE_MISMATCH"


def test_retryable_agent_failure_targets_its_actor() -> None:
    failure = AgentResult.failed(
        actor="payment",
        invocation=1,
        context_version=1,
        retry_count_for_context=0,
        error_code="PAYMENT_RESULT_INCOMPLETE",
        failed_stage="agent_result",
        retryable=True,
        safe_detail="Payment result is incomplete",
    )
    results = active_results()
    results.pop("payment")

    report = asyncio.run(
        DeterministicVerifier(contracts()).verify(
            package(None, active=results, failures=(failure,))
        )
    )

    assert report.verdict == "retry_required"
    assert report.target_actor == "payment"
    assert report.error_code == "PAYMENT_RESULT_INCOMPLETE"


def test_exhausted_agent_failure_is_terminal() -> None:
    failure = AgentResult.failed(
        actor="payment",
        invocation=2,
        context_version=1,
        retry_count_for_context=1,
        error_code="MCP_TIMEOUT_EXHAUSTED",
        failed_stage="get_payment",
        retryable=False,
        safe_detail="Payment evidence timed out",
    )

    report = asyncio.run(
        DeterministicVerifier(contracts()).verify(
            package(None, active={}, failures=(failure,))
        )
    )

    assert report.verdict == "failed"
    assert report.error_code == "AGENT_FAILURE_NOT_RECOVERABLE"


def test_semantic_verifier_runs_only_after_deterministic_checks_pass() -> None:
    calls: list[VerificationPackage] = []

    async def semantic(value: VerificationPackage) -> VerificationReport:
        calls.append(value)
        return VerificationReport.retry(
            "policy",
            "RESPONSIBILITY_ACTION_MISMATCH",
            "Recheck responsible party and resolution actions",
        )

    verifier = DeterministicVerifier(
        contracts(), semantic_verifier=RunnableLambda(semantic)
    )
    report = asyncio.run(verifier.verify(package(valid_candidate())))

    assert len(calls) == 1
    assert report.error_code == "RESPONSIBILITY_ACTION_MISMATCH"

    invalid = valid_candidate()
    invalid["case_id"] = "OTHER_CASE"
    calls.clear()
    terminal = asyncio.run(verifier.verify(package(invalid)))
    assert terminal.error_code == "CASE_ID_MISMATCH"
    assert calls == []


def test_invalid_semantic_report_is_rejected() -> None:
    async def semantic(package: VerificationPackage) -> VerificationReport:
        del package
        return VerificationReport(
            verdict="retry_required",
            target_actor="unknown-agent",
            error_code="BAD_TARGET",
            feedback="Bad target",
            retryable=True,
        )

    verifier = DeterministicVerifier(
        contracts(), semantic_verifier=RunnableLambda(semantic)
    )

    with pytest.raises(ValueError, match="target_actor"):
        asyncio.run(verifier.verify(package(valid_candidate())))
