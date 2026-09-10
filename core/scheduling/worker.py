"""轻量 spawn 入口。预算必须在适配器及 numpy/GDAL 导入前生效。"""
import json
import os
import queue
import threading
import time
from pathlib import Path


class Cancelled(Exception):
    pass


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".writing")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def apply_allowance(claim, stage):
    threads = str(claim["threads"])
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[key] = "1"
    os.environ["GDAL_NUM_THREADS"] = threads if claim["kind"] == "compute" else "1"
    os.environ["GDAL_CACHEMAX"] = str(max(16, min(256, claim["memory"] // (16 * 1024 ** 2))))
    os.environ["GDAL_HTTP_MAX_RETRY"] = "2"
    os.environ["GDAL_HTTP_TIMEOUT"] = "60"
    os.environ["GDAL_HTTP_MAX_TOTAL_CONNECTIONS"] = str(max(1, claim["connections"]))
    os.environ["VSI_CACHE"] = "FALSE"
    os.environ["GTAI_NODE_THREADS"] = threads
    os.environ["GTAI_NODE_CONNECTIONS"] = str(max(1, claim["connections"]))
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(stage / "tmp")
    (stage / "tmp").mkdir(exist_ok=True)


def worker_main(spec, control, progress, cancel):
    stage = Path(spec["staging_dir"])
    stage.mkdir(parents=True, exist_ok=True)
    apply_allowance(spec["claim"], stage)
    import psutil
    if os.name != "nt":
        os.setsid()
    identity = {"pid": os.getpid(), "started": psutil.Process().create_time(),
                "attempt_id": spec["attempt_id"], "batch": spec["batch"],
                "authorization": spec["authorization"]}
    write_json(stage / "identity.json", identity)
    control.send(identity)
    # 没有控制器的开始确认时，不得开始工作。
    if not control.poll(30):
        return
    try:
        if control.recv() != "start":
            return
    except EOFError:
        return

    def watch_parent():
        parent = None
        try:
            parent = (os.getppid(), psutil.Process(os.getppid()).create_time())
        except psutil.Error:
            pass
        while True:
            try:
                if control.poll(1):
                    message = control.recv()
                    if message == "stop":
                        cancel.set()
            except (EOFError, OSError):
                # 管道关闭只在父进程确已消失时终止。Windows spawn 在控制器
                # 线程切换期间可能短暂报告 EOF，不能把存活的服务误判为父死。
                alive = False
                if parent:
                    try:
                        p = psutil.Process(parent[0])
                        alive = abs(p.create_time() - parent[1]) < .02 and p.is_running()
                    except psutil.Error:
                        pass
                if alive:
                    time.sleep(.2)
                    continue
                if os.name != "nt":
                    import signal
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                for child in psutil.Process().children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                os._exit(70)

    threading.Thread(target=watch_parent, daemon=True, name="parent-channel").start()
    peak = [0]
    finished = threading.Event()

    def sample():
        proc = psutil.Process()
        while not finished.wait(.2):
            try:
                rss = sum(p.memory_info().rss for p in [proc, *proc.children(recursive=True)])
                peak[0] = max(peak[0], rss)
            except psutil.Error:
                pass

    threading.Thread(target=sample, daemon=True, name="node-peak").start()
    last_progress = [0.]

    def report(*args):
        if cancel.is_set():
            raise Cancelled("已取消本次尝试")
        now = time.monotonic()
        if now - last_progress[0] < .25:
            return
        last_progress[0] = now
        try:
            progress.put_nowait({"attempt_id": spec["attempt_id"], "message": str(args)[-1500:]})
        except queue.Full:
            pass

    envelope = {"attempt_id": spec["attempt_id"], "authorization": spec["authorization"],
                "batch": spec["batch"], "task_version": spec["task_version"],
                "allowance": spec["claim"], "status": "failed"}
    try:
        # 唯一生产适配器；测试可注入可导入的有界探针，Web 不接收此参数。
        from importlib import import_module
        module, function = spec.get("handler", "core.scheduling.adapters:execute_node").split(":")
        result = getattr(import_module(module), function)(spec, report, cancel)
        if cancel.is_set():
            raise Cancelled("已取消本次尝试")
        envelope.update(status="succeeded", result=result)
    except Cancelled as e:
        envelope.update(status="cancelled", error=str(e), error_kind="cancelled")
    except Exception as e:
        import errno
        resource = isinstance(e, MemoryError) or isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.ENOMEM)
        envelope.update(error=str(e)[:2000], error_kind="resource" if resource else getattr(e, "kind", "execution"))
    finally:
        finished.set()
        envelope["peak_memory_bytes"] = max(peak[0], psutil.Process().memory_info().rss)
        write_json(stage / "completion.json", envelope)
        # 通知可丢，completion.json 是结果交接的权威描述。无需等待提交回执。
        try:
            control.send({"completed": True})
        except (BrokenPipeError, EOFError, OSError):
            pass
        progress.cancel_join_thread()
        control.close()
