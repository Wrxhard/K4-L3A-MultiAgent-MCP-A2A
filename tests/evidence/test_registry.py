import pytest

from student_agent.domain import EvidenceDomain, EvidenceRecord, EvidenceSource, Fact
from student_agent.evidence import EvidenceRegistry, EvidenceRegistryError

EVIDENCE_REF = "ev_abcdefghijklmnopqrstuvwxyz"


def record(*, case_id: str = "CASE_001", data: object | None = None) -> EvidenceRecord:
    return EvidenceRecord(
        case_id=case_id,
        evidence_ref=EVIDENCE_REF,
        result_hash="sha256:" + "a" * 64,
        domain=EvidenceDomain.ORDER,
        tool_name="get_order",
        data=data if data is not None else {"order_status": "canceled"},
    )


def test_registry_rejects_cross_case_evidence() -> None:
    registry = EvidenceRegistry("CASE_001")
    with pytest.raises(EvidenceRegistryError, match="cross-case"):
        registry.register(record(case_id="CASE_002"))


def test_registry_is_append_only() -> None:
    registry = EvidenceRegistry("CASE_001")
    registry.register(record())
    with pytest.raises(EvidenceRegistryError, match="already registered"):
        registry.register(record())


def test_registry_rejects_preconsumed_evidence() -> None:
    registry = EvidenceRegistry("CASE_001")
    value = record()
    preconsumed = EvidenceRecord(
        case_id=value.case_id,
        evidence_ref=value.evidence_ref,
        result_hash=value.result_hash,
        domain=value.domain,
        tool_name=value.tool_name,
        data=value.data,
        consumed_by=("order-item-agent",),
    )
    with pytest.raises(EvidenceRegistryError, match="pre-existing consumers"):
        registry.register(preconsumed)


def test_registry_does_not_share_mutable_data_with_caller() -> None:
    data = {"events": [{"status": "confirmed"}]}
    registry = EvidenceRegistry("CASE_001")
    registry.register(record(data=data))
    data["events"][0]["status"] = "tampered"
    assert registry.get(EVIDENCE_REF).data["events"][0]["status"] == "confirmed"


def test_get_returns_copy_of_authoritative_record() -> None:
    registry = EvidenceRegistry("CASE_001")
    registry.register(record())
    returned = registry.get(EVIDENCE_REF)
    returned.data["order_status"] = "tampered"
    assert registry.get(EVIDENCE_REF).data["order_status"] == "canceled"


def test_registry_tracks_consumers_idempotently() -> None:
    registry = EvidenceRegistry("CASE_001")
    registry.register(record())
    registry.mark_consumed(EVIDENCE_REF, "order-item-agent")
    updated = registry.mark_consumed(EVIDENCE_REF, "order-item-agent")
    assert updated.consumed_by == ("order-item-agent",)


def test_registry_rejects_unconsumed_output_reference() -> None:
    registry = EvidenceRegistry("CASE_001")
    registry.register(record())
    with pytest.raises(EvidenceRegistryError, match="unconsumed"):
        registry.validate_refs((EVIDENCE_REF,), require_consumed=True)


def test_registry_links_consumed_evidence_to_fact() -> None:
    registry = EvidenceRegistry("CASE_001")
    registry.register(record())
    registry.mark_consumed(EVIDENCE_REF, "order-item-agent")
    fact = Fact(
        fact_id="fact-order-status",
        fact_code="ORDER_STATUS",
        value="canceled",
        source=EvidenceSource.MCP,
        evidence_refs=(EVIDENCE_REF,),
    )
    registry.link_fact(fact)
    assert registry.refs_for_fact(fact.fact_id) == (EVIDENCE_REF,)


def test_registry_rejects_case_input_fact_linkage() -> None:
    registry = EvidenceRegistry("CASE_001")
    fact = Fact(
        fact_id="fact-claimed-order",
        fact_code="CLAIMED_ORDER_ID",
        value="order-1",
        source=EvidenceSource.CASE_INPUT,
    )
    with pytest.raises(EvidenceRegistryError, match="only MCP facts"):
        registry.link_fact(fact)
