import pytest

from student_agent.domain import EvidenceDomain
from student_agent.evidence import (
    DEFAULT_TOOL_SPECS,
    ORDER_ITEM_AGENT,
    PAYMENT_AGENT,
    ToolCatalog,
    ToolCatalogError,
)


def test_default_catalog_contains_all_discovered_tools() -> None:
    catalog = ToolCatalog()
    assert set(catalog.names) == {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_product_context",
        "get_customer_history",
        "get_policy",
    }
    assert len(DEFAULT_TOOL_SPECS) == 10


def test_catalog_enforces_actor_allowlist() -> None:
    catalog = ToolCatalog()
    spec = catalog.authorize(PAYMENT_AGENT, "get_payment_timeline")
    assert spec.domain is EvidenceDomain.PAYMENT

    with pytest.raises(ToolCatalogError, match="not allowed"):
        catalog.authorize(ORDER_ITEM_AGENT, "get_payment_timeline")


def test_catalog_validates_exact_arguments() -> None:
    catalog = ToolCatalog()
    catalog.validate_arguments(
        "get_order", {"case_id": "CASE_001", "order_id": "order-1"}
    )

    with pytest.raises(ToolCatalogError, match="missing"):
        catalog.validate_arguments("get_order", {"case_id": "CASE_001"})
    with pytest.raises(ToolCatalogError, match="extra"):
        catalog.validate_arguments(
            "get_order",
            {"case_id": "CASE_001", "order_id": "order-1", "seller_id": "seller-1"},
        )


def test_catalog_rejects_non_string_argument_at_runtime_boundary() -> None:
    with pytest.raises(ToolCatalogError, match="invalid=.*order_id"):
        ToolCatalog().validate_arguments(
            "get_order", {"case_id": "CASE_001", "order_id": None}
        )


def test_catalog_discovery_rejects_missing_configured_tool() -> None:
    catalog = ToolCatalog()
    with pytest.raises(ToolCatalogError, match="missing configured tools"):
        catalog.validate_discovery(["get_order"])


def test_catalog_discovery_reports_unknown_server_tools() -> None:
    catalog = ToolCatalog()
    extras = catalog.validate_discovery([*catalog.names, "future_read_only_tool"])
    assert extras == ("future_read_only_tool",)


def test_payment_rows_are_explicit_fallback_for_timeline() -> None:
    spec = ToolCatalog().get("get_order_payments")
    assert spec.fallback_for == "get_payment_timeline"
