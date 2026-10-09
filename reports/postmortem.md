# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: tập trung phân tích hệ thống và quy trình nhằm ngăn chặn sự cố tái diễn.

## 1. Timeline (mọi dòng đều có evidence trỏ vào log thật)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:26:29 | Outage bắt đầu (Region A bị netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T05:26:31 | User đầu tiên bị ảnh hưởng (ReadTimeout 503) | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T05:26:44 | Health check alert (Region A sang UNHEALTHY) | `reports/health-events.jsonl:2` |
| 2026-10-09T05:26:46 | Operator confirm cutover (Runbook khởi động) | `reports/runbook-run.jsonl:2` |
| 2026-10-09T05:26:57 | Resolved (Request đầu tiên thành công từ Region B) | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `27.9s` · gap: `-272.1s` (hồi phục nhanh hơn mục tiêu 272.1 giây, đạt tiêu chuẩn khắt khe)
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `-298.0s` (tổn thất dữ liệu tối thiểu)
- **Bước tốn nhiều giây nhất:** `Health-check detect floor (15.0s)` — vì cơ chế anti-flapping yêu cầu 3 lần probe lỗi liên tiếp (`threshold=3`) với chu kỳ thăm dò `interval=5.0s`. Đây là ngưỡng trần bắt buộc trong thiết kế để đảm bảo không kích hoạt failover sai lầm khi mạng chập chờn tạm thời.

## 3. Root cause (5 whys)

1. **Tại sao user nhận lỗi 503 ReadTimeout?** Vì Region A bị cô lập mạng (mô phỏng `netblock` DROP packets), các kết nối HTTP tới Region A bị treo cho đến khi timeout.
2. **Tại sao Edge Proxy không tự đổi hướng lưu lượng ngay khi lỗi?** Vì Edge Proxy sử dụng cơ chế định tuyến DNS/active region tĩnh có TTL cache (5s), không tích hợp circuit breaker động ở tầng proxy để tránh chuyển hướng khi chưa xác nhận tính sẵn sàng của vùng phụ.
3. **Tại sao Region B không thể nhận traffic ngay lập tức?** Vì Region B được duy trì ở chế độ Warm Standby (pool_state cold/warm, không có model weights và vector DB trống) để tối ưu chi phí hạ tầng GPU.
4. **Tại sao cần mất 15 giây để xác định outage?** Vì hệ thống áp dụng nguyên lý chống flapping (`interval=5s, threshold=3`), đòi hỏi đủ bằng chứng trước khi khẳng định toàn bộ vùng A đã chết.
5. **Nếu đây là outage thật, bước nào trong runbook sẽ thất bại?** Bước `2_restore_snapshot` có thể thất bại nếu kho lưu trữ bản sao (`state/_replica/`) nằm chung vùng địa lý với Region A, hoặc version của embedding model không tương thích với schema vector DB khi restore.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Tối ưu hóa chu kỳ health check xuống `interval=2s`, `timeout=1.5s`, `threshold=3` | SRE Team | 2026-10-16 | Giảm RTO 9.0s (detect floor từ 15s còn 6s) |
| 2 | Áp dụng cơ chế Change Data Capture (CDC) đồng bộ vector DB liên tục thay cho batch 30s | Data Engineering Team | 2026-10-23 | Giảm RPO về < 0.5s (0 document bị mất) |
| 3 | Preload model weights vào bộ nhớ RAM của Region B trước khi cần scale GPU | AI Platform Team | 2026-10-30 | Giảm GPU warm-up time từ 6.2s xuống 2.0s |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - `5.0s × 3 = 15.0s`.
   - Với RTO đo được thực tế là 27.9s, Detection floor chiếm: `15.0 / 27.9 ≈ 53.76%` tổng thời gian RTO của hệ thống.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Nếu hạ interval xuống 1s (threshold giữ 3), detection floor giảm từ 15s xuống 3s, giúp RTO giảm **12.0 giây** (RTO từ 27.9s xuống ~15.9s).
   - **Cái giá phải trả (Trade-off):** Nguy cơ **flapping** tăng vọt. Mọi sự cố gián đoạn mạng tạm thời (network jitter hoặc micro-drop kéo dài 3-4s) sẽ bị coi là thảm họa sập vùng, dẫn đến việc kích hoạt chu trình failover tốn kém không cần thiết và làm hệ thống dao động qua lại giữa 2 vùng liên tục.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - `docs_lost` (trong bài đo được là 1 document bị mất trong khoảng 2.0s cuối) đại diện cho dữ liệu giao dịch của người dùng đã được gửi vào Region A nhưng chưa kịp đồng bộ sang bản sao trước thời điểm thảm họa.
   - Nếu Region A mất dữ liệu vĩnh viễn, khách hàng sẽ mất vĩnh viễn các tài liệu này: câu hỏi truy vấn của khách hàng trong tương lai về nội dung này sẽ bị trả lời sai hoặc không tìm thấy. Do đó, hệ thống production cần có hàng đợi bất biến (như Kafka log hoặc transactional outbox) để có thể replay lại các message trong khoảng thời gian RPO bị mất.
