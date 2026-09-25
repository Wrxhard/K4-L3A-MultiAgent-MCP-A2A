# Kiến trúc Coordinator

## Mục tiêu

Coordinator chịu trách nhiệm điều phối các agent, lưu ngữ cảnh có cấu trúc,
tạo kết quả nháp, gửi kết quả cho Verifier và chạy lại đúng agent khi cần.

Coordinator không tự suy luận nghiệp vụ và không tạo evidence giả.

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

## Hai loại retry

### Retry kỹ thuật

Coordinator tự retry khi lỗi rõ ràng là tạm thời:

- `MCP_TIMEOUT`;
- `CONNECTION_RESET`.

Loại retry này không cần gọi Verifier để ra một quyết định hiển nhiên.

### Retry semantic

Verifier yêu cầu retry khi phát hiện lỗi như:

- thiếu evidence;
- sai entity scope;
- kết luận không khớp evidence;
- refund không nhất quán;
- action không khớp bên chịu trách nhiệm.

Feedback phải chỉ rõ `target_actor`, `error_code` và nội dung cần sửa.

## Quy tắc retry

- Mỗi actor có tối đa 2 attempt, bao gồm lần đầu.
- Toàn workflow có tối đa 5 vòng verification.
- Retry Order/Item sẽ chạy lại Payment, Shipment và Policy.
- Retry Payment hoặc Shipment sẽ chạy lại Policy.
- Retry Policy không chạy lại các specialist.
- Hết giới hạn thì coordinator dừng với lỗi có cấu trúc, không tạo kết quả giả.

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
- Mỗi kết quả phải có `actor`, `attempt`, `status` và payload đúng role.
- Evidence chỉ được dùng trong đúng case đã tạo ra nó.
- Không tự tạo `evidence_ref`.
- Error routing dùng `error_code`, không parse error message.
- Verifier kiểm tra độc lập, không tin lời giải thích của specialist nếu evidence
  không hỗ trợ kết luận.

Thiết kế kỹ thuật đầy đủ nằm tại
`docs/superpowers/specs/2026-09-25-coordinator-context-retry-design.md`.
