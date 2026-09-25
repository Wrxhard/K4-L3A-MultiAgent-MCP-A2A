from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from student_agent.domain import DraftAssessment

from .contracts import AdjudicationContext

ALLOWED_CRITIC_ERROR_CODES = frozenset(
    {
        "SEMANTIC_ISSUE_UNSUPPORTED",
        "CLAIM_VERDICT_UNSUPPORTED",
        "POLICY_CONFLICT",
        "RESPONSIBILITY_UNSUPPORTED",
        "ACTION_CONFLICT",
        "CONFIDENCE_TOO_HIGH",
        "MISSING_REQUIRED_EVIDENCE",
    }
)
ALLOWED_CHALLENGED_FIELDS = frozenset(
    {
        "assessment.primary_issue",
        "assessment.case_status",
        "assessment.confidence",
        "claim_assessments",
        "root_cause_analysis",
        "financial_resolution",
        "resolution_actions",
    }
)
ACTION_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


@dataclass(frozen=True, slots=True)
class CriticContext:
    payload: dict[str, Any]


def build_critic_context(
    draft: DraftAssessment,
    adjudication: AdjudicationContext,
    *,
    deterministic_error_codes: tuple[str, ...] = (),
) -> CriticContext:
    payload = {
        "draft": {
            "primary_issue": draft.primary_issue.value,
            "case_status": draft.case_status.value,
            "confidence": str(draft.confidence),
            "claim_decisions": [
                {"claim_id": item.claim_id, "verdict": item.verdict.value}
                for item in draft.claim_assessments
            ],
            "cause_codes": [cause.cause_code for cause in draft.ranked_causes],
            "responsible_party_types": [
                party.party_type.value for party in draft.responsible_parties
            ],
            "has_refund": draft.recommended_refund_brl > 0,
            "resolution_actions": [
                action
                for action in draft.resolution_actions
                if ACTION_CODE_PATTERN.fullmatch(action)
            ],
        },
        "candidate_issues": adjudication.payload["candidate_issues"],
        "facts": adjudication.payload["facts"],
        "deterministic_error_codes": list(deterministic_error_codes),
        "output_contract": {
            "verdict": {"allowed": ["pass", "reject"]},
            "error_codes": {"allowed": sorted(ALLOWED_CRITIC_ERROR_CODES)},
            "challenged_fields": {"allowed": sorted(ALLOWED_CHALLENGED_FIELDS)},
            "recommended_confidence_cap": "null or decimal between 0 and 1",
        },
    }
    return CriticContext(payload=payload)
