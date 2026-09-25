# Coordinator Context and Verifier-Directed Retry Design

## Purpose

Implement only the L3A coordinator layer. The coordinator must retain structured
execution context, assemble the final candidate output from team-owned result
fragments, give the verifier enough observable state to diagnose a failed step,
and perform bounded retries directed by verifier feedback.

The Order/Item, Payment, and Shipment agents are owned by Nam. The Policy agent
is owned by Dat. The Verifier is owned by Huy. This change defines their shared
interfaces but does not implement any of those agents.

## Success criteria

- Each agent can be implemented and tested independently against a typed contract.
- The coordinator records every attempt and never discards earlier results.
- The verifier receives agent results, failures, evidence references, attempts,
  and the current candidate output without receiving private reasoning.
- A verifier retry decision identifies one actor and causes only the necessary
  work to be repeated.
- Deterministic tool and model failures are retried locally in gateway/sub-agent
  code without spending a coordinator invocation or verifier call.
- A specialist retry invalidates and reruns Policy because Policy depends on all
  specialist results.
- Retry loops are bounded by both per-actor and workflow-wide limits.
- The coordinator never fabricates output fields or evidence to recover from a
  failed workflow.
- Public trace events conform to `trace-event-v1.schema.json`.

## Scope

### Included

- Shared coordinator-facing dataclasses and callable protocols.
- Per-case execution state and immutable attempt history.
- Staged specialist dispatch: `order_item` discovery first, followed by parallel
  `payment` and `shipment` work using the normalized entity context.
- Policy dispatch after the required specialist results are available.
- Deterministic candidate-output assembly.
- Verification package creation and verifier-directed retry routing.
- Retry-limit and invalid-feedback handling.
- Observable trace emission.
- Unit tests using deterministic fake agent callables.
- A Markdown integration guide for the other team members.

### Excluded

- Domain reasoning inside specialist, Policy, or Verifier agents.
- LLM prompts, provider SDK selection, and model configuration.
- MCP transport retry inside `EvidenceGateway`.
- Persisting private prompts, transcripts, or chain-of-thought.
- Inventing a fallback competition answer when required agent output is absent.

## Framework architecture

The coordinator uses LangGraph `StateGraph` as its explicit workflow state
machine and LangChain `Runnable` objects as the execution contract for concrete
agents.

Required dependency ranges:

- `langgraph>=1.2,<2`
- `langchain>=1.4,<2`

`CoordinatorState` is a typed graph state containing `CaseState`, the current
candidate, the latest verification report, and terminal error information. A
small runtime context exposes the immutable `AgentRegistry` and trace writer to
nodes without serializing those objects into graph state.

The compiled graph contains these phases:

```text
START
  -> order_item
  -> payment_and_shipment
  -> policy
  -> assemble
  -> verify
  -> route_after_verification
       -> END when passed
       -> retry_target -> assemble when retryable
       -> fail -> END for a terminal coordinator error
```

`payment_and_shipment` invokes both LangChain runnables concurrently with
`asyncio.gather` after Order/Item discovery. `retry_target` invokes only the
target actor, plus downstream recomputations required by dependency changes.
This keeps graph topology understandable while preserving targeted retry
semantics.

Gateway/sub-agent local retries remain inside their concrete runnable and are
not implemented as LangGraph node retries. Coordinator task recovery for
`AGENT_TIMEOUT`, `AGENT_CRASHED`, and `AGENT_NO_RESPONSE` is bounded inside the
shared agent-invocation helper so one failed concurrent actor does not force an
unrelated actor to rerun.

The graph is compiled without a persistent checkpointer in this scope. State is
isolated per `ainvoke` call and the existing public JSONL trace remains the
submission audit artifact. Persistence, resume-after-process-restart, LangSmith,
and human-in-the-loop interrupts are out of scope.

Official API references used by the implementation plan:

- https://reference.langchain.com/python/langgraph/graph/state/StateGraph
- https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_node
- https://reference.langchain.com/python/langgraph/types/RetryPolicy

## Components and ownership

### `student_agent.agent_contracts`

This module is the stable boundary shared by all team members.

`AgentTask` contains:

- `case_id`: correlation identifier.
- `actor`: target role.
- `invocation`: one-based total call number for that actor.
- `context_version`: version of the upstream dependency snapshot.
- `retry_count_for_context`: retry number for the same snapshot; zero for the
  first call and for a recomputation after dependencies change.
- `case`: original case payload.
- `context`: a scoped, serializable snapshot of successful upstream results.
- `feedback`: the most recent verifier feedback for this actor, or `None`.

`AgentResult` contains:

- `actor`, `invocation`, `context_version`, and `retry_count_for_context`
  matching the task.
- `status`: `completed` or `failed`.
- A role-specific typed payload when completed. `OrderItemPayload`,
  `PaymentPayload`, `ShipmentPayload`, and `PolicyPayload` prevent arbitrary
  dictionaries from becoming an implicit cross-team API.
- `evidence_refs`: evidence actually consumed by the actor.
- `tool_calls`: observable tool-call summaries.
- `failed_stage`, `error_code`, `retryable`, and a sanitized `safe_detail` for a
  failed attempt. Raw exceptions and MCP response bodies are not shared.

The three specialist payloads contain factual findings, normalized entities,
and evidence-backed observations. They do not make the final policy decision.
`PolicyPayload` contains the semantic fields needed in the final answer:
assessment, optional claim assessments, root cause analysis, data conflicts,
financial resolution, and resolution actions.

Each tool-call summary records its local attempt count and final status. Local
retry details are observable to coordinator after the agent returns, but the
coordinator does not execute those retries.

`VerificationPackage` contains:

- The case identifier and verification round.
- The current candidate output, or `None` when upstream failure prevents assembly.
- Complete per-actor attempt history.
- Active results used for this round.
- Unresolved workflow failures.

`VerificationReport` contains:

- `verdict`: `passed`, `retry_required`, or `failed`.
- `target_actor`: required only for `retry_required`.
- `error_code`, `feedback`, and `retryable`.

`AgentRegistry` contains async callables for `order_item`, `payment`, `shipment`,
`policy`, and `verifier`. The coordinator depends only on this registry and not
on concrete agent modules.

### `student_agent.coordinator`

`Coordinator` owns execution order, state transitions, merge behavior, trace
events, and retry limits. It does not perform domain reasoning.

`CaseState` stores:

- Original case and case identifier.
- Attempt history keyed by actor.
- Current active result keyed by actor.
- Current candidate output.
- Verification history.
- Workflow round count.
- Agent invocation count, dependency `context_version`, and same-context retry
  count as separate values.

Unexpected exceptions from an agent callable are normalized into a failed
`AgentResult`; this lets the verifier see the failed actor and stage instead of
losing the workflow context.

### `student_agent.workflow`

The existing public `solve_case` entry point delegates to `Coordinator`. Until
the other team members provide concrete agents, callers use an explicit
`AgentRegistry`. The integration boundary must fail clearly when a registry is
missing; it must not silently substitute fake agents.

## Data flow

1. Validate that the input has a usable `case_id`.
2. Dispatch Order/Item with attempt `1` to resolve the authoritative order,
   item, seller, payment, and shipment identifiers available for downstream use.
3. Store the result or normalized failure in `CaseState`.
4. When Order/Item completes, dispatch Payment and Shipment concurrently with
   the normalized entity context. Store every result or normalized failure.
5. If every required specialist completed, dispatch Policy with a context
   snapshot containing active specialist results.
6. Assemble `candidate_output` when all required active results are complete.
7. Build a `VerificationPackage` and invoke Verifier. The package may contain a
   `None` candidate when an upstream failure needs diagnosis.
8. On `passed`, return the candidate only if it exists.
9. On `retry_required`, validate the target and limits, append the feedback,
   invoke the target again, and preserve the earlier attempt.
10. When a specialist is retried successfully, invalidate and rerun its
   downstream dependants before rebuilding the candidate. Retrying Order/Item
   reruns Payment, Shipment, and Policy; retrying Payment or Shipment reruns
   Policy.
11. On `failed`, invalid feedback, or exhausted limits, raise a structured
    coordinator exception. Do not manufacture an output.

Before returning an `AgentResult`, gateway/sub-agent code locally retries
allowlisted transient tool failures. Coordinator does not rerun the whole agent
for `MCP_TIMEOUT`, `CONNECTION_RESET`, `RATE_LIMITED`, or `HTTP_5XX`. Sub-agent
code may perform one deterministic repair when a model response is invalid.

Coordinator only performs operational recovery when the agent task itself
raises, times out, crashes, or returns no result. Permanent, semantic, or
unknown completed-agent failures remain visible to Verifier.

Payment and Shipment run concurrently only after Order/Item discovery, so each
receives a stable identifier set. Retries are sequential so each verification
round observes a stable state.

## Candidate-output ownership and merge rules

The coordinator always writes:

- `schema_version = "day09-l3a-output-v2"`
- `case_id`

Policy exclusively owns:

- `assessment`
- `claim_assessments` when claims are emitted
- `root_cause_analysis`
- `data_conflicts`
- `financial_resolution`
- `resolution_actions`

The following fields are collected from all completed agents:

- `affected_entities`: union each entity list while preserving first-seen order.
- `evidence_refs`: union while preserving first-seen order.

Specialist observations are retained in the verification context but are not
copied into final semantic fields.

Role-specific payload validation rejects unknown or foreign-owned fields before
assembly. Conflicting specialist observations remain explicit inputs to Policy
and Verifier; coordinator does not resolve semantic conflicts by applying
last-write-wins behavior.

The assembled candidate is validated later by the existing output contract at
the CLI boundary. Verifier receives the candidate before finalization and may
request a targeted correction.

## Retry policy

Defaults:

- Maximum local attempts for one tool/model operation: `2`, owned by the
  gateway or sub-agent implementation.
- Maximum coordinator recovery attempts for one agent task and context version:
  `1` retry after the initial invocation.
- Maximum semantic retries for one actor and context version: `1`.
- Maximum verification rounds: `5`.
- Valid retry targets: `order_item`, `payment`, `shipment`, and `policy`.
- Verifier itself is not a retry target in the initial implementation.
- Coordinator operational recovery is restricted to `AGENT_TIMEOUT`,
  `AGENT_CRASHED`, and `AGENT_NO_RESPONSE`.

There are three retry paths with exactly one owner per failure class:

1. Gateway/sub-agent local retry for `MCP_TIMEOUT`, `CONNECTION_RESET`,
   `RATE_LIMITED`, `HTTP_5XX`, and one invalid-model-response repair.
2. Coordinator task recovery for `AGENT_TIMEOUT`, `AGENT_CRASHED`, or
   `AGENT_NO_RESPONSE`.
3. Verifier-directed retry for semantic, evidence, scope, or consistency errors;
   coordinator validates the report and performs the invocation.

Retry policies must not stack for the same error code. For example, an MCP
timeout exhausted by sub-agent code is returned as `MCP_TIMEOUT_EXHAUSTED` and
is not automatically retried again by coordinator.

A retry request is accepted only when:

- Verdict is `retry_required`.
- `retryable` is `true`.
- `target_actor` is valid.
- That actor has remaining same-context semantic retry allowance.
- The workflow has remaining verification rounds.

Retrying a specialist invalidates the active Policy result even if the specialist
fails again. Retrying Order/Item also invalidates and reruns Payment and Shipment
because their inputs may have changed. Policy runs again only after all
specialists have active completed results. Retrying Policy preserves specialist
results.

An invocation caused by changed upstream data is a recomputation, not a retry.
It increments `agent_invocation` and `context_version`, but resets the
same-context retry count. This prevents a correct Policy agent from losing its
retry allowance merely because Payment or Shipment produced a new result.

## Error handling

Coordinator exceptions use stable error codes for tests and integration:

- `INVALID_CASE`
- `INVALID_AGENT_RESULT`
- `INVALID_VERIFIER_REPORT`
- `INVALID_RETRY_TARGET`
- `RETRY_NOT_ALLOWED`
- `RETRY_LIMIT_EXCEEDED`
- `VERIFICATION_LIMIT_EXCEEDED`
- `WORKFLOW_FAILED`

Error messages may aid developers but routing logic must use codes, not parse
free-form text.

The stable operational codes are owned as follows:

- Gateway/sub-agent: `MCP_TIMEOUT`, `CONNECTION_RESET`, `RATE_LIMITED`,
  `HTTP_5XX`, `INVALID_MODEL_RESPONSE`.
- Coordinator: `AGENT_TIMEOUT`, `AGENT_CRASHED`, `AGENT_NO_RESPONSE`.
- Verifier feedback: domain-specific semantic codes such as
  `MISSING_EVIDENCE`, `WRONG_ENTITY_SCOPE`, or `POLICY_RULE_MISAPPLIED`.

## Trace behavior

The coordinator emits only observable events allowed by the existing schema:

- `task_assigned` before each agent attempt.
- `handoff` after an agent result or verifier-directed retry.
- `verification_completed` after each verifier report.

`decision_code` carries stable status/retry codes. `attributes` may contain
scalar values such as attempt, verification round, status, error code, and failed
stage. `evidence_refs` contains only validated evidence identifiers supplied by
agents. Prompts, raw conversations, raw exceptions, raw MCP bodies, secrets, and
private reasoning are excluded.

## Testing strategy

Tests use async fake callables and real coordinator state transitions. They do
not test a mock framework's invocation bookkeeping as a substitute for behavior.

Required cases:

- Happy path builds the expected candidate and preserves evidence/entity order.
- Order/Item completes before Payment and Shipment receive their tasks.
- Payment and Shipment receive the normalized entity context from Order/Item.
- Verifier sees a failed specialist attempt in its package.
- Allowlisted MCP/model failures retry inside sub-agent code without another
  coordinator invocation or verifier round.
- An exhausted local MCP retry does not trigger an automatic coordinator retry.
- Agent task timeout/crash/no-response triggers bounded coordinator recovery.
- Non-allowlisted failures remain visible to Verifier.
- Verifier retries Payment once and the second result replaces only the active
  Payment result while history retains both attempts.
- A specialist retry causes Policy to rerun with the updated context.
- An Order/Item retry invalidates Payment, Shipment, and Policy.
- A Policy retry does not rerun specialists.
- Policy recomputation after an upstream context change does not consume its
  same-context semantic retry allowance.
- Invalid and non-retryable verifier feedback is rejected.
- Per-actor and global verification limits stop loops deterministically.
- Unexpected agent exceptions become structured failed results.
- Role-specific payload validation rejects fields owned by another role.
- Conflicting specialist observations reach Policy and Verifier without silent
  overwrite.
- Raw exception details do not enter verification packages or traces.
- Trace events validate against the public trace schema.

The full existing pytest suite and Ruff checks must pass before completion.

## Team handoff document

`docs/team-agent-implementation-guide.md` will document:

- Exact callable signatures and field ownership.
- Role-specific payload models and factual-versus-semantic ownership.
- Completed and failed result examples.
- Verification report examples.
- Retry semantics and dependency invalidation.
- How to test an implementation with fake tasks.
- A Superpowers workflow for each owner: brainstorm the agent behavior, approve
  its design, write failing contract tests, implement minimally, and verify the
  complete suite before handoff.

## Assumptions

- Every input case has a string `case_id`.
- Agent payloads and context snapshots are JSON-compatible dictionaries.
- Order/Item is the authoritative discovery stage unless the final input
  contract proves that all downstream identifiers are independently available;
  that later optimization must not change the shared result contracts.
- Evidence identifiers have already been obtained through the authorized MCP
  gateway; the coordinator never creates them.
- Concrete agents will be registered later without changing coordinator logic.
