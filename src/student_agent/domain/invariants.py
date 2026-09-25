from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from .enums import WorkflowState
from .models import DraftAssessment, EvidenceRecord


class InvariantError(ValueError):
    """Raised when a deterministic domain invariant is violated."""


ALLOWED_STATE_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.RECEIVED: frozenset({WorkflowState.PLANNING, WorkflowState.FAILED}),
    WorkflowState.PLANNING: frozenset({WorkflowState.COLLECTING_EVIDENCE, WorkflowState.FAILED}),
    WorkflowState.COLLECTING_EVIDENCE: frozenset(
        {WorkflowState.POLICY_CHECK, WorkflowState.DEGRADED, WorkflowState.FAILED}
    ),
    WorkflowState.DEGRADED: frozenset(
        {WorkflowState.POLICY_CHECK, WorkflowState.GENERATING_CANDIDATES, WorkflowState.FAILED}
    ),
    WorkflowState.POLICY_CHECK: frozenset(
        {WorkflowState.GENERATING_CANDIDATES, WorkflowState.DEGRADED, WorkflowState.FAILED}
    ),
    WorkflowState.GENERATING_CANDIDATES: frozenset(
        {WorkflowState.MODEL_ADJUDICATING, WorkflowState.FAILED}
    ),
    WorkflowState.MODEL_ADJUDICATING: frozenset(
        {WorkflowState.BUILDING_DRAFT, WorkflowState.FAILED}
    ),
    WorkflowState.BUILDING_DRAFT: frozenset({WorkflowState.MODEL_CRITIQUING, WorkflowState.FAILED}),
    WorkflowState.MODEL_CRITIQUING: frozenset(
        {WorkflowState.REVISING, WorkflowState.VERIFYING, WorkflowState.FAILED}
    ),
    WorkflowState.REVISING: frozenset(
        {WorkflowState.MODEL_CRITIQUING, WorkflowState.VERIFYING, WorkflowState.FAILED}
    ),
    WorkflowState.VERIFYING: frozenset({WorkflowState.FINALIZED, WorkflowState.FAILED}),
    WorkflowState.FINALIZED: frozenset(),
    WorkflowState.FAILED: frozenset(),
}


def parse_decimal(value: str | int | float | Decimal, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise InvariantError(f"{field_name} must be a decimal value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvariantError(f"{field_name} must be a decimal value") from exc
    if not result.is_finite():
        raise InvariantError(f"{field_name} must be finite")
    return result


def require_non_negative(value: Decimal, *, field_name: str) -> None:
    if value < 0:
        raise InvariantError(f"{field_name} cannot be negative")


def require_case_scope(expected_case_id: str, records: Iterable[Any]) -> None:
    mismatched = [
        getattr(record, "case_id", None)
        for record in records
        if getattr(record, "case_id", None) != expected_case_id
    ]
    if mismatched:
        raise InvariantError(
            f"records must belong to case {expected_case_id!r}; mismatched={mismatched!r}"
        )


def require_known_evidence_refs(
    evidence_refs: Iterable[str], records: Iterable[EvidenceRecord]
) -> None:
    known = {record.evidence_ref for record in records}
    unknown = sorted(set(evidence_refs) - known)
    if unknown:
        raise InvariantError(f"unknown evidence references: {unknown}")


def require_consumed_evidence_refs(
    evidence_refs: Iterable[str], records: Iterable[EvidenceRecord]
) -> None:
    by_ref = {record.evidence_ref: record for record in records}
    requested = set(evidence_refs)
    unknown = sorted(requested - set(by_ref))
    if unknown:
        raise InvariantError(f"unknown evidence references: {unknown}")
    unconsumed = sorted(ref for ref in requested if not by_ref[ref].consumed_by)
    if unconsumed:
        raise InvariantError(f"unconsumed evidence references: {unconsumed}")


def require_subset(subset: Iterable[str], superset: Iterable[str], *, label: str) -> None:
    missing = sorted(set(subset) - set(superset))
    if missing:
        raise InvariantError(f"{label} contains values outside its parent set: {missing}")


def require_refund_total(draft: DraftAssessment) -> None:
    line_total = sum((line.amount_brl for line in draft.refund_lines), Decimal("0"))
    if line_total != draft.recommended_refund_brl:
        raise InvariantError(
            "refund line total does not equal recommended_refund_brl: "
            f"{line_total} != {draft.recommended_refund_brl}"
        )


def require_state_transition(current: WorkflowState, target: WorkflowState) -> None:
    if target not in ALLOWED_STATE_TRANSITIONS[current]:
        raise InvariantError(f"invalid workflow transition: {current.value} -> {target.value}")
