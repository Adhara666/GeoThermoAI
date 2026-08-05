# -*- coding: utf-8 -*-
"""只读监控脚本：不进入容器执行任务逻辑，仅通过 docker CLI 观察。
监控指标：CPU/内存、输出目录大小、服务日志、fd、/api/bootstrap 响应时间。
异常特征：bootstrap 响应 >5s 或失败 = 线程池被占满（卡死前兆）；
日志出现 Traceback/ERROR = 服务错误；目录大小长时间不变 + CPU 低 = 疑似停滞。
注意：用 subprocess 列表参数，避免 Windows shell 差异。
"""
import subprocess
import time
import datetime
import urllib.request

CONTAINER = "geothermoai_test"
OUT_DIR = "/home/studio_service/project/output"
API = "http://127.0.0.1:7860/api/bootstrap"


def shl(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if err:
            out = out + (" | " if out else "") + err[:200]
        return out
    except Exception as e:
        return f"<err {e}>"


def api_ms():
    t0 = time.time()
    try:
        with urllib.request.urlopen(API, timeout=10) as r:
            r.read()
            return f"{r.status} {(time.time()-t0)*1000:.0f}ms"
    except Exception as e:
        return f"FAIL {(time.time()-t0)*1000:.0f}ms {type(e).__name__}"


def main():
    start = time.time()
    last_size = None
    last_size_t = 0.0
    while True:
        time.sleep(20)
        t = datetime.datetime.now().strftime("%H:%M:%S")
        stats = shl(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} | {{.MemUsage}}", CONTAINER])
        size_raw = shl(["docker", "exec", CONTAINER, "du", "-sh", OUT_DIR])
        logs_raw = shl(["docker", "logs", "--since", "22s", CONTAINER])
        logs = "\n".join(logs_raw.splitlines()[-10:]) if logs_raw else ""
        fd_raw = shl(["docker", "exec", CONTAINER, "sh", "-c",
                      "pid=$(pgrep -f 'python3 server.py' | head -1); "
                      "echo fd_total=$(ls /proc/$pid/fd 2>/dev/null | wc -l); "
                      "echo tif_fd=$(ls -l /proc/$pid/fd 2>/dev/null | grep -c '\\.tif')"])
        api = api_ms()
        errs = [l for l in logs.splitlines() if "Traceback" in l or "ERROR" in l or "Exception" in l]
        print(f"--- [{t}] elapsed={time.time()-start:.0f}s ---", flush=True)
        print(f"STATS: {stats}", flush=True)
        print(f"OUT: {size_raw}", flush=True)
        print(f"FD: {fd_raw}", flush=True)
        print(f"API /api/bootstrap: {api}", flush=True)
        if logs:
            print("LOGS:", flush=True)
            print(logs, flush=True)
        if errs:
            print(f"!! 日志异常 {len(errs)} 条", flush=True)
        if size_raw and last_size and size_raw == last_size:
            print(f"!! 输出目录 {int(time.time()-last_size_t)}s 无变化（CPU 满载+fd 活跃=仍在计算）", flush=True)
        if size_raw != last_size:
            last_size = size_raw
            last_size_t = time.time()


if __name__ == "__main__":
    main()
