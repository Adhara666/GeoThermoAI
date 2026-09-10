"""可控 HTTP 服务验收真实流式传输、Range 校验、续传与取消。"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.scheduling import transfer
from core.scheduling.worker import Cancelled

PAYLOAD = bytes(range(256)) * 256


class Source(BaseHTTPRequestHandler):
    size = 18 * 1024 ** 2 + 17
    behavior = "range"
    version = '"v1"'
    hits = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            raw_range = self.headers.get("Range", "")
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", raw_range)
            start, end = (int(match[1]), min(int(match[2]), cls.size-1)) if match else (0, cls.size-1)
            with cls.lock:
                cls.hits.append((start, end))
            if cls.behavior == "ignore" or not match:
                start, end = 0, cls.size-1
                self.send_response(200)
            else:
                self.send_response(206)
                cr_start = start + 1 if cls.behavior == "wrong" and end > 0 else start
                self.send_header("Content-Range", f"bytes {cr_start}-{end}/{cls.size}")
            if cls.behavior != "unknown":
                self.send_header("Content-Length", str(end - start + 1))
            self.send_header("ETag", cls.version)
            self.end_headers()
            pos = start
            while pos <= end:
                n = min(len(PAYLOAD) - pos % len(PAYLOAD), end - pos + 1)
                self.wfile.write(PAYLOAD[pos % len(PAYLOAD):pos % len(PAYLOAD)+n])
                pos += n
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with cls.lock:
                cls.active -= 1


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/asset"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "asset.tif"
        self.cancel = threading.Event()
        transfer.configure(3, 64*1024**2, 1024**2, self.cancel)
        Source.behavior = "range"
        Source.hits = []
        Source.peak = 0

    def tearDown(self):
        self.cancel.clear()
        self.temp.cleanup()

    def expected(self):
        h = hashlib.sha256()
        n = Source.size
        while n:
            data = PAYLOAD[:min(n, len(PAYLOAD))]
            h.update(data)
            n -= len(data)
        return h.hexdigest()

    def test_range_bounded_connections_and_content(self):
        out = transfer.fetch_file(self.url, self.path, workers=8)
        self.assertEqual(out["size"], Source.size)
        self.assertEqual(out["sha256"], self.expected())
        self.assertLessEqual(Source.peak, 3)
        self.assertIsInstance(out["path"], str)

    def test_ignored_range_falls_back_without_buffering_full_response(self):
        Source.behavior = "ignore"
        out = transfer.fetch_file(self.url, self.path)
        self.assertEqual(out["sha256"], self.expected())

    def test_wrong_range_never_assembles_corrupt_file(self):
        Source.behavior = "wrong"
        out = transfer.fetch_file(self.url, self.path)
        self.assertEqual(out["sha256"], self.expected())

    def test_cancel_preserves_only_verified_blocks_and_resume(self):
        def progress(*args):
            self.cancel.set()
        with self.assertRaises(Cancelled):
            transfer.fetch_file(self.url, self.path, workers=1, progress=progress)
        self.assertFalse(self.path.exists())
        state = json.loads(Path(str(self.path) + ".ranges.json").read_text())
        completed = set(state["blocks"])
        self.assertTrue(completed)
        self.cancel.clear()
        Source.hits.clear()
        out = transfer.fetch_file(self.url, self.path, workers=1)
        self.assertEqual(out["sha256"], self.expected())
        block_requests = [a for a, b in Source.hits if b != 0]
        self.assertTrue(all(int(i)*transfer.BLOCK not in block_requests for i in completed))

    def test_corrupt_or_changed_version_invalidates_resume(self):
        def progress(*args):
            self.cancel.set()
        with self.assertRaises(Cancelled):
            transfer.fetch_file(self.url, self.path, workers=1, progress=progress)
        self.cancel.clear()
        Source.version = '"v2"'
        Source.hits.clear()
        out = transfer.fetch_file(self.url, self.path, workers=1)
        self.assertEqual(out["source_version"], '"v2"')
        self.assertEqual(out["sha256"], self.expected())
        self.assertIn((0, transfer.BLOCK-1), Source.hits)

    def test_unknown_length_stops_at_disk_reservation(self):
        Source.behavior = "unknown"
        transfer.configure(1, 1024**2, 1024**2, self.cancel)
        with self.assertRaises(OSError):
            transfer.fetch_file(self.url, self.path, parallel=False)
        self.assertLessEqual(Path(str(self.path) + ".partial").stat().st_size, 1024**2)

    def test_single_writer_for_same_cached_asset(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(transfer.fetch_file, self.url, self.path, workers=2) for _ in range(2)]
            results = [j.result() for j in jobs]
        self.assertEqual(results[0]["sha256"], results[1]["sha256"])
        self.assertEqual(sum(a == 0 and b == transfer.BLOCK-1 for a, b in Source.hits), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
