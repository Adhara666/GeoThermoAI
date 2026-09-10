# -*- coding: utf-8 -*-
"""产物与恢复（升级第五阶段）— 引用保护。

依据总体技术方案 §10.4：引用分成两类——

  - **持久依赖**（pinned=1）：有效下游、可恢复任务、最终模型等仍需要
    该产物。即使当前没有进程读文件，也不能删除。
  - **活动使用**（pinned=0，带有效期）：正在计算、下载或打包。
    登记使用者和有效期，结束后释放；重启时对账真实使用者。

清理前必须同时没有持久保护和活动使用（§11.4）。
"""

from typing import Any, Dict, List, Optional

from core.state_kernel.store import utcnow_iso


def acquire_ref_tx(conn, *, artifact_id: str, consumer_type: str,
                   consumer_id: str, usage: str, pinned: bool = False,
                   expires_at: Optional[str] = None) -> None:
    """登记引用（同一消费者同用途唯一，§3.2 artifact_refs 约束）。"""
    conn.execute(
        "INSERT OR IGNORE INTO artifact_refs (artifact_id, consumer_type,"
        " consumer_id, usage, pinned) VALUES (?, ?, ?, ?, ?)",
        (artifact_id, consumer_type, consumer_id, usage, 1 if pinned else 0),
    )
    if expires_at and not pinned:
        # 活动使用的有效期由清理服务到期回收；这里只记录
        conn.execute(
            "UPDATE artifact_refs SET usage = usage || ? WHERE artifact_id = ?"
            " AND consumer_type = ? AND consumer_id = ?",
            (f"@expires:{expires_at}", artifact_id, consumer_type, consumer_id),
        )


def release_ref_tx(conn, *, artifact_id: str, consumer_type: str,
                   consumer_id: str, usage: str = "") -> int:
    """释放引用；返回释放的行数。"""
    sql = ("DELETE FROM artifact_refs WHERE artifact_id = ? AND consumer_type = ?"
           " AND consumer_id = ?")
    args: List[Any] = [artifact_id, consumer_type, consumer_id]
    if usage:
        sql += " AND usage = ?"
        args.append(usage)
    cur = conn.execute(sql, args)
    return cur.rowcount


def release_consumer_tx(conn, *, consumer_type: str, consumer_id: str) -> int:
    """尝试结束/消费者退出时释放其全部活动引用（保留 pinned 持久依赖）。"""
    cur = conn.execute(
        "DELETE FROM artifact_refs WHERE consumer_type = ? AND consumer_id = ?"
        " AND pinned = 0",
        (consumer_type, consumer_id),
    )
    return cur.rowcount


def is_protected(conn, artifact_id: str) -> bool:
    """清理守门（§11.4）：存在持久依赖或未过期活动使用即受保护。"""
    rows = conn.execute(
        "SELECT pinned, usage FROM artifact_refs WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    now = utcnow_iso()
    for pinned, usage in rows:
        if pinned:
            return True
        # 活动使用：usage 带 @expires: 标记的已过期视为释放
        if "@expires:" in (usage or ""):
            try:
                expires = (usage or "").split("@expires:", 1)[1]
                if expires > now:
                    return True
            except ValueError:
                return True
        else:
            return True
    return False


def list_refs(conn, artifact_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT consumer_type, consumer_id, usage, pinned FROM artifact_refs"
        " WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    return [{"consumer_type": r[0], "consumer_id": r[1], "usage": r[2],
             "pinned": bool(r[3])} for r in rows]
