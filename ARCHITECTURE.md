# L3A Architecture Record

Tài liệu này là quyết định kiến trúc chính thức của team cho bài L3A. Nội dung mô tả
các thành phần và hành vi có thể kiểm chứng từ source, output và observable trace;
không lưu prompt bí mật hoặc chain-of-thought.

## 1. Mục tiêu và nguyên tắc thiết kế

Hệ thống điều tra khiếu nại bằng cách tách một case thành các nhiệm vụ chuyên môn,
lấy dữ liệu có thẩm quyền qua MCP, tổng hợp kết luận và kiểm chứng trước khi sinh output.

1. Customer message là claim cần xác minh, không phải ground truth.
2. Chỉ MCP Evidence Gateway cung cấp evidence có thẩm quyền.
3. Không tự tạo, sửa hoặc tái sử dụng `evidence_ref` giữa các case.
4. Mỗi agent có trách nhiệm và quyền gọi tool tối thiểu.
5. Handoff dùng message/artifact có cấu trúc, không dùng hội thoại tự do.
6. Workflow hữu hạn: retry và revision đều có giới hạn.
7. Adjudicator tạo kết luận; Verifier độc lập kiểm tra kết luận đó.
8. Mọi quyết định ảnh hưởng tới output phải truy được về evidence đã consume.
9. Model chỉ xử lý semantic judgment trên facts đã xác minh; model không gọi MCP,
   không tính tiền và không tạo identifier/evidence.
10. Tổng unique model parameters dưới 10B và mọi model output đều qua deterministic gate.

Kiến trúc được chọn là **evidence-first deterministic supervisor + typed specialists
+ evidence graph + heterogeneous small-model review + deterministic verifier**.
Team dùng Qwen3-4B làm Semantic Adjudicator và Phi-4-mini-instruct 3.8B làm Independent
Critic, tổng khoảng 7.8B unique parameters. Hai model không debate tự do và không có
quyền vượt qua evidence/policy constraints.

## 2. System overview

```text
inputs/<case_id>.json
          │
          ▼
┌─────────────────────┐       InvestigationPlan
│ Intake/Coordinator  │─────────────────────────────────────┐
└──────────┬──────────┘                                     │
           │ task_assigned                                  │
     ┌─────┴──────────────┬──────────────────┐               │
     ▼                    ▼                  ▼               │
┌──────────────┐   ┌──────────────┐   ┌──────────────┐       │
│ Order/Item   │   │ Payment      │   │ Shipment     │       │
│ Specialist   │   │ Specialist   │   │ Specialist   │       │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘       │
       │ MCP               │ MCP              │ MCP           │
       └──────────────┬────┴──────────────┬────┘               │
                      ▼                   │                    │
             ┌──────────────────┐         │                    │
             │ Evidence         │◀────────┘                    │
             │ Blackboard       │                              │
             └────────┬─────────┘                              │
                      │ verified facts                         │
                      ▼                                        │
             ┌──────────────────┐       MCP policy             │
             │ Policy Specialist│─────────────────────────────┘
             └────────┬─────────┘
                      ▼
             ┌──────────────────┐
             │ Rule Candidate   │
             │ Generator        │
             └────────┬─────────┘
                      ▼
             ┌──────────────────┐
             │ Qwen3-4B         │
             │ Semantic Judge   │
             └────────┬────────┘
                      ▼
             ┌──────────────────┐
             │ Deterministic    │
             │ Output Builder   │
             └────────┬─────────┘
                      ▼
             ┌──────────────────┐
             │ Phi-4-mini       │── reject once ──▶ Revision
             │ Independent Critic│                    │
             └────────┬─────────┘                    │
                      │ pass/fallback ◀──────────────┘
                      ▼
             ┌──────────────────┐
             │ Final Verifier   │
             └────────┬─────────┘
                      ▼
       outputs/<case_id>.json + traces/trace.jsonl
```

`day09 run` tạo `case_received` trước khi gọi workflow và `case_finalized` sau khi
output pass public schema. Workflow phát các event còn lại.

## 3. Agent ownership

| Actor | Input | Trách nhiệm | Tool scope | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Raw case | Validate scope, chuẩn hóa claim/entity, lập kế hoạch, giao task, điều phối revision | Không gọi domain tool trong luồng chuẩn | `InvestigationPlan`, `TaskRequest`, final handoff |
| Order/item | Task và order/item IDs | Xác minh order, item, seller, giá trị, cancellation/unavailability | `order`, `item`, `seller`; `product` khi cần | `OrderItemReport` |
| Payment | Task và payment/order IDs | Xác minh charge, split, duplicate, paid total và refund state | `payment`, `refund` | `PaymentReport` |
| Shipment | Task và shipment/order IDs | Xây timeline, đối chiếu ETA/actual, phân biệt seller/logistics delay | `shipment`; `order` chỉ để đối chiếu | `ShipmentReport` |
| Policy | Verified facts và candidate issue | Xác định policy, eligibility, responsibility và action constraints | `policy` | `PolicyReport` hoặc `NeedFact` |
| Candidate generator | Verified facts và policy | Dùng rules thu hẹp còn 1–3 issue hợp lệ và tạo phản chứng | Không gọi MCP/model | `CandidateSet` |
| Semantic adjudicator | Candidate set, fact aliases | Qwen3-4B chọn issue/verdict, fact codes và confidence band | Chỉ model API; không MCP | `SemanticDecision` |
| Output builder | Semantic decision + verified facts | Deterministically map evidence, party, refund và actions | Không gọi MCP/model | `DraftAssessment` |
| Independent critic | Draft + supporting/counter facts | Phi-4-mini tìm lỗi semantic/policy, trả error codes | Chỉ model API; không MCP | `CriticReport` |
| Final verifier | Draft, workspace, evidence registry | Kiểm schema, scope, provenance, completeness, money và consistency | Không gọi MCP/model | `VerificationReport` |

Tool discovery xác nhận 10 tool. Allowlist baseline:

- Order/item: `get_order`, `get_order_items`, `get_sellers`,
  `get_product_context`;
- Payment/refund: `get_order_payments`, `get_payment_timeline`,
  `get_refund_timeline`;
- Shipment: `get_shipment_summary`;
- Policy: `get_policy`;
- Customer-history capability: `get_customer_history`, chỉ dùng khi workflow có
  `customer_unique_id` được authoritative evidence cung cấp.

Mọi tool trừ policy/customer history nhận `case_id` và `order_id`. `get_policy` nhận
`case_id` và `policy_version`; `get_customer_history` nhận `case_id` và
`customer_unique_id`. Nếu cần fact ngoài scope, specialist trả `NeedFact` để
Coordinator giao cho đúng owner.

Tool selection tuân theo **minimum sufficient evidence**:

- `get_payment_timeline` đã chứa base payments và lifecycle events, nên không gọi thêm
  `get_order_payments` cho cùng kết luận trừ khi timeline không đủ hoặc lỗi;
- `get_sellers` chỉ cần khi thuộc tính seller ngoài `seller_id` ảnh hưởng kết luận;
- `get_product_context` chỉ cần khi product/category ảnh hưởng claim;
- `get_customer_history` không dùng ở baseline nếu không có `customer_unique_id`;
- `get_refund_timeline` chỉ gọi cho refund claim hoặc khi policy/verified facts yêu cầu.

## 4. Orchestration và state machine

```text
RECEIVED
   └──▶ PLANNING
          └──▶ COLLECTING_EVIDENCE
                  ├──▶ DEGRADED (thiếu evidence sau retry)
                  └──▶ POLICY_CHECK
                           └──▶ GENERATING_CANDIDATES
                                  └──▶ MODEL_ADJUDICATING
                                         └──▶ BUILDING_DRAFT
                                                └──▶ MODEL_CRITIQUING
                                                       ├──▶ VERIFYING
                                                       └──▶ REVISING ──▶ MODEL_CRITIQUING
                                                              │
                                                              └── max one revision
```

Các transition hợp lệ được khai báo tường minh. Termination conditions:

- mọi required task kết thúc bằng `completed`, `not_found` hoặc `failed`;
- mỗi logical MCP call retry tối đa một lần và chỉ khi lỗi transient/idempotent;
- critic/verifier chỉ được yêu cầu một revision cycle;
- sau revision vẫn thiếu evidence thì trả `insufficient_evidence` hoặc
  `needs_investigation`, không lặp tiếp;
- specialist không handoff trực tiếp cho specialist khác.

Qwen3-4B và Phi-4-mini được gọi cho mọi case để kiến trúc đa-model là observable và
có thể đánh giá. Nếu model timeout, trả JSON sai hoặc chọn giá trị ngoài candidate set,
deterministic fallback tiếp quản; model failure không làm mất evidence hợp lệ.

Baseline chạy specialist tuần tự để trace ổn định. Chỉ bật song song sau khi test
chứng minh MCP client an toàn với concurrent calls và ordering vẫn tái lập được.

## 5. A2A protocol nội bộ

Hệ thống áp dụng semantics A2A bằng protocol nội bộ có kiểu dữ liệu rõ ràng; không
tuyên bố là full A2A wire-protocol implementation.

```python
AgentMessage(
    message_id: str,
    case_id: str,
    correlation_id: str,
    task_id: str,
    sender: str,
    recipient: str,
    message_type: Literal[
        "task_request", "task_result", "need_fact", "verification_result"
    ],
    attempt: int,
    payload: object,
    evidence_refs: tuple[str, ...],
)
```

- `case_id` là scope tương đương A2A context.
- `task_id` là duy nhất trong một run và xác định specialist task.
- `correlation_id` nối response với request.
- `attempt` bắt đầu từ 1 và không vượt retry policy.
- Receiver reject message sai recipient, sai case hoặc task đã terminal.

`TaskRequest` chứa mục tiêu, entity IDs, required facts và deadline. Specialist trả
typed report đóng vai trò artifact, không trả đoạn văn tự do:

```python
SpecialistReport(
    case_id: str,
    task_id: str,
    actor: str,
    status: Literal["completed", "partial", "not_found", "failed"],
    facts: tuple[Fact, ...],
    evidence_refs: tuple[str, ...],
    conflicts: tuple[Conflict, ...],
    warnings: tuple[str, ...],
)
```

`Fact` có `fact_code`, `value`, `entity_id` và `evidence_refs`. Metadata từ input
được đánh dấu `source="case_input"` và không được coi là verified business fact.

### Observable trace mapping

| Internal action | Trace event |
| --- | --- |
| Coordinator tạo task | `task_assigned` |
| Specialist dùng MCP response | `tool_result_consumed` |
| Specialist trả report | `handoff` |
| Policy ra quyết định | `policy_decided` |
| Qwen trả semantic decision | `handoff` từ `semantic-adjudicator` tới `output-builder` |
| Phi trả critic report | `handoff` từ `independent-critic` tới `coordinator` |
| Verifier hoàn tất | `verification_completed` |

Trace chỉ chứa actor, target, decision code, tool name, evidence refs và thuộc tính
ngắn có thể quan sát. Không ghi prompt, chain-of-thought, credential hoặc raw message.

## 6. Evidence lifecycle

Evidence blackboard là registry append-only, tạo mới cho từng case.

```text
MCP call
  → gateway validates public envelope
  → specialist validates expected domain/data shape
  → register immutable EvidenceRecord
  → emit tool_result_consumed
  → derive Fact linked to evidence_ref
  → include relevant refs in SpecialistReport
  → Adjudicator links refs to claim/output
  → Verifier checks registry + trace linkage
```

`EvidenceRecord` gồm `case_id`, `evidence_ref`, `result_hash`, `domain`, `tool_name`,
raw `data`, `warnings` và actors đã consume.

Invariants:

1. Không overwrite record có cùng `evidence_ref`.
2. Evidence trong output phải tồn tại trong registry đúng case.
3. Claim evidence là tập con của top-level `evidence_refs`.
4. Mỗi output evidence ref liên kết với ít nhất một fact/claim/decision.
5. Không đưa evidence không liên quan vào output chỉ vì đã gọi tool.
6. Raw evidence không bị sửa; normalized fact là object riêng.
7. Warning/conflict phải ảnh hưởng confidence.

## 7. Investigation và adjudication policy

Coordinator dùng input chỉ để chọn hướng điều tra; kết luận dựa trên verified facts.

Đã kiểm kê 100 input: tất cả có cùng top-level fields `case_id`, `opened_at`,
`customer_request`, `policy_version`. `customer_request` chứa `language`, `message`,
`claimed_order_id` và `claims`. Mỗi primary topic xuất hiện 10 lần; cả 100 case đều
có thêm claim `requested_full_refund`. Claim topic chỉ là candidate routing signal,
không phải verdict.

| Candidate claim | Required specialists |
| --- | --- |
| Canceled/unavailable nhưng đã trả tiền | Order/item, Payment, Policy |
| Giao hàng trễ | Order/item, Shipment, Policy |
| Duplicate charge | Payment, Order/item để đối chiếu expected total, Policy |
| Split payment | Payment, Order/item để đối chiếu expected total |
| Refund pending/failed | Payment/refund, Policy |
| Claim mơ hồ | Order/item trước; mở rộng theo evidence |

Nhiều payment record không tự động là duplicate charge; phải đối chiếu expected total
và semantics giao dịch. Late delivery chỉ được quy trách nhiệm sau khi timeline xác
định nơi phát sinh chậm trễ.

Refund dùng `Decimal` nội bộ và chỉ đổi sang JSON number ở biên output. Confidence
được tính bằng rule tái lập dựa trên evidence coverage, source agreement, policy,
warning/conflict và model review. Model chỉ trả band `high`, `medium`, `low` hoặc
`insufficient`; code chuyển band thành số và áp hard cap khi thiếu evidence.

Observed MCP shapes cho thấy `price`, `freight_value`, `payment_value` và
`amount_brl` thường là decimal string, trong khi `policy.rules.*.refund_brl` là JSON
number. Adapter phải parse tất cả qua `Decimal(str(value))`. `get_refund_timeline`
có thể trả tool error khi order không có refund record; đây là not-found/domain-absent,
không phải bằng chứng rằng refund đã thành công.

## 8. Failure policy

| Failure | Retry? | Fallback | Trace decision code |
| --- | --- | --- | --- |
| MCP timeout/network transient | Một lần nếu idempotent | Partial/failed; degrade nếu evidence bắt buộc | `MCP_TRANSIENT_EXHAUSTED` |
| Unknown tool/invalid arguments | Không | Fail fast vì lỗi code/config | `TOOL_CONTRACT_ERROR` |
| Entity not found | Không, trừ alternate ID đã xác nhận | `not_found`, không tạo dữ liệu | `ENTITY_NOT_FOUND` |
| Evidence envelope/schema sai | Không | Reject, không đăng ký evidence | `INVALID_EVIDENCE_ENVELOPE` |
| Source conflict | Không gọi lặp cùng query | Ghi conflict; chọn nguồn theo policy hoặc unresolved | `SOURCE_CONFLICT` |
| Invalid specialist report | Sửa một lần | Reject report và degrade | `INVALID_SPECIALIST_REPORT` |
| Policy cần fact | Một follow-up đúng owner | Sau đó needs investigation | `POLICY_NEEDS_FACT` |
| Qwen timeout/invalid JSON | Một repair attempt | Rule-based candidate winner, giảm confidence | `ADJUDICATOR_FALLBACK` |
| Qwen chọn ngoài candidate/fact aliases | Không | Reject model result, deterministic fallback | `ADJUDICATOR_CONSTRAINT_VIOLATION` |
| Phi timeout/invalid JSON | Một repair attempt | Deterministic verifier tiếp quản | `CRITIC_FALLBACK` |
| Phi reject draft | Một revision | Qwen nhận error codes + counter facts, không nhận chain-of-thought | `CRITIC_REVISION_REQUIRED` |
| Verification có thể sửa | Một revision | Rebuild từ cùng verified facts | `REVISION_REQUIRED` |
| Verification không thể sửa | Không | Insufficient evidence hoặc fail contract | `UNRESOLVED_VERIFICATION_FAILURE` |

Không retry lỗi business, not-found, schema hoặc authorization. Không biến missing
evidence thành dữ liệu phỏng đoán.

## 9. Verification invariants

### Schema

- đúng `day09-l3a-output-v2`, đủ field, đúng enum/bounds;
- array unique, không quá `maxItems`;
- cause code và evidence ref đúng pattern.

### Scope và provenance

- output `case_id` bằng input case;
- entity thuộc case hoặc được evidence của case xác nhận;
- mọi evidence ref tồn tại trong registry đúng case;
- evidence có `tool_result_consumed` bởi actor hợp lệ.

### Claim và evidence linkage

- claim ID có trong input;
- verdict mạnh có evidence trực tiếp;
- claim evidence là tập con của top-level evidence;
- evidence gián tiếp không được dùng để chứng minh kết luận mạnh.

### Money

- currency là `BRL`, amount không âm;
- tổng refund lines bằng `recommended_refund_brl`;
- refund không vượt thiệt hại được evidence hỗ trợ;
- `no_action` không đi cùng refund/action mâu thuẫn.

### Responsibility, action và calibration

- seller responsibility map tới seller ID đã xác minh;
- logistics responsibility cần shipment/timeline evidence;
- payment provider responsibility cần payment/refund evidence;
- actions không trùng và phù hợp issue/status;
- confidence trong `[0, 1]`, giảm khi thiếu evidence/conflict;
- `insufficient_evidence` không có confidence cao bất hợp lý.

### Model boundary

- Qwen issue nằm trong `CandidateSet` và chỉ viện dẫn fact/evidence aliases đã cấp;
- Phi chỉ trả `pass/reject`, error codes, challenged fields và confidence cap;
- model không trả raw `evidence_ref`, số tiền cuối, tool name hoặc entity mới;
- output builder chỉ map alias đã validate về evidence ref thật;
- model output không hợp lệ phải bị reject trước khi xây submission output.

## 10. Security và privacy

- Team API key chỉ đọc từ `.env` qua `Settings`.
- Không ghi secret vào output, trace, exception hoặc fixture.
- Raw MCP data chỉ tồn tại trong memory của case, không serialize vào trace.
- Customer message không được điều khiển trực tiếp tool name/arguments.
- Tool discovery result được validate trước khi lập allowlist.

## 11. Reproducibility

- Python 3.11/3.12 được khuyến nghị; package yêu cầu `>=3.11`.
- Dependencies lấy từ `pyproject.toml`; ghi commit SHA dùng cho submission.
- Cases chạy tuần tự theo `case-set.json`; in-case concurrency baseline là `1`.
- MCP retry tối đa `1`; revision tối đa `1`; routing không dùng randomness.
- Không dùng shared mutable state giữa các case.
- Model portfolio: `Qwen/Qwen3-4B` + `microsoft/Phi-4-mini-instruct` (3.8B),
  tổng khoảng 7.8B unique parameters; một replica cho mỗi checkpoint.
- Cả hai model dùng temperature `0`, constrained JSON schema và output token cap.
- Model endpoint/version/quantization phải pin trong config; không ghi API key.

```powershell
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 mcp-tools
ruff check .
pytest -q tests/test_starter.py
day09 run
day09 validate
day09 package --output dist/submission.zip
```

## 12. Model governance và quyết định thiết kế

### Phân bổ tham số

| Model | Budget | Nhiệm vụ | Không được làm |
| --- | ---: | --- | --- |
| Qwen3-4B | 4.0B | Semantic issue/verdict, conflict selection, root-cause ranking, confidence band | MCP, evidence ID, money, final JSON |
| Phi-4-mini-instruct | 3.8B | Independent critique, policy/semantic inconsistency, confidence challenge | MCP, override trực tiếp, money, final JSON |
| Deterministic Python | 0B | Routing, evidence, policy lookup, arithmetic, provenance, schema, final authority | N/A |

Tổng unique parameters khoảng 7.8B, dưới giới hạn 10B. Không thêm router model vì
input đã có structured claim topics; dùng model cho routing sẽ dự đoán lại dữ liệu đã
có và tăng failure surface.

### Lý do không dùng debate

Hai model có vai trò bất đối xứng và chỉ một revision. Qwen đề xuất, Phi phản biện,
deterministic verifier quyết định. Không broadcast conversation hoặc cho model bỏ
phiếu, nhằm tránh conformity, context pollution và vòng lặp khó tái lập.

### Model contracts

Qwen input chỉ gồm candidate issues, normalized facts, counter-facts, policy summary
và aliases `E1...En`. Output gồm `selected_issue`, `claim_verdict`,
`supporting_fact_codes`, `supporting_evidence_aliases`, `confidence_band`.

Phi input gồm sanitized draft, supporting/counter facts, policy constraints và kết
quả deterministic checks. Output gồm `verdict`, `error_codes`, `challenged_fields`,
`recommended_confidence_cap`. Không lưu chain-of-thought vào trace hoặc artifact.

## 13. Các quyết định cần xác nhận bằng discovery

Tool names và required arguments đã được discovery. Trước khi hoàn tất adapter, team
còn phải kiểm kê các biến thể `data.events`, warning và error trên đủ nhóm case. Không
suy rộng enum chỉ từ một sample. Mapping được lưu trong `ToolCatalog` và có test;
việc đổi mapping không làm thay đổi ownership hoặc protocol nêu trên.

## 14. Tài liệu triển khai

Quy ước module, kế hoạch chia việc, test matrix, PR rules và Definition of Done nằm
tại `docs/IMPLEMENTATION_GUIDE.md`.

## 15. Design references

- Qwen3-4B official model card: https://huggingface.co/Qwen/Qwen3-4B
- Phi-4-mini official model card: https://huggingface.co/microsoft/Phi-4-mini-instruct
- A2A specification: https://a2a-protocol.org/latest/specification/
- MCP architecture: https://modelcontextprotocol.io/docs/learn/architecture
- LangGraph supervisor pattern: https://github.com/langchain-ai/langgraph-supervisor-py
- Multi-agent debate failure study: https://arxiv.org/abs/2509.05396
- Homogeneous debate vs self-correction study: https://arxiv.org/abs/2605.00914
