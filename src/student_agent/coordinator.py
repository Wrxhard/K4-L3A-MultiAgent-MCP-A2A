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
    _MAX_VERIFICATION_ROUNDS = 5

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
        await asyncio.gather(
            self._invoke_actor("payment", case_state, runtime),
            self._invoke_actor("shipment", case_state, runtime),
        )
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
        if len(case_state.verification_history) >= self._MAX_VERIFICATION_ROUNDS:
            return {
                "case_state": case_state,
                "latest_report": state.get("latest_report"),
                "terminal_error": CoordinatorError(
                    "VERIFICATION_LIMIT_EXCEEDED",
                    "maximum verification rounds exceeded",
                ),
            }
        package = self._verification_package(case_state)
        report = await runtime.context["registry"].verifier.ainvoke(package)
        try:
            validate_verification_report(report)
        except (AttributeError, TypeError, ValueError):
            if (
                isinstance(report, VerificationReport)
                and report.verdict == "retry_required"
                and not report.retryable
            ):
                code = "RETRY_NOT_ALLOWED"
            elif (
                isinstance(report, VerificationReport)
                and report.verdict == "retry_required"
                and report.target_actor not in ACTORS
            ):
                code = "INVALID_RETRY_TARGET"
            else:
                code = "INVALID_VERIFIER_REPORT"
            return {
                "case_state": case_state,
                "latest_report": None,
                "terminal_error": CoordinatorError(code, "verifier returned an invalid report"),
            }
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
        runtime: Runtime[CoordinatorContext],
    ) -> dict[str, Any]:
        case_state = state["case_state"]
        report = state.get("latest_report")
        if report is None or report.verdict != "retry_required":
            return self._retry_error(case_state, "INVALID_VERIFIER_REPORT")
        if not report.retryable:
            return self._retry_error(case_state, "RETRY_NOT_ALLOWED")
        if report.target_actor not in ACTORS:
            return self._retry_error(case_state, "INVALID_RETRY_TARGET")
        target: Actor = report.target_actor
        if case_state.retry_counts[target] >= 1:
            return self._retry_error(case_state, "RETRY_LIMIT_EXCEEDED")

        case_state.feedback[target] = AgentFeedback(
            error_code=report.error_code or "VERIFICATION_FAILED",
            message=report.feedback or "Recheck the assigned result",
        )
        case_state.retry_counts[target] += 1
        case_state.active_results.pop(target, None)
        case_state.candidate_output = None
        runtime.context["trace"].emit(
            case_id=case_state.case_id,
            event_type="retry_scheduled",
            actor="coordinator",
            target=target,
            decision_code=report.error_code or "VERIFICATION_FAILED",
            attributes={
                "context_version": case_state.context_versions[target],
                "retry_count": case_state.retry_counts[target],
            },
        )

        if target == "order_item":
            self._invalidate(case_state, "payment", "shipment", "policy")
            await self._invoke_actor("order_item", case_state, runtime)
            if self._completed(case_state, "order_item"):
                await asyncio.gather(
                    self._invoke_actor("payment", case_state, runtime),
                    self._invoke_actor("shipment", case_state, runtime),
                )
                if all(self._completed(case_state, actor) for actor in ACTORS[:3]):
                    await self._invoke_actor("policy", case_state, runtime)
        elif target in ("payment", "shipment"):
            self._invalidate(case_state, "policy")
            await self._invoke_actor(target, case_state, runtime)
            if self._completed(case_state, target):
                await self._invoke_actor("policy", case_state, runtime)
        else:
            await self._invoke_actor("policy", case_state, runtime)

        return {"case_state": case_state, "terminal_error": None}

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
        try:
            while True:
                result = await self._call_actor(actor, case_state, runtime)
                self._record_result(case_state, result, runtime.context["trace"])
                if result.status == "completed":
                    return result
                if result.error_code not in {
                    "AGENT_CRASHED",
                    "AGENT_TIMEOUT",
                    "AGENT_NO_RESPONSE",
                }:
                    return result
                if case_state.retry_counts[actor] >= 1:
                    return result
                case_state.retry_counts[actor] += 1
        finally:
            case_state.feedback.pop(actor, None)

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
        try:
            result = await runtime.context["registry"].for_actor(actor).ainvoke(task)
            if result is None:
                return self._failed_invocation(task, "AGENT_NO_RESPONSE")
            validate_agent_result(task, result)
            return result
        except TimeoutError:
            return self._failed_invocation(task, "AGENT_TIMEOUT")
        except Exception:
            return self._failed_invocation(task, "AGENT_CRASHED")

    @staticmethod
    def _failed_invocation(task: AgentTask, error_code: str) -> AgentResult:
        details = {
            "AGENT_TIMEOUT": "Agent invocation timed out",
            "AGENT_NO_RESPONSE": "Agent returned no response",
            "AGENT_CRASHED": "Agent invocation failed",
        }
        return AgentResult.failed(
            actor=task.actor,
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code=error_code,
            failed_stage="agent_invocation",
            retryable=True,
            safe_detail=details[error_code],
        )

    @staticmethod
    def _retry_error(case_state: CaseState, code: str) -> dict[str, Any]:
        messages = {
            "INVALID_VERIFIER_REPORT": "retry requested without a valid report",
            "RETRY_NOT_ALLOWED": "verifier marked the retry as not allowed",
            "INVALID_RETRY_TARGET": "verifier selected an invalid retry target",
            "RETRY_LIMIT_EXCEEDED": "same-context retry limit exceeded",
        }
        return {
            "case_state": case_state,
            "terminal_error": CoordinatorError(code, messages[code]),
        }

    @staticmethod
    def _invalidate(case_state: CaseState, *actors: Actor) -> None:
        for actor in actors:
            case_state.active_results.pop(actor, None)
            case_state.feedback.pop(actor, None)
            case_state.context_versions[actor] += 1
            case_state.retry_counts[actor] = 0

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
