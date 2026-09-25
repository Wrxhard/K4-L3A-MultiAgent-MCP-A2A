# LangGraph Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the coordinator-only LangGraph workflow that retains structured agent context, assembles the L3A candidate output, and executes bounded task recovery and verifier-directed semantic retries.

**Architecture:** A compiled LangGraph `StateGraph` owns the case lifecycle while concrete team agents are injected as LangChain `Runnable[AgentTask, AgentResult]` values. The graph runs Order/Item first, Payment and Shipment concurrently, Policy next, then assembly and verification; a dedicated retry node reruns only the target and its downstream dependants.

**Tech Stack:** Python 3.11+, LangGraph `>=1.2,<2`, LangChain `>=1.4,<2`, dataclasses, asyncio, pytest 8.4, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-25-coordinator-context-retry-design.md`

## Global Constraints

- Implement coordinator orchestration only; do not implement Order/Item, Payment, Shipment, Policy, or Verifier domain logic.
- Do not create or infer `evidence_ref` values.
- Do not store prompts, chain-of-thought, raw MCP bodies, secrets, or raw exception text in state or trace.
- Local MCP/model retries remain inside team agent runnables; coordinator recovers only `AGENT_TIMEOUT`, `AGENT_CRASHED`, and `AGENT_NO_RESPONSE`.
- Semantic retry requires a valid `VerificationReport` and is limited to one retry per actor and `context_version`.
- A changed upstream result increments downstream `context_version`; downstream recomputation is not a same-context retry.
- Preserve the existing `solve_case(case, gateway, trace)` positional API. Until team runnables are wired, its optional keyword-only registry must fail with a clear configuration error rather than fabricate agents.
- All public trace records must validate against `trace-event-v1.schema.json`.

## Review Focus

- Empty or non-string `case_id` must fail with `INVALID_CASE` before any runnable is invoked; covered in Task 2.
- A completed result whose payload class does not match its actor must fail with `INVALID_AGENT_RESULT`; covered in Task 1.
- An agent-provided `safe_detail` containing a team-key pattern must be rejected and never enter verification state; covered in Task 1.
- Verifier `passed` with no candidate output must fail with `INVALID_VERIFIER_REPORT`; covered in Task 3.
- A locally exhausted MCP error must reach Verifier but must not trigger coordinator task recovery; covered in Task 3.

---

## File structure

- Create `src/student_agent/agent_contracts.py`: immutable cross-team task/result/payload contracts, validation helpers, and `AgentRegistry`.
- Create `src/student_agent/coordinator.py`: mutable per-case state, LangGraph construction, agent invocation/recovery, candidate assembly, retry routing, and coordinator exceptions.
- Modify `src/student_agent/workflow.py`: public entry point delegating to `Coordinator` when a registry is provided.
- Modify `pyproject.toml`: declare LangGraph and LangChain runtime dependencies.
- Create `tests/test_agent_contracts.py`: contract and sanitization behavior.
- Create `tests/__init__.py` and `tests/coordinator_fakes.py`: shared deterministic runnables used only by coordinator/workflow tests.
- Create `tests/test_coordinator.py`: graph order, candidate assembly, failure context, retries, invalidation, and limits.
- Create `tests/test_workflow.py`: public adapter behavior and real trace-schema integration.
- Create `docs/team-agent-implementation-guide.md`: exact integration contract and Superpowers/TDD handoff for Nam, Dat, and Huy.

### Task 1: Typed cross-team contracts

**Files:**

- Create: `src/student_agent/agent_contracts.py`
- Create: `tests/test_agent_contracts.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Produces: `Actor`, `AgentStatus`, `VerificationVerdict`, `EntitySet`, `Observation`, `ToolCallSummary`, `OrderItemPayload`, `PaymentPayload`, `ShipmentPayload`, `PolicyPayload`, `AgentFeedback`, `AgentTask`, `AgentResult`, `VerificationPackage`, `VerificationReport`, `AgentRegistry`, and `validate_agent_result(task, result)`.
- Consumes: LangChain `Runnable[AgentTask, AgentResult]` and `Runnable[VerificationPackage, VerificationReport]`.

- [ ] **Step 1: Add the runtime dependencies and write failing contract tests**

Add these dependencies to `[project].dependencies` in `pyproject.toml`:

```toml
"langchain>=1.4,<2",
"langgraph>=1.2,<2",
```

Create `tests/test_agent_contracts.py` with focused tests using values that match the public schema vocabulary:

```python
from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableLambda

from student_agent.agent_contracts import (
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
)


def test_validate_agent_result_rejects_payload_owned_by_another_actor() -> None:
    task = AgentTask(
        case_id="CASE_001",
        actor="payment",
        invocation=1,
        context_version=1,
        retry_count_for_context=0,
        case={"case_id": "CASE_001"},
        context={},
        feedback=None,
    )
    result = AgentResult.completed(
        actor="payment",
        invocation=1,
        context_version=1,
        retry_count_for_context=0,
        payload=ShipmentPayload(EntitySet(), ()),
    )

    with pytest.raises(ValueError, match="payload"):
        validate_agent_result(task, result)


def test_failed_result_rejects_secret_in_safe_detail() -> None:
    with pytest.raises(ValueError, match="safe_detail"):
        AgentResult.failed(
            actor="order_item",
            invocation=1,
            context_version=1,
            retry_count_for_context=0,
            error_code="AGENT_CRASHED",
            failed_stage="agent_task",
            retryable=True,
            safe_detail="sk-team-1234567890abcdef leaked",
        )


def test_registry_returns_the_runnable_for_each_actor() -> None:
    async def agent(task: AgentTask) -> AgentResult:
        return AgentResult.failed(
            actor=task.actor,
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code="TEST_FAILURE",
            failed_stage="test",
            retryable=False,
            safe_detail="test failure",
        )

    async def verifier(package: VerificationPackage) -> VerificationReport:
        return VerificationReport.failed("TEST_FAILURE", "test failure")

    runnable = RunnableLambda(agent)
    registry = AgentRegistry(
        order_item=runnable,
        payment=runnable,
        shipment=runnable,
        policy=runnable,
        verifier=RunnableLambda(verifier),
    )

    assert registry.for_actor("order_item") is runnable
    assert registry.for_actor("payment") is runnable
    assert registry.for_actor("shipment") is runnable
    assert registry.for_actor("policy") is runnable
```

- [ ] **Step 2: Run the contract tests and confirm RED**

Run:

```powershell
pytest -q tests/test_agent_contracts.py
```

Expected: collection fails because `student_agent.agent_contracts` does not exist.

- [ ] **Step 3: Implement the immutable contracts and validation**

Create `src/student_agent/agent_contracts.py` using frozen dataclasses. Use these exact field shapes:

```python
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

Actor = Literal["order_item", "payment", "shipment", "policy"]
AgentStatus = Literal["completed", "failed"]
VerificationVerdict = Literal["passed", "retry_required", "failed"]


@dataclass(frozen=True)
class EntitySet:
    order_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    payment_references: tuple[str, ...] = ()
    shipment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    code: str
    subject_id: str | None
    facts: Mapping[str, JSONValue]
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCallSummary:
    tool_name: str
    attempts: int
    status: Literal["completed", "failed"]
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class OrderItemPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class PaymentPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class ShipmentPayload:
    entities: EntitySet
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class PolicyPayload:
    assessment: Mapping[str, JSONValue]
    claim_assessments: tuple[Mapping[str, JSONValue], ...]
    root_cause_analysis: Mapping[str, JSONValue]
    data_conflicts: tuple[Mapping[str, JSONValue], ...]
    financial_resolution: Mapping[str, JSONValue]
    resolution_actions: tuple[str, ...]
```

Define the remaining immutable messages with these fields:

```python
@dataclass(frozen=True)
class AgentFeedback:
    error_code: str
    message: str
    required_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    actor: Actor
    invocation: int
    context_version: int
    retry_count_for_context: int
    case: Mapping[str, JSONValue]
    context: Mapping[str, Any]
    feedback: AgentFeedback | None


AgentPayload: TypeAlias = (
    OrderItemPayload | PaymentPayload | ShipmentPayload | PolicyPayload
)


@dataclass(frozen=True)
class AgentResult:
    actor: Actor
    invocation: int
    context_version: int
    retry_count_for_context: int
    status: AgentStatus
    payload: AgentPayload | None
    evidence_refs: tuple[str, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    failed_stage: str | None
    error_code: str | None
    retryable: bool
    safe_detail: str | None


@dataclass(frozen=True)
class VerificationPackage:
    case_id: str
    verification_round: int
    candidate_output: Mapping[str, Any] | None
    attempt_history: Mapping[Actor, tuple[AgentResult, ...]]
    active_results: Mapping[Actor, AgentResult]
    unresolved_failures: tuple[AgentResult, ...]


@dataclass(frozen=True)
class VerificationReport:
    verdict: VerificationVerdict
    target_actor: Actor | str | None
    error_code: str | None
    feedback: str | None
    retryable: bool
```

Implement `AgentResult.completed(...)` and `AgentResult.failed(...)` class methods so callers cannot accidentally combine a completed payload with failure fields. Implement `VerificationReport.passed()`, `VerificationReport.retry(target_actor, error_code, feedback)`, and `VerificationReport.failed(error_code, feedback)` constructors. Reject duplicate IDs/evidence refs, non-positive invocation/context versions, negative retry counts, `safe_detail` longer than 160 characters, and the existing `sk-team-...` secret pattern.

Define `AgentRegistry` with four agent runnables and one verifier runnable:

```python
@dataclass(frozen=True)
class AgentRegistry:
    order_item: Runnable[AgentTask, AgentResult]
    payment: Runnable[AgentTask, AgentResult]
    shipment: Runnable[AgentTask, AgentResult]
    policy: Runnable[AgentTask, AgentResult]
    verifier: Runnable[VerificationPackage, VerificationReport]

    def for_actor(self, actor: Actor) -> Runnable[AgentTask, AgentResult]:
        return getattr(self, actor)
```

`validate_agent_result` must check counter equality with the task and enforce this actor-to-payload mapping:

```python
PAYLOAD_TYPE_BY_ACTOR = {
    "order_item": OrderItemPayload,
    "payment": PaymentPayload,
    "shipment": ShipmentPayload,
    "policy": PolicyPayload,
}
```

- [ ] **Step 4: Run contract tests and the existing suite**

Run:

```powershell
pytest -q tests/test_agent_contracts.py
pytest -q
ruff check src tests
```

Expected: all contract tests and the existing five tests pass; Ruff exits zero.

- [ ] **Step 5: Commit Task 1**

```powershell
git add pyproject.toml src/student_agent/agent_contracts.py tests/test_agent_contracts.py
git commit -m "feat: define coordinator agent contracts"
```

### Task 2: Happy-path LangGraph and deterministic assembly

**Files:**

- Create: `src/student_agent/coordinator.py`
- Create: `tests/__init__.py`
- Create: `tests/coordinator_fakes.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**

- Consumes: all Task 1 contracts and a trace object exposing `emit(...)`.
- Produces: `Coordinator`, `CoordinatorError`, `CaseState`, and `Coordinator.solve(case, registry, trace) -> dict[str, Any]`.

- [ ] **Step 1: Write failing tests for graph order, context, and output assembly**

Create `tests/test_coordinator.py`. Use `asyncio.run` rather than an async pytest plugin. Put deterministic reusable handlers in `tests/coordinator_fakes.py` and import them from both coordinator and workflow tests.

Define these reusable test helpers at the top of that file:

```python
class RecordingTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, **event: object) -> dict[str, object]:
        self.events.append(event)
        return event


def completed_order_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="order_item",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=OrderItemPayload(
            EntitySet(
                order_ids=("ORDER_1",),
                item_ids=("ITEM_1",),
                seller_ids=("SELLER_1",),
                payment_references=("PAYMENT_1",),
                shipment_ids=("SHIPMENT_1",),
            ),
            (),
        ),
        evidence_refs=("ev_order000000000000000001",),
    )


def completed_payment_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="payment",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=PaymentPayload(
            EntitySet(payment_references=("PAYMENT_1",)),
            (),
        ),
        evidence_refs=("ev_payment0000000000000001",),
    )


def completed_shipment_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="shipment",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=ShipmentPayload(EntitySet(shipment_ids=("SHIPMENT_1",)), ()),
        evidence_refs=("ev_shipment000000000000001",),
    )


def completed_policy_result(task: AgentTask) -> AgentResult:
    return AgentResult.completed(
        actor="policy",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=PolicyPayload(
            assessment={
                "primary_issue": "refund_pending",
                "case_status": "action_required",
                "confidence": 0.9,
            },
            claim_assessments=(),
            root_cause_analysis={
                "ranked_causes": [{"cause_code": "REFUND_DELAY", "rank": 1}],
                "responsible_parties": [
                    {"party_type": "payment_provider", "party_id": None}
                ],
            },
            data_conflicts=(),
            financial_resolution={
                "currency": "BRL",
                "recommended_refund_brl": 10.0,
                "refund_lines": [
                    {
                        "reason_code": "REFUND_PENDING",
                        "amount_brl": 10.0,
                        "entity_id": "ITEM_1",
                    }
                ],
            },
            resolution_actions=("FOLLOW_UP_REFUND",),
        ),
        evidence_refs=("ev_policy00000000000000001",),
    )


def make_registry(order_item, payment, shipment, policy, verifier) -> AgentRegistry:
    return AgentRegistry(
        order_item=RunnableLambda(order_item),
        payment=RunnableLambda(payment),
        shipment=RunnableLambda(shipment),
        policy=RunnableLambda(policy),
        verifier=RunnableLambda(verifier),
    )


async def passing_order_item(task: AgentTask) -> AgentResult:
    return completed_order_result(task)


async def passing_payment(task: AgentTask) -> AgentResult:
    return completed_payment_result(task)


async def passing_shipment(task: AgentTask) -> AgentResult:
    return completed_shipment_result(task)


async def passing_policy(task: AgentTask) -> AgentResult:
    return completed_policy_result(task)


async def passing_verifier(package: VerificationPackage) -> VerificationReport:
    return VerificationReport.passed()
```

The happy-path test must assert all of these behaviors in one end-to-end graph invocation:

```python
def test_coordinator_runs_staged_graph_and_builds_candidate() -> None:
    events: list[str] = []
    seen_contexts: dict[str, dict[str, object]] = {}
    payment_started = asyncio.Event()
    shipment_started = asyncio.Event()

    async def order_item(task: AgentTask) -> AgentResult:
        events.append("order_item")
        return completed_order_result(task)

    async def payment(task: AgentTask) -> AgentResult:
        assert "order_item" in task.context["active_results"]
        seen_contexts["payment"] = task.context
        payment_started.set()
        await shipment_started.wait()
        events.append("payment")
        return completed_payment_result(task)

    async def shipment(task: AgentTask) -> AgentResult:
        assert "order_item" in task.context["active_results"]
        seen_contexts["shipment"] = task.context
        shipment_started.set()
        await payment_started.wait()
        events.append("shipment")
        return completed_shipment_result(task)

    async def policy(task: AgentTask) -> AgentResult:
        assert set(task.context["active_results"]) == {
            "order_item", "payment", "shipment"
        }
        events.append("policy")
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        assert package.candidate_output is not None
        events.append("verifier")
        return VerificationReport.passed()

    output = asyncio.run(
        Coordinator().solve(
            {"case_id": "CASE_001"},
            make_registry(order_item, payment, shipment, policy, verifier),
            RecordingTrace(),
        )
    )

    assert events[0] == "order_item"
    assert events[-2:] == ["policy", "verifier"]
    assert output["schema_version"] == "day09-l3a-output-v2"
    assert output["case_id"] == "CASE_001"
    assert output["affected_entities"]["order_ids"] == ["ORDER_1"]
    assert output["evidence_refs"] == list(dict.fromkeys(output["evidence_refs"]))
    assert output["assessment"]["case_status"] == "action_required"
```

Also add `test_invalid_case_fails_before_invoking_agents`, passing `{"case_id": ""}` and asserting `CoordinatorError.code == "INVALID_CASE"` and an empty event list.

Add a conflict-preservation test whose Policy and Verifier handlers assert the
two source values remain separate:

```python
def test_conflicting_specialist_observations_are_preserved() -> None:
    async def payment(task: AgentTask) -> AgentResult:
        return AgentResult.completed(
            actor="payment",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=PaymentPayload(
                EntitySet(payment_references=("PAYMENT_1",)),
                (Observation("PAYMENT_STATUS", "PAYMENT_1", {"status": "paid"}),),
            ),
        )

    async def shipment(task: AgentTask) -> AgentResult:
        return AgentResult.completed(
            actor="shipment",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=ShipmentPayload(
                EntitySet(shipment_ids=("SHIPMENT_1",)),
                (Observation("PAYMENT_STATUS", "PAYMENT_1", {"status": "unknown"}),),
            ),
        )

    async def policy(task: AgentTask) -> AgentResult:
        active = task.context["active_results"]
        assert active["payment"].payload.observations[0].facts["status"] == "paid"
        assert active["shipment"].payload.observations[0].facts["status"] == "unknown"
        return completed_policy_result(task)

    async def verifier(package: VerificationPackage) -> VerificationReport:
        assert package.active_results["payment"].payload.observations
        assert package.active_results["shipment"].payload.observations
        return VerificationReport.passed()

    registry = make_registry(
        passing_order_item, payment, shipment, policy, verifier
    )
    asyncio.run(
        Coordinator().solve({"case_id": "CASE_001"}, registry, RecordingTrace())
    )
```

- [ ] **Step 2: Run the happy-path tests and confirm RED**

Run:

```powershell
pytest -q tests/test_coordinator.py -k "staged_graph or invalid_case"
```

Expected: collection fails because `student_agent.coordinator` does not exist.

- [ ] **Step 3: Implement state, graph topology, invocation, and assembly**

Create `src/student_agent/coordinator.py` with:

```python
@dataclass
class CaseState:
    case: dict[str, Any]
    histories: dict[Actor, list[AgentResult]]
    active_results: dict[Actor, AgentResult]
    invocations: dict[Actor, int]
    context_versions: dict[Actor, int]
    retry_counts: dict[Actor, int]
    feedback: dict[Actor, AgentFeedback]
    verification_history: list[VerificationReport]
    candidate_output: dict[str, Any] | None = None


class CoordinatorGraphState(TypedDict):
    case_state: CaseState
    latest_report: VerificationReport | None
    terminal_error: CoordinatorError | None


class CoordinatorContext(TypedDict):
    registry: AgentRegistry
    trace: TraceWriter
```

Compile one graph in `Coordinator.__init__`:

```python
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
```

Nodes receive `runtime: Runtime[CoordinatorContext]`. `_payment_and_shipment_node` creates both tasks from the same Order/Item snapshot and awaits them with `asyncio.gather`; record results into state only after both awaitables finish. Downstream nodes skip their runnable when required active results are absent so Verifier receives a package with the upstream failure.

`_assemble_candidate` must:

- require completed results for all four actors;
- union each `EntitySet` field in specialist order `order_item`, `payment`,
  `shipment`, preserving first occurrence;
- union result-level evidence references in the same actor order;
- copy semantic fields only from `PolicyPayload`;
- omit `claim_assessments` when Policy returns an empty tuple;
- never mutate an `AgentResult` payload.

Call the graph with:

```python
final_state = await self._graph.ainvoke(
    initial_state,
    context={"registry": registry, "trace": trace},
)
```

Raise `CoordinatorError` when the returned state contains `terminal_error`; otherwise require and return `case_state.candidate_output`.

- [ ] **Step 4: Run the focused and full tests**

Run:

```powershell
pytest -q tests/test_coordinator.py -k "staged_graph or invalid_case"
pytest -q
ruff check src tests
```

Expected: staged execution, concurrency proof, candidate assembly, and the existing suite pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/student_agent/coordinator.py tests/__init__.py tests/coordinator_fakes.py tests/test_coordinator.py
git commit -m "feat: add LangGraph coordinator happy path"
```

### Task 3: Failure context, recovery, and verifier-directed retry

**Files:**

- Modify: `src/student_agent/coordinator.py`
- Modify: `tests/test_coordinator.py`

**Interfaces:**

- Extends: `Coordinator.solve` behavior without changing its signature.
- Produces: stable coordinator error codes and dependency-aware `context_version` transitions.

- [ ] **Step 1: Write failing tests for coordinator task recovery**

Add tests proving:

```python
def test_agent_crash_is_recovered_once_without_verifier_round() -> None:
    calls = 0
    verifier_packages: list[VerificationPackage] = []

    async def order_item(task: AgentTask) -> AgentResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret backend detail")
        assert task.invocation == 2
        assert task.retry_count_for_context == 1
        return completed_order_result(task)

    registry = make_registry(
        order_item,
        passing_payment,
        passing_shipment,
        passing_policy,
        recording_passing_verifier(verifier_packages),
    )
    output = asyncio.run(
        Coordinator().solve({"case_id": "CASE_001"}, registry, RecordingTrace())
    )

    assert output["case_id"] == "CASE_001"
    assert calls == 2
    assert len(verifier_packages) == 1
    assert "secret backend detail" not in repr(verifier_packages)
```

Use these exact recording helpers alongside the passing handlers:

```python
def recording_passing_verifier(packages: list[VerificationPackage]):
    async def verifier(package: VerificationPackage) -> VerificationReport:
        packages.append(package)
        return VerificationReport.passed()

    return verifier
```

Add `test_exhausted_local_mcp_failure_is_not_recovered_by_coordinator`: return a failed Order/Item result with `MCP_TIMEOUT_EXHAUSTED`, assert the agent is invoked once, and assert Verifier receives that failed result in `attempt_history` with no candidate.

- [ ] **Step 2: Run the recovery tests and confirm RED**

Run:

```powershell
pytest -q tests/test_coordinator.py -k "crash_is_recovered or exhausted_local"
```

Expected: failures show missing bounded recovery and/or missing incomplete verification package.

- [ ] **Step 3: Implement bounded task recovery and safe exception normalization**

Implement a shared `_invoke_actor` helper. For each call it must construct a fresh `AgentTask`, emit `task_assigned`, await the LangChain runnable with `ainvoke`, and validate the result. When the runnable raises, normalize without copying `str(exc)`:

```python
error_code = "AGENT_TIMEOUT" if isinstance(exc, TimeoutError) else "AGENT_CRASHED"
safe_detail = "Agent task timed out" if error_code == "AGENT_TIMEOUT" else "Agent task failed"
```

Retry once for normalized coordinator-owned codes. Treat a `None` result as `AGENT_NO_RESPONSE` and retry once. Never automatically recover a returned `AgentResult.failed(...)`, including `MCP_TIMEOUT_EXHAUSTED`.

Append both coordinator-owned failed attempts and the eventual success to `histories[actor]`. Only a completed validated result becomes `active_results[actor]`.

- [ ] **Step 4: Write failing tests for semantic retries and invalid reports**

Add these behaviors as separate tests:

1. Verifier returns `retry_required(target_actor="payment")`, Payment invocation 2 receives the feedback and `retry_count_for_context == 1`, Policy recomputes at a higher `context_version` with retry count zero, then Verifier passes.
2. Verifier targets `order_item`; Order/Item, Payment, Shipment, and Policy all run again, and downstream context versions increase.
3. Verifier targets Policy; specialist invocation counts remain one.
4. Verifier requests a second same-context retry for Policy; raise `RETRY_LIMIT_EXCEEDED`.
5. Verifier returns `passed` when `candidate_output is None`; raise `INVALID_VERIFIER_REPORT`.
6. Verifier returns `retry_required` with `retryable=False`; raise `RETRY_NOT_ALLOWED`.
7. Verifier returns target actor text outside the `Actor` literal; raise `INVALID_RETRY_TARGET`.

Use a queued verifier runnable in tests:

```python
reports = iter(
    [
        VerificationReport.retry(
            target_actor="payment",
            error_code="MISSING_PAYMENT_EVIDENCE",
            feedback="Recheck payment status and charged amount",
        ),
        VerificationReport.passed(),
    ]
)

async def verifier(package: VerificationPackage) -> VerificationReport:
    packages.append(package)
    return next(reports)
```

- [ ] **Step 5: Run semantic-retry tests and confirm RED**

Run:

```powershell
pytest -q tests/test_coordinator.py -k "verifier or retry_limit or invalid_retry"
```

Expected: failures identify missing retry routing, invalidation, and report validation.

- [ ] **Step 6: Implement retry routing and dependency invalidation**

`_verify_node` builds an immutable `VerificationPackage` containing:

- current candidate or `None`;
- tuple history for every actor;
- copy of current active results;
- all active failed results not replaced by success;
- incremented verification round.

Validate reports before recording them. `_retry_target_node` must apply these cascades:

```python
DEPENDANTS = {
    "order_item": ("payment", "shipment", "policy"),
    "payment": ("policy",),
    "shipment": ("policy",),
    "policy": (),
}
```

For a semantic retry, increment the target's same-context retry count. Invalidate dependant active results, increment each dependant's `context_version`, reset each dependant's same-context retry count, and recompute them in dependency order. Order/Item retry recomputes Payment and Shipment concurrently, then Policy. Payment or Shipment retry recomputes Policy. Policy retry invokes only Policy.

Count every verifier call and stop before call six with `VERIFICATION_LIMIT_EXCEEDED`.

- [ ] **Step 7: Run focused tests, full suite, and Ruff**

Run:

```powershell
pytest -q tests/test_coordinator.py
pytest -q
ruff check src tests
```

Expected: all recovery, semantic retry, invalidation, safety, and pre-existing tests pass.

- [ ] **Step 8: Commit Task 3**

```powershell
git add src/student_agent/coordinator.py tests/test_coordinator.py
git commit -m "feat: add bounded coordinator retry routing"
```

### Task 4: Public workflow adapter, trace verification, and team handoff

**Files:**

- Modify: `src/student_agent/workflow.py`
- Create: `tests/test_workflow.py`
- Create: `docs/team-agent-implementation-guide.md`
- Modify: `ARCHITECTURE.md`

**Interfaces:**

- Consumes: `Coordinator`, `AgentRegistry`, existing `EvidenceGateway`, and `TraceWriter`.
- Produces: `solve_case(case, gateway, trace, *, registry=None)` and a stable integration guide for the other agent owners.

- [ ] **Step 1: Write failing public-adapter and real-trace tests**

Create `tests/test_workflow.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent.agent_contracts import AgentRegistry
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from tests.coordinator_fakes import (
    make_registry,
    passing_order_item,
    passing_payment,
    passing_policy,
    passing_shipment,
    passing_verifier,
)


def make_contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def make_passing_registry() -> AgentRegistry:
    return make_registry(
        passing_order_item,
        passing_payment,
        passing_shipment,
        passing_policy,
        passing_verifier,
    )


def test_solve_case_requires_explicit_registry(
    tmp_path: Path,
) -> None:
    contracts = make_contracts()
    gateway_stub = object()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    with pytest.raises(RuntimeError, match="AgentRegistry"):
        asyncio.run(
            solve_case(
                {"case_id": "CASE_001"},
                gateway_stub,
                trace,
            )
        )


def test_solve_case_with_registry_emits_schema_valid_trace(
    tmp_path: Path,
) -> None:
    contracts = make_contracts()
    gateway_stub = object()
    path = tmp_path / "trace.jsonl"
    trace = TraceWriter(path, contracts)

    output = asyncio.run(
        solve_case(
            {"case_id": "CASE_001"},
            gateway_stub,
            trace,
            registry=make_passing_registry(),
        )
    )

    assert output["case_id"] == "CASE_001"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {event["event_type"] for event in events} >= {
        "task_assigned",
        "handoff",
        "verification_completed",
    }
    for event in events:
        contracts.validate_trace(event, "test trace")
```

- [ ] **Step 2: Run workflow tests and confirm RED**

Run:

```powershell
pytest -q tests/test_workflow.py
```

Expected: `solve_case` rejects the new keyword argument or still raises the starter `NotImplementedError`.

- [ ] **Step 3: Wire the public adapter and schema-valid trace events**

Change `solve_case` to:

```python
async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    registry: AgentRegistry | None = None,
) -> dict[str, Any]:
    del gateway
    if registry is None:
        raise RuntimeError(
            "AgentRegistry is required until the team agent implementations are wired"
        )
    return await Coordinator().solve(case, registry, trace)
```

In coordinator trace emission, use only public-schema event types:

- `task_assigned` before every agent invocation;
- `handoff` after every returned or normalized result and every verifier retry decision;
- `verification_completed` after every report.

Keep `attributes` scalar-only. Put attempt counters and error codes in attributes, evidence identifiers in `evidence_refs`, actor role in `actor`, and retry target in `target`. Never include `safe_detail`, task context, payload, or raw exception text.

- [ ] **Step 4: Write the team implementation guide and update architecture record**

Create `docs/team-agent-implementation-guide.md` in Vietnamese with:

- the exact Task 1 dataclass fields;
- `RunnableLambda` adapter examples for one specialist, Policy, and Verifier;
- the factual-versus-semantic field ownership table;
- local retry ownership for MCP/model operations;
- coordinator recovery and semantic retry behavior;
- context-version/recomputation rules;
- completed and failed `AgentResult` examples;
- passed, retry, and failed `VerificationReport` examples;
- a checklist for Nam, Dat, and Huy to run Superpowers brainstorming, approve their role design, use TDD, and run the complete test suite before handoff.

Replace the unfinished coordinator sections in `ARCHITECTURE.md` with the finalized coordinator flow, actor ownership, retry table, observable trace rules, and loop limits. Do not document prompts or private reasoning.

- [ ] **Step 5: Run workflow tests, all tests, Ruff, and package metadata check**

Run:

```powershell
pytest -q tests/test_workflow.py
pytest -q
ruff check src tests
python -m pip check
```

Expected: every command exits zero; pytest reports the full suite with no failures; Ruff reports no diagnostics; pip reports no broken requirements.

- [ ] **Step 6: Review the final diff for scope and secrets**

Run:

```powershell
git diff --check
git diff --stat origin/phuc...HEAD
rg -n "sk-team-[A-Za-z0-9_-]{16,128}" src tests docs ARCHITECTURE.md
```

Expected: diff check is clean; changes are limited to coordinator contracts/orchestration/tests/docs; the secret scan finds only deliberate test placeholders or documentation warnings and no real credential.

- [ ] **Step 7: Commit Task 4**

```powershell
git add src/student_agent/workflow.py src/student_agent/coordinator.py tests/test_workflow.py docs/team-agent-implementation-guide.md ARCHITECTURE.md
git commit -m "docs: document coordinator agent integration"
```

## Final verification

- [ ] Run `pytest -q` and record the exact passed-test count.
- [ ] Run `ruff check src tests` and record its zero-diagnostic result.
- [ ] Run `python -m pip check` and record whether the environment has unrelated pre-existing conflicts.
- [ ] Run `git status --short` and confirm only intended plan-tracking changes remain.
- [ ] Compare every success criterion in the design spec with its implementing test before claiming completion.
