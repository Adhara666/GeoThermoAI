"""有界文件传输。响应、块和 Future 都有上限，返回文件描述而不是 bytes。"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .worker import Cancelled, write_json

BLOCK = 4 * 1024 ** 2
BUFFER = 256 * 1024
_cancel = None
_disk_limit = 20 * 1024 ** 3
_margin = 1024 ** 3
_connections = threading.BoundedSemaphore(8)


class TransferError(RuntimeError):
    kind = "network"


class RangeRejected(TransferError):
    pass


def configure(connections, disk_limit, margin, cancel):
    global _connections, _disk_limit, _margin, _cancel
    _connections = threading.BoundedSemaphore(max(1, connections))
    _disk_limit, _margin, _cancel = disk_limit, margin, cancel


def check_cancel():
    if _cancel is not None and _cancel.is_set():
        raise Cancelled("下载已取消，已验证分片保留")


def stable_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


@contextmanager
def file_lock(path, *, blocking=True):
    """操作系统锁随进程退出释放，不凭 PID 文本抢锁。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    try:
        f.seek(0)
        if f.read(1) == b"":
            f.write(b"0")
            f.flush()
    except PermissionError:
        # Windows 上另一个持有者可能已锁住文件；锁循环会等待它释放。
        f.seek(0)
    locked = False
    try:
        while not locked:
            check_cancel()
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, OSError):
                if not blocking:
                    raise RuntimeError("本台账已有活动调度器") from None
                time.sleep(.1)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


@contextmanager
def response(url, headers):
    import requests
    while not _connections.acquire(timeout=.2):
        check_cancel()
    try:
        check_cancel()
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, headers={"Accept-Encoding": "identity", **(headers or {})},
                             stream=True, timeout=(30, 60)) as result:
                result.raise_for_status()
                yield result
    finally:
        _connections.release()


def file_hash(path, offset=0, length=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        f.seek(offset)
        remaining = length
        while remaining is None or remaining > 0:
            data = f.read(BUFFER if remaining is None else min(BUFFER, remaining))
            if not data:
                break
            h.update(data)
            if remaining is not None:
                remaining -= len(data)
    return h.hexdigest()


def _room(path, written, extra):
    check_cancel()
    if written + extra > _disk_limit:
        raise OSError(28, f"下载已到磁盘预留上限 {_disk_limit} 字节，需要调整预算")
    if shutil.disk_usage(Path(path).parent).free < _margin + extra:
        raise OSError(28, "磁盘余量不足，下载受控停止")


def _sequential(url, path, headers, progress, label):
    partial = path.with_suffix(path.suffix + ".partial")
    with response(url, headers) as r:
        if r.status_code != 200:
            raise TransferError(f"顺序下载状态不正确 {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        if total:
            _room(path, 0, total)
        written = 0
        with partial.open("wb") as f:
            for data in r.iter_content(BUFFER):
                if not data:
                    continue
                _room(path, written, len(data))
                f.write(data)
                written += len(data)
                if progress:
                    progress(written, total or None, label)
            f.flush()
            os.fsync(f.fileno())
        if total and written != total:
            raise TransferError(f"下载长度不符，期望 {total}，实际 {written}")
        version = r.headers.get("ETag") or r.headers.get("Last-Modified")
    if not written:
        raise TransferError("下载得到空文件")
    os.replace(partial, path)
    return {"path": str(path), "size": written, "source_version": version,
            "sha256": file_hash(path), "completed_ranges": [[0, written - 1]]}


def fetch_file(url, destination, *, headers=None, workers=8, progress=None, label="", parallel=True):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    if (not urlsplit(url).scheme or os.path.isabs(url)) and Path(url).is_file():
        source = Path(url)
        _room(path, 0, source.stat().st_size)
        shutil.copyfile(source, path)
        return {"path": str(path), "size": path.stat().st_size, "source_version": None,
                "sha256": file_hash(path), "completed_ranges": [[0, path.stat().st_size - 1]]}
    # 锁覆盖探测、续传、完整性校验。等待者绝不消费另一个写入者的 partial。
    with file_lock(str(path) + ".lock"):
        done_path = Path(str(path) + ".download.json")
        if path.is_file() and done_path.is_file():
            previous = json.loads(done_path.read_text())
            if previous.get("identity") == stable_url(url) and previous.get("size") == path.stat().st_size and previous.get("sha256") == file_hash(path):
                return previous
        last_error = None
        for attempt in range(3):
            check_cancel()
            try:
                result = _ranges(url, path, headers, workers, progress, label) if parallel else _sequential(url, path, headers, progress, label)
                result["identity"] = stable_url(url)
                write_json(done_path, result)
                return result
            except Cancelled:
                raise
            except Exception as e:
                # requests 的网络异常也继承 OSError；只有本地写盘错误立即退出。
                if isinstance(e, OSError) and getattr(e, "errno", None) in (28, 12, 13):
                    raise
                last_error = type(e).__name__
                if attempt < 2:
                    for _ in range((attempt + 1) * 5):
                        check_cancel()
                        time.sleep(.1)
        # 不将可能携带签名 URL 的 requests 异常写进持久日志。
        raise TransferError(f"资产下载失败，已重试三次，错误类型 {last_error}")


def _ranges(url, path, headers, workers, progress, label):
    with response(url, {**(headers or {}), "Range": "bytes=0-0"}) as probe:
        cr = re.fullmatch(r"bytes 0-0/(\d+)", probe.headers.get("Content-Range", ""))
        version = probe.headers.get("ETag")
        if version and version.startswith("W/"):
            version = None
        total = int(cr[1]) if cr else 0
        supported = probe.status_code == 206 and cr and version
    if not supported:
        # 忽略 Range 或无可靠源版本时只使用一次流式响应。
        return _sequential(url, path, headers, progress, label)
    _room(path, 0, total)
    partial = Path(str(path) + ".partial")
    journal = Path(str(path) + ".ranges.json")
    state = {"size": total, "version": version, "identity": stable_url(url), "blocks": {}}
    try:
        old = json.loads(journal.read_text())
        if all(old.get(k) == state[k] for k in ("size", "version", "identity")) and partial.is_file():
            for index, checksum in old.get("blocks", {}).items():
                i = int(index)
                if 0 <= i * BLOCK < total and file_hash(partial, i * BLOCK, min(BLOCK, total - i * BLOCK)) == checksum:
                    state["blocks"][index] = checksum
    except (OSError, ValueError):
        pass
    with partial.open("r+b" if partial.exists() else "w+b") as f:
        f.truncate(total)
    state_lock = threading.Lock()
    abort = threading.Event()
    count = (total + BLOCK - 1) // BLOCK

    def get_block(index):
        start, end = index * BLOCK, min(total, (index + 1) * BLOCK) - 1
        for retry in range(3):
            if abort.is_set():
                return
            check_cancel()
            try:
                with response(url, {**(headers or {}), "Range": f"bytes={start}-{end}", "If-Range": version}) as r:
                    if r.status_code != 206 or r.headers.get("Content-Range") != f"bytes {start}-{end}/{total}" or r.headers.get("ETag") != version:
                        raise RangeRejected("Range 响应区间或源版本变化")
                    h, written = hashlib.sha256(), 0
                    with partial.open("r+b") as f:
                        f.seek(start)
                        for data in r.iter_content(BUFFER):
                            check_cancel()
                            if abort.is_set():
                                return
                            if written + len(data) > end - start + 1:
                                raise RangeRejected("分片响应过长")
                            f.write(data)
                            h.update(data)
                            written += len(data)
                        f.flush()
                        os.fsync(f.fileno())
                    if written != end - start + 1:
                        raise TransferError("分片长度不足")
                with state_lock:
                    state["blocks"][str(index)] = h.hexdigest()
                    write_json(journal, state)
                    if progress:
                        progress(sum(min(BLOCK, total - int(i) * BLOCK) for i in state["blocks"]), total, label)
                return
            except (RangeRejected, Cancelled):
                abort.set()
                raise
            except Exception as e:
                if retry == 2 or isinstance(e, OSError) and getattr(e, "errno", None) in (28, 12, 13):
                    abort.set()
                    raise

    remaining = iter(i for i in range(count) if str(i) not in state["blocks"])
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = set()
            for _ in range(max(1, workers)):
                i = next(remaining, None)
                if i is not None:
                    futures.add(pool.submit(get_block, i))
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for f in done:
                    f.result()
                    i = next(remaining, None)
                    if i is not None:
                        futures.add(pool.submit(get_block, i))
    except RangeRejected:
        abort.set()
        # 线程池已经关闭，旧分片不能与顺序响应混拼。
        journal.unlink(missing_ok=True)
        return _sequential(url, path, headers, progress, label)
    if len(state["blocks"]) != count:
        raise TransferError("分片不完整")
    os.replace(partial, path)
    journal.unlink(missing_ok=True)
    return {"path": str(path), "size": total, "source_version": version,
            "sha256": file_hash(path), "completed_ranges": [[0, total - 1]]}
