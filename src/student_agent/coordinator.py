from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from . import OUTPUT_SCHEMA_VERSION
from .agent_contracts import (
    ACTORS,
    Actor,
    AgentFeedback,
    AgentRegistry,
    AgentResult,
    AgentTask,
    EntitySet,
    OrderItemPayload,
    PaymentPayload,
    PolicyPayload,
    ShipmentPayload,
    VerificationPackage,
    VerificationReport,
    validate_agent_result,
    validate_verification_report,
)


class TraceSink(Protocol):
    def emit(self, **event: Any) -> dict[str, Any]: ...


class CoordinatorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _actor_map(default: Any) -> dict[Actor, Any]:
    return {actor: default() if callable(default) else default for actor in ACTORS}


@dataclass
class CaseState:
    case: dict[str, Any]
    histories: dict[Actor, list[AgentResult]] = field(default_factory=lambda: _actor_map(list))
    active_results: dict[Actor, AgentResult] = field(default_factory=dict)
    invocations: dict[Actor, int] = field(default_factory=lambda: _actor_map(0))
    context_versions: dict[Actor, int] = field(default_factory=lambda: _actor_map(1))
    retry_counts: dict[Actor, int] = field(default_factory=lambda: _actor_map(0))
    feedback: dict[Actor, AgentFeedback] = field(default_factory=dict)
    verification_history: list[VerificationReport] = field(default_factory=list)
    candidate_output: dict[str, Any] | None = None

    @property
    def case_id(self) -> str:
        return self.case["case_id"]


class CoordinatorGraphState(TypedDict):
    case_state: CaseState
    latest_report: VerificationReport | None
    terminal_error: CoordinatorError | None


class CoordinatorContext(TypedDict):
    registry: AgentRegistry
    trace: TraceSink


class Coordinator:
    def __init__(self) -> None:
        builder = StateGraph(CoordinatorGraphState, context_schema=CoordinatorContext)
        builder.add_node("order_item", self._order_item_node)
        builder.add_node("payment_and_shipment", self._payment_and_shipment_node)
        builder.add_node("policy", self._policy_node)
        builder.add_node("assemble", self._assemble_node)
        builder.add_node("verify", self._verify_node)
        builder.add_node("retry_target", self._retry_target_node)
        builder.add_node("fail", self._fail_node)
        builder.add_edge(START, "order_item")
        builder.add_edge("order_item", "payment_and_shipment")
        builder.add_edge("payment_and_shipment", "policy")
        builder.add_edge("policy", "assemble")
        builder.add_edge("assemble", "verify")
        builder.add_conditional_edges(
            "verify",
            self._route_after_verification,
            {"passed": END, "retry": "retry_target", "failed": "fail"},
        )
        builder.add_edge("retry_target", "assemble")
        builder.add_edge("fail", END)
        self._graph = builder.compile()

    async def solve(
        self,
        case: dict[str, Any],
        registry: AgentRegistry,
        trace: TraceSink,
    ) -> dict[str, Any]:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise CoordinatorError("INVALID_CASE", "case_id must be a non-empty string")
        initial: CoordinatorGraphState = {
            "case_state": CaseState(dict(case)),
            "latest_report": None,
            "terminal_error": None,
        }
        final = await self._graph.ainvoke(
            initial,
            context={"registry": registry, "trace": trace},
        )
        error = final.get("terminal_error")
        if error is not None:
            raise error
        candidate = final["case_state"].candidate_output
        if candidate is None:
            raise CoordinatorError("WORKFLOW_FAILED", "workflow produced no candidate output")
        return candidate

    async def _order_item_node(
        self,
        state: CoordinatorGraphState,
        runtime: Runtime[CoordinatorContext],
    ) -> dict[str, CaseState]:
        case_state = state["case_state"]
        await self._invoke_actor("order_item", case_state, runtime)
        return {"case_state": case_state}

    async def _payment_and_shipment_node(
        self,
        state: CoordinatorGraphState,
        runtime: Runtime[CoordinatorContext],
    ) -> dict[str, CaseState]:
        case_state = state["case_state"]
        if not self._completed(case_state, "order_item"):
            return {"case_state": case_state}
        payment, shipment = await asyncio.gather(
            self._call_actor("payment", case_state, runtime),
            self._call_actor("shipment", case_state, runtime),
        )
        self._record_result(case_state, payment, runtime.context["trace"])
        self._record_result(case_state, shipment, runtime.context["trace"])
        return {"case_state": case_state}

    async def _policy_node(
        self,
        state: CoordinatorGraphState,
        runtime: Runtime[CoordinatorContext],
    ) -> dict[str, CaseState]:
        case_state = state["case_state"]
        if all(self._completed(case_state, actor) for actor in ACTORS[:3]):
            await self._invoke_actor("policy", case_state, runtime)
        return {"case_state": case_state}

    async def _assemble_node(
        self,
        state: CoordinatorGraphState,
    ) -> dict[str, CaseState]:
        case_state = state["case_state"]
        case_state.candidate_output = self._assemble_candidate(case_state)
        return {"case_state": case_state}

    async def _verify_node(
        self,
        state: CoordinatorGraphState,
        runtime: Runtime[CoordinatorContext],
    ) -> dict[str, Any]:
        case_state = state["case_state"]
        package = self._verification_package(case_state)
        report = await runtime.context["registry"].verifier.ainvoke(package)
        validate_verification_report(report)
        case_state.verification_history.append(report)
        runtime.context["trace"].emit(
            case_id=case_state.case_id,
            event_type="verification_completed",
            actor="verifier",
            target=report.target_actor,
            decision_code=report.error_code or report.verdict.upper(),
            attributes={"round": package.verification_round, "verdict": report.verdict},
        )
        terminal_error = state.get("terminal_error")
        if report.verdict == "passed" and case_state.candidate_output is None:
            terminal_error = CoordinatorError(
                "INVALID_VERIFIER_REPORT", "verifier passed without a candidate output"
            )
        elif report.verdict == "failed":
            terminal_error = CoordinatorError(
                report.error_code or "WORKFLOW_FAILED",
                report.feedback or "verifier rejected the candidate",
            )
        return {
            "case_state": case_state,
            "latest_report": report,
            "terminal_error": terminal_error,
        }

    async def _retry_target_node(
        self,
        state: CoordinatorGraphState,
    ) -> dict[str, Any]:
        return {
            "case_state": state["case_state"],
            "terminal_error": CoordinatorError(
                "RETRY_NOT_ALLOWED", "retry routing is not available"
            ),
        }

    async def _fail_node(self, state: CoordinatorGraphState) -> dict[str, Any]:
        return {
            "case_state": state["case_state"],
            "terminal_error": state.get("terminal_error")
            or CoordinatorError("WORKFLOW_FAILED", "workflow verification failed"),
        }

    def _route_after_verification(
        self, state: CoordinatorGraphState
    ) -> Literal["passed", "retry", "failed"]:
        if state.get("terminal_error") is not None:
            return "failed"
        report = state.get("latest_report")
        if report is None:
            return "failed"
        if report.verdict == "passed":
            return "passed"
        if report.verdict == "retry_required":
            return "retry"
        return "failed"

    async def _invoke_actor(
        self,
        actor: Actor,
        case_state: CaseState,
        runtime: Runtime[CoordinatorContext],
    ) -> AgentResult:
        result = await self._call_actor(actor, case_state, runtime)
        self._record_result(case_state, result, runtime.context["trace"])
        return result

    async def _call_actor(
        self,
        actor: Actor,
        case_state: CaseState,
        runtime: Runtime[CoordinatorContext],
    ) -> AgentResult:
        case_state.invocations[actor] += 1
        task = AgentTask(
            case_id=case_state.case_id,
            actor=actor,
            invocation=case_state.invocations[actor],
            context_version=case_state.context_versions[actor],
            retry_count_for_context=case_state.retry_counts[actor],
            case=dict(case_state.case),
            context={"active_results": dict(case_state.active_results)},
            feedback=case_state.feedback.get(actor),
        )
        runtime.context["trace"].emit(
            case_id=case_state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="TASK_ASSIGNED",
            attributes={
                "invocation": task.invocation,
                "context_version": task.context_version,
                "retry_count": task.retry_count_for_context,
            },
        )
        result = await runtime.context["registry"].for_actor(actor).ainvoke(task)
        validate_agent_result(task, result)
        return result

    @staticmethod
    def _record_result(case_state: CaseState, result: AgentResult, trace: TraceSink) -> None:
        case_state.histories[result.actor].append(result)
        if result.status == "completed":
            case_state.active_results[result.actor] = result
        else:
            case_state.active_results.pop(result.actor, None)
        trace.emit(
            case_id=case_state.case_id,
            event_type="handoff",
            actor=result.actor,
            target="coordinator",
            decision_code=(result.error_code or "AGENT_COMPLETED"),
            evidence_refs=list(result.evidence_refs),
            attributes={
                "invocation": result.invocation,
                "context_version": result.context_version,
                "status": result.status,
            },
        )

    @staticmethod
    def _completed(case_state: CaseState, actor: Actor) -> bool:
        result = case_state.active_results.get(actor)
        return result is not None and result.status == "completed"

    def _assemble_candidate(self, case_state: CaseState) -> dict[str, Any] | None:
        if not all(self._completed(case_state, actor) for actor in ACTORS):
            return None
        order_payload = case_state.active_results["order_item"].payload
        payment_payload = case_state.active_results["payment"].payload
        shipment_payload = case_state.active_results["shipment"].payload
        policy_payload = case_state.active_results["policy"].payload
        if not isinstance(order_payload, OrderItemPayload):
            raise CoordinatorError("INVALID_AGENT_RESULT", "invalid Order/Item payload")
        if not isinstance(payment_payload, PaymentPayload):
            raise CoordinatorError("INVALID_AGENT_RESULT", "invalid Payment payload")
        if not isinstance(shipment_payload, ShipmentPayload):
            raise CoordinatorError("INVALID_AGENT_RESULT", "invalid Shipment payload")
        if not isinstance(policy_payload, PolicyPayload):
            raise CoordinatorError("INVALID_AGENT_RESULT", "invalid Policy payload")
        entities = self._merge_entities(
            order_payload.entities, payment_payload.entities, shipment_payload.entities
        )
        evidence_refs = self._ordered_union(
            *(
                case_state.active_results[actor].evidence_refs
                for actor in ACTORS
            )
        )
        candidate: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": case_state.case_id,
            "assessment": dict(policy_payload.assessment),
            "affected_entities": entities,
            "root_cause_analysis": dict(policy_payload.root_cause_analysis),
            "evidence_refs": evidence_refs,
            "data_conflicts": [dict(item) for item in policy_payload.data_conflicts],
            "financial_resolution": dict(policy_payload.financial_resolution),
            "resolution_actions": list(policy_payload.resolution_actions),
        }
        if policy_payload.claim_assessments:
            candidate["claim_assessments"] = [
                dict(item) for item in policy_payload.claim_assessments
            ]
        return candidate

    def _merge_entities(self, *sets: EntitySet) -> dict[str, list[str]]:
        names = (
            "order_ids",
            "item_ids",
            "seller_ids",
            "payment_references",
            "shipment_ids",
        )
        return {
            name: self._ordered_union(*(getattr(entity_set, name) for entity_set in sets))
            for name in names
        }

    @staticmethod
    def _ordered_union(*groups: tuple[str, ...]) -> list[str]:
        return list(dict.fromkeys(value for group in groups for value in group))

    @staticmethod
    def _verification_package(case_state: CaseState) -> VerificationPackage:
        unresolved = tuple(
            history[-1]
            for actor in ACTORS
            if (history := case_state.histories[actor]) and history[-1].status == "failed"
        )
        return VerificationPackage(
            case_id=case_state.case_id,
            verification_round=len(case_state.verification_history) + 1,
            candidate_output=case_state.candidate_output,
            attempt_history={
                actor: tuple(case_state.histories[actor]) for actor in ACTORS
            },
            active_results=dict(case_state.active_results),
            unresolved_failures=unresolved,
        )
