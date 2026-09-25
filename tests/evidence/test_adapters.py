import pytest

from student_agent.domain import EvidenceDomain
from student_agent.evidence import EvidenceAdapterError, ToolCatalog, adapt_evidence

EVIDENCE_REF = "ev_abcdefghijklmnopqrstuvwxyz"
RESULT_HASH = "sha256:" + "a" * 64


def envelope(*, domain: str, data: object, warnings: list[str] | None = None) -> dict:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": EVIDENCE_REF,
        "result_hash": RESULT_HASH,
        "domain": domain,
        "data": data,
        "warnings": warnings or [],
    }


def test_adapter_builds_case_scoped_record() -> None:
    record = adapt_evidence(
        envelope(domain="order", data={"order_id": "order-1", "order_status": "canceled"}),
        case_id="CASE_001",
        tool_name="get_order",
        catalog=ToolCatalog(),
        expected_order_id="order-1",
    )
    assert record.case_id == "CASE_001"
    assert record.domain is EvidenceDomain.ORDER
    assert record.evidence_ref == EVIDENCE_REF


def test_adapter_rejects_wrong_domain_for_tool() -> None:
    with pytest.raises(EvidenceAdapterError, match="expected 'order'"):
        adapt_evidence(
            envelope(domain="payment", data={"order_id": "order-1"}),
            case_id="CASE_001",
            tool_name="get_order",
            catalog=ToolCatalog(),
            expected_order_id="order-1",
        )


def test_adapter_rejects_wrong_order_in_object() -> None:
    with pytest.raises(EvidenceAdapterError, match="order_id mismatch"):
        adapt_evidence(
            envelope(domain="order", data={"order_id": "other-order"}),
            case_id="CASE_001",
            tool_name="get_order",
            catalog=ToolCatalog(),
            expected_order_id="order-1",
        )


def test_adapter_rejects_wrong_order_in_rows() -> None:
    with pytest.raises(EvidenceAdapterError, match="outside order"):
        adapt_evidence(
            envelope(
                domain="item",
                data=[
                    {"order_id": "order-1", "order_item_id": "item-1"},
                    {"order_id": "other-order", "order_item_id": "item-2"},
                ],
            ),
            case_id="CASE_001",
            tool_name="get_order_items",
            catalog=ToolCatalog(),
            expected_order_id="order-1",
        )


def test_adapter_requires_scope_for_order_tool() -> None:
    with pytest.raises(EvidenceAdapterError, match="expected_order_id"):
        adapt_evidence(
            envelope(domain="order", data={"order_id": "order-1"}),
            case_id="CASE_001",
            tool_name="get_order",
            catalog=ToolCatalog(),
        )


def test_adapter_rejects_wrong_policy_version() -> None:
    with pytest.raises(EvidenceAdapterError, match="policy_version mismatch"):
        adapt_evidence(
            envelope(domain="policy", data={"policy_version": "OTHER", "rules": {}}),
            case_id="CASE_001",
            tool_name="get_policy",
            catalog=ToolCatalog(),
            expected_policy_version="EC_POLICY_V1",
        )


def test_adapter_requires_policy_scope_for_policy_tool() -> None:
    with pytest.raises(EvidenceAdapterError, match="expected_policy_version"):
        adapt_evidence(
            envelope(domain="policy", data={"policy_version": "EC_POLICY_V1"}),
            case_id="CASE_001",
            tool_name="get_policy",
            catalog=ToolCatalog(),
        )


def test_adapter_rejects_invalid_warning_shape() -> None:
    value = envelope(domain="order", data={"order_id": "order-1"})
    value["warnings"] = [""]
    with pytest.raises(EvidenceAdapterError, match="warnings"):
        adapt_evidence(
            value,
            case_id="CASE_001",
            tool_name="get_order",
            catalog=ToolCatalog(),
            expected_order_id="order-1",
        )


def test_adapter_rejects_unsupported_schema() -> None:
    value = envelope(domain="order", data={"order_id": "order-1"})
    value["schema_version"] = "future-version"
    with pytest.raises(EvidenceAdapterError, match="schema_version"):
        adapt_evidence(
            value,
            case_id="CASE_001",
            tool_name="get_order",
            catalog=ToolCatalog(),
            expected_order_id="order-1",
        )
