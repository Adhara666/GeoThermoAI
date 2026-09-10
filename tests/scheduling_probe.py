"""只供验收注入的进程探针，不注册为生产能力。"""
import json
import os
import time
from pathlib import Path


def execute(spec, report, cancel):
    from core.scheduling.worker import Cancelled
    p = json.loads(spec["node"]["params"])
    if p.get("crash"):
        os._exit(19)
    if p.get("fail_once"):
        marker = Path(spec["staging_dir"]).parent.parent / (spec["node"]["id"] + ".failed_once")
        if not marker.exists():
            marker.write_text("failure injected")
            from core.scheduling.transfer import TransferError
            raise TransferError("验收注入网络失败")
    if p.get("question") and not p.get("execution_answer"):
        return {"question": {"prompt": "选择是否继续", "candidates": [{"id": "accept", "label": "接受"}]}}
    until = time.monotonic() + p.get("duration", .2)
    while time.monotonic() < until:
        if cancel.is_set():
            raise Cancelled("探针响应取消")
        report("probe", time.monotonic())
        time.sleep(.03)
    # 第五阶段验收注入：在暂存工作区产出文件，验证两步提交全链路
    for rel, content in (p.get("emit_files") or {}).items():
        target = Path(spec["staging_dir"]) / "work" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return {"context": {"pid": os.getpid(), "threads": os.environ["OMP_NUM_THREADS"], "finished": time.time()},
            "message": "探针完成"}
