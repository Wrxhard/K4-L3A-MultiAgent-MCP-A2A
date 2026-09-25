from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .enums import (
    CaseStatus,
    ClaimVerdict,
    ConfidenceBand,
    CriticVerdict,
    EvidenceDomain,
    EvidenceSource,
    MessageType,
    PartyType,
    PrimaryIssue,
    TaskStatus,
    WorkflowState,
)

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
RESULT_HASH_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
CAUSE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique values")


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    topic: str

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim_id")
        _require_text(self.topic, "topic")


@dataclass(frozen=True, slots=True)
class EntityIndex:
    order_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    payment_references: tuple[str, ...] = ()
    shipment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "order_ids",
            "item_ids",
            "seller_ids",
            "payment_references",
            "shipment_ids",
        ):
            values = getattr(self, field_name)
            _require_unique(values, field_name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} cannot contain empty identifiers")


@dataclass(frozen=True, slots=True)
class Fact:
    fact_id: str
    fact_code: str
    value: Any
    source: EvidenceSource
    entity_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.fact_id, "fact_id")
        _require_text(self.fact_code, "fact_code")
        _require_unique(self.evidence_refs, "evidence_refs")
        if self.source is EvidenceSource.MCP and not self.evidence_refs:
            raise ValueError("an MCP fact must reference at least one evidence record")
        if self.source is EvidenceSource.CASE_INPUT and self.evidence_refs:
            raise ValueError("case-input metadata cannot claim MCP evidence")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Conflict:
    field: str
    sources: tuple[str, ...]
    selected_source: str | None
    resolution_code: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.field, "field")
        _require_text(self.resolution_code, "resolution_code")
        _require_unique(self.sources, "sources")
        _require_unique(self.evidence_refs, "evidence_refs")
        if len(self.sources) < 2:
            raise ValueError("a conflict requires at least two distinct sources")
        if self.selected_source is not None and self.selected_source not in self.sources:
            raise ValueError("selected_source must be one of sources")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    case_id: str
    evidence_ref: str
    result_hash: str
    domain: EvidenceDomain
    tool_name: str
    data: Any
    warnings: tuple[str, ...] = ()
    consumed_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("case_id", "evidence_ref", "result_hash", "domain", "tool_name"):
            value = getattr(self, field_name)
            _require_text(value.value if isinstance(value, EvidenceDomain) else value, field_name)
        if not EVIDENCE_REF_PATTERN.fullmatch(self.evidence_ref):
            raise ValueError("evidence_ref does not match the public contract")
        if not RESULT_HASH_PATTERN.fullmatch(self.result_hash):
            raise ValueError("result_hash does not match the public contract")
        _require_unique(self.warnings, "warnings")
        _require_unique(self.consumed_by, "consumed_by")


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    case_id: str
    actor: str
    objective: str
    entity_ids: tuple[str, ...] = ()
    required_fact_codes: tuple[str, ...] = ()
    attempt: int = 1
    status: TaskStatus = TaskStatus.PENDING

    def __post_init__(self) -> None:
        for field_name in ("task_id", "case_id", "actor", "objective"):
            _require_text(getattr(self, field_name), field_name)
        _require_unique(self.entity_ids, "entity_ids")
        _require_unique(self.required_fact_codes, "required_fact_codes")
        if not 1 <= self.attempt <= 2:
            raise ValueError("attempt must be within [1, 2]")


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    case_id: str
    correlation_id: str
    task_id: str
    sender: str
    recipient: str
    message_type: MessageType
    payload: Any
    attempt: int = 1
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "case_id",
            "correlation_id",
            "task_id",
            "sender",
            "recipient",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_unique(self.evidence_refs, "evidence_refs")
        if not 1 <= self.attempt <= 2:
            raise ValueError("attempt must be within [1, 2]")


@dataclass(frozen=True, slots=True)
class SpecialistReport:
    case_id: str
    task_id: str
    actor: str
    status: TaskStatus
    facts: tuple[Fact, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("case_id", "task_id", "actor"):
            _require_text(getattr(self, field_name), field_name)
        _require_unique(self.evidence_refs, "evidence_refs")
        _require_unique(self.warnings, "warnings")
        terminal_statuses = {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIAL,
            TaskStatus.NOT_FOUND,
            TaskStatus.FAILED,
        }
        if self.status not in terminal_statuses:
            raise ValueError("a specialist report must have a terminal status")
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        _require_unique(fact_ids, "facts.fact_id")
        referenced = {ref for fact in self.facts for ref in fact.evidence_refs}
        if not referenced.issubset(set(self.evidence_refs)):
            raise ValueError("fact evidence must be included in report evidence_refs")


@dataclass(frozen=True, slots=True)
class CandidateSet:
    case_id: str
    issues: tuple[PrimaryIssue, ...]
    supporting_fact_ids: tuple[str, ...]
    counter_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_unique(self.issues, "issues")
        _require_unique(self.supporting_fact_ids, "supporting_fact_ids")
        _require_unique(self.counter_fact_ids, "counter_fact_ids")
        if not 1 <= len(self.issues) <= 3:
            raise ValueError("candidate set must contain between one and three issues")


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    claim_id: str
    verdict: ClaimVerdict
    supporting_fact_codes: tuple[str, ...] = ()
    supporting_evidence_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim_id")
        _require_unique(self.supporting_fact_codes, "supporting_fact_codes")
        _require_unique(self.supporting_evidence_aliases, "supporting_evidence_aliases")


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    case_id: str
    selected_issue: PrimaryIssue
    claim_decisions: tuple[ClaimDecision, ...]
    supporting_fact_codes: tuple[str, ...]
    supporting_evidence_aliases: tuple[str, ...]
    confidence_band: ConfidenceBand

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if not isinstance(self.selected_issue, PrimaryIssue):
            raise ValueError("selected_issue must be a PrimaryIssue")
        _require_unique(
            tuple(item.claim_id for item in self.claim_decisions), "claim_decisions.claim_id"
        )
        _require_unique(self.supporting_fact_codes, "supporting_fact_codes")
        _require_unique(self.supporting_evidence_aliases, "supporting_evidence_aliases")


@dataclass(frozen=True, slots=True)
class CriticReport:
    case_id: str
    verdict: CriticVerdict
    error_codes: tuple[str, ...] = ()
    challenged_fields: tuple[str, ...] = ()
    recommended_confidence_cap: Decimal | None = None

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_unique(self.error_codes, "error_codes")
        _require_unique(self.challenged_fields, "challenged_fields")
        if self.verdict is CriticVerdict.PASS and self.error_codes:
            raise ValueError("a passing critic report cannot contain errors")
        if self.verdict is CriticVerdict.REJECT and not self.error_codes:
            raise ValueError("a rejected critic report must contain an error code")
        if self.recommended_confidence_cap is not None and not (
            Decimal("0") <= self.recommended_confidence_cap <= Decimal("1")
        ):
            raise ValueError("recommended_confidence_cap must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class ClaimAssessment:
    claim_id: str
    verdict: ClaimVerdict
    confidence: Decimal
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim_id")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("claim confidence must be within [0, 1]")
        _require_unique(self.evidence_refs, "evidence_refs")


@dataclass(frozen=True, slots=True)
class RankedCause:
    cause_code: str
    rank: int

    def __post_init__(self) -> None:
        _require_text(self.cause_code, "cause_code")
        if not CAUSE_CODE_PATTERN.fullmatch(self.cause_code):
            raise ValueError("cause_code does not match the public contract")
        if not 1 <= self.rank <= 5:
            raise ValueError("rank must be within [1, 5]")


@dataclass(frozen=True, slots=True)
class ResponsibleParty:
    party_type: PartyType
    party_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.party_type, PartyType):
            raise ValueError("party_type must be a PartyType")


@dataclass(frozen=True, slots=True)
class RefundLine:
    reason_code: str
    amount_brl: Decimal
    entity_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.reason_code, "reason_code")
        if self.amount_brl < 0:
            raise ValueError("refund line amount cannot be negative")


@dataclass(frozen=True, slots=True)
class DraftAssessment:
    case_id: str
    primary_issue: PrimaryIssue
    case_status: CaseStatus
    confidence: Decimal
    claim_assessments: tuple[ClaimAssessment, ...]
    entities: EntityIndex
    ranked_causes: tuple[RankedCause, ...]
    responsible_parties: tuple[ResponsibleParty, ...]
    evidence_refs: tuple[str, ...]
    conflicts: tuple[Conflict, ...]
    recommended_refund_brl: Decimal
    refund_lines: tuple[RefundLine, ...]
    resolution_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if not isinstance(self.primary_issue, PrimaryIssue):
            raise ValueError("primary_issue must be a PrimaryIssue")
        if not isinstance(self.case_status, CaseStatus):
            raise ValueError("case_status must be a CaseStatus")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be within [0, 1]")
        if self.recommended_refund_brl < 0:
            raise ValueError("recommended_refund_brl cannot be negative")
        _require_unique(self.evidence_refs, "evidence_refs")
        _require_unique(self.resolution_actions, "resolution_actions")
        _require_unique(
            tuple(item.claim_id for item in self.claim_assessments),
            "claim_assessments.claim_id",
        )
        ranks = tuple(cause.rank for cause in self.ranked_causes)
        _require_unique(tuple(str(rank) for rank in ranks), "ranked_causes.rank")
        cause_codes = tuple(cause.cause_code for cause in self.ranked_causes)
        _require_unique(cause_codes, "ranked_causes.cause_code")
        claim_refs = {ref for claim in self.claim_assessments for ref in claim.evidence_refs}
        if not claim_refs.issubset(set(self.evidence_refs)):
            raise ValueError("claim evidence must be included in draft evidence_refs")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    case_id: str
    passed: bool
    error_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    required_followup_fact_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_unique(self.error_codes, "error_codes")
        _require_unique(self.warnings, "warnings")
        _require_unique(self.required_followup_fact_codes, "required_followup_fact_codes")
        if self.passed and self.error_codes:
            raise ValueError("a passing verification report cannot contain errors")
        if not self.passed and not self.error_codes:
            raise ValueError("a failed verification report must contain an error code")


@dataclass(frozen=True, slots=True)
class CaseWorkspace:
    case_id: str
    opened_at: datetime
    policy_version: str
    claims: tuple[Claim, ...]
    entities: EntityIndex
    state: WorkflowState = WorkflowState.RECEIVED
    tasks: tuple[AgentTask, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    reports: tuple[SpecialistReport, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    revision_count: int = 0

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.policy_version, "policy_version")
        if self.opened_at.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        if not 0 <= self.revision_count <= 1:
            raise ValueError("revision_count must be within [0, 1]")
        _require_unique(tuple(claim.claim_id for claim in self.claims), "claims.claim_id")
        _require_unique(tuple(task.task_id for task in self.tasks), "tasks.task_id")
        _require_unique(
            tuple(record.evidence_ref for record in self.evidence), "evidence.evidence_ref"
        )
        if any(task.case_id != self.case_id for task in self.tasks):
            raise ValueError("all tasks must belong to the workspace case")
        if any(record.case_id != self.case_id for record in self.evidence):
            raise ValueError("all evidence must belong to the workspace case")
        if any(report.case_id != self.case_id for report in self.reports):
            raise ValueError("all reports must belong to the workspace case")
