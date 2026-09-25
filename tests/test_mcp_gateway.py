from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from student_agent.evidence import EvidenceToolError
from student_agent.mcp_gateway import EvidenceGateway


class SessionStub:
    def __init__(self, result: object) -> None:
        self.result = result

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> object:
        del tool_name, arguments
        return self.result


class ContractsStub:
    def validate_evidence(self, evidence: object, label: str) -> None:
        assert isinstance(evidence, dict)
        assert label.startswith("MCP tool ")


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def test_gateway_reads_current_sdk_snake_case_fields() -> None:
    evidence = {"evidence_ref": "ev-1"}
    result = SimpleNamespace(
        is_error=False,
        structured_content=evidence,
        content=[],
    )
    gateway = EvidenceGateway(SessionStub(result), ContractsStub())  # type: ignore[arg-type]

    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="O-1"))

    assert actual == evidence


def test_gateway_raises_for_current_sdk_error_result() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[text_block("order not found")],
    )
    gateway = EvidenceGateway(SessionStub(result), ContractsStub())  # type: ignore[arg-type]

    with pytest.raises(EvidenceToolError) as captured:
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="missing"))

    assert captured.value.not_found is True


def test_gateway_falls_back_to_single_json_text_block() -> None:
    evidence = {"evidence_ref": "ev-2"}
    result = SimpleNamespace(
        is_error=False,
        structured_content=None,
        content=[text_block(json.dumps(evidence))],
    )
    gateway = EvidenceGateway(SessionStub(result), ContractsStub())  # type: ignore[arg-type]

    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="O-1"))

    assert actual == evidence
