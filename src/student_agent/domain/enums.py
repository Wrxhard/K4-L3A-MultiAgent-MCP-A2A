from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    RECEIVED = "received"
    PLANNING = "planning"
    COLLECTING_EVIDENCE = "collecting_evidence"
    POLICY_CHECK = "policy_check"
    GENERATING_CANDIDATES = "generating_candidates"
    MODEL_ADJUDICATING = "model_adjudicating"
    BUILDING_DRAFT = "building_draft"
    MODEL_CRITIQUING = "model_critiquing"
    REVISING = "revising"
    VERIFYING = "verifying"
    DEGRADED = "degraded"
    FINALIZED = "finalized"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PARTIALLY_SUPPORTED = "partially_supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class PrimaryIssue(StrEnum):
    CANCELED_ORDER_PAID = "canceled_order_paid"
    UNAVAILABLE_ORDER_PAID = "unavailable_order_paid"
    LATE_DELIVERY_SELLER = "late_delivery_seller"
    LATE_DELIVERY_LOGISTICS = "late_delivery_logistics"
    VALID_SPLIT_PAYMENT = "valid_split_payment"
    PAYMENT_MISMATCH = "payment_mismatch"
    DUPLICATE_CHARGE = "duplicate_charge"
    REFUND_PENDING = "refund_pending"
    REFUND_FAILED = "refund_failed"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class CaseStatus(StrEnum):
    ACTION_REQUIRED = "action_required"
    NO_ACTION = "no_action"
    NEEDS_INVESTIGATION = "needs_investigation"


class PartyType(StrEnum):
    SELLER = "seller"
    PLATFORM = "platform"
    LOGISTICS_PROVIDER = "logistics_provider"
    PAYMENT_PROVIDER = "payment_provider"
    CUSTOMER = "customer"
    UNKNOWN = "unknown"


class EvidenceDomain(StrEnum):
    ORDER = "order"
    ITEM = "item"
    PAYMENT = "payment"
    SHIPMENT = "shipment"
    SELLER = "seller"
    CUSTOMER = "customer"
    PRODUCT = "product"
    REFUND = "refund"
    POLICY = "policy"


class ConfidenceBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class EvidenceSource(StrEnum):
    MCP = "mcp"
    CASE_INPUT = "case_input"


class MessageType(StrEnum):
    TASK_REQUEST = "task_request"
    TASK_RESULT = "task_result"
    NEED_FACT = "need_fact"
    VERIFICATION_RESULT = "verification_result"


class CriticVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"


class DecisionCode(StrEnum):
    CHECK_ORDER_STATE = "CHECK_ORDER_STATE"
    CHECK_PAYMENT_TOTAL = "CHECK_PAYMENT_TOTAL"
    CHECK_SHIPMENT_TIMELINE = "CHECK_SHIPMENT_TIMELINE"
    PAYMENT_SPLIT_VALID = "PAYMENT_SPLIT_VALID"
    DUPLICATE_PAYMENT_CONFIRMED = "DUPLICATE_PAYMENT_CONFIRMED"
    POLICY_REFUND_ELIGIBLE = "POLICY_REFUND_ELIGIBLE"
    POLICY_NEEDS_FACT = "POLICY_NEEDS_FACT"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    TOOL_CONTRACT_ERROR = "TOOL_CONTRACT_ERROR"
    INVALID_EVIDENCE_ENVELOPE = "INVALID_EVIDENCE_ENVELOPE"
    MCP_TRANSIENT_EXHAUSTED = "MCP_TRANSIENT_EXHAUSTED"
    INVALID_SPECIALIST_REPORT = "INVALID_SPECIALIST_REPORT"
    ADJUDICATOR_FALLBACK = "ADJUDICATOR_FALLBACK"
    ADJUDICATOR_CONSTRAINT_VIOLATION = "ADJUDICATOR_CONSTRAINT_VIOLATION"
    CRITIC_FALLBACK = "CRITIC_FALLBACK"
    CRITIC_REVISION_REQUIRED = "CRITIC_REVISION_REQUIRED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    UNRESOLVED_VERIFICATION_FAILURE = "UNRESOLVED_VERIFICATION_FAILURE"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
