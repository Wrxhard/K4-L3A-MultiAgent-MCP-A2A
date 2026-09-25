from __future__ import annotations

from typing import Any
from pathlib import Path

from langchain_core.runnables import RunnableLambda

from .agent_contracts import (
    AgentRegistry,
    AgentResult,
    AgentTask,
    OrderItemPayload,
    PaymentPayload,
    PolicyPayload,
    ShipmentPayload,
    EntitySet,
    Observation,
    ToolCallSummary,
)
from .coordinator import Coordinator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import DeterministicVerifier
from .contracts import Contracts
from .specialists import (
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
    SpecialistResult,
)
from .llm import get_llm


def _make_adapter(spec, gateway: EvidenceGateway, trace: TraceWriter, payload_cls, observation_code: str):
    async def _run(task: AgentTask) -> AgentResult:
        case_id = task.case_id
        customer_request = task.case.get("customer_request", {})
        order_id = str(customer_request.get("claimed_order_id", ""))
        customer_context = str(task.case)
        
        try:
            res: SpecialistResult = await spec.investigate(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
                customer_context=customer_context,
            )
        except Exception as exc:
            return AgentResult.failed(
                actor=task.actor,
                invocation=task.invocation,
                context_version=task.context_version,
                retry_count_for_context=task.retry_count_for_context,
                error_code="AGENT_CRASHED",
                failed_stage="investigate",
                retryable=True,
                safe_detail=str(exc)[:160]
            )
            
        if res.errors:
            return AgentResult.failed(
                actor=task.actor,
                invocation=task.invocation,
                context_version=task.context_version,
                retry_count_for_context=task.retry_count_for_context,
                error_code="INVESTIGATION_FAILED",
                failed_stage="investigate",
                retryable=True,
                safe_detail=", ".join(res.errors)[:160],
                evidence_refs=tuple(res.evidence_refs),
            )
            
        entities = EntitySet(
            order_ids=tuple(res.entities.get("order_ids", [])),
            item_ids=tuple(res.entities.get("item_ids", [])),
            seller_ids=tuple(res.entities.get("seller_ids", [])),
            payment_references=tuple(res.entities.get("payment_references", [])),
            shipment_ids=tuple(res.entities.get("shipment_ids", [])),
        )
        obs = Observation(
            code=observation_code,
            subject_id=order_id,
            facts=res.facts,
            evidence_refs=tuple(res.evidence_refs)
        )
        payload = payload_cls(entities=entities, observations=(obs,))
        return AgentResult.completed(
            actor=task.actor,
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=payload,
            evidence_refs=tuple(res.evidence_refs),
        )
    return RunnableLambda(_run)


def _make_policy_adapter(spec: PolicySpecialist, gateway: EvidenceGateway, trace: TraceWriter):
    async def _run(task: AgentTask) -> AgentResult:
        case_id = task.case_id
        customer_request = task.case
        policy_version = str(task.case.get("policy_version", "EC_POLICY_V1"))

        active = task.context.get("active_results", {})
        order_ar = active.get("order_item")
        payment_ar = active.get("payment")
        shipment_ar = active.get("shipment")

        def _to_spec_res(ar: AgentResult | None, actor: str) -> SpecialistResult:
            if not ar or not ar.payload:
                return SpecialistResult(actor=actor)
            payload = ar.payload
            facts = payload.observations[0].facts if getattr(payload, "observations", None) else {}
            entities_dict = {
                "order_ids": set(getattr(payload.entities, "order_ids", [])),
                "item_ids": set(getattr(payload.entities, "item_ids", [])),
                "seller_ids": set(getattr(payload.entities, "seller_ids", [])),
                "payment_references": set(getattr(payload.entities, "payment_references", [])),
                "shipment_ids": set(getattr(payload.entities, "shipment_ids", [])),
            }
            return SpecialistResult(
                actor=actor,
                facts=dict(facts),
                evidence_refs=list(ar.evidence_refs),
                entities=entities_dict
            )

        order_res = _to_spec_res(order_ar, "order_item")
        payment_res = _to_spec_res(payment_ar, "payment")
        shipment_res = _to_spec_res(shipment_ar, "shipment")

        try:
            res_dict = await spec.adjudicate(
                case_id=case_id,
                policy_version=policy_version,
                customer_request=dict(customer_request),
                order_res=order_res,
                payment_res=payment_res,
                shipment_res=shipment_res,
                gateway=gateway,
                trace=trace,
            )
        except Exception as exc:
            return AgentResult.failed(
                actor=task.actor,
                invocation=task.invocation,
                context_version=task.context_version,
                retry_count_for_context=task.retry_count_for_context,
                error_code="POLICY_EVALUATION_FAILED",
                failed_stage="adjudicate",
                retryable=True,
                safe_detail=str(exc)[:160]
            )

        payload = PolicyPayload(
            assessment=res_dict.get("assessment", {}),
            claim_assessments=tuple(res_dict.get("claim_assessments", [])),
            root_cause_analysis=res_dict.get("root_cause_analysis", {}),
            data_conflicts=tuple(res_dict.get("data_conflicts", [])),
            financial_resolution=res_dict.get("financial_resolution", {}),
            resolution_actions=tuple(res_dict.get("resolution_actions", [])),
        )

        policy_ev = res_dict.get("policy_evidence_ref")
        ev_refs = [policy_ev] if policy_ev else []
        tool_calls = (
            ToolCallSummary("get_policy", 1, "completed", tuple(ev_refs)),
        ) if ev_refs else ()

        return AgentResult.completed(
            actor=task.actor,
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=payload,
            evidence_refs=tuple(ev_refs),
            tool_calls=tool_calls,
        )
    return RunnableLambda(_run)


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    registry: AgentRegistry | None = None,
) -> dict[str, Any]:
    """Run one case through the coordinator with explicitly wired team agents."""
    if registry is None:
        llm = get_llm()
        order_spec = OrderSpecialist(llm=llm)
        payment_spec = PaymentSpecialist(llm=llm)
        shipment_spec = ShipmentSpecialist(llm=llm)
        policy_spec = PolicySpecialist(llm=llm)

        contracts_path = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
        
        registry = AgentRegistry(
            order_item=_make_adapter(order_spec, gateway, trace, OrderItemPayload, "ORDER_INFO"),
            payment=_make_adapter(payment_spec, gateway, trace, PaymentPayload, "PAYMENT_INFO"),
            shipment=_make_adapter(shipment_spec, gateway, trace, ShipmentPayload, "SHIPMENT_INFO"),
            policy=_make_policy_adapter(policy_spec, gateway, trace),
            verifier=DeterministicVerifier(Contracts(contracts_path)).as_runnable(),
        )

    return await Coordinator().solve(case, registry, trace)
