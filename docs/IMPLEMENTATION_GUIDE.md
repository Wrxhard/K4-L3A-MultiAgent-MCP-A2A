# L3A Multi-Agent Implementation Guide

Tài liệu giúp các thành viên triển khai cùng một kiến trúc mà không phụ thuộc kiến
thức ngầm. `ARCHITECTURE.md` là nguồn quyết định thiết kế; file này mô tả cách thực
hiện, chia việc và kiểm tra.

## 1. Phạm vi baseline

- workflow in-process, không chạy nhiều service;
- routing deterministic, specialist chạy tuần tự;
- typed message/report;
- evidence registry riêng cho từng case;
- candidate generation bằng rules có thể test;
- Qwen3-4B semantic adjudicator và Phi-4-mini 3.8B independent critic;
- deterministic output builder và final verifier;
- tổng unique model parameters khoảng 7.8B, không dùng framework orchestration.

Không tối ưu concurrency hoặc thêm framework trước khi baseline pass end-to-end.

## 2. Cấu trúc module mục tiêu

```text
src/student_agent/
├── workflow.py                 # composition root, solve_case()
├── domain/
│   ├── models.py               # Fact, Conflict, reports, draft
│   ├── enums.py                # states, codes, statuses
│   └── invariants.py           # pure validation helpers
├── agents/
│   ├── coordinator.py
│   ├── order_item.py
│   ├── payment.py
│   ├── shipment.py
│   ├── policy.py
│   ├── adjudicator.py
│   ├── critic.py
│   └── verifier.py
├── orchestration/
│   ├── messages.py             # A2A-inspired envelopes
│   ├── state_machine.py
│   └── workspace.py            # per-case blackboard
├── evidence/
│   ├── registry.py
│   └── tool_catalog.py         # discovered tool mapping/allowlists
└── models/
    ├── client.py               # model endpoint abstraction
    ├── contracts.py            # constrained input/output schemas
    └── config.py               # pinned model IDs and generation config
```

Không cần tạo tất cả file ngay. Ưu tiên shared data contracts và test trước, rồi tách
module khi interface đã ổn định.

## 3. Shared data contracts

Team thống nhất contract trước khi viết agent.

```python
@dataclass(frozen=True)
class Fact:
    fact_code: str
    value: object
    entity_id: str | None
    source: Literal["mcp", "case_input"]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRecord:
    case_id: str
    evidence_ref: str
    result_hash: str
    domain: str
    tool_name: str
    data: object
    warnings: tuple[str, ...]
```

Business fact từ MCP phải có evidence ref. Dữ liệu input chỉ dùng định tuyến/truy
vấn và mang `source="case_input"`.

Specialist report phải có header chung, facts có cấu trúc và status hữu hạn. Evidence
agents không chứa final output dictionary. Qwen tạo semantic decision; deterministic
output builder mới được xây draft output.

Decision code là constant ổn định, ví dụ:

```text
CHECK_ORDER_STATE
CHECK_PAYMENT_TOTAL
CHECK_SHIPMENT_TIMELINE
PAYMENT_SPLIT_VALID
DUPLICATE_PAYMENT_CONFIRMED
POLICY_REFUND_ELIGIBLE
REVISION_REQUIRED
VERIFICATION_PASSED
```

Không tạo decision code động từ customer message.

## 4. Trình tự thực thi `solve_case`

```python
async def solve_case(case, gateway, trace):
    workspace = CaseWorkspace.from_case(case)
    plan = coordinator.plan(workspace)

    for task in plan.domain_tasks:
        emit_task_assigned(task)
        report = await dispatch(task, gateway, workspace.evidence, trace)
        validate_report(report)
        workspace.accept(report)
        emit_handoff(report)

    policy_task = coordinator.plan_policy_task(workspace)
    if policy_task is not None:
        policy_report = await policy_agent.execute(policy_task, ...)
        workspace.accept(policy_report)

    candidates = candidate_generator.generate(workspace)
    semantic = await qwen_adjudicator.decide(candidates, workspace.fact_view())
    semantic = semantic_gate.validate_or_fallback(semantic, candidates, workspace)
    draft = output_builder.build(semantic, workspace)
    critique = await phi_critic.review(draft, workspace.critic_view())

    if critique.requires_revision:
        semantic = await qwen_adjudicator.revise(
            semantic, critique.error_codes, workspace.counter_facts()
        )
        semantic = semantic_gate.validate_or_fallback(semantic, candidates, workspace)
        draft = output_builder.build(semantic, workspace)

    verification = verifier.verify(draft, workspace)

    emit_verification(verification)
    return finalize(draft, verification)
```

Mọi helper phát trace nhận trực tiếp `case_id`, không đọc current case từ global.
Qwen và Phi được gọi một lần cho mọi case; Qwen chỉ được gọi lần hai nếu Phi trả
`reject`. Model failure luôn đi vào deterministic fallback và không làm mất evidence.

## 5. Quy trình MCP discovery

Integration owner thực hiện:

1. Chạy `day09 mcp-tools` với credential thật.
2. Thu thập tên, description và input schema của tool từ MCP session.
3. Lập bảng tool → domain → owner → required arguments.
4. Lưu mô tả không chứa credential/private payload.
5. Implement `ToolCatalog`.
6. Viết test fail nếu configured tool không có trong discovery result.

| Domain | Discovered tool | Required args | Owner | Data adapter |
| --- | --- | --- | --- | --- |
| order | `get_order` | `case_id`, `order_id` | Order/item | object |
| items | `get_order_items` | `case_id`, `order_id` | Order/item | array |
| seller | `get_sellers` | `case_id`, `order_id` | Order/item | array |
| product | `get_product_context` | `case_id`, `order_id` | Order/item | array |
| base payment | `get_order_payments` | `case_id`, `order_id` | Payment | array |
| payment lifecycle | `get_payment_timeline` | `case_id`, `order_id` | Payment | `{order_id,payments,events}` |
| refund | `get_refund_timeline` | `case_id`, `order_id` | Payment | `{order_id,events}` |
| shipment | `get_shipment_summary` | `case_id`, `order_id` | Shipment | object + events |
| policy | `get_policy` | `case_id`, `policy_version` | Policy | `{currency,policy_version,rules}` |
| customer history | `get_customer_history` | `case_id`, `customer_unique_id` | Coordinator/Order | chưa lấy mẫu |

Input inventory xác nhận 100 case cùng shape và mỗi primary claim topic có 10 case;
`requested_full_refund` xuất hiện trong mọi case và phải được đánh giá độc lập.

Selection rules:

- ưu tiên `get_payment_timeline`; không gọi thêm `get_order_payments` nếu timeline đã
  cung cấp base rows và events cần thiết;
- không gọi seller/product/customer-history nếu domain đó không hỗ trợ kết luận;
- chỉ gọi refund timeline cho refund-related investigation;
- parse mọi monetary string bằng `Decimal`, không dùng `float` để cộng tiền;
- tool error do không có refund record được phân loại domain-absent/not-found, không
  biến thành fact `refund_completed`.

Không commit Team API key hoặc raw competition evidence.

## 6. Trace recipe

Specialist task thành công:

```text
task_assigned
  → MCP call
  → tool_result_consumed (mỗi evidence thực sự dùng)
  → handoff
```

Policy task:

```text
task_assigned
  → tool_result_consumed
  → policy_decided
  → handoff
```

Verifier phát `verification_completed`. Nếu cần revision, lần đầu dùng
`REVISION_REQUIRED`, sau revision phát kết quả cuối. Không emit consumed cho response
bị reject trước khi sử dụng.

Model events dùng `task_assigned` và `handoff` với actor `semantic-adjudicator` hoặc
`independent-critic`. Trace chỉ ghi model ID/version, decision code và các evidence
refs thực sự hỗ trợ artifact; không ghi prompt, completion hoặc chain-of-thought.

## 7. Test strategy

### Unit tests cho specialist

- parse response đúng domain;
- reject response sai domain;
- giữ nguyên evidence ref;
- not-found không sinh fact giả;
- warning được propagate;
- tool ngoài allowlist bị reject.

### Pure rule tests

- canceled + paid → `canceled_order_paid`;
- unavailable + paid → `unavailable_order_paid`;
- nhiều payment nhưng tổng hợp lệ → `valid_split_payment`;
- duplicated excess → `duplicate_charge`;
- seller dispatch trễ → `late_delivery_seller`;
- carrier delay sau handoff đúng hạn → `late_delivery_logistics`;
- refund đang xử lý → `refund_pending`;
- refund failure → `refund_failed`;
- thiếu evidence bắt buộc → `insufficient_evidence`.

### Verifier tests

- cross-case/unknown evidence ref;
- claim ref không nằm trong top-level refs;
- refund total lệch refund lines;
- seller responsibility thiếu seller ID;
- logistics responsibility thiếu shipment evidence;
- duplicate actions;
- confidence quá cao khi evidence thiếu.

### Model contract tests

- Qwen không được chọn issue ngoài candidate set;
- Qwen chỉ được viện dẫn fact/evidence aliases đã cấp;
- invalid JSON, timeout và unknown enum kích hoạt deterministic fallback;
- Phi chỉ được trả allowed error codes/challenged fields;
- Phi reject chỉ tạo tối đa một revision;
- model không được tạo amount, entity ID, tool name hoặc raw evidence ref;
- cùng fixture/config phải cho structured result ổn định ở temperature 0.

### Evaluation matrix

So sánh trên cùng fixtures: rules-only, Qwen-only review và Qwen+Phi. Chỉ giữ model
portfolio nếu semantic accuracy/calibration tăng mà schema, provenance và consistency
không giảm. Theo dõi invalid-output rate, fallback rate, latency và VRAM.

### Workflow tests

Fake gateway ghi mọi call để assert:

```python
assert all(call.case_id == case["case_id"] for call in gateway.calls)
```

Lifecycle phải bảo đảm task assignment trước handoff, evidence consumed trước khi đưa
vào output, verification sau specialist handoff, tối đa một retry/revision.

Mọi output fixture được validate bằng `Contracts` thật; không copy JSON Schema vào test.

## 8. Chia việc gợi ý

Với team 4 người:

| Vai trò | Phạm vi | Deliverable |
| --- | --- | --- |
| A — Orchestration owner | Models, messages, workspace, coordinator, trace | End-to-end skeleton với fake case |
| B — Commerce owner | Order/item/seller và shipment adapters/rules | Reports + tests order/shipment |
| C — Finance/policy owner | Payment/refund/policy adapters/rules | Reports + tests payment/policy |
| D — Model/quality owner | Qwen/Phi contracts, output builder, verifier, eval | Structured model layer + invariant suite |

Team 3 người gộp B/C theo workload; team 5 người tách Shipment. Mỗi owner review ít
nhất một module ngoài domain của mình để tránh knowledge silo.

## 9. Integration order

1. Shared models và fake gateway.
2. Evidence registry và trace helpers.
3. Vertical slice rules-only: coordinator → order agent → output builder → verifier.
4. Payment specialist.
5. Shipment specialist.
6. Policy specialist.
7. Full routing matrix và deterministic candidate generator.
8. Qwen adjudicator với constrained schema + fallback.
9. Phi critic với bounded revision.
10. Real MCP adapters.
11. Chạy 100 case, ablation và calibration.

Vertical slice đầu tiên phải chạy được bằng fixture trước khi mở rộng domain.

## 10. Pull request rules

Mỗi PR cần:

- phạm vi một interface hoặc một domain;
- unit tests cho happy path và failure path;
- không chứa `.env`, input/output thật hoặc API key;
- không đổi public contract;
- document decision code mới;
- tool call mới nằm trong allowlist đúng owner;
- evidence dùng trong kết luận có trace linkage;
- model change có model ID/version, schema test và ablation result;
- reviewer khác owner xác nhận invariant.

Không merge PR chỉ có prompt/heuristic mà không có structured-output test.

## 11. Debugging checklist

Nếu schema pass nhưng điểm thấp, kiểm tra:

1. `primary_issue` có dựa trên verified fact?
2. Có thiếu required evidence domain?
3. Có evidence không liên quan trong top-level refs?
4. Evidence có đúng team/run/case và có trace linkage?
5. Claim verdict có direct evidence?
6. Refund, status, responsibility và actions có nhất quán?
7. Confidence có quá cao so với coverage?
8. Workflow có assignment, handoff, verification?
9. Model fallback/revision rate có tăng bất thường?
10. Qwen/Phi có bị cấp raw context hoặc evidence không liên quan?

Không sửa output bằng tay sau `day09 run`; sửa rule/adapter rồi chạy lại.

## 12. Definition of Done

Một domain hoàn thành khi:

- có typed input/output contract;
- chỉ gọi tool trong allowlist;
- giữ nguyên evidence metadata;
- trace đúng lifecycle;
- test success, not-found, invalid evidence và conflict nếu applicable;
- không dùng global mutable state;
- không tạo identifier/evidence giả;
- docs và decision codes đã cập nhật.

Model layer chỉ hoàn thành khi:

- tổng unique parameters được ghi nhận và dưới 10B;
- model IDs, quantization và generation config được pin;
- Qwen/Phi dùng schema khác nhau đúng vai trò;
- mọi output đi qua deterministic gate;
- timeout/invalid JSON có fallback;
- không persist prompt/completion/chain-of-thought;
- ablation chứng minh không làm giảm hard-gate metrics.

Toàn hệ thống hoàn thành khi:

- 100 input pass `day09 validate-inputs`;
- mỗi case sinh đúng một output;
- `day09 validate`, tests và lint pass;
- `ARCHITECTURE.md` khớp implementation;
- ZIP chỉ có manifest, trace, outputs và không có secret;
- một thành viên khác có thể clone, cấu hình và tái lập run theo docs.
