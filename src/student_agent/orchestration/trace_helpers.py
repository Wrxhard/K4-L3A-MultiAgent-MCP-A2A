from __future__ import annotations

from typing import Any, Protocol

from student_agent.domain import (
    AgentTask,
    EvidenceSource,
    SpecialistReport,
    TaskStatus,
    VerificationReport,
)
from student_agent.evidence import EvidenceRegistry


class TraceSink(Protocol):
    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]: ...


class LifecycleTraceError(ValueError):
    """Raised before an invalid or misleading lifecycle event can be emitted."""


class CaseTrace:
    """Case-scoped trace facade that enforces the observable workflow lifecycle."""

    def __init__(self, case_id: str, sink: TraceSink, evidence: EvidenceRegistry) -> None:
        if not case_id or not case_id.strip():
            raise LifecycleTraceError("case_id must be a non-empty string")
        if evidence.case_id != case_id:
            raise LifecycleTraceError("trace and evidence registry must have the same case_id")
        self.case_id = case_id
        self._sink = sink
        self._evidence = evidence
        self._received = False
        self._finalized = False
        self._verification_results: list[bool] = []
        self._tasks: dict[str, AgentTask] = {}
        self._handoffs: set[str] = set()

    def _require_received(self) -> None:
        if not self._received:
            raise LifecycleTraceError("case_received must be emitted first")
        if self._finalized:
            raise LifecycleTraceError("no events may be emitted after case_finalized")

    def case_received(self, *, actor: str = "coordinator") -> dict[str, Any]:
        if self._received:
            raise LifecycleTraceError("case_received was already emitted")
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="case_received",
            actor=actor,
        )
        self._received = True
        return event

    def task_assigned(self, task: AgentTask, *, actor: str = "coordinator") -> dict[str, Any]:
        self._require_received()
        if task.case_id != self.case_id:
            raise LifecycleTraceError("cannot assign a task from another case")
        if task.status is not TaskStatus.PENDING:
            raise LifecycleTraceError("a newly assigned task must be pending")
        if task.task_id in self._tasks:
            raise LifecycleTraceError(f"task was already assigned: {task.task_id}")
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=actor,
            target=task.actor,
            attributes={"task_id": task.task_id, "attempt": task.attempt},
        )
        self._tasks[task.task_id] = task
        return event

    def tool_result_consumed(
        self, *, actor: str, tool_name: str, evidence_ref: str
    ) -> dict[str, Any]:
        self._require_received()
        if actor not in {task.actor for task in self._tasks.values()}:
            raise LifecycleTraceError(f"actor has no assigned task: {actor}")
        record = self._evidence.get(evidence_ref)
        if record.tool_name != tool_name:
            raise LifecycleTraceError(
                f"tool/evidence mismatch: expected {record.tool_name!r}, got {tool_name!r}"
            )
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        self._evidence.mark_consumed(evidence_ref, actor)
        return event

    def policy_decided(
        self,
        *,
        task_id: str,
        decision_code: str,
        evidence_refs: tuple[str, ...],
    ) -> dict[str, Any]:
        self._require_received()
        task = self._get_task(task_id)
        self._evidence.validate_refs(evidence_refs, require_consumed=True)
        return self._sink.emit(
            case_id=self.case_id,
            event_type="policy_decided",
            actor=task.actor,
            decision_code=decision_code,
            evidence_refs=list(evidence_refs),
            attributes={"task_id": task_id},
        )

    def handoff(
        self, report: SpecialistReport, *, target: str = "coordinator"
    ) -> dict[str, Any]:
        self._require_received()
        if report.case_id != self.case_id:
            raise LifecycleTraceError("cannot hand off a report from another case")
        task = self._get_task(report.task_id)
        if task.actor != report.actor:
            raise LifecycleTraceError("report actor does not own the assigned task")
        if report.task_id in self._handoffs:
            raise LifecycleTraceError(f"task was already handed off: {report.task_id}")
        self._evidence.validate_refs(report.evidence_refs, require_consumed=True)
        for fact in report.facts:
            if fact.source is EvidenceSource.MCP:
                self._evidence.link_fact(fact)
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=report.actor,
            target=target,
            evidence_refs=list(report.evidence_refs),
            attributes={"task_id": report.task_id, "status": report.status.value},
        )
        self._handoffs.add(report.task_id)
        return event

    def verification_completed(
        self, report: VerificationReport, *, actor: str = "verifier"
    ) -> dict[str, Any]:
        self._require_received()
        if report.case_id != self.case_id:
            raise LifecycleTraceError("cannot verify a report from another case")
        if not self._handoffs:
            raise LifecycleTraceError("verification requires at least one specialist handoff")
        if self._verification_results and self._verification_results[-1]:
            raise LifecycleTraceError("a passing verification is already final")
        if len(self._verification_results) >= 2:
            raise LifecycleTraceError("verification retry limit exceeded")
        decision_code = "VERIFICATION_PASSED" if report.passed else report.error_codes[0]
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="verification_completed",
            actor=actor,
            decision_code=decision_code,
            attributes={"passed": report.passed},
        )
        self._verification_results.append(report.passed)
        return event

    def case_finalized(
        self,
        *,
        evidence_refs: tuple[str, ...],
        actor: str = "coordinator",
    ) -> dict[str, Any]:
        self._require_received()
        if not self._verification_results:
            raise LifecycleTraceError("case_finalized requires verification_completed")
        self._evidence.validate_refs(evidence_refs, require_consumed=True)
        event = self._sink.emit(
            case_id=self.case_id,
            event_type="case_finalized",
            actor=actor,
            evidence_refs=list(evidence_refs),
        )
        self._finalized = True
        return event

    def _get_task(self, task_id: str) -> AgentTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise LifecycleTraceError(f"task was not assigned: {task_id}") from exc
