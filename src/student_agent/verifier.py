from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from langchain_core.runnables import Runnable, RunnableLambda

from .agent_contracts import (
    ACTORS,
    OrderItemPayload,
    PaymentPayload,
    ShipmentPayload,
    VerificationPackage,
    VerificationReport,
    validate_verification_report,
)
from .contracts import ContractError, Contracts

POLICY_SCHEMA_FIELDS = {
    "assessment",
    "claim_assessments",
    "root_cause_analysis",
    "data_conflicts",
    "financial_resolution",
    "resolution_actions",
}
ENTITY_FIELDS = (
    "order_ids",
    "item_ids",
    "seller_ids",
    "payment_references",
    "shipment_ids",
)


class DeterministicVerifier:
    def __init__(
        self,
        contracts: Contracts,
        semantic_verifier: Runnable[VerificationPackage, VerificationReport] | None = None,
    ) -> None:
        self._contracts = contracts
        self._semantic_verifier = semantic_verifier

    def as_runnable(self) -> Runnable[VerificationPackage, VerificationReport]:
        return RunnableLambda(self.verify)

    async def verify(self, package: VerificationPackage) -> VerificationReport:
        failure_report = self._agent_failure_report(package)
        if failure_report is not None:
            return failure_report
        candidate = package.candidate_output
        if candidate is None:
            return self._terminal("CANDIDATE_MISSING", "No candidate output was assembled")
        if candidate.get("case_id") != package.case_id:
            return self._terminal("CASE_ID_MISMATCH", "Candidate case_id does not match")
        schema_report = self._schema_report(package, candidate)
        if schema_report is not None:
            return schema_report
        if list(candidate["evidence_refs"]) != self._expected_evidence(package):
            return self._terminal(
                "EVIDENCE_MERGE_MISMATCH",
                "Candidate evidence does not match active agent evidence",
            )
        if candidate["affected_entities"] != self._expected_entities(package):
            return self._terminal(
                "ENTITY_MERGE_MISMATCH",
                "Candidate entities do not match active specialist entities",
            )
        candidate_evidence = set(candidate["evidence_refs"])
        for claim in candidate.get("claim_assessments", []):
            if not set(claim["evidence_refs"]).issubset(candidate_evidence):
                return self._policy_retry(
                    package,
                    "CLAIM_EVIDENCE_MISMATCH",
                    "A claim cites evidence outside candidate evidence_refs",
                )
        financial = candidate["financial_resolution"]
        expected_total = sum(
            (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
            Decimal(0),
        )
        if expected_total != Decimal(str(financial["recommended_refund_brl"])):
            return self._policy_retry(
                package,
                "REFUND_TOTAL_MISMATCH",
                "recommended_refund_brl does not equal refund line total",
            )
        for conflict in candidate["data_conflicts"]:
            selected = conflict["selected_source"]
            if selected is not None and selected not in conflict["sources"]:
                return self._policy_retry(
                    package,
                    "INVALID_CONFLICT_SELECTION",
                    "A selected conflict source is not listed in sources",
                )
        ranks = [
            cause["rank"]
            for cause in candidate["root_cause_analysis"]["ranked_causes"]
        ]
        if len(ranks) != len(set(ranks)):
            return self._policy_retry(
                package,
                "DUPLICATE_ROOT_CAUSE_RANK",
                "Root-cause ranks must be unique",
            )
        if self._semantic_verifier is not None:
            report = await self._semantic_verifier.ainvoke(package)
            validate_verification_report(report)
            return report
        return VerificationReport.passed()

    def _agent_failure_report(
        self, package: VerificationPackage
    ) -> VerificationReport | None:
        failures = {failure.actor: failure for failure in package.unresolved_failures}
        for actor in ACTORS:
            failure = failures.get(actor)
            if failure is None:
                continue
            if failure.retryable and failure.retry_count_for_context < 1:
                return VerificationReport.retry(
                    actor,
                    failure.error_code or "AGENT_RESULT_FAILED",
                    failure.safe_detail or f"Retry {actor} after failed result",
                )
            return self._terminal(
                "AGENT_FAILURE_NOT_RECOVERABLE",
                f"{actor} failed without remaining retry allowance",
            )
        return None

    def _schema_report(
        self, package: VerificationPackage, candidate: Mapping[str, Any]
    ) -> VerificationReport | None:
        try:
            self._contracts.validate_output(candidate, "candidate")
        except ContractError as exc:
            message = str(exc)
            location = self._schema_location(message)
            top_level = location.split(".", 1)[0]
            owns_error = top_level in POLICY_SCHEMA_FIELDS or any(
                f"'{field}' is required" in message for field in POLICY_SCHEMA_FIELDS
            )
            if owns_error:
                return self._policy_retry(
                    package,
                    "POLICY_OUTPUT_SCHEMA_INVALID",
                    f"Policy output violates schema at {location}",
                )
            return self._terminal(
                "CANDIDATE_SCHEMA_INVALID",
                f"Coordinator-owned candidate field violates schema at {location}",
            )
        return None

    @staticmethod
    def _schema_location(message: str) -> str:
        parts = message.split(":", 2)
        return parts[1] if len(parts) > 2 else "$"

    def _policy_retry(
        self,
        package: VerificationPackage,
        error_code: str,
        feedback: str,
    ) -> VerificationReport:
        result = package.active_results.get("policy")
        if result is None or result.retry_count_for_context >= 1:
            return self._terminal(
                "POLICY_RETRY_EXHAUSTED",
                f"{error_code}: Policy has no remaining same-context retry",
            )
        return VerificationReport.retry("policy", error_code, feedback)

    @staticmethod
    def _terminal(error_code: str, feedback: str) -> VerificationReport:
        return VerificationReport.failed(error_code, feedback)

    @staticmethod
    def _expected_evidence(package: VerificationPackage) -> list[str]:
        return list(
            dict.fromkeys(
                evidence_ref
                for actor in ACTORS
                if (result := package.active_results.get(actor)) is not None
                for evidence_ref in result.evidence_refs
            )
        )

    @staticmethod
    def _expected_entities(package: VerificationPackage) -> dict[str, list[str]]:
        payloads: list[OrderItemPayload | PaymentPayload | ShipmentPayload] = []
        expected_types = {
            "order_item": OrderItemPayload,
            "payment": PaymentPayload,
            "shipment": ShipmentPayload,
        }
        for actor, expected_type in expected_types.items():
            result = package.active_results.get(actor)  # type: ignore[arg-type]
            if result is not None and isinstance(result.payload, expected_type):
                payloads.append(result.payload)
        return {
            field: list(
                dict.fromkeys(
                    value
                    for payload in payloads
                    for value in getattr(payload.entities, field)
                )
            )
            for field in ENTITY_FIELDS
        }
