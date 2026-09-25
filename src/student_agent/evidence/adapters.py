from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from student_agent.domain import EvidenceDomain, EvidenceRecord

from .tool_catalog import ToolCatalog


class EvidenceAdapterError(ValueError):
    """Raised when an MCP evidence object is valid syntactically but unsafe to consume."""


def _require_order_scope(data: Any, expected_order_id: str) -> None:
    if isinstance(data, Mapping):
        observed = data.get("order_id")
        if observed is not None and observed != expected_order_id:
            raise EvidenceAdapterError(
                f"evidence order_id mismatch: expected {expected_order_id!r}, got {observed!r}"
            )
        return
    if isinstance(data, list):
        mismatched = sorted(
            {
                row.get("order_id")
                for row in data
                if isinstance(row, Mapping)
                and row.get("order_id") is not None
                and row.get("order_id") != expected_order_id
            }
        )
        if mismatched:
            raise EvidenceAdapterError(
                f"evidence contains rows outside order {expected_order_id!r}: {mismatched!r}"
            )


def adapt_evidence(
    evidence: Mapping[str, Any],
    *,
    case_id: str,
    tool_name: str,
    catalog: ToolCatalog,
    expected_order_id: str | None = None,
    expected_policy_version: str | None = None,
) -> EvidenceRecord:
    if evidence.get("schema_version") != "day09-mcp-evidence-v1":
        raise EvidenceAdapterError("unsupported MCP evidence schema_version")

    spec = catalog.get(tool_name)
    try:
        domain = EvidenceDomain(evidence["domain"])
        evidence_ref = evidence["evidence_ref"]
        result_hash = evidence["result_hash"]
        data = evidence["data"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceAdapterError("MCP response is missing a valid evidence field") from exc

    if domain is not spec.domain:
        raise EvidenceAdapterError(
            f"tool {tool_name} returned domain {domain.value!r}; expected {spec.domain.value!r}"
        )

    raw_warnings = evidence.get("warnings", [])
    if not isinstance(raw_warnings, list) or not all(
        isinstance(warning, str) and warning for warning in raw_warnings
    ):
        raise EvidenceAdapterError("evidence warnings must be an array of non-empty strings")

    if "order_id" in spec.required_arguments:
        if expected_order_id is None:
            raise EvidenceAdapterError(f"expected_order_id is required for tool {tool_name}")
        _require_order_scope(data, expected_order_id)

    if "policy_version" in spec.required_arguments:
        if expected_policy_version is None:
            raise EvidenceAdapterError(f"expected_policy_version is required for tool {tool_name}")
        if not isinstance(data, Mapping):
            raise EvidenceAdapterError("policy evidence data must be an object")
        observed_version = data.get("policy_version")
        if observed_version != expected_policy_version:
            raise EvidenceAdapterError(
                "policy_version mismatch: "
                f"expected {expected_policy_version!r}, got {observed_version!r}"
            )

    try:
        return EvidenceRecord(
            case_id=case_id,
            evidence_ref=evidence_ref,
            result_hash=result_hash,
            domain=domain,
            tool_name=tool_name,
            data=data,
            warnings=tuple(raw_warnings),
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceAdapterError(str(exc)) from exc
