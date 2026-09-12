# -*- coding: utf-8 -*-
"""状态内核 — 任务草稿的原子操作（升级第二阶段：理解层）。

依据总体技术方案 §3.4「草稿修改」原子操作与 §3.5「任务、节点和文件状态分开保存」：

  同一短事务内完成：比较对象版本 → 应用补丁 → 递增任务版本 → 记录事件。

本模块所有函数都接收**已打开写事务**的连接（由 `StateStore.submit_write`
提供），自身不开事务、不做模型调用与文件操作。读函数接收只读连接。

槽位（`tasks.slots`）统一形状（§4.2「每个槽位保存值、来源、确认状态、
证据消息和版本」）::

    {
      "fields": {
        "region": {"value": ..., "source": "user", "confirmed": true,
                    "evidence": "武汉 7 月", "detail": {...}},
        ...
      },
      "negations": [
        {"field": "region", "value": "武汉", "evidence": "不是武汉",
         "at": "2026-09-10T..."}
      ]
    }
"""

import json
from typing import Any, Dict, List, Optional

from core.state_kernel.store import (
    append_event,
    insert_versioned,
    new_id,
    update_versioned,
)

# 任务汇总状态（§3.5「任务汇总状态」推导表；阶段 2 只用到其中一部分，
# 其余取值留给阶段 3–5 的编译器与调度器写入）
TASK_DRAFT = "draft"                    # 目标尚未编译
TASK_AWAITING_INFO = "awaiting_info"    # 有阻塞语义问题，等用户补信息
TASK_READY = "ready"                    # 信息齐全，等待编译/派发（阶段 3 起改写为 queued）
TASK_QUEUED = "queued"                  # 已就绪，等待资源
TASK_RUNNING = "running"                # 当前运行至少有一个执行中的节点
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

# 未结束的任务状态：绑定「唯一合理未完成任务」时的候选范围（§4.2 绑定优先级）
OPEN_STATUSES = (TASK_DRAFT, TASK_AWAITING_INFO, TASK_READY, TASK_QUEUED,
                 TASK_RUNNING)


def empty_slots() -> Dict[str, Any]:
    """空槽位包（字段表 + 否定记录）。"""
    return {"fields": {}, "negations": []}


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def normalize_slots(raw: Any) -> Dict[str, Any]:
    """把任意来源的槽位包归一化为 {fields, negations}（缺失部分补空）。"""
    data = _loads(raw, {})
    if not isinstance(data, dict):
        return empty_slots()
    fields = data.get("fields")
    negations = data.get("negations")
    return {
        "fields": fields if isinstance(fields, dict) else {},
        "negations": [n for n in (negations or []) if isinstance(n, dict)],
    }


def row_to_task(row: Any, columns: List[str]) -> Dict[str, Any]:
    """把 tasks 行转成字典，并把 JSON 列解开（供理解层直接消费）。"""
    task = dict(zip(columns, row))
    task["slots"] = normalize_slots(task.get("slots"))
    task["ambiguity"] = _loads(task.get("ambiguity"), [])
    task["accumulated"] = _loads(task.get("accumulated"), {})
    return task


# ── 写操作（必须在写事务内调用） ─────────────────────────────────


def create_task(
    conn,
    *,
    user_id: str,
    project_id: str,
    conversation_id: str,
    capability: str,
    slots: Optional[Dict[str, Any]] = None,
    label: str = "",
    summary_status: str = TASK_DRAFT,
    ambiguity: Optional[List[str]] = None,
    priority: int = 0,
    origin_message_id: str = "",
    origin_command_id: str = "",
) -> str:
    """登记一个任务草稿（版本从 1 起），并写 `task.created` 事件。"""
    task_id = new_id()
    insert_versioned(
        conn,
        "tasks",
        object_id=task_id,
        fields={
            "user_id": user_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "capability": capability,
            "slots": normalize_slots(slots),
            "ambiguity": list(ambiguity or []),
            "summary_status": summary_status,
            "priority": int(priority),
            "label": label,
            "origin_message_id": origin_message_id,
            "origin_command_id": origin_command_id,
        },
    )
    append_event(
        conn,
        type="task.created",
        user_id=user_id,
        conversation_id=conversation_id,
        task_id=task_id,
        object_type="task",
        object_id=task_id,
        object_version=1,
        payload={"capability": capability, "label": label,
                 "summary_status": summary_status},
    )
    return task_id


def patch_task(
    conn,
    task_id: str,
    expected_version: int,
    *,
    slots: Optional[Dict[str, Any]] = None,
    capability: Optional[str] = None,
    summary_status: Optional[str] = None,
    ambiguity: Optional[List[str]] = None,
    label: Optional[str] = None,
    priority: Optional[int] = None,
    event_type: str = "task.updated",
    event_payload: Optional[Dict[str, Any]] = None,
) -> int:
    """按乐观锁修改任务草稿；版本不符抛 StaleVersionError（§3.4「草稿修改」）。

    返回新版本号。调用方拿到的是「本次修改后的版本」，后续问题目标要绑定它。
    """
    patch: Dict[str, Any] = {}
    if slots is not None:
        patch["slots"] = normalize_slots(slots)
    if capability is not None:
        patch["capability"] = capability
    if summary_status is not None:
        patch["summary_status"] = summary_status
    if ambiguity is not None:
        patch["ambiguity"] = list(ambiguity)
    if label is not None:
        patch["label"] = label
    if priority is not None:
        patch["priority"] = int(priority)

    new_version = update_versioned(conn, "tasks", task_id, expected_version, patch)
    row = conn.execute(
        "SELECT user_id, conversation_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    append_event(
        conn,
        type=event_type,
        user_id=row[0] if row else None,
        conversation_id=row[1] if row else None,
        task_id=task_id,
        object_type="task",
        object_id=task_id,
        object_version=new_version,
        payload={**(event_payload or {}),
                 "changed": sorted(k for k in patch)},
    )
    return new_version


# ── 读操作（只读连接，短事务） ───────────────────────────────────


_TASK_COLUMNS = (
    "id, user_id, project_id, conversation_id, capability, version, slots,"
    " ambiguity, summary_status, priority, current_run_id, accumulated,"
    " created_at, updated_at, label, origin_message_id, origin_command_id"
)
_TASK_FIELDS = [c.strip() for c in _TASK_COLUMNS.split(",")]


def load_task(conn, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row_to_task(row, _TASK_FIELDS) if row is not None else None


def list_tasks(
    conn,
    *,
    user_id: str,
    conversation_id: str = "",
    statuses: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """按对话（可选）与状态过滤列出任务，按创建时间升序（编号稳定）。"""
    sql = f"SELECT {_TASK_COLUMNS} FROM tasks WHERE user_id = ?"
    args: List[Any] = [user_id]
    if conversation_id:
        sql += " AND conversation_id = ?"
        args.append(conversation_id)
    if statuses:
        sql += " AND summary_status IN (%s)" % ",".join("?" for _ in statuses)
        args.extend(statuses)
    sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?"
    args.append(int(limit))
    return [row_to_task(r, _TASK_FIELDS) for r in conn.execute(sql, args).fetchall()]


def list_open_tasks(conn, *, user_id: str,
                    conversation_id: str = "") -> List[Dict[str, Any]]:
    return list_tasks(conn, user_id=user_id, conversation_id=conversation_id,
                      statuses=list(OPEN_STATUSES))
