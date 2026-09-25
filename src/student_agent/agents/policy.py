from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from student_agent.domain import (
    AgentTask,
    EvidenceSource,
    Fact,
    InvariantError,
    PartyType,
    PrimaryIssue,
    SpecialistReport,
    TaskStatus,
    parse_decimal,
)
from student_agent.evidence import (
    POLICY_AGENT,
    EvidenceAdapterError,
    EvidenceRegistry,
    ToolCatalog,
    adapt_evidence,
)
from student_agent.orchestration import CaseTrace

from .order_item import Gateway


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    rule_found: bool
    refund_eligible: bool | None
    refund_brl: Decimal | None
    resolution_actions: tuple[str, ...]
    responsible_party: PartyType | None
    decision_code: str


def _actions(rule: Mapping[str, Any]) -> tuple[str, ...]:
    raw = rule.get("resolution_actions", rule.get("actions", ()))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(
        isinstance(value, str) and value.strip() and len(value) <= 80 for value in raw
    ):
        if raw:
            raise EvidenceAdapterError("policy actions must be strings")
        return ()
    return tuple(dict.fromkeys(raw))


def evaluate_policy(
    data: Mapping[str, Any],
    *,
    expected_policy_version: str,
    issue: PrimaryIssue,
    paid_total_brl: Decimal | None,
) -> PolicyDecision:
    if data.get("currency") != "BRL":
        raise EvidenceAdapterError("policy currency must be BRL")
    if data.get("policy_version") != expected_policy_version:
        raise EvidenceAdapterError("policy_version mismatch")
    rules = data.get("rules")
    if not isinstance(rules, Mapping):
        raise EvidenceAdapterError("policy rules must be an object")
    raw_rule = rules.get(issue.value)
    if not isinstance(raw_rule, Mapping):
        return PolicyDecision(False, None, None, (), None, "POLICY_NEEDS_FACT")

    eligible = raw_rule.get("refund_eligible", raw_rule.get("eligible"))
    if eligible is not None and not isinstance(eligible, bool):
        raise EvidenceAdapterError("policy refund eligibility must be boolean")
    refund_brl: Decimal | None = None
    if raw_rule.get("refund_brl") is not None:
        try:
            refund_brl = parse_decimal(raw_rule["refund_brl"], field_name="policy.refund_brl")
        except InvariantError as exc:
            raise EvidenceAdapterError(str(exc)) from exc
        if refund_brl < 0:
            raise EvidenceAdapterError("policy refund_brl cannot be negative")
    elif raw_rule.get("refund_type") == "full" and eligible is True:
        refund_brl = paid_total_brl

    raw_party = raw_rule.get("responsible_party")
    try:
        party = PartyType(raw_party) if raw_party is not None else None
    except ValueError as exc:
        raise EvidenceAdapterError("policy responsible_party is invalid") from exc
    if refund_brl is not None and paid_total_brl is not None and refund_brl > paid_total_brl:
        raise EvidenceAdapterError("policy refund exceeds the verified paid total")
    decision_code = (
        "POLICY_REFUND_ELIGIBLE"
        if eligible is True
        else "POLICY_REFUND_INELIGIBLE"
        if eligible is False
        else "POLICY_NEEDS_FACT"
    )
    return PolicyDecision(
        rule_found=True,
        refund_eligible=eligible,
        refund_brl=refund_brl,
        resolution_actions=_actions(raw_rule),
        responsible_party=party,
        decision_code=decision_code,
    )


def _fact(
    *, task: AgentTask, code: str, value: object, evidence_ref: str
) -> Fact:
    return Fact(
        fact_id=f"{task.task_id}-{code.lower()}",
        fact_code=code,
        value=value,
        source=EvidenceSource.MCP,
        evidence_refs=(evidence_ref,),
    )


async def investigate_policy(
    task: AgentTask,
    *,
    policy_version: str,
    issue: PrimaryIssue,
    paid_total_brl: Decimal | None,
    gateway: Gateway,
    catalog: ToolCatalog,
    registry: EvidenceRegistry,
    trace: CaseTrace,
) -> SpecialistReport:
    if task.actor != POLICY_AGENT:
        raise ValueError("policy specialist received a task owned by another actor")
    tool_name = "get_policy"
    catalog.authorize(task.actor, tool_name)
    arguments = {"case_id": task.case_id, "policy_version": policy_version}
    catalog.validate_arguments(tool_name, arguments)
    envelope = await gateway.call(
        tool_name,
        case_id=task.case_id,
        policy_version=policy_version,
    )
    record = adapt_evidence(
        envelope,
        case_id=task.case_id,
        tool_name=tool_name,
        catalog=catalog,
        expected_policy_version=policy_version,
    )
    if not isinstance(record.data, Mapping):
        raise EvidenceAdapterError("policy evidence data must be an object")
    registry.register(record)
    trace.tool_result_consumed(
        actor=task.actor,
        tool_name=tool_name,
        evidence_ref=record.evidence_ref,
    )
    decision = evaluate_policy(
        record.data,
        expected_policy_version=policy_version,
        issue=issue,
        paid_total_brl=paid_total_brl,
    )
    facts = [
        _fact(
            task=task,
            code="POLICY_VERSION",
            value=policy_version,
            evidence_ref=record.evidence_ref,
        ),
        _fact(
            task=task,
            code="POLICY_RULE_FOUND",
            value=decision.rule_found,
            evidence_ref=record.evidence_ref,
        ),
    ]
    optional_facts = (
        ("POLICY_REFUND_ELIGIBLE", decision.refund_eligible),
        ("POLICY_REFUND_BRL", decision.refund_brl),
        (
            "POLICY_ACTIONS",
            decision.resolution_actions if decision.rule_found else None,
        ),
        (
            "POLICY_RESPONSIBLE_PARTY",
            decision.responsible_party.value if decision.responsible_party else None,
        ),
    )
    facts.extend(
        _fact(task=task, code=code, value=value, evidence_ref=record.evidence_ref)
        for code, value in optional_facts
        if value is not None
    )
    trace.policy_decided(
        task_id=task.task_id,
        decision_code=decision.decision_code,
        evidence_refs=(record.evidence_ref,),
    )
    return SpecialistReport(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        status=TaskStatus.COMPLETED if decision.rule_found else TaskStatus.PARTIAL,
        facts=tuple(facts),
        evidence_refs=(record.evidence_ref,),
        warnings=record.warnings,
    )
