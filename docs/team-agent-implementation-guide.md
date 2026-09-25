# Hướng dẫn tích hợp agent với Coordinator

Tài liệu này là hợp đồng bàn giao cho Nam (Order/Item, Payment, Shipment) và anh Đạt (Policy). Coordinator và Verifier không phụ thuộc vào prompt hoặc model cụ thể; mỗi agent chỉ cần được bọc thành LangChain `Runnable` và tuân thủ các dataclass trong `student_agent.agent_contracts`.

## 1. Luồng chạy

```text
Order/Item
    ├── Payment ──┐
    └── Shipment ─┼── Policy ── assemble ── Verifier
                  └───────────────────────────┘
```

Payment và Shipment chạy song song sau khi Order/Item thành công. Policy chỉ chạy khi cả ba specialist có kết quả `completed`. Verifier nhận candidate cùng toàn bộ lịch sử attempt; nếu yêu cầu sửa, Coordinator gọi lại đúng actor và các dependency cần thiết.

## 2. Hợp đồng dữ liệu chính xác

- `EntitySet`: `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`; tất cả là `tuple[str, ...]`.
- `Observation`: `code`, `subject_id`, `facts`, `evidence_refs`.
- `ToolCallSummary`: `tool_name`, `attempts`, `status`, `evidence_refs`, `error_code`.
- `OrderItemPayload`, `PaymentPayload`, `ShipmentPayload`: `entities`, `observations`.
- `PolicyPayload`: `assessment`, `claim_assessments`, `root_cause_analysis`, `data_conflicts`, `financial_resolution`, `resolution_actions`.
- `AgentFeedback`: `error_code`, `message`, `required_checks`.
- `AgentTask`: `case_id`, `actor`, `invocation`, `context_version`, `retry_count_for_context`, `case`, `context`, `feedback`.
- `AgentResult`: `actor`, `invocation`, `context_version`, `retry_count_for_context`, `status`, `payload`, `evidence_refs`, `tool_calls`, `failed_stage`, `error_code`, `retryable`, `safe_detail`.
- `VerificationPackage`: `case_id`, `verification_round`, `candidate_output`, `attempt_history`, `active_results`, `unresolved_failures`.
- `VerificationReport`: `verdict`, `target_actor`, `error_code`, `feedback`, `retryable`.
- `AgentRegistry`: `order_item`, `payment`, `shipment`, `policy`, `verifier`.

Không tự tạo hoặc sửa `evidence_ref`. Một result thành công phải dùng đúng payload theo actor. Luôn sao chép bốn field từ task sang result: `actor`, `invocation`, `context_version`, `retry_count_for_context`.

## 3. Mẫu specialist

```python
from langchain_core.runnables import RunnableLambda
from student_agent.agent_contracts import (
    AgentResult, AgentTask, EntitySet, Observation, OrderItemPayload,
)

async def run_order_item(task: AgentTask) -> AgentResult:
    evidence = await lookup_order_and_items(task.case_id, task.case)
    payload = OrderItemPayload(
        entities=EntitySet(
            order_ids=tuple(evidence.order_ids),
            item_ids=tuple(evidence.item_ids),
            seller_ids=tuple(evidence.seller_ids),
            payment_references=tuple(evidence.payment_references),
            shipment_ids=tuple(evidence.shipment_ids),
        ),
        observations=(Observation(
            code="ORDER_FOUND",
            subject_id=evidence.order_ids[0],
            facts={"status": evidence.status},
            evidence_refs=tuple(evidence.evidence_refs),
        ),),
    )
    return AgentResult.completed(
        actor=task.actor,
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=payload,
        evidence_refs=tuple(evidence.evidence_refs),
    )

order_item_runnable = RunnableLambda(run_order_item)
```

Nếu không thể hoàn tất, trả failure có cấu trúc; không raise cho lỗi nghiệp vụ đã biết:

```python
return AgentResult.failed(
    actor=task.actor,
    invocation=task.invocation,
    context_version=task.context_version,
    retry_count_for_context=task.retry_count_for_context,
    error_code="ORDER_NOT_FOUND",
    failed_stage="get_order",
    retryable=False,
    safe_detail="Order evidence was not found",
)
```

`safe_detail` phải ngắn, an toàn để quan sát và không chứa key, raw response, prompt hoặc exception text.

## 4. Mẫu Policy và ownership

Policy đọc `task.context["active_results"]`, tổng hợp facts của ba specialist và chỉ sở hữu các trường semantic:

```python
async def run_policy(task: AgentTask) -> AgentResult:
    active = task.context["active_results"]
    payload = build_policy_payload(active, task.feedback)
    return AgentResult.completed(
        actor="policy",
        invocation=task.invocation,
        context_version=task.context_version,
        retry_count_for_context=task.retry_count_for_context,
        payload=payload,
        evidence_refs=collect_used_evidence(active),
    )

policy_runnable = RunnableLambda(run_policy)
```

| Nhóm dữ liệu | Actor sở hữu |
| --- | --- |
| Order, item, seller và liên kết payment/shipment | Order/Item |
| Trạng thái charge, refund, amount và currency nguồn | Payment |
| Tracking, carrier, timeline và trạng thái giao hàng | Shipment |
| Assessment, claim decision, root cause, conflict, financial recommendation và action | Policy |
| Merge top-level, union entity/evidence, counter và routing | Coordinator |
| Schema, evidence linkage, consistency, money total và retry recommendation | Verifier |

Policy không được thay facts nguồn để làm output “đẹp hơn”. Khi nguồn xung đột, ghi rõ `data_conflicts` và chọn nguồn có căn cứ.

## 5. Retry và context

Có ba owner retry tách biệt:

1. Gateway/sub-agent retry tối đa một lần cho lỗi tool/model tạm thời như timeout, connection reset, rate limit, HTTP 5xx hoặc một lần sửa model response sai format.
2. Coordinator recovery tối đa một lần khi toàn agent task timeout, crash hoặc không trả response.
3. Verifier yêu cầu semantic retry tối đa một lần cho mỗi actor và cùng `context_version`.

Khi local retry đã hết, agent trả mã `*_EXHAUSTED`; Coordinator không tự retry lỗi đó. Một result `failed` đã trả về cũng không được Coordinator operational-retry.

- Retry Order/Item chạy lại Payment, Shipment, Policy với context version mới.
- Retry Payment hoặc Shipment chạy lại Policy với context version mới.
- Retry Policy không chạy lại specialist.
- Recompute do upstream đổi có retry count bằng 0; đó không phải semantic retry.
- Workflow gọi Verifier tối đa 5 round.

## 6. Verifier

Verifier mặc định là deterministic và có thể thêm semantic reviewer:

```python
from langchain_core.runnables import RunnableLambda
from student_agent.verifier import DeterministicVerifier

async def semantic_review(package):
    return VerificationReport.passed()

semantic = RunnableLambda(semantic_review)
verifier = DeterministicVerifier(
    contracts,
    semantic_verifier=semantic,
).as_runnable()
```

Các dạng report hợp lệ:

```python
VerificationReport.passed()
VerificationReport.retry("payment", "MISSING_PAYMENT_EVIDENCE", "Recheck payment")
VerificationReport.failed("CANDIDATE_SCHEMA_INVALID", "Coordinator merge is invalid")
```

Verifier chỉ đề xuất target; Coordinator xác thực target, retryable flag, budget và dependency trước khi chạy lại.

## 7. Đăng ký agent

```python
registry = AgentRegistry(
    order_item=RunnableLambda(run_order_item),
    payment=RunnableLambda(run_payment),
    shipment=RunnableLambda(run_shipment),
    policy=RunnableLambda(run_policy),
    verifier=verifier,
)

output = await solve_case(case, gateway, trace, registry=registry)
```

`.env` được `Settings.load(root)` nạp từ đúng root dự án. Không đọc, log hoặc truyền API key vào task, result hay trace.

## 8. Checklist bàn giao

- Chạy Superpowers brainstorming và thống nhất thiết kế riêng cho role trước khi code.
- Được người phụ trách approve thiết kế role và tool ownership.
- Viết test RED cho happy path, not-found, malformed response và local retry exhausted.
- Implement bằng TDD; không thay contract hoặc Coordinator ngầm.
- Validate mọi MCP response trước khi tạo observation/result.
- Chạy `pytest -q` và `ruff check src tests` trên toàn repo.
- Bàn giao Runnable, test, danh sách tool được phép gọi và các error code ổn định.
