# L3A Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Không ghi prompt bí mật, chain-of-thought hoặc credential.

## 1. Tổng quan

```text
Input → Coordinator → Order/Item → Payment ─┐
                              └→ Shipment ──┼→ Policy → Assemble → Verifier → Output
MCP Gateway ←──────────── specialist agents ┘                         │
TraceWriter ←──────────── observable events ←─────────────────────────┘
```

Coordinator là LangGraph `StateGraph`. Các agent được inject dưới dạng LangChain `Runnable[AgentTask, AgentResult]`. State giữ active result, toàn bộ attempt history, invocation, context version, retry count, feedback và verification history theo từng case.

## 2. Ownership

| Actor | Input | Trách nhiệm | Output |
| --- | --- | --- | --- |
| Coordinator | Case và `AgentRegistry` | Điều phối, context, merge, retry và dependency invalidation | Candidate L3A |
| Order/Item | Case gốc | Xác định order, item, seller và reference liên quan | `OrderItemPayload` |
| Payment | Kết quả Order/Item | Charge, refund, amount, currency và payment evidence | `PaymentPayload` |
| Shipment | Kết quả Order/Item | Tracking, carrier, timeline và shipment evidence | `ShipmentPayload` |
| Policy | Ba specialist result | Assessment, claim, root cause, conflict, tiền và action | `PolicyPayload` |
| Verifier | Candidate và attempt history | Kiểm tra deterministic; semantic review là tùy chọn | `VerificationReport` |

Mỗi specialist chỉ gọi nhóm MCP tool thuộc domain của mình. Policy không thay đổi facts nguồn. Coordinator và Verifier không gọi tool nghiệp vụ.

## 3. A2A và dependency

Mọi task/result được correlation bằng `case_id`, `actor`, `invocation`, `context_version` và `retry_count_for_context`. Agent phải trả đúng các giá trị nhận trong task. Chỉ result `completed` và đúng payload type mới trở thành active result.

Payment và Shipment chạy song song. Policy chỉ chạy khi Order/Item, Payment và Shipment đều completed. Retry một upstream actor làm mất hiệu lực các active result downstream; recomputation tăng `context_version` downstream và reset retry count.

## 4. Evidence lifecycle

MCP response phải qua schema validation trước khi được agent sử dụng. Agent giữ nguyên `evidence_ref`, gắn nó vào observation và result; Coordinator tạo ordered union cho output. Verifier đối chiếu evidence top-level với active results và evidence của claim. Evidence không được tự tạo, sửa hoặc dùng chéo case.

## 5. Failure và retry

| Failure | Owner retry | Giới hạn | Khi hết retry |
| --- | --- | --- | --- |
| Tool/model timeout, reset, rate limit, HTTP 5xx, invalid model response | Gateway/sub-agent | 1 retry sau lần đầu | Trả result failed với mã `*_EXHAUSTED` |
| Agent task timeout, crash hoặc không response | Coordinator | 1 recovery cùng context | Gửi failure history cho Verifier |
| Sai semantic, evidence, scope hoặc consistency | Verifier đề xuất, Coordinator thực thi | 1 lần/actor/context | `RETRY_LIMIT_EXCEEDED` |
| Not found hoặc lỗi nghiệp vụ không retryable | Không retry tự động | 0 | Verifier quyết định terminal failure |
| Source conflict | Policy biểu diễn conflict | Không dùng retry để che conflict | Giữ các nguồn và selected source có căn cứ |

Không retry chồng cùng failure class. Đặc biệt, Coordinator không retry một MCP timeout đã exhausted trong sub-agent. Verifier được gọi tối đa 5 round và không phải retry target.

Cascade retry:

- Order/Item → chạy lại Order/Item, Payment, Shipment và Policy.
- Payment → chạy lại Payment và Policy.
- Shipment → chạy lại Shipment và Policy.
- Policy → chỉ chạy lại Policy.

## 6. Verification invariants

Trước khi finalize, Verifier kiểm tra ít nhất: case/schema version, output schema, entity union, evidence union, claim-evidence linkage, tổng tiền bằng `Decimal`, conflict selected source hợp lệ, root-cause rank không trùng và unresolved agent failures. Lỗi merge thuộc Coordinator là terminal; lỗi field semantic thuộc Policy có thể target Policy nếu còn budget.

## 7. Observable trace

Các event chính là `task_assigned`, `handoff`, `verification_completed`, cùng lifecycle event của CLI. Trace chỉ chứa counter, code, actor, target và evidence reference. Không ghi task context, payload đầy đủ, `safe_detail`, exception text, prompt hoặc credential. Mọi event phải pass `day09-trace-event-v1`.

## 8. Tái lập và cấu hình

`Settings.load(root)` nạp `.env` tại root dự án. Dependency được giới hạn version trong `pyproject.toml`. Không dùng random cho quyết định workflow. Lệnh kiểm tra chuẩn:

```text
pytest -q
ruff check src tests
python -m pip check
day09 validate
```

Chi tiết tích hợp agent xem `docs/team-agent-implementation-guide.md`; thiết kế retry đầy đủ xem `docs/superpowers/specs/2026-09-25-coordinator-context-retry-design.md`.
