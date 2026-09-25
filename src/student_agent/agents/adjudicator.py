from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from student_agent.domain import (
    CandidateSet,
    ClaimDecision,
    ClaimVerdict,
    ConfidenceBand,
    PrimaryIssue,
    SemanticDecision,
)
from student_agent.models import AdjudicationContext, StructuredModelClient

QWEN_MODEL_ID = "Qwen/Qwen3-4B"
SYSTEM_PROMPT = """You are a bounded semantic adjudicator.
Return exactly one JSON object matching output_contract. Select only supplied candidates,
fact codes, evidence aliases, claim IDs, verdicts, and confidence bands. Do not calculate
money, create identifiers, request tools, quote customer text, or provide reasoning.
"""


class AdjudicationError(ValueError):
    """Raised when model output violates the bounded semantic contract."""


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    decision: SemanticDecision
    evidence_refs: tuple[str, ...]
    used_fallback: bool
    attempts: int
    decision_code: str


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AdjudicationError(f"{field} must be an array of strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise AdjudicationError(f"{field} must contain unique values")
    return result


def validate_adjudication(
    value: dict[str, Any],
    *,
    case_id: str,
    candidates: CandidateSet,
    context: AdjudicationContext,
) -> SemanticDecision:
    expected_keys = {
        "selected_issue",
        "claim_decisions",
        "supporting_fact_codes",
        "supporting_evidence_aliases",
        "confidence_band",
    }
    if set(value) != expected_keys:
        raise AdjudicationError("adjudicator response has unexpected or missing fields")
    try:
        selected_issue = PrimaryIssue(value["selected_issue"])
        confidence_band = ConfidenceBand(value["confidence_band"])
    except (TypeError, ValueError) as exc:
        raise AdjudicationError("adjudicator returned an unknown enum value") from exc
    if selected_issue not in candidates.issues:
        raise AdjudicationError("selected_issue is outside the candidate set")

    fact_codes = _string_tuple(value["supporting_fact_codes"], "supporting_fact_codes")
    aliases = _string_tuple(
        value["supporting_evidence_aliases"], "supporting_evidence_aliases"
    )
    if not set(fact_codes).issubset(context.allowed_fact_codes):
        raise AdjudicationError("response cites an unknown fact code")
    if not set(aliases).issubset(context.alias_to_evidence_ref):
        raise AdjudicationError("response cites an unknown evidence alias")

    raw_claims = value["claim_decisions"]
    if not isinstance(raw_claims, list):
        raise AdjudicationError("claim_decisions must be an array")
    decisions: list[ClaimDecision] = []
    for raw in raw_claims:
        if not isinstance(raw, dict) or set(raw) != {
            "claim_id",
            "verdict",
            "supporting_fact_codes",
            "supporting_evidence_aliases",
        }:
            raise AdjudicationError("claim decision has unexpected or missing fields")
        try:
            verdict = ClaimVerdict(raw["verdict"])
        except (TypeError, ValueError) as exc:
            raise AdjudicationError("claim decision has an unknown verdict") from exc
        claim_fact_codes = _string_tuple(
            raw["supporting_fact_codes"], "claim.supporting_fact_codes"
        )
        claim_aliases = _string_tuple(
            raw["supporting_evidence_aliases"], "claim.supporting_evidence_aliases"
        )
        if not set(claim_fact_codes).issubset(context.allowed_fact_codes):
            raise AdjudicationError("claim cites an unknown fact code")
        if not set(claim_aliases).issubset(context.alias_to_evidence_ref):
            raise AdjudicationError("claim cites an unknown evidence alias")
        if verdict is ClaimVerdict.SUPPORTED and (not claim_fact_codes or not claim_aliases):
            raise AdjudicationError("a supported claim requires fact and evidence support")
        decisions.append(
            ClaimDecision(
                claim_id=raw.get("claim_id"),
                verdict=verdict,
                supporting_fact_codes=claim_fact_codes,
                supporting_evidence_aliases=claim_aliases,
            )
        )
    if tuple(decision.claim_id for decision in decisions) != context.claim_ids:
        raise AdjudicationError("claim decisions must match input claim order exactly")
    if selected_issue is not PrimaryIssue.INSUFFICIENT_EVIDENCE and (
        not fact_codes or not aliases
    ):
        raise AdjudicationError("a substantive issue requires fact and evidence support")
    return SemanticDecision(
        case_id=case_id,
        selected_issue=selected_issue,
        claim_decisions=tuple(decisions),
        supporting_fact_codes=fact_codes,
        supporting_evidence_aliases=aliases,
        confidence_band=confidence_band,
    )


def fallback_decision(
    *, case_id: str, candidates: CandidateSet, context: AdjudicationContext
) -> SemanticDecision:
    selected_issue = candidates.issues[0]
    fact_codes = tuple(
        dict.fromkeys(
            context.fact_id_to_code[fact_id]
            for fact_id in candidates.supporting_fact_ids
            if fact_id in context.fact_id_to_code
        )
    )
    aliases = tuple(context.alias_to_evidence_ref)
    claims = tuple(
        ClaimDecision(
            claim_id=claim_id,
            verdict=(
                ClaimVerdict.SUPPORTED
                if context.payload["claims"][index]["topic"] == selected_issue.value
                else ClaimVerdict.INSUFFICIENT_EVIDENCE
            ),
            supporting_fact_codes=fact_codes,
            supporting_evidence_aliases=aliases,
        )
        for index, claim_id in enumerate(context.claim_ids)
    )
    return SemanticDecision(
        case_id=case_id,
        selected_issue=selected_issue,
        claim_decisions=claims,
        supporting_fact_codes=fact_codes,
        supporting_evidence_aliases=aliases,
        confidence_band=(
            ConfidenceBand.INSUFFICIENT
            if selected_issue is PrimaryIssue.INSUFFICIENT_EVIDENCE
            else ConfidenceBand.MEDIUM
        ),
    )


async def adjudicate(
    *,
    case_id: str,
    candidates: CandidateSet,
    context: AdjudicationContext,
    client: StructuredModelClient | None,
    model_id: str = QWEN_MODEL_ID,
    revision_feedback: tuple[str, ...] = (),
) -> AdjudicationResult:
    fallback_code = "ADJUDICATOR_FALLBACK"
    if client is not None:
        for attempt in (1, 2):
            payload = dict(context.payload)
            if revision_feedback:
                payload["revision_feedback"] = list(revision_feedback)
            if attempt == 2:
                payload["repair_instruction"] = (
                    "Previous response violated the contract. Return only a corrected JSON object."
                )
            try:
                raw = await client.generate_json(
                    model_id=model_id,
                    system_prompt=SYSTEM_PROMPT,
                    payload=payload,
                    temperature=0.0,
                    max_tokens=900,
                )
                decision = validate_adjudication(
                    raw,
                    case_id=case_id,
                    candidates=candidates,
                    context=context,
                )
                refs = tuple(
                    context.alias_to_evidence_ref[alias]
                    for alias in decision.supporting_evidence_aliases
                )
                return AdjudicationResult(
                    decision=decision,
                    evidence_refs=refs,
                    used_fallback=False,
                    attempts=attempt,
                    decision_code="ADJUDICATION_ACCEPTED",
                )
            except AdjudicationError:
                fallback_code = "ADJUDICATOR_CONSTRAINT_VIOLATION"
                continue
            except Exception:
                fallback_code = "ADJUDICATOR_FALLBACK"
                continue
    decision = fallback_decision(case_id=case_id, candidates=candidates, context=context)
    refs = tuple(
        context.alias_to_evidence_ref[alias]
        for alias in decision.supporting_evidence_aliases
    )
    return AdjudicationResult(
        decision=decision,
        evidence_refs=refs,
        used_fallback=True,
        attempts=2 if client is not None else 0,
        decision_code=fallback_code,
    )
