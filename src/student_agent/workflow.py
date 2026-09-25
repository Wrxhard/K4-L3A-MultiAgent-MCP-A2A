from __future__ import annotations

from typing import Any

from .agents import (
    Gateway,
    build_order_only_draft,
    draft_to_output,
    investigate_order_items,
    make_order_item_task,
    normalize_case,
    verify_draft,
)
from .evidence import EvidenceRegistry, ToolCatalog
from .orchestration import CaseTrace, TraceSink


async def solve_case(
    case: dict[str, Any], gateway: Gateway, trace: TraceSink
) -> dict[str, Any]:
    """Run the Phase-3 order/item vertical slice.

    Payment and policy specialists are intentionally not inferred here. Until those
    phases are integrated, the slice emits a verified insufficient-evidence result.
    """
    normalized = normalize_case(case)
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
    draft = build_order_only_draft(normalized, report)
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
