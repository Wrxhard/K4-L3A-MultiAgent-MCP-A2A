from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent.agent_contracts import VerificationPackage, VerificationReport
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from tests.coordinator_fakes import (
    make_passing_registry,
    make_registry,
    passing_order_item,
    passing_payment,
    passing_policy,
    passing_shipment,
)


def make_contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_solve_case_requires_explicit_registry(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", make_contracts())

    with pytest.raises(RuntimeError, match="AgentRegistry"):
        asyncio.run(solve_case({"case_id": "CASE_001"}, object(), trace))  # type: ignore[arg-type]


def test_solve_case_with_registry_emits_schema_valid_trace(tmp_path: Path) -> None:
    contracts = make_contracts()
    path = tmp_path / "trace.jsonl"
    trace = TraceWriter(path, contracts)

    output = asyncio.run(
        solve_case(
            {"case_id": "CASE_001"},
            object(),  # type: ignore[arg-type]
            trace,
            registry=make_passing_registry(),
        )
    )

    assert output["case_id"] == "CASE_001"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {event["event_type"] for event in events} >= {
        "task_assigned",
        "handoff",
        "verification_completed",
    }
    for event in events:
        contracts.validate_trace(event, "test trace")


def test_verifier_retry_emits_only_schema_valid_trace(tmp_path: Path) -> None:
    contracts = make_contracts()
    path = tmp_path / "trace.jsonl"
    reports = iter(
        (
            VerificationReport.retry("policy", "POLICY_MISMATCH", "Recheck policy"),
            VerificationReport.passed(),
        )
    )

    async def verifier(package: VerificationPackage) -> VerificationReport:
        del package
        return next(reports)

    registry = make_registry(
        passing_order_item,
        passing_payment,
        passing_shipment,
        passing_policy,
        verifier,
    )
    asyncio.run(
        solve_case(
            {"case_id": "CASE_001"},
            object(),  # type: ignore[arg-type]
            TraceWriter(path, contracts),
            registry=registry,
        )
    )

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    retry_handoffs = [
        event
        for event in events
        if event["event_type"] == "handoff"
        and event.get("decision_code") == "POLICY_MISMATCH"
    ]
    assert len(retry_handoffs) == 1
    for event in events:
        contracts.validate_trace(event, "retry trace")
