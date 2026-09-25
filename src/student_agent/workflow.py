from __future__ import annotations

from typing import Any

from .agents import (
    QWEN_MODEL_ID,
    Gateway,
    adjudicate,
    build_route_plan,
    build_rules_draft,
    draft_to_output,
    generate_candidates,
    investigate_order_items,
    investigate_payments,
    investigate_policy,
    investigate_shipment,
    make_adjudicator_task,
    make_order_item_task,
    make_payment_task,
    make_policy_task,
    make_shipment_task,
    normalize_case,
    verify_draft,
)
from .evidence import (
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    EvidenceRegistry,
    ToolCatalog,
)
from .models import StructuredModelClient, build_adjudication_context
from .orchestration import CaseTrace, TraceSink


async def solve_case(
    case: dict[str, Any],
    gateway: Gateway,
    trace: TraceSink,
    adjudicator_client: StructuredModelClient | None = None,
    adjudicator_model_id: str = QWEN_MODEL_ID,
) -> dict[str, Any]:
    """Run the deterministic specialist workflow and build a verified case output."""
    normalized = normalize_case(case)
    route = build_route_plan(normalized.claims)
    registry = EvidenceRegistry(normalized.case_id)
    lifecycle = CaseTrace(normalized.case_id, trace, registry)
    catalog = ToolCatalog()

    lifecycle.case_received()
    task = make_order_item_task(normalized)
    lifecycle.task_assigned(task)
    report = await investigate_order_items(
        task,
        order_id=normalized.claimed_order_id,
        gateway=gateway,
        catalog=catalog,
        registry=registry,
        trace=lifecycle,
    )
    lifecycle.handoff(report)
    expected_total = next(
        (
            fact.value
            for fact in report.facts
            if fact.fact_code == "ORDER_EXPECTED_TOTAL_BRL"
        ),
        None,
    )
    reports = [report]
    claim_topics = tuple(claim.topic for claim in normalized.claims)
    if route.includes(PAYMENT_AGENT):
        payment_task = make_payment_task(normalized)
        lifecycle.task_assigned(payment_task)
        payment_report = await investigate_payments(
            payment_task,
            order_id=normalized.claimed_order_id,
            claim_topics=claim_topics,
            expected_total_brl=expected_total,
            gateway=gateway,
            catalog=catalog,
            registry=registry,
            trace=lifecycle,
        )
        lifecycle.handoff(payment_report)
        reports.append(payment_report)
    if route.includes(SHIPMENT_AGENT):
        shipment_task = make_shipment_task(normalized)
        lifecycle.task_assigned(shipment_task)
        shipment_report = await investigate_shipment(
            shipment_task,
            order_id=normalized.claimed_order_id,
            gateway=gateway,
            catalog=catalog,
            registry=registry,
            trace=lifecycle,
        )
        lifecycle.handoff(shipment_report)
        reports.append(shipment_report)
    candidates = generate_candidates(normalized, tuple(reports))
    candidate_issue = candidates.issues[0]
    paid_total = next(
        (
            fact.value
            for specialist_report in reports
            for fact in specialist_report.facts
            if fact.fact_code == "PAYMENT_TOTAL_BRL"
        ),
        None,
    )
    if route.includes(POLICY_AGENT):
        policy_task = make_policy_task(normalized)
        lifecycle.task_assigned(policy_task)
        policy_report = await investigate_policy(
            policy_task,
            policy_version=normalized.policy_version,
            issue=candidate_issue,
            paid_total_brl=paid_total,
            gateway=gateway,
            catalog=catalog,
            registry=registry,
            trace=lifecycle,
        )
        lifecycle.handoff(policy_report)
        reports.append(policy_report)
    adjudication_context = build_adjudication_context(
        normalized,
        candidates,
        tuple(reports),
    )
    adjudicator_task = make_adjudicator_task(normalized)
    lifecycle.task_assigned(adjudicator_task)
    adjudication = await adjudicate(
        case_id=normalized.case_id,
        candidates=candidates,
        context=adjudication_context,
        client=adjudicator_client,
        model_id=adjudicator_model_id,
    )
    lifecycle.model_handoff(
        task_id=adjudicator_task.task_id,
        target="output-builder",
        model_id=adjudicator_model_id,
        decision_code=adjudication.decision_code,
        evidence_refs=adjudication.evidence_refs,
        attempts=adjudication.attempts,
    )
    draft = build_rules_draft(
        normalized,
        tuple(reports),
        candidates,
        adjudication.decision,
        adjudication_context.alias_to_evidence_ref,
    )
    verification = verify_draft(
        draft,
        expected_case_id=normalized.case_id,
        expected_claim_ids=tuple(claim.claim_id for claim in normalized.claims),
        registry=registry,
    )
    lifecycle.verification_completed(verification)
    if not verification.passed:
        raise ValueError(f"vertical slice verification failed: {verification.error_codes}")
    lifecycle.case_finalized(evidence_refs=draft.evidence_refs)
    return draft_to_output(draft)
