from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from student_agent.domain import CriticReport, CriticVerdict
from student_agent.models import (
    ALLOWED_CHALLENGED_FIELDS,
    ALLOWED_CRITIC_ERROR_CODES,
    CriticContext,
    StructuredModelClient,
)

PHI_MODEL_ID = "microsoft/Phi-4-mini-instruct"
SYSTEM_PROMPT = """You are an independent bounded critic.
Inspect only the supplied sanitized draft, candidate issues, facts, and deterministic
checks. Return exactly one JSON object matching output_contract. Use only allowed error
codes and challenged fields. Do not rewrite the answer, calculate money, call tools,
create identifiers, or provide reasoning.
"""


class CriticContractError(ValueError):
    """Raised when a critic response exceeds its bounded authority."""


@dataclass(frozen=True, slots=True)
class CriticResult:
    report: CriticReport
    used_fallback: bool
    attempts: int
    decision_code: str


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CriticContractError(f"{field} must be an array of strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise CriticContractError(f"{field} must contain unique values")
    return result


def validate_critic_response(value: dict[str, Any], *, case_id: str) -> CriticReport:
    if set(value) != {
        "verdict",
        "error_codes",
        "challenged_fields",
        "recommended_confidence_cap",
    }:
        raise CriticContractError("critic response has unexpected or missing fields")
    try:
        verdict = CriticVerdict(value["verdict"])
    except (TypeError, ValueError) as exc:
        raise CriticContractError("critic returned an unknown verdict") from exc
    error_codes = _strings(value["error_codes"], "error_codes")
    fields = _strings(value["challenged_fields"], "challenged_fields")
    if not set(error_codes).issubset(ALLOWED_CRITIC_ERROR_CODES):
        raise CriticContractError("critic returned an unknown error code")
    if not set(fields).issubset(ALLOWED_CHALLENGED_FIELDS):
        raise CriticContractError("critic challenged an unknown field")
    raw_cap = value["recommended_confidence_cap"]
    try:
        cap = None if raw_cap is None else Decimal(str(raw_cap))
    except (InvalidOperation, ValueError) as exc:
        raise CriticContractError("confidence cap must be a decimal or null") from exc
    if cap is not None and (not cap.is_finite() or not Decimal("0") <= cap <= Decimal("1")):
        raise CriticContractError("confidence cap must be within [0, 1]")
    try:
        return CriticReport(
            case_id=case_id,
            verdict=verdict,
            error_codes=error_codes,
            challenged_fields=fields,
            recommended_confidence_cap=cap,
        )
    except ValueError as exc:
        raise CriticContractError(str(exc)) from exc


def deterministic_critic(
    *, case_id: str, context: CriticContext
) -> CriticReport:
    deterministic_errors = tuple(context.payload["deterministic_error_codes"])
    allowed_errors = tuple(
        code for code in deterministic_errors if code in ALLOWED_CRITIC_ERROR_CODES
    )
    if allowed_errors:
        return CriticReport(
            case_id=case_id,
            verdict=CriticVerdict.REJECT,
            error_codes=allowed_errors,
            challenged_fields=(),
            recommended_confidence_cap=Decimal("0.50"),
        )
    return CriticReport(case_id=case_id, verdict=CriticVerdict.PASS)


async def critique(
    *,
    case_id: str,
    context: CriticContext,
    client: StructuredModelClient | None,
    model_id: str = PHI_MODEL_ID,
) -> CriticResult:
    fallback_code = "CRITIC_FALLBACK"
    if client is not None:
        for attempt in (1, 2):
            payload = dict(context.payload)
            if attempt == 2:
                payload["repair_instruction"] = (
                    "Previous response violated the contract. Return only corrected JSON."
                )
            try:
                raw = await client.generate_json(
                    model_id=model_id,
                    system_prompt=SYSTEM_PROMPT,
                    payload=payload,
                    temperature=0.0,
                    max_tokens=500,
                )
                report = validate_critic_response(raw, case_id=case_id)
                return CriticResult(
                    report=report,
                    used_fallback=False,
                    attempts=attempt,
                    decision_code=(
                        "CRITIC_ACCEPTED"
                        if report.verdict is CriticVerdict.PASS
                        else "CRITIC_REVISION_REQUIRED"
                    ),
                )
            except CriticContractError:
                fallback_code = "CRITIC_CONSTRAINT_VIOLATION"
                continue
            except Exception:
                fallback_code = "CRITIC_FALLBACK"
                continue
    report = deterministic_critic(case_id=case_id, context=context)
    return CriticResult(
        report=report,
        used_fallback=True,
        attempts=2 if client is not None else 0,
        decision_code=fallback_code,
    )
