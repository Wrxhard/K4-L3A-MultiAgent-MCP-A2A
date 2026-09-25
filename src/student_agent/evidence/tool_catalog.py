from __future__ import annotations

from dataclasses import dataclass

from student_agent.domain import EvidenceDomain

ORDER_ITEM_AGENT = "order-item-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"


class ToolCatalogError(ValueError):
    """Raised when discovered tools or tool ownership violate the catalog."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    owner: str
    domain: EvidenceDomain
    required_arguments: tuple[str, ...]
    purpose: str
    fallback_for: str | None = None


DEFAULT_TOOL_SPECS = (
    ToolSpec(
        "get_order",
        ORDER_ITEM_AGENT,
        EvidenceDomain.ORDER,
        ("case_id", "order_id"),
        "authoritative order row",
    ),
    ToolSpec(
        "get_order_items",
        ORDER_ITEM_AGENT,
        EvidenceDomain.ITEM,
        ("case_id", "order_id"),
        "item, seller, price and freight rows",
    ),
    ToolSpec(
        "get_sellers",
        ORDER_ITEM_AGENT,
        EvidenceDomain.SELLER,
        ("case_id", "order_id"),
        "seller attributes when seller identity alone is insufficient",
    ),
    ToolSpec(
        "get_product_context",
        ORDER_ITEM_AGENT,
        EvidenceDomain.PRODUCT,
        ("case_id", "order_id"),
        "product/category context when it affects a claim",
    ),
    ToolSpec(
        "get_customer_history",
        ORDER_ITEM_AGENT,
        EvidenceDomain.CUSTOMER,
        ("case_id", "customer_unique_id"),
        "customer order history when an authoritative unique ID is available",
    ),
    ToolSpec(
        "get_payment_timeline",
        PAYMENT_AGENT,
        EvidenceDomain.PAYMENT,
        ("case_id", "order_id"),
        "preferred payment rows and lifecycle events",
    ),
    ToolSpec(
        "get_order_payments",
        PAYMENT_AGENT,
        EvidenceDomain.PAYMENT,
        ("case_id", "order_id"),
        "base payment rows when timeline evidence is unavailable",
        fallback_for="get_payment_timeline",
    ),
    ToolSpec(
        "get_refund_timeline",
        PAYMENT_AGENT,
        EvidenceDomain.REFUND,
        ("case_id", "order_id"),
        "refund lifecycle for refund-related investigations",
    ),
    ToolSpec(
        "get_shipment_summary",
        SHIPMENT_AGENT,
        EvidenceDomain.SHIPMENT,
        ("case_id", "order_id"),
        "delivery timeline, shipping limits and responsibility events",
    ),
    ToolSpec(
        "get_policy",
        POLICY_AGENT,
        EvidenceDomain.POLICY,
        ("case_id", "policy_version"),
        "machine-readable case-scoped policy",
    ),
)


class ToolCatalog:
    def __init__(self, specs: tuple[ToolSpec, ...] = DEFAULT_TOOL_SPECS) -> None:
        names = [spec.name for spec in specs]
        if len(names) != len(set(names)):
            raise ToolCatalogError("tool catalog contains duplicate names")
        self._by_name = {spec.name: spec for spec in specs}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def get(self, tool_name: str) -> ToolSpec:
        try:
            return self._by_name[tool_name]
        except KeyError as exc:
            raise ToolCatalogError(f"tool is not configured: {tool_name}") from exc

    def tools_for_actor(self, actor: str) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._by_name.values() if spec.owner == actor)

    def authorize(self, actor: str, tool_name: str) -> ToolSpec:
        spec = self.get(tool_name)
        if spec.owner != actor:
            raise ToolCatalogError(
                f"actor {actor!r} is not allowed to call {tool_name!r}; owner={spec.owner!r}"
            )
        return spec

    def validate_arguments(self, tool_name: str, arguments: dict[str, object]) -> None:
        spec = self.get(tool_name)
        missing = sorted(set(spec.required_arguments) - set(arguments))
        extra = sorted(set(arguments) - set(spec.required_arguments))
        invalid = sorted(
            name
            for name, value in arguments.items()
            if not isinstance(value, str) or not value.strip()
        )
        if missing or extra or invalid:
            raise ToolCatalogError(
                f"invalid arguments for {tool_name}: "
                f"missing={missing}, extra={extra}, invalid={invalid}"
            )

    def validate_discovery(self, discovered_tools: list[str]) -> tuple[str, ...]:
        discovered = set(discovered_tools)
        missing = sorted(set(self._by_name) - discovered)
        if missing:
            raise ToolCatalogError(f"MCP discovery is missing configured tools: {missing}")
        return tuple(sorted(discovered - set(self._by_name)))
