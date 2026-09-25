from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from student_agent.domain import EvidenceRecord, EvidenceSource, Fact


class EvidenceRegistryError(ValueError):
    """Raised when evidence registration or linkage violates provenance rules."""


class EvidenceRegistry:
    """Case-scoped, append-only storage for authoritative MCP evidence."""

    def __init__(self, case_id: str) -> None:
        if not case_id or not case_id.strip():
            raise EvidenceRegistryError("case_id must be a non-empty string")
        self.case_id = case_id
        self._records: dict[str, EvidenceRecord] = {}
        self._fact_links: dict[str, tuple[str, ...]] = {}

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(deepcopy(record) for record in self._records.values())

    def register(self, record: EvidenceRecord) -> None:
        if record.case_id != self.case_id:
            raise EvidenceRegistryError(
                f"cross-case evidence rejected: registry={self.case_id!r}, "
                f"record={record.case_id!r}"
            )
        if record.evidence_ref in self._records:
            raise EvidenceRegistryError(f"evidence_ref already registered: {record.evidence_ref}")
        if record.consumed_by:
            raise EvidenceRegistryError("new evidence cannot have pre-existing consumers")
        self._records[record.evidence_ref] = deepcopy(record)

    def get(self, evidence_ref: str) -> EvidenceRecord:
        try:
            return deepcopy(self._records[evidence_ref])
        except KeyError as exc:
            raise EvidenceRegistryError(f"unknown evidence_ref: {evidence_ref}") from exc

    def mark_consumed(self, evidence_ref: str, actor: str) -> EvidenceRecord:
        if not actor or not actor.strip():
            raise EvidenceRegistryError("actor must be a non-empty string")
        record = self.get(evidence_ref)
        if actor in record.consumed_by:
            return record
        updated = replace(record, consumed_by=(*record.consumed_by, actor))
        self._records[evidence_ref] = updated
        return deepcopy(updated)

    def validate_refs(self, evidence_refs: tuple[str, ...], *, require_consumed: bool) -> None:
        unknown = sorted(set(evidence_refs) - set(self._records))
        if unknown:
            raise EvidenceRegistryError(f"unknown evidence references: {unknown}")
        if require_consumed:
            unconsumed = sorted(
                ref for ref in set(evidence_refs) if not self._records[ref].consumed_by
            )
            if unconsumed:
                raise EvidenceRegistryError(f"unconsumed evidence references: {unconsumed}")

    def link_fact(self, fact: Fact) -> None:
        if fact.source is not EvidenceSource.MCP:
            raise EvidenceRegistryError("only MCP facts can be linked to evidence")
        self.validate_refs(fact.evidence_refs, require_consumed=True)
        existing = self._fact_links.get(fact.fact_id)
        if existing is not None and existing != fact.evidence_refs:
            raise EvidenceRegistryError(
                f"fact already linked to different evidence: {fact.fact_id}"
            )
        self._fact_links[fact.fact_id] = fact.evidence_refs

    def refs_for_fact(self, fact_id: str) -> tuple[str, ...]:
        try:
            return self._fact_links[fact_id]
        except KeyError as exc:
            raise EvidenceRegistryError(f"fact has no evidence linkage: {fact_id}") from exc
