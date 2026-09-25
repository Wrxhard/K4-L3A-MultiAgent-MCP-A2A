# Kiến trúc Coordinator

## Mục tiêu

Coordinator chịu trách nhiệm điều phối các agent, lưu ngữ cảnh có cấu trúc,
tạo kết quả nháp, gửi kết quả cho Verifier và chạy lại đúng agent khi cần.

Coordinator không tự suy luận nghiệp vụ và không tạo evidence giả.

## Framework

- LangGraph `StateGraph` quản lý các bước và trạng thái workflow.
- LangChain `Runnable` là interface chung để Coordinator gọi từng agent.
- Agent của mỗi thành viên có thể dùng model/tool khác nhau miễn implement đúng
  Runnable input/output contract.
- Graph không bật persistent checkpoint trong scope hiện tại; mỗi case có state
  riêng và JSONL trace vẫn là audit artifact chính.

## Thành viên phụ trách

| Thành phần | Người phụ trách | Trách nhiệm |
| --- | --- | --- |
| Coordinator | Người triển khai coordinator | Điều phối, lưu state, retry và dựng output |
| Order/Item, Payment, Shipment | Nam | Thu thập dữ kiện và evidence theo domain |
| Policy | Anh Đạt | Áp dụng policy và đưa ra quyết định nghiệp vụ |
| Verifier | Huy | Kiểm tra output, evidence và yêu cầu retry |

## Luồng chính

```text
Input
  ↓
Coordinator tạo CaseState
  ↓
Order/Item tìm và chuẩn hóa các ID
  ↓
Payment + Shipment chạy song song
  ↓
Policy đưa ra quyết định nghiệp vụ
  ↓
Coordinator dựng candidate_output
  ↓
Verifier kiểm tra
  ├── pass  → Coordinator trả output
  └── retry → Coordinator gọi lại đúng agent
```

Trong LangGraph, luồng được chia thành các node `order_item`,
`payment_and_shipment`, `policy`, `assemble`, `verify` và `retry_target`.

Order/Item chạy trước vì Payment và Shipment có thể cần `order_id`, `item_id`,
`payment_reference` hoặc `shipment_id` đã được xác nhận.

## Ngữ cảnh Coordinator lưu

Với mỗi case, Coordinator lưu:

- input ban đầu;
- kết quả hiện tại của từng agent;
- toàn bộ lịch sử attempt;
- evidence reference đã sử dụng;
- tool-call summary;
- error code và bước bị lỗi;
- feedback của Verifier;
- candidate output hiện tại.

Coordinator không lưu hoặc truyền chain-of-thought, prompt bí mật, API key, raw
exception hay toàn bộ MCP response.

## Trách nhiệm dữ liệu

Các specialist chỉ trả về dữ kiện:

- entity đã xác minh;
- observation;
- evidence reference;
- trạng thái tool call;
- lỗi có cấu trúc nếu thất bại.

Policy nhận dữ kiện của các specialist và trả về:

- `assessment`;
- `claim_assessments` nếu có;
- `root_cause_analysis`;
- `data_conflicts`;
- `financial_resolution`;
- `resolution_actions`.

Coordinator tạo:

- `schema_version`;
- `case_id`;
- danh sách entity đã hợp nhất;
- danh sách evidence không trùng;
- candidate output hoàn chỉnh từ kết quả Policy.

## Ba loại retry

### Retry cục bộ trong gateway hoặc sub-agent

Gateway hoặc sub-agent tự retry bằng code khi lỗi xảy ra trong một tool/model
operation:

- `MCP_TIMEOUT`;
- `CONNECTION_RESET`;
- `RATE_LIMITED`;
- `HTTP_5XX`;
- `INVALID_MODEL_RESPONSE` (self-repair một lần).

Loại retry này không chạy lại toàn agent và không cần Coordinator hay Verifier
ra quyết định. Kết quả agent vẫn phải báo số lần local retry trong tool-call
summary.

Nếu local retry đã hết, agent trả mã như `MCP_TIMEOUT_EXHAUSTED`. Coordinator
không tự động retry lại cùng lỗi, tránh nhân số MCP call qua nhiều tầng.

### Recovery của Coordinator

Coordinator chỉ chạy lại toàn agent khi agent task không thể tự phục hồi:

- `AGENT_TIMEOUT`;
- `AGENT_CRASHED`;
- `AGENT_NO_RESPONSE`.

Mỗi agent task và một phiên bản context chỉ được recovery một lần.

### Retry semantic

Verifier yêu cầu retry khi phát hiện lỗi như:

- thiếu evidence;
- sai entity scope;
- kết luận không khớp evidence;
- refund không nhất quán;
- action không khớp bên chịu trách nhiệm.

Feedback phải chỉ rõ `target_actor`, `error_code` và nội dung cần sửa.

## Quy tắc retry

- Mỗi tool/model operation có tối đa 2 local attempt.
- Mỗi agent task có tối đa 1 coordinator recovery cho cùng context.
- Mỗi actor có tối đa 1 semantic retry cho cùng context.
- Toàn workflow có tối đa 5 vòng verification.
- Retry Order/Item sẽ chạy lại Payment, Shipment và Policy.
- Retry Payment hoặc Shipment sẽ chạy lại Policy.
- Retry Policy không chạy lại các specialist.
- Hết giới hạn thì coordinator dừng với lỗi có cấu trúc, không tạo kết quả giả.

Policy chạy lại do Payment hoặc Shipment có dữ liệu mới là `recompute`, không
phải retry lỗi. Coordinator lưu riêng:

- `agent_invocation`: tổng số lần agent được gọi;
- `context_version`: phiên bản input phụ thuộc;
- `retry_count_for_context`: số retry với cùng input.

Khi `context_version` tăng, `retry_count_for_context` được reset.

## Dạng feedback của Verifier

```json
{
  "verdict": "retry_required",
  "target_actor": "payment",
  "error_code": "MISSING_PAYMENT_EVIDENCE",
  "feedback": "Kiểm tra lại payment status và charged amount",
  "retryable": true
}
```

## Nguyên tắc tích hợp

- Mỗi agent implement đúng interface chung, không import trực tiếp agent khác.
- Mỗi kết quả phải có `actor`, `invocation`, `context_version`,
  `retry_count_for_context`, `status` và payload đúng role.
- Evidence chỉ được dùng trong đúng case đã tạo ra nó.
- Không tự tạo `evidence_ref`.
- Error routing dùng `error_code`, không parse error message.
- Verifier kiểm tra độc lập, không tin lời giải thích của specialist nếu evidence
  không hỗ trợ kết luận.

Thiết kế kỹ thuật đầy đủ nằm tại
`docs/superpowers/specs/2026-09-25-coordinator-context-retry-design.md`.
