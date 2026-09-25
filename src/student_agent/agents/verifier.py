from __future__ import annotations

from student_agent.domain import (
    DraftAssessment,
    InvariantError,
    VerificationReport,
    require_refund_total,
    require_subset,
)
from student_agent.evidence import EvidenceRegistry, EvidenceRegistryError


def verify_draft(
    draft: DraftAssessment,
    *,
    expected_case_id: str,
    expected_claim_ids: tuple[str, ...],
    registry: EvidenceRegistry,
) -> VerificationReport:
    errors: list[str] = []
    if draft.case_id != expected_case_id:
        errors.append("CASE_ID_MISMATCH")
    actual_claim_ids = tuple(item.claim_id for item in draft.claim_assessments)
    if set(actual_claim_ids) != set(expected_claim_ids):
        errors.append("CLAIM_SET_MISMATCH")
    try:
        registry.validate_refs(draft.evidence_refs, require_consumed=True)
    except EvidenceRegistryError:
        errors.append("INVALID_EVIDENCE_REFS")
    try:
        require_refund_total(draft)
    except InvariantError:
        errors.append("REFUND_TOTAL_MISMATCH")
    for claim in draft.claim_assessments:
        try:
            require_subset(
                claim.evidence_refs,
                draft.evidence_refs,
                label=f"claim {claim.claim_id} evidence",
            )
        except InvariantError:
            errors.append("CLAIM_EVIDENCE_OUTSIDE_OUTPUT")
            break

    unique_errors = tuple(dict.fromkeys(errors))
    return VerificationReport(
        case_id=expected_case_id,
        passed=not unique_errors,
        error_codes=unique_errors,
    )
