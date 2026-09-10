"""一次性 2 GiB 流式下载验收，不加入常规单元测试套件。"""
import http.server
import json
import os
import psutil
import threading
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scheduling import transfer

SIZE = 2 * 1024 ** 3


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        raw = self.headers.get("Range", "")
        start, end = 0, SIZE - 1
        if raw.startswith("bytes="):
            start, end = [int(x) if x else 0 for x in raw[6:].split("-", 1)]
            if not self.headers.get("Range").split("-", 1)[1]:
                end = SIZE - 1
        end = min(end, SIZE - 1)
        self.send_response(206 if raw else 200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        self.send_header("ETag", '"phase4-large-probe"')
        self.end_headers()
        remain = end - start + 1
        block = b"\0" * (256 * 1024)
        while remain:
            n = min(remain, len(block))
            self.wfile.write(block[:n])
            remain -= n

    def log_message(self, *_):
        pass


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = baseline
    stop = threading.Event()

    def sample():
        nonlocal peak
        while not stop.wait(.05):
            peak = max(peak, process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        with tempfile.TemporaryDirectory(prefix="gtai-large-") as directory:
            transfer.configure(8, 3 * 1024 ** 3, 1024 ** 2, None)
            result = transfer.fetch_file(f"http://127.0.0.1:{server.server_port}/large.bin", Path(directory) / "large.bin", workers=8)
            actual = Path(result["path"]).stat().st_size
            print(json.dumps({"size": actual, "rss_baseline": baseline, "rss_peak": peak,
                              "rss_delta": peak - baseline, "sha256": result["sha256"]}))
            return 0 if actual == SIZE and peak - baseline < 256 * 1024 ** 2 else 1
    finally:
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
