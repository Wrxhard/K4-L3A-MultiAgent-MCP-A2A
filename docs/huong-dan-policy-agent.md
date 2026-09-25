# Hướng dẫn triển khai Policy Agent

> **Người phụ trách:** Anh Đạt
>
> **Vai trò:** Nhận dữ kiện từ các specialist (Order/Item, Payment, Shipment),
> áp dụng policy nghiệp vụ của hệ thống thương mại điện tử, và trả về quyết
> định cuối cùng dưới dạng `PolicyPayload`.

---

## Mục lục

1. [Tổng quan vai trò Policy Agent](#1-tổng-quan-vai-trò-policy-agent)
2. [Vị trí trong workflow](#2-vị-trí-trong-workflow)
3. [Input — AgentTask](#3-input--agenttask)
4. [Output — AgentResult với PolicyPayload](#4-output--agentresult-với-policypayload)
5. [Chi tiết từng field của PolicyPayload](#5-chi-tiết-từng-field-của-policypayload)
6. [Sử dụng MCP Evidence Gateway](#6-sử-dụng-mcp-evidence-gateway)
7. [Emit trace event](#7-emit-trace-event)
8. [Xử lý lỗi và retry](#8-xử-lý-lỗi-và-retry)
9. [Những gì Verifier sẽ kiểm tra Policy](#9-những-gì-verifier-sẽ-kiểm-tra-policy)
10. [Quy tắc quan trọng — KHÔNG được vi phạm](#10-quy-tắc-quan-trọng--không-được-vi-phạm)
11. [Ví dụ code triển khai](#11-ví-dụ-code-triển-khai)
12. [Checklist TDD để triển khai](#12-checklist-tdd-để-triển-khai)
13. [Tham khảo thêm](#13-tham-khảo-thêm)

---

## 1. Tổng quan vai trò Policy Agent

Policy Agent là agent **cuối cùng** trong chuỗi specialist. Nó **không** thu
thập dữ liệu trực tiếp từ MCP (trừ khi cần gọi tool `get_policy` để lấy quy
tắc nghiệp vụ). Thay vào đó, Policy nhận toàn bộ dữ kiện đã xác minh từ ba
specialist trước đó và đưa ra **quyết định nghiệp vụ**.

```
Specialist data flow:
  Order/Item → Payment + Shipment (song song) → Policy → Coordinator assembles output
```

**Policy sở hữu các trường nghiệp vụ sau trong output cuối cùng:**

| Field                  | Mô tả                                         |
| ---------------------- | ---------------------------------------------- |
| `assessment`           | Đánh giá chính: vấn đề chính, trạng thái, confidence |
| `claim_assessments`    | Đánh giá từng claim của khách hàng (nếu có)    |
| `root_cause_analysis`  | Phân tích nguyên nhân gốc, xếp hạng, bên chịu trách nhiệm |
| `data_conflicts`       | Mâu thuẫn dữ liệu giữa các source             |
| `financial_resolution` | Quyết định hoàn tiền: tổng, chi tiết từng dòng |
| `resolution_actions`   | Danh sách hành động giải quyết                 |

> **Lưu ý:** Policy **không** sở hữu `affected_entities`, `evidence_refs` ở cấp
> output, `schema_version`, hay `case_id`. Những trường đó do Coordinator xử lý.

---

## 2. Vị trí trong workflow

```text
┌──────────────┐
│  Input case  │
└──────┬───────┘
       ▼
┌──────────────┐
│  Order/Item  │  ← Chạy đầu tiên, xác nhận order_id, item_id, ...
└──────┬───────┘
       ▼
┌──────────────────────────┐
│  Payment  +   Shipment   │  ← Chạy song song sau Order/Item
└──────────┬───────────────┘
           ▼
┌──────────────┐
│   POLICY     │  ← BẠN Ở ĐÂY — nhận tất cả dữ kiện, đưa ra quyết định
└──────┬───────┘
       ▼
┌──────────────┐
│  Coordinator │  ← Ghép candidate output
│  assembles   │
└──────┬───────┘
       ▼
┌──────────────┐
│  Verifier    │  ← Kiểm tra, có thể yêu cầu Policy retry
└──────────────┘
```

Policy **chỉ** được gọi khi cả ba specialist đã hoàn thành thành công. Nếu một
specialist fail, Policy sẽ không được gọi cho đến khi specialist đó được retry
thành công.

---

## 3. Input — AgentTask

Khi Coordinator gọi Policy agent, bạn sẽ nhận một `AgentTask` với cấu trúc:

```python
from student_agent.agent_contracts import AgentTask

@dataclass(frozen=True)
class AgentTask:
    case_id: str                        # ID của case đang xử lý
    actor: Literal["policy"]            # Luôn là "policy" cho agent của bạn
    invocation: int                     # Lần gọi thứ mấy (bắt đầu từ 1)
    context_version: int                # Phiên bản input phụ thuộc
    retry_count_for_context: int        # Số lần retry cùng context (0 = lần đầu)
    case: Mapping[str, JSONValue]       # Payload gốc của case (customer message, ...)
    context: Mapping[str, Any]          # ← QUAN TRỌNG: chứa active_results
    feedback: AgentFeedback | None      # Feedback từ Verifier nếu đây là retry
```

### Cách đọc context

`task.context["active_results"]` là dict chứa kết quả của các specialist:

```python
active_results = task.context["active_results"]

# Truy cập dữ kiện Order/Item
order_result: AgentResult = active_results["order_item"]
order_payload: OrderItemPayload = order_result.payload
# order_payload.entities  → EntitySet(order_ids=(...), item_ids=(...), ...)
# order_payload.observations → tuple[Observation, ...]

# Truy cập dữ kiện Payment
payment_result: AgentResult = active_results["payment"]
payment_payload: PaymentPayload = payment_result.payload
# payment_payload.entities → EntitySet(payment_references=(...), ...)
# payment_payload.observations → tuple[Observation, ...]

# Truy cập dữ kiện Shipment
shipment_result: AgentResult = active_results["shipment"]
shipment_payload: ShipmentPayload = shipment_result.payload
# shipment_payload.entities → EntitySet(shipment_ids=(...), ...)
# shipment_payload.observations → tuple[Observation, ...]
```

### Cách đọc feedback khi retry

Nếu Verifier phát hiện lỗi trong output của Policy, bạn sẽ nhận feedback:

```python
if task.feedback is not None:
    print(task.feedback.error_code)       # VD: "REFUND_TOTAL_MISMATCH"
    print(task.feedback.message)          # VD: "recommended_refund_brl != sum(refund_lines)"
    print(task.feedback.required_checks)  # VD: ("refund_total", "refund_lines")
```

Khi nhận feedback, bạn cần sửa đúng phần lỗi mà Verifier chỉ ra.

---

## 4. Output — AgentResult với PolicyPayload

Policy agent phải trả về một `AgentResult` khi thành công:

```python
from student_agent.agent_contracts import (
    AgentResult,
    PolicyPayload,
    ToolCallSummary,
)

result = AgentResult.completed(
    actor="policy",
    invocation=task.invocation,
    context_version=task.context_version,
    retry_count_for_context=task.retry_count_for_context,
    payload=PolicyPayload(
        assessment={
            "primary_issue": "refund_pending",         # enum bắt buộc
            "case_status": "action_required",           # enum bắt buộc
            "confidence": 0.85,                         # 0.0 - 1.0
        },
        claim_assessments=(                             # tuple, có thể rỗng ()
            {
                "claim_id": "customer_claims_no_refund",
                "verdict": "supported",                 # enum
                "confidence": 0.9,
                "evidence_refs": ["ev_payment0000000000000001"],
            },
        ),
        root_cause_analysis={
            "ranked_causes": [
                {"cause_code": "REFUND_DELAY", "rank": 1},
                {"cause_code": "PAYMENT_SYSTEM_ERROR", "rank": 2},
            ],
            "responsible_parties": [
                {"party_type": "payment_provider", "party_id": None},
                {"party_type": "platform", "party_id": None},
            ],
        },
        data_conflicts=(                                # tuple, có thể rỗng ()
            {
                "field": "payment_status",
                "sources": ["payment_api", "order_api"],
                "selected_source": "payment_api",
                "resolution_code": "TRUSTED_SOURCE_PRIORITY",
            },
        ),
        financial_resolution={
            "currency": "BRL",                          # PHẢI là "BRL"
            "recommended_refund_brl": 150.50,           # PHẢI = sum(refund_lines)
            "refund_lines": [
                {
                    "reason_code": "ITEM_NOT_DELIVERED",
                    "amount_brl": 100.00,
                    "entity_id": "ITEM_001",
                },
                {
                    "reason_code": "SHIPPING_OVERCHARGE",
                    "amount_brl": 50.50,
                    "entity_id": "ITEM_002",
                },
            ],
        },
        resolution_actions=(
            "ISSUE_REFUND",
            "NOTIFY_SELLER",
            "FLAG_FOR_REVIEW",
        ),
    ),
    evidence_refs=(                                     # Evidence đã dùng
        "ev_payment0000000000000001",
        "ev_policy00000000000000001",
    ),
    tool_calls=(                                        # Tóm tắt tool đã gọi
        ToolCallSummary(
            tool_name="get_policy",
            attempts=1,
            status="completed",
            evidence_refs=("ev_policy00000000000000001",),
        ),
    ),
)
```

Khi gặp lỗi không thể phục hồi:

```python
result = AgentResult.failed(
    actor="policy",
    invocation=task.invocation,
    context_version=task.context_version,
    retry_count_for_context=task.retry_count_for_context,
    error_code="INSUFFICIENT_SPECIALIST_DATA",
    failed_stage="policy_evaluation",
    retryable=True,                                   # True nếu có thể retry
    safe_detail="Specialist data incomplete for policy decision",
)
```

---

## 5. Chi tiết từng field của PolicyPayload

### 5.1. `assessment`

```json
{
  "primary_issue": "<enum>",
  "case_status": "<enum>",
  "confidence": 0.85
}
```

**`primary_issue`** — giá trị cho phép (từ schema):

| Giá trị                     | Khi nào dùng                                     |
| --------------------------- | ------------------------------------------------ |
| `canceled_order_paid`       | Đơn bị hủy nhưng đã thanh toán                  |
| `unavailable_order_paid`    | Sản phẩm không có sẵn nhưng đã thanh toán        |
| `late_delivery_seller`      | Giao hàng muộn do seller                         |
| `late_delivery_logistics`   | Giao hàng muộn do logistics                      |
| `valid_split_payment`       | Thanh toán chia nhỏ hợp lệ                       |
| `payment_mismatch`          | Số tiền thanh toán không khớp                     |
| `duplicate_charge`          | Bị tính tiền trùng                                |
| `refund_pending`            | Hoàn tiền đang chờ xử lý                         |
| `refund_failed`             | Hoàn tiền thất bại                                |
| `unsupported_claim`         | Claim không được hỗ trợ bởi evidence              |
| `insufficient_evidence`     | Không đủ evidence để kết luận                      |

**`case_status`** — giá trị cho phép:

| Giá trị               | Ý nghĩa                             |
| ---------------------- | ------------------------------------ |
| `action_required`      | Cần hành động (hoàn tiền, liên hệ…) |
| `no_action`            | Không cần hành động                  |
| `needs_investigation`  | Cần điều tra thêm                    |

**`confidence`**: Số từ 0.0 đến 1.0. Điểm `calibration` trong scoring sẽ phạt
nếu confidence quá cao mà `primary_issue` sai, hoặc quá thấp mà đúng.
Công thức: `1 - (confidence - correctness)²`.

### 5.2. `claim_assessments` (optional)

```json
[
  {
    "claim_id": "string (1-64 ký tự)",
    "verdict": "supported | unsupported | partially_supported | insufficient_evidence",
    "confidence": 0.9,
    "evidence_refs": ["ev_..."]
  }
]
```

- Tối đa **5** claim.
- `evidence_refs` của claim **phải** nằm trong `evidence_refs` ở cấp output.
  Verifier sẽ check rule `CLAIM_EVIDENCE_MISMATCH`.
- Nếu không có claim, trả về tuple rỗng `()` — Coordinator sẽ bỏ qua field
  `claim_assessments` trong output.

### 5.3. `root_cause_analysis`

```json
{
  "ranked_causes": [
    {"cause_code": "UPPERCASE_CODE (3-80 ký tự, regex: ^[A-Z][A-Z0-9_]{2,79}$)", "rank": 1},
    {"cause_code": "ANOTHER_CAUSE", "rank": 2}
  ],
  "responsible_parties": [
    {"party_type": "seller | platform | logistics_provider | payment_provider | customer | unknown", "party_id": "string | null"}
  ]
}
```

- Tối đa **5** cause, rank từ 1 đến 5.
- **Rank không được trùng!** Verifier sẽ kiểm tra `DUPLICATE_ROOT_CAUSE_RANK`.
- `party_type` phải là một trong các enum trên.
- `responsible_parties` tối đa **5**.

### 5.4. `data_conflicts`

```json
[
  {
    "field": "tên field bị conflict (1-100 ký tự)",
    "sources": ["source_a", "source_b"],
    "selected_source": "source_a",
    "resolution_code": "lý do chọn source này (1-80 ký tự)"
  }
]
```

- Tối đa **5** conflict.
- `sources` phải có tối thiểu **2**, tối đa **5** giá trị, không trùng.
- `selected_source` phải là `null` hoặc **nằm trong** `sources`. Verifier sẽ
  check `INVALID_CONFLICT_SELECTION`.
- Nếu không có conflict, trả về tuple rỗng `()`.

### 5.5. `financial_resolution`

```json
{
  "currency": "BRL",
  "recommended_refund_brl": 150.50,
  "refund_lines": [
    {
      "reason_code": "ITEM_NOT_DELIVERED (1-80 ký tự)",
      "amount_brl": 100.00,
      "entity_id": "ITEM_001 | null"
    },
    {
      "reason_code": "SHIPPING_OVERCHARGE",
      "amount_brl": 50.50,
      "entity_id": "ITEM_002"
    }
  ]
}
```

> ⚠️ **QUY TẮC QUAN TRỌNG NHẤT:**
>
> `recommended_refund_brl` **PHẢI BẰNG** tổng `amount_brl` của tất cả
> `refund_lines`. Verifier dùng `Decimal` arithmetic để kiểm tra, nên sai
> dù 0.01 cũng bị reject: `REFUND_TOTAL_MISMATCH`.
>
> Nếu không cần hoàn tiền: `recommended_refund_brl = 0` và `refund_lines = []`.

- `currency` **luôn** là `"BRL"`.
- `amount_brl` >= 0 cho mỗi line.
- Tối đa **10** refund lines.

### 5.6. `resolution_actions`

```json
["ISSUE_REFUND", "NOTIFY_SELLER", "FLAG_FOR_REVIEW"]
```

- Tối đa **8** action, không trùng.
- Mỗi action từ 1-80 ký tự.
- Phải nhất quán với `assessment` và `responsible_parties`. Scoring component
  `consistency` (10%) sẽ cross-check status/refund/action consistency.

---

## 6. Sử dụng MCP Evidence Gateway

Policy có thể cần gọi MCP tool `get_policy` để lấy quy tắc nghiệp vụ. Đây là
domain `"policy"` trong schema MCP.

```python
# Trong hàm implement policy agent, bạn cần nhận gateway qua context hoặc closure

# Gọi tool
evidence = await gateway.call(
    "get_policy",
    case_id=task.case_id,
    # thêm argument nếu tool yêu cầu
)

evidence_ref = evidence["evidence_ref"]   # VD: "ev_policy00000000000000001"
policy_data = evidence["data"]            # Dữ liệu policy rules

# Ghi trace khi dùng evidence
trace.emit(
    case_id=task.case_id,
    event_type="tool_result_consumed",
    actor="policy",
    tool_name="get_policy",
    evidence_refs=[evidence_ref],
)
```

### Quy tắc MCP

- **Luôn truyền đúng `case_id`** — evidence thuộc case nào chỉ dùng cho case
  đó.
- **Không tự tạo `evidence_ref`** — chỉ dùng ref do MCP trả về.
- **Không dùng evidence chéo case.**
- **Dùng tool discovery** (`await gateway.list_tools()`) để biết tên tool chính
  xác. Không đoán tên.
- **Local retry**: Nếu gọi MCP bị timeout hoặc lỗi tạm thời, bạn nên retry tại
  chỗ (tối đa 2 attempts). Sau đó nếu vẫn fail, trả `AgentResult.failed()` với
  error code phù hợp (VD: `MCP_TIMEOUT_EXHAUSTED`).

---

## 7. Emit trace event

Policy agent nên emit trace event khi đưa ra quyết định nghiệp vụ:

```python
trace.emit(
    case_id=task.case_id,
    event_type="policy_decided",
    actor="policy",
    decision_code=assessment["primary_issue"].upper(),
    evidence_refs=[...list of evidence refs used...],
    attributes={
        "confidence": assessment["confidence"],
        "case_status": assessment["case_status"],
        "refund_brl": financial_resolution["recommended_refund_brl"],
    },
)
```

**Event types cho phép** (trong trace schema):

| Event type               | Khi nào dùng                           |
| ------------------------ | -------------------------------------- |
| `task_assigned`          | Coordinator gọi (tự động)              |
| `tool_result_consumed`   | Sau khi dùng evidence từ MCP tool      |
| `policy_decided`         | Sau khi Policy đưa ra quyết định       |
| `handoff`                | Coordinator ghi (tự động)              |
| `verification_completed` | Verifier ghi (tự động)                 |

**Không được ghi** vào trace: prompts, chain-of-thought, API key, raw exception,
raw MCP response body.

---

## 8. Xử lý lỗi và retry

### 8.1. Ba tầng retry (bạn chỉ chịu trách nhiệm tầng 1)

| Tầng | Owner | Mã lỗi | Giới hạn |
| ---- | ----- | ------- | -------- |
| 1. **Local retry** (MCP/model) | **Policy agent (bạn)** | `MCP_TIMEOUT`, `CONNECTION_RESET`, `RATE_LIMITED`, `HTTP_5XX`, `INVALID_MODEL_RESPONSE` | 2 attempts/operation |
| 2. **Coordinator recovery** | Coordinator | `AGENT_TIMEOUT`, `AGENT_CRASHED`, `AGENT_NO_RESPONSE` | 1 recovery/context |
| 3. **Semantic retry** | Verifier → Coordinator | Domain-specific codes | 1 retry/actor/context_version |

### 8.2. Khi nào Policy bị gọi lại

Policy sẽ bị gọi lại trong các trường hợp:

1. **Verifier yêu cầu retry Policy** — `task.feedback` sẽ chứa lý do. Bạn cần
   sửa phần lỗi.
   - `retry_count_for_context` sẽ tăng lên 1.
   - Chỉ được retry **1 lần** cho cùng context version.

2. **Specialist upstream thay đổi** (VD: Payment được retry) — Policy sẽ được
   **recompute** với context mới.
   - `context_version` tăng, `retry_count_for_context` reset về 0.
   - Đây là **recompute**, không phải retry. Bạn vẫn còn 1 lần retry cho
     context mới này.

3. **Policy agent bị crash/timeout** — Coordinator sẽ recovery 1 lần.

### 8.3. Template xử lý feedback

```python
async def policy_agent(task: AgentTask) -> AgentResult:
    # Kiểm tra feedback từ Verifier
    if task.feedback is not None:
        # Đây là retry — sửa phần lỗi Verifier chỉ ra
        error_code = task.feedback.error_code
        if error_code == "REFUND_TOTAL_MISMATCH":
            # Tính lại refund...
            pass
        elif error_code == "DUPLICATE_ROOT_CAUSE_RANK":
            # Sửa rank trùng...
            pass
        elif error_code == "CLAIM_EVIDENCE_MISMATCH":
            # Sửa evidence refs trong claim...
            pass
        elif error_code == "INVALID_CONFLICT_SELECTION":
            # Sửa selected_source...
            pass

    # Logic chính: phân tích specialist data và đưa ra quyết định
    ...
```

---

## 9. Những gì Verifier sẽ kiểm tra Policy

Verifier chạy các deterministic check theo thứ tự. Lỗi đầu tiên sẽ được trả về.
Các lỗi **thuộc Policy** sẽ target retry cho bạn:

| Error code                     | Nguyên nhân                                       | Hậu quả                |
| ------------------------------ | ------------------------------------------------- | ----------------------- |
| `POLICY_OUTPUT_SCHEMA_INVALID` | Field do Policy sở hữu vi phạm JSON Schema        | Retry policy            |
| `CLAIM_EVIDENCE_MISMATCH`      | Evidence refs trong claim không nằm trong evidence cấp output | Retry policy |
| `REFUND_TOTAL_MISMATCH`        | `recommended_refund_brl ≠ sum(refund_lines.amount_brl)` | Retry policy |
| `INVALID_CONFLICT_SELECTION`   | `selected_source` không nằm trong `sources`        | Retry policy            |
| `DUPLICATE_ROOT_CAUSE_RANK`    | Hai cause có cùng `rank`                           | Retry policy            |

Nếu Policy đã retry 1 lần cho cùng context_version mà vẫn fail → Verifier trả
`POLICY_RETRY_EXHAUSTED` (terminal, workflow dừng).

### Lỗi KHÔNG thuộc Policy (terminal):

| Error code                     | Do ai                |
| ------------------------------ | -------------------- |
| `CANDIDATE_MISSING`            | Coordinator          |
| `CASE_ID_MISMATCH`            | Coordinator          |
| `CANDIDATE_SCHEMA_INVALID`    | Coordinator (envelope fields) |
| `EVIDENCE_MERGE_MISMATCH`     | Coordinator          |
| `ENTITY_MERGE_MISMATCH`       | Coordinator          |

---

## 10. Quy tắc quan trọng — KHÔNG được vi phạm

> ❌ **KHÔNG** tự tạo `evidence_ref`. Chỉ dùng ref do MCP trả về.
>
> ❌ **KHÔNG** dùng evidence chéo case (evidence từ CASE_001 không được dùng cho
> CASE_002).
>
> ❌ **KHÔNG** bịa dữ liệu — nếu specialist data thiếu, trả
> `primary_issue: "insufficient_evidence"` hoặc `case_status: "needs_investigation"`.
>
> ❌ **KHÔNG** ghi chain-of-thought, prompt, API key vào trace hoặc
> `safe_detail`.
>
> ❌ **KHÔNG** trả `safe_detail` dài quá 160 ký tự hoặc chứa pattern
> `sk-team-...`.
>
> ❌ **KHÔNG** import trực tiếp module của agent khác. Giao tiếp qua contract.
>
> ✅ **PHẢI** đảm bảo `recommended_refund_brl == sum(amount_brl)`.
>
> ✅ **PHẢI** đảm bảo rank trong `ranked_causes` là duy nhất.
>
> ✅ **PHẢI** đảm bảo `selected_source` nằm trong `sources` (hoặc null).
>
> ✅ **PHẢI** đảm bảo evidence_refs trong `claim_assessments` là subset của
> evidence_refs cấp agent result.
>
> ✅ **PHẢI** map `invocation`, `context_version`, `retry_count_for_context` từ
> task vào result.

---

## 11. Ví dụ code triển khai

### 11.1. Cấu trúc file

Tạo file tại: `src/student_agent/policy_agent.py`

```python
"""Policy Agent — áp dụng policy nghiệp vụ dựa trên dữ kiện specialist."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableLambda, Runnable

from .agent_contracts import (
    AgentResult,
    AgentTask,
    PolicyPayload,
    ToolCallSummary,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def create_policy_runnable(
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> Runnable[AgentTask, AgentResult]:
    """Tạo LangChain Runnable cho Policy agent."""

    async def _run(task: AgentTask) -> AgentResult:
        return await run_policy(task, gateway, trace)

    return RunnableLambda(_run)


async def run_policy(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Logic chính của Policy agent."""

    try:
        # 1. Đọc dữ kiện từ specialist
        active = task.context["active_results"]
        order_payload = active["order_item"].payload
        payment_payload = active["payment"].payload
        shipment_payload = active["shipment"].payload

        collected_evidence_refs: list[str] = []

        # 2. (Optional) Gọi MCP để lấy policy rules
        try:
            policy_evidence = await gateway.call(
                "get_policy",
                case_id=task.case_id,
            )
            policy_ref = policy_evidence["evidence_ref"]
            policy_data = policy_evidence["data"]
            collected_evidence_refs.append(policy_ref)

            trace.emit(
                case_id=task.case_id,
                event_type="tool_result_consumed",
                actor="policy",
                tool_name="get_policy",
                evidence_refs=[policy_ref],
            )
        except Exception:
            # Nếu không cần policy tool hoặc tool không khả dụng
            policy_data = None

        # 3. Phân tích dữ kiện và đưa ra quyết định
        #    TODO: Đây là phần bạn cần implement logic nghiệp vụ chính
        #    Xem xét các observation từ specialist để xác định:
        #    - primary_issue là gì?
        #    - refund có cần không? bao nhiêu?
        #    - nguyên nhân gốc rễ?
        #    - có data conflict không?

        assessment = _determine_assessment(
            task, order_payload, payment_payload, shipment_payload, policy_data
        )
        root_cause = _analyze_root_cause(
            order_payload, payment_payload, shipment_payload
        )
        financial = _calculate_financial_resolution(
            assessment, order_payload, payment_payload
        )
        conflicts = _detect_data_conflicts(
            payment_payload, shipment_payload
        )
        actions = _determine_resolution_actions(assessment, financial)
        claims = _assess_claims(task, collected_evidence_refs)

        # 4. Emit trace
        trace.emit(
            case_id=task.case_id,
            event_type="policy_decided",
            actor="policy",
            decision_code=assessment["primary_issue"].upper(),
            evidence_refs=collected_evidence_refs,
            attributes={
                "confidence": assessment["confidence"],
                "case_status": assessment["case_status"],
            },
        )

        # 5. Trả kết quả
        return AgentResult.completed(
            actor="policy",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            payload=PolicyPayload(
                assessment=assessment,
                claim_assessments=tuple(claims),
                root_cause_analysis=root_cause,
                data_conflicts=tuple(conflicts),
                financial_resolution=financial,
                resolution_actions=tuple(actions),
            ),
            evidence_refs=tuple(collected_evidence_refs),
            tool_calls=(
                ToolCallSummary(
                    tool_name="get_policy",
                    attempts=1,
                    status="completed",
                    evidence_refs=tuple(collected_evidence_refs),
                ),
            ) if collected_evidence_refs else (),
        )

    except Exception as exc:
        # Lỗi không phục hồi được → trả failed result
        return AgentResult.failed(
            actor="policy",
            invocation=task.invocation,
            context_version=task.context_version,
            retry_count_for_context=task.retry_count_for_context,
            error_code="POLICY_EVALUATION_FAILED",
            failed_stage="policy_evaluation",
            retryable=True,
            safe_detail=f"Policy evaluation failed: {type(exc).__name__}",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Các hàm helper — TODO: Bạn cần implement logic nghiệp vụ thực tế ở đây
# ──────────────────────────────────────────────────────────────────────────────

def _determine_assessment(task, order_payload, payment_payload, shipment_payload, policy_data):
    """TODO: Implement logic xác định primary_issue, case_status, confidence.

    Gợi ý:
    - Xem observations từ specialist để phát hiện vấn đề
    - Ánh xạ từ dữ kiện sang primary_issue enum
    - Đặt confidence dựa trên chất lượng evidence
    """
    raise NotImplementedError("Implement assessment logic")


def _analyze_root_cause(order_payload, payment_payload, shipment_payload):
    """TODO: Implement logic phân tích nguyên nhân gốc.

    Gợi ý:
    - Xếp hạng nguyên nhân theo mức độ chắc chắn
    - Xác định bên chịu trách nhiệm
    - Đảm bảo rank không trùng
    """
    raise NotImplementedError("Implement root cause analysis")


def _calculate_financial_resolution(assessment, order_payload, payment_payload):
    """TODO: Implement logic tính hoàn tiền.

    ⚠️ QUAN TRỌNG: recommended_refund_brl PHẢI = sum(refund_lines.amount_brl)
    Dùng Decimal nếu cần chính xác.
    """
    raise NotImplementedError("Implement financial resolution")


def _detect_data_conflicts(payment_payload, shipment_payload):
    """TODO: So sánh observations từ các specialist để phát hiện conflict.

    Gợi ý:
    - Nếu cùng field nhưng giá trị khác nhau → data conflict
    - selected_source phải nằm trong sources
    """
    raise NotImplementedError("Implement conflict detection")


def _determine_resolution_actions(assessment, financial):
    """TODO: Xác định danh sách hành động cần thực hiện.

    Gợi ý:
    - Phải nhất quán với assessment và responsible_parties
    - VD: nếu action_required → nên có ít nhất 1 action
    """
    raise NotImplementedError("Implement resolution actions")


def _assess_claims(task, evidence_refs):
    """TODO: Đánh giá các claim của khách hàng (nếu case có claim).

    Gợi ý:
    - Lấy claim từ customer message trong task.case
    - evidence_refs trong claim PHẢI nằm trong evidence_refs cấp output
    """
    return []  # Trả về list rỗng nếu không có claim
```

### 11.2. Đăng ký vào AgentRegistry

Khi tích hợp vào `workflow.py`:

```python
from student_agent.agent_contracts import AgentRegistry
from student_agent.policy_agent import create_policy_runnable

registry = AgentRegistry(
    order_item=...,       # Do Nam triển khai
    payment=...,          # Do Nam triển khai
    shipment=...,         # Do Nam triển khai
    policy=create_policy_runnable(gateway, trace),
    verifier=deterministic_verifier.as_runnable(),
)

output = await solve_case(case, gateway, trace, registry=registry)
```

---

## 12. Checklist TDD để triển khai

### Bước 1: Viết test trước

```bash
# Tạo tests/test_policy_agent.py với các test case:
```

- [ ] Test happy path: specialist data đầy đủ → Policy trả `PolicyPayload`
  hợp lệ
- [ ] Test `recommended_refund_brl == sum(refund_lines.amount_brl)` luôn đúng
- [ ] Test rank trong `ranked_causes` không trùng
- [ ] Test `selected_source` nằm trong `sources` (hoặc null)
- [ ] Test claim `evidence_refs` là subset của agent evidence_refs
- [ ] Test feedback handling: nhận `REFUND_TOTAL_MISMATCH` → sửa và trả output
  đúng
- [ ] Test khi specialist data thiếu → trả `insufficient_evidence` hoặc
  `needs_investigation`
- [ ] Test MCP tool fail → trả `AgentResult.failed(...)` với error code đúng
- [ ] Test `safe_detail` không chứa secret pattern
- [ ] Test counters (`invocation`, `context_version`, `retry_count`) match task

### Bước 2: Chạy test → RED

```powershell
pytest -q tests/test_policy_agent.py
```

### Bước 3: Implement logic nghiệp vụ

Hoàn thành các hàm `_determine_assessment`, `_analyze_root_cause`,
`_calculate_financial_resolution`, v.v.

### Bước 4: Chạy test → GREEN

```powershell
pytest -q tests/test_policy_agent.py
pytest -q                                 # Toàn bộ suite
ruff check src tests                      # Lint
```

### Bước 5: Tích hợp và chạy end-to-end

```powershell
day09 run
day09 validate
```

### Bước 6: Commit

```powershell
git add src/student_agent/policy_agent.py tests/test_policy_agent.py
git commit -m "feat: implement policy agent"
```

---

## 13. Tham khảo thêm

| Tài liệu | Đường dẫn |
| --------- | --------- |
| README chính | `README.md` |
| Kiến trúc tổng quan | `ARCHITECTURE.md` |
| Kiến trúc Coordinator | `docs/kien-truc-coordinator.md` |
| Output schema (JSON) | `contracts/schemas/l3a-output-v2.schema.json` |
| Trace event schema | `contracts/schemas/trace-event-v1.schema.json` |
| MCP evidence schema | `contracts/schemas/mcp-evidence-response-v1.schema.json` |
| Scoring policy | `contracts/scoring/scoring-policy-v2.json` |
| Agent contracts code | `src/student_agent/agent_contracts.py` |
| Coordinator code | `src/student_agent/coordinator.py` |
| Verifier code | `src/student_agent/verifier.py` |
| Retry design spec | `docs/superpowers/specs/2026-09-25-coordinator-context-retry-design.md` |

---

> **Lưu ý cuối:** Competition chấm điểm dựa trên kết quả (semantic 45%,
> evidence 15%, provenance 15%, consistency 10%) — không phải số lượng code
> hay framework. Hãy tập trung vào **chất lượng quyết định nghiệp vụ** và
> **sự chính xác của evidence**.
