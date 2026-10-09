# Runbook 1 trang — Region chính down

Runbook xử lý sự cố khẩn cấp lúc 3h sáng dành cho kỹ sư trực on-call. Mọi bước đều có lệnh thực thi trực tiếp và tín hiệu xác nhận hoàn thành rõ ràng.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 dr/health_checker.py --interval 2 --threshold 3 --duration 10` hoặc `python3 chaos/kill_region.py status` | `a.alive=false` hoặc `/readyz` của Region A fail ≥ 3 lần liên tiếp; Region B còn hoạt động | On-call SRE |
| 2 | Mở incident + Bấm giờ RTO | `python3 -c "import time; print('INCIDENT_OPENED:', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))"` | Incident ID được tạo trên kênh trực ban (#incident-alerts) và mốc `t_operator` được ghi nhận | On-call SRE / Incident Commander |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Output trả về JSON metadata với `restored_at`, `vectors.sqlite` và `weights/model.bin` có trên đĩa | On-call SRE / Data Platform Engineer |
| 4 | Scale pool warm → full | `echo "full" > state/region-b/pool_state && curl -s http://127.0.0.1:8002/readyz` | Endpoint `http://127.0.0.1:8002/readyz` trả về HTTP status 200 và `"ready": true` | On-call SRE |
| 5 | DNS/LB cutover | `printf "b" > edge/active_region` | `curl -s http://127.0.0.1:8080/edge/state` trả về `"active_region": "b"` | On-call SRE |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -s http://127.0.0.1:8080/v1/infer; done` | Toàn bộ 10 request trả về status 200, error rate = 0%, p95 latency < 50ms | On-call SRE |
| 7 | Đo RTO + Postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Kết quả trả về `"rto_verdict": "PASS"`, log ghi nhận đầy đủ trong `reports/postmortem.md` | Incident Commander / Tech Lead |

---

### Quy trình Rollback (Failover ngược về Region A)

**Điều kiện bắt buộc trước khi Rollback:**
1. **Region A đã phục hồi hoàn toàn:** Process Region A đã online trở lại, được restore qua lệnh `python3 chaos/kill_region.py restore --region a --backend bare`.
2. **Health check Region A xanh ổn định:** Endpoint `http://127.0.0.1:8001/readyz` trả về HTTP 200 liên tục ít nhất **15 phút** không có bất kỳ timeout hoặc rớt kết nối nào.
3. **Đồng bộ hóa ngược trạng thái dữ liệu (Reverse State Sync):** Tất cả documents mới được ingest vào Region B trong thời gian sự cố phải được snapshot và restore ngược về Region A (`python3 state/snapshot.py put --region b --backend fs && python3 state/snapshot.py get --region a --backend fs`) để tránh mất mát dữ liệu hoặc phân mảnh phân tán.

**Ai quyết định Rollback:**
- **Thẩm quyền quyết định:** Duy nhất **Incident Commander (IC)** hoặc **Tech Lead hệ thống** có quyền ký duyệt rollback. Tuyệt đối cấm cấu hình full-auto failback không có circuit breaker để ngăn chặn hiện tượng flapping (hai region đảo chiều liên tục làm tăng thời gian downtime).
- **Lệnh thực hiện rollback:**
  ```bash
  printf "a" > edge/active_region
  curl -s http://127.0.0.1:8080/edge/state
  ```
