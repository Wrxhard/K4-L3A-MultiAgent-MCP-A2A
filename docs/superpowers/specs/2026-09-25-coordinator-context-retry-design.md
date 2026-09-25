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
- Specialist dispatch for `order_item`, `payment`, and `shipment`.
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

## Components and ownership

### `student_agent.agent_contracts`

This module is the stable boundary shared by all team members.

`AgentTask` contains:

- `case_id`: correlation identifier.
- `actor`: target role.
- `attempt`: one-based attempt number for that actor.
- `case`: original case payload.
- `context`: a scoped, serializable snapshot of successful upstream results.
- `feedback`: the most recent verifier feedback for this actor, or `None`.

`AgentResult` contains:

- `actor` and `attempt` matching the task.
- `status`: `completed` or `failed`.
- `output_fragment`: actor-owned candidate fields when completed.
- `evidence_refs`: evidence actually consumed by the actor.
- `tool_calls`: observable tool-call summaries.
- `failed_stage`, `error_code`, and `error_message` for a failed attempt.

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
2. Dispatch Order/Item, Payment, and Shipment with attempt `1`.
3. Store every returned result or normalized failure in `CaseState`.
4. If every required specialist completed, dispatch Policy with a context
   snapshot containing active specialist results.
5. Assemble `candidate_output` when all required active results are complete.
6. Build a `VerificationPackage` and invoke Verifier. The package may contain a
   `None` candidate when an upstream failure needs diagnosis.
7. On `passed`, return the candidate only if it exists.
8. On `retry_required`, validate the target and limits, append the feedback,
   invoke the target again, and preserve the earlier attempt.
9. When a specialist is retried successfully, invalidate and rerun Policy before
   rebuilding the candidate.
10. On `failed`, invalid feedback, or exhausted limits, raise a structured
    coordinator exception. Do not manufacture an output.

Specialists may initially be dispatched concurrently because they depend only
on the original case. Retries are sequential so each verification round observes
a stable state.

## Candidate-output ownership and merge rules

The coordinator always writes:

- `schema_version = "day09-l3a-output-v2"`
- `case_id`

Payment exclusively owns:

- `financial_resolution`

Policy exclusively owns:

- `assessment`
- `root_cause_analysis`
- `data_conflicts`
- `resolution_actions`

The following fields are collected from all completed agents:

- `affected_entities`: union each entity list while preserving first-seen order.
- `evidence_refs`: union while preserving first-seen order.
- `claim_assessments`: concatenate unique `claim_id` values.

An actor may only return fields assigned above. Unknown or foreign-owned fields
are coordinator contract errors. Conflicting duplicate claim IDs are contract
errors rather than last-write-wins merges.

The assembled candidate is validated later by the existing output contract at
the CLI boundary. Verifier receives the candidate before finalization and may
request a targeted correction.

## Retry policy

Defaults:

- Maximum attempts per actor: `2` total, including the first attempt.
- Maximum verification rounds: `5`.
- Valid retry targets: `order_item`, `payment`, `shipment`, and `policy`.
- Verifier itself is not a retry target in the initial implementation.

A retry request is accepted only when:

- Verdict is `retry_required`.
- `retryable` is `true`.
- `target_actor` is valid.
- That actor has remaining attempts.
- The workflow has remaining verification rounds.

Retrying a specialist invalidates the active Policy result even if the specialist
fails again. Policy runs again only after all specialists have active completed
results. Retrying Policy preserves specialist results.

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

## Trace behavior

The coordinator emits only observable events allowed by the existing schema:

- `task_assigned` before each agent attempt.
- `handoff` after an agent result or verifier-directed retry.
- `verification_completed` after each verifier report.

`decision_code` carries stable status/retry codes. `attributes` may contain
scalar values such as attempt, verification round, status, error code, and failed
stage. `evidence_refs` contains only validated evidence identifiers supplied by
agents. Prompts, raw conversations, secrets, and private reasoning are excluded.

## Testing strategy

Tests use async fake callables and real coordinator state transitions. They do
not test a mock framework's invocation bookkeeping as a substitute for behavior.

Required cases:

- Happy path builds the expected candidate and preserves evidence/entity order.
- Verifier sees a failed specialist attempt in its package.
- Verifier retries Payment once and the second result replaces only the active
  Payment result while history retains both attempts.
- A specialist retry causes Policy to rerun with the updated context.
- A Policy retry does not rerun specialists.
- Invalid and non-retryable verifier feedback is rejected.
- Per-actor and global verification limits stop loops deterministically.
- Unexpected agent exceptions become structured failed results.
- Actor ownership and duplicate-claim conflicts fail candidate assembly.
- Trace events validate against the public trace schema.

The full existing pytest suite and Ruff checks must pass before completion.

## Team handoff document

`docs/team-agent-implementation-guide.md` will document:

- Exact callable signatures and field ownership.
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
- Evidence identifiers have already been obtained through the authorized MCP
  gateway; the coordinator never creates them.
- Concrete agents will be registered later without changing coordinator logic.
