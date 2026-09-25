import asyncio
from decimal import Decimal
from typing import Any

import pytest

from student_agent.agents import analyze_payments, investigate_payments
from student_agent.domain import AgentTask, PrimaryIssue
from student_agent.evidence import (
    EvidenceAdapterError,
    EvidenceRegistry,
    EvidenceToolError,
    ToolCatalog,
)
from student_agent.orchestration import CaseTrace


def payment(value: str, reference: str, *, payment_type: str = "credit_card") -> dict:
    return {
        "order_id": "order-1",
        "payment_reference": reference,
        "payment_type": payment_type,
        "payment_installments": 1,
        "payment_value": value,
    }


def test_exact_multi_payment_total_is_valid_split() -> None:
    result = analyze_payments(
        [payment("60.00", "pay-1"), payment("50.00", "pay-2", payment_type="voucher")],
        expected_total_brl=Decimal("110.00"),
    )
    assert result.issue is PrimaryIssue.VALID_SPLIT_PAYMENT
    assert result.paid_total_brl == Decimal("110.00")


def test_repeated_excess_transaction_is_duplicate_charge() -> None:
    result = analyze_payments(
        [payment("110.00", "pay-1"), payment("110.00", "pay-2")],
        expected_total_brl=Decimal("110.00"),
    )
    assert result.issue is PrimaryIssue.DUPLICATE_CHARGE


def test_nonduplicate_difference_is_payment_mismatch() -> None:
    result = analyze_payments(
        [payment("60.00", "pay-1"), payment("60.00", "pay-2", payment_type="voucher")],
        expected_total_brl=Decimal("110.00"),
    )
    assert result.issue is PrimaryIssue.PAYMENT_MISMATCH


def test_single_exact_payment_has_no_payment_anomaly() -> None:
    result = analyze_payments(
        [payment("110.00", "pay-1")], expected_total_brl=Decimal("110.00")
    )
    assert result.issue is None


def test_invalid_payment_amount_is_rejected() -> None:
    with pytest.raises(EvidenceAdapterError, match="decimal"):
        analyze_payments(
            [payment("not-money", "pay-1")], expected_total_brl=Decimal("110.00")
        )


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        del case_id, arguments
        self.calls.append(tool_name)
        response = self.responses[tool_name]
        if isinstance(response, Exception):
            raise response
        return response


class FakeTraceSink:
    def emit(self, **event: Any) -> dict[str, Any]:
        return event


def envelope(ref: str, domain: str, data: object, fill: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": ref,
        "result_hash": "sha256:" + fill * 64,
        "domain": domain,
        "data": data,
        "warnings": [],
    }


def payment_task() -> AgentTask:
    return AgentTask(
        task_id="CASE_001-payment-1",
        case_id="CASE_001",
        actor="payment-agent",
        objective="Verify payments",
    )


def run_specialist(
    gateway: FakeGateway, *, claim_topics: tuple[str, ...] = ("duplicate_charge",)
):
    registry = EvidenceRegistry("CASE_001")
    trace = CaseTrace("CASE_001", FakeTraceSink(), registry)
    trace.case_received()
    task = payment_task()
    trace.task_assigned(task)
    report = asyncio.run(
        investigate_payments(
            task,
            order_id="order-1",
            claim_topics=claim_topics,
            expected_total_brl=Decimal("110.00"),
            gateway=gateway,
            catalog=ToolCatalog(),
            registry=registry,
            trace=trace,
        )
    )
    return report, registry


def test_specialist_prefers_timeline_without_redundant_base_call() -> None:
    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "ev_payment_abcdefghijklmnopqrstuvwxyz",
                "payment",
                {"order_id": "order-1", "payments": [payment("110.00", "pay-1")]},
                "a",
            )
        }
    )
    report, _ = run_specialist(gateway)
    assert gateway.calls == ["get_payment_timeline"]
    assert {fact.fact_code for fact in report.facts} >= {
        "PAYMENT_TOTAL_BRL",
        "PAYMENT_COUNT",
        "PAYMENT_REFERENCES",
    }


def test_specialist_uses_base_payment_fallback_when_timeline_has_no_rows() -> None:
    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "ev_timeline_abcdefghijklmnopqrstuvwxyz",
                "payment",
                {"order_id": "order-1", "events": []},
                "a",
            ),
            "get_order_payments": envelope(
                "ev_basepay_abcdefghijklmnopqrstuvwxyz",
                "payment",
                [payment("110.00", "pay-1")],
                "b",
            ),
        }
    )
    report, _ = run_specialist(gateway)
    assert gateway.calls == ["get_payment_timeline", "get_order_payments"]
    assert report.evidence_refs == ("ev_basepay_abcdefghijklmnopqrstuvwxyz",)


@pytest.mark.parametrize(
    ("state", "expected_issue"),
    [("processing", "refund_pending"), ("failed", "refund_failed")],
)
def test_refund_state_is_normalized_without_guessing_completion(
    state: str, expected_issue: str
) -> None:
    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "ev_payment_abcdefghijklmnopqrstuvwxyz",
                "payment",
                {"order_id": "order-1", "payments": [payment("110.00", "pay-1")]},
                "a",
            ),
            "get_refund_timeline": envelope(
                "ev_refunds_abcdefghijklmnopqrstuvwxyz",
                "refund",
                {"order_id": "order-1", "events": [{"status": state}]},
                "b",
            ),
        }
    )
    report, _ = run_specialist(gateway, claim_topics=(expected_issue,))
    issue_fact = next(fact for fact in report.facts if fact.fact_code == "REFUND_ISSUE")
    assert issue_fact.value == expected_issue


def test_empty_refund_timeline_does_not_create_completed_fact() -> None:
    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "ev_payment_abcdefghijklmnopqrstuvwxyz",
                "payment",
                {"order_id": "order-1", "payments": [payment("110.00", "pay-1")]},
                "a",
            ),
            "get_refund_timeline": envelope(
                "ev_refunds_abcdefghijklmnopqrstuvwxyz",
                "refund",
                {"order_id": "order-1", "events": []},
                "b",
            ),
        }
    )
    report, _ = run_specialist(gateway, claim_topics=("refund_pending",))
    assert "REFUND_STATUS" not in {fact.fact_code for fact in report.facts}


def test_missing_refund_record_is_partial_and_never_completed() -> None:
    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "ev_payment_abcdefghijklmnopqrstuvwxyz",
                "payment",
                {"order_id": "order-1", "payments": [payment("110.00", "pay-1")]},
                "a",
            ),
            "get_refund_timeline": EvidenceToolError(
                "get_refund_timeline", "no refund record", not_found=True
            ),
        }
    )
    report, _ = run_specialist(gateway, claim_topics=("refund_pending",))
    assert report.status.value == "partial"
    assert "REFUND_STATUS" not in {fact.fact_code for fact in report.facts}
    assert any("remains unknown" in warning for warning in report.warnings)
