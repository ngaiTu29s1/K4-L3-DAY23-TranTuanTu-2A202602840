# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T05:25:15` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.1s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +16.8s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +23.0s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +23.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | +27.9s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:37` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `27.9s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `2.0s` / `1` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm `interval_s` (ví dụ từ 5s xuống 2s) hoặc giảm threshold (nhưng tăng rủi ro flapping) |
| Snapshot restore | 1.8s | 2_restore → 3_scale trong `reports/failover-events.jsonl:2` | Dùng ổ SSD NVMe tốc độ cao hơn hoặc đồng bộ dữ liệu liên tục theo streaming CDC |
| GPU pool warm-up | 6.2s | `waited_s` ở `reports/failover-events.jsonl:4` | Duy trì Region B ở chế độ warm/hot standby hoặc preload weights sẵn trong GPU memory |
| DNS/LB TTL cache | 4.9s | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:37` và `reports/failover-events.jsonl:5`) | Giảm DNS TTL trên Edge Proxy từ 5s xuống 1s hoặc dùng Global Load Balancer có active health check |
