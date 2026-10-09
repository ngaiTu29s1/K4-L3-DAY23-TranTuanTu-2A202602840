"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw) -> dict:
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rec = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    ans = input(f"{msg} [y/N]: ").strip().lower()
    return ans in ["y", "yes"]


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook theo đúng trình tự chuẩn."""
    t_start = time.time()

    # Tìm t_outage từ chaos log nếu có
    t_outage = None
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_path.exists():
        for line in chaos_path.read_text().splitlines():
            if line.strip():
                try:
                    c_ev = json.loads(line)
                    if c_ev.get("action") == "kill":
                        t_outage = c_ev.get("ts")
                except Exception:
                    pass

    # 1. xac_nhan_outage: Chờ health check alert (để đảm bảo t_cutover >= t_detect và chống flapping)
    health_log = pathlib.Path("reports/health-events.jsonl")
    alert_detected = False
    wait_health_start = time.time()
    while time.time() - wait_health_start < 25.0:
        if health_log.exists():
            for line in health_log.read_text().splitlines():
                if line.strip():
                    try:
                        ev = json.loads(line)
                        if (ev.get("event") == "state_change"
                                and ev.get("to") == "UNHEALTHY"
                                and ev.get("region") == primary):
                            if t_outage is None or ev.get("ts") >= t_outage:
                                alert_detected = True
                                break
                    except Exception:
                        pass
        if alert_detected:
            break
        time.sleep(0.3)

    p_ok, p_reason = health_checker.probe(primary, timeout=1.5)
    t_ok, t_reason = health_checker.probe(target, timeout=1.5)
    step(1, "xac_nhan_outage", primary=primary, primary_ok=p_ok, primary_reason=p_reason,
         target=target, target_ok=t_ok, target_reason=t_reason, health_alert_detected=alert_detected)

    # 2. thong_bao_incident
    t_operator = time.time()
    notify_delay_s = round(t_operator - t_outage, 2) if t_outage else None
    step(2, "thong_bao_incident", t_outage=t_outage, t_operator=t_operator,
         notify_delay_s=notify_delay_s, message="Incident opened: region down")

    if not confirm(auto, f"Xác nhận kích hoạt failover từ region {primary} sang {target}?"):
        step(2, "failover_cancelled", reason="Operator aborted")
        return {"ok": False, "aborted": True}

    # 3. scale_gpu_pool (gọi failover.failover DUY NHẤT 1 LẦN)
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_ok=fo_res.get("ok"), target=target, waited_s=fo_res.get("waited_s"))
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "failover": fo_res}

    # 4. verify_state_replica
    step(4, "verify_state_replica",
         target=target,
         rpo_seconds=fo_res.get("rpo_seconds"),
         docs_lost=fo_res.get("docs_lost"),
         embed_model_version=fo_res.get("embed_model_version"))

    # 5. dns_cutover
    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text().strip() if active_file.exists() else None
    step(5, "dns_cutover", target=target, active_region=active_region, ok=fo_res.get("ok"))

    # 6. verify_golden_signals (10 requests mẫu vào region phụ)
    latencies = []
    errors = 0
    num_reqs = 10
    with httpx.Client(timeout=3.0) as client:
        for i in range(num_reqs):
            t0 = time.time()
            try:
                r = client.get(f"{URL[target]}/v1/infer", params={"q": f"runbook verification {i}"})
                lat = (time.time() - t0) * 1000.0
                latencies.append(lat)
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
    latencies.sort()
    p95_ms = round(latencies[int(0.95 * (len(latencies) - 1))], 1) if latencies else None
    err_rate = round(errors / num_reqs, 2)
    step(6, "verify_golden_signals", requests=num_reqs, error_rate=err_rate, p95_latency_ms=p95_ms)

    # 7. post_incident
    elapsed_s = round(time.time() - t_start, 2)
    cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=cmd, status="RESOLVED")

    return {
        "ok": True,
        "elapsed_s": elapsed_s,
        "failover_result": fo_res,
        "golden_signals": {"p95_latency_ms": p95_ms, "error_rate": err_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
