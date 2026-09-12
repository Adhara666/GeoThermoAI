# -*- coding: utf-8 -*-
"""产物与恢复（升级第五阶段）— 有记录的受控清理与丢失检测。

依据总体技术方案 §11.4：

  清理过程使用「登记待删 → 受控删除 → 确认已清理」：控制循环先检查
  引用并标记；文件服务解析真实路径和链接目标，确认位于该运行或指定
  缓存根内才删除；最后更新可用性和用量。删除失败记录原因并有界重试。

  「已清理」（按政策清理，历史节点仍成功）和「意外丢失」（本应保留的
  文件缺失/校验不一致，如实标损坏）必须区分（§10.3）。
"""

import shutil
from pathlib import Path
from typing import Any, Dict, List

from core.state_kernel.store import append_event
from core.artifacts.publisher import (
    AV_AVAILABLE,
    AV_CLEANED,
    AV_MISSING,
    AV_PENDING_DELETE,
    RETENTION_KEEP_FOREVER,
)

# 允许删除文件的路径前缀白名单：运行目录与缓存根（§11.4 防路径逃逸）。
# 由调用方传入实际根列表。


def request_cleanup_tx(conn, *, artifact_id: str, allowed_roots: List[str],
                       reason: str = "") -> Dict[str, Any]:
    """登记待删 → 受控删除 → 确认已清理（同一事务内完成状态流转）。

    守门顺序（§10.4/§11.4）：
      1) 保留类别为 keep_forever 的正式结果默认不自动删除；
      2) 存在持久依赖或活动使用 → 拒绝；
      3) 真实路径必须落在 allowed_roots 内才执行删除（防路径逃逸）。
    删除失败记录原因（availability 退回 available，等待有界重试）。
    """
    from core.artifacts.refs import is_protected

    row = conn.execute(
        "SELECT path, availability, retention_class FROM artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"产物不存在：{artifact_id}")
    path, availability, retention = str(row[0]), row[1], row[2]
    if retention == RETENTION_KEEP_FOREVER:
        return {"deleted": False, "reason": "正式结果默认不自动删除（keep_forever）"}
    if availability != AV_AVAILABLE:
        return {"deleted": False, "reason": f"当前可用性为 {availability}，无需清理"}

    if is_protected(conn, artifact_id):
        return {"deleted": False, "reason": "产物仍被持久依赖或活动使用引用"}

    resolved = Path(path).resolve()
    roots = [Path(r).resolve() for r in allowed_roots]
    if not any(_is_relative_to(resolved, root) for root in roots):
        return {"deleted": False, "reason": "文件路径不在允许的运行目录或缓存根内"}

    conn.execute("UPDATE artifacts SET availability = ? WHERE id = ?",
                 (AV_PENDING_DELETE, artifact_id))
    try:
        missing_before = not resolved.exists()
        if not missing_before:
            resolved.unlink()
    except OSError as e:
        conn.execute("UPDATE artifacts SET availability = ? WHERE id = ?",
                     (AV_AVAILABLE, artifact_id))
        return {"deleted": False, "reason": f"删除失败（将重试）：{e}"}

    conn.execute("UPDATE artifacts SET availability = ? WHERE id = ?",
                 (AV_CLEANED, artifact_id))
    append_event(
        conn, type="artifact.cleaned", object_type="artifact",
        object_id=artifact_id,
        payload={"path": path, "reason": reason,
                 "already_absent": missing_before},
    )
    return {"deleted": True, "already_absent": missing_before, "path": path}


def mark_missing_tx(conn, *, artifact_id: str, detail: str) -> None:
    """本应保留的文件缺失或校验不一致 → 如实标损坏（§10.3）。"""
    row = conn.execute(
        "SELECT availability FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    if row is None or row[0] != AV_AVAILABLE:
        return
    conn.execute("UPDATE artifacts SET availability = ? WHERE id = ?",
                 (AV_MISSING, artifact_id))
    append_event(
        conn, type="artifact.missing", object_type="artifact",
        object_id=artifact_id, payload={"detail": detail[:500]},
    )


def sweep_missing_tx(conn, *, limit: int = 200) -> List[Dict[str, Any]]:
    """启动/巡检对账：只检查数据库登记过的正式产物（不扫全仓库、
    不整库哈希，§11.1 第 4 条），缺失或校验不一致的标 missing。"""
    rows = conn.execute(
        "SELECT id, path, content_hash FROM artifacts"
        " WHERE availability = ? LIMIT ?",
        (AV_AVAILABLE, limit),
    ).fetchall()
    reported: List[Dict[str, Any]] = []
    for artifact_id, path, content_hash in rows:
        p = Path(path)
        if not p.is_file():
            mark_missing_tx(conn, artifact_id=artifact_id,
                            detail="登记产物文件缺失")
            reported.append({"artifact_id": artifact_id, "path": path,
                             "problem": "missing"})
            continue
        if content_hash:
            import hashlib
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != content_hash:
                mark_missing_tx(conn, artifact_id=artifact_id,
                                detail="内容校验不一致")
                reported.append({"artifact_id": artifact_id, "path": path,
                                 "problem": "hash_mismatch"})
    return reported


def cleanup_stale_intents_tx(conn, *, run_roots: List[str],
                             limit: int = 50) -> List[Dict[str, Any]]:
    """对账 abandoned 提交意向残留的未采用目录（§10.2 竞态收尾）。"""
    from core.artifacts.publisher import INTENT_ABANDONED
    rows = conn.execute(
        "SELECT id, target_dir FROM commit_intents WHERE status = ? LIMIT ?",
        (INTENT_ABANDONED, limit),
    ).fetchall()
    out = []
    for intent_id, target_dir in rows:
        target = Path(target_dir)
        if target.exists() and any(_is_relative_to(target, Path(r).resolve())
                                   for r in run_roots):
            shutil.rmtree(target, ignore_errors=True)
            out.append({"intent_id": intent_id, "removed": str(target)})
    return out


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
