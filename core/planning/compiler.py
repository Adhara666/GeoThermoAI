# -*- coding: utf-8 -*-
"""计划编译 — 编译器：任务草稿 → 运行 + 节点图（升级第三阶段）。

依据总体技术方案 §5.1–5.3：

  - 确认后的任务草稿编译成节点图（节点、依赖边、完整生产主链模板）；
  - 未知步骤必须返回校验失败，不能删掉后继续说计划有效；
  - 快照在进入执行队列前建立（本编译事务内完成），排队期间修改设置
    不影响已提交任务；
  - 节点参数逐项来自快照（§5.3 参数来源表），文件路径不由模型生成。

本阶段只编译落库（runs/nodes/node_edges 状态为排队/待派发），
**不真正派发执行**（阶段 4 调度器的事）；不改 7 步主链定义本身。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from core.agent.understanding import operations as ops
from core.agent.understanding.slotbook import SlotBook
from core.planning.catalog import (
    CAPABILITY_TEMPLATES,
    NODE_TYPES,
    TEMPLATE_VERSION,
    validate_step_names,
)
from core.planning.snapshot import build_snapshot, snapshot_hash
from core.state_kernel.store import (
    append_event,
    insert_versioned,
    new_id,
    update_versioned,
    utcnow_iso,
)

# 注意：runs 表无 version 列（运行版本语义由 task_version + superseded_by
# 承担，§3.2），因此对 runs 的写入用本模块的 _insert_run/_patch_run，
# 不使用 insert_versioned/update_versioned。


class CompileError(Exception):
    """编译校验失败（能力不支持 / 缺必需字段 / 未知步骤 / 参数越界）。"""


def _settings_get(settings: Dict[str, Any], path: str) -> Any:
    node: Any = settings
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _insert_run(conn, *, run_id: str, fields: Dict[str, Any]) -> str:
    """插入运行（runs 无 version 列：运行版本语义由 task_version +
    superseded_by 承担，§3.2；并发安全由单一写入者保证）。"""
    now = utcnow_iso()
    cols = ["id", "created_at", "updated_at"]
    vals: List[Any] = [run_id, now, now]
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO runs ({', '.join(cols)}) VALUES ({placeholders})", vals
    )
    return run_id


def _patch_run(conn, run_id: str, patch: Dict[str, Any]) -> None:
    """更新运行的少量字段（superseded_by / cancel_requested / status）。"""
    sets, vals = [], []
    for k, v in patch.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.extend([utcnow_iso(), run_id])
    cur = conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)
    if cur.rowcount != 1:
        raise KeyError(f"运行不存在：{run_id}")


def _validate_params(capability: str, snapshot: Dict[str, Any]) -> None:
    """参数越界检查：快照值必须落在能力目录允许的取值域内（§5.1）。"""
    template = CAPABILITY_TEMPLATES[capability]
    allowed = template["allowed_params"] or {}
    params = snapshot.get("params") or {}
    for key, domain in allowed.items():
        if domain is None or key not in params:
            continue
        value = params[key].get("value")
        if isinstance(domain, tuple) and len(domain) == 2 \
                and all(isinstance(v, (int, float)) for v in domain):
            low, high = domain
            if not isinstance(value, (int, float)) or not (low <= value <= high):
                raise CompileError(
                    f"参数 {key}={value!r} 超出允许范围 [{low}, {high}]"
                )
        elif key == "datasets" and isinstance(value, list):
            if not value or any(v not in domain for v in value):
                raise CompileError("指定数据集合为空或包含未知数据源")
        elif isinstance(domain, tuple) and value is not None and value not in domain:
            raise CompileError(f"参数 {key}={value!r} 不在允许取值 {domain} 内")


def compile_task_tx(
    conn,
    *,
    task_id: str,
    expected_task_version: int,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """「编译任务」原子操作：快照 → 运行 → 节点图 → 任务状态推进。

    在 BEGIN IMMEDIATE 事务内执行（由 StateStore.submit_write 提供 conn）。
    settings 由调用方在**进队前**读取并传入；快照建立后与设置解耦。
    """
    from core.state_kernel import tasks as tasks_tx

    task_row = tasks_tx.load_task(conn, task_id)
    if task_row is None:
        raise CompileError(f"任务不存在：{task_id}")
    capability = str(task_row.get("capability") or "")
    template = CAPABILITY_TEMPLATES.get(capability)
    if template is None:
        raise CompileError(f"未知能力，拒绝编译：{capability}")
    if template["chain"] is None:
        raise CompileError(f"能力 {template['label']} 只查询，不建计算图")

    # 1) 必需字段齐全：有值且当前值未被否定（缺项走理解层追问，
    #    编译器显式拒绝而不是猜）
    book = SlotBook(task_row.get("slots") or {})

    def _field_ok(f: str) -> bool:
        if not book.has_value(f):
            return False
        return not book.is_negated(f, book.value(f))

    missing = [f for f in template["required_fields"] if not _field_ok(f)]
    if missing:
        raise CompileError(f"缺少编译前必需信息：{missing}")

    # 2) 步骤名校验：模板产生的技能集合必须在 7 步主链 + 填洞之内
    steps = list(dict.fromkeys(
        NODE_TYPES[node_type]["skill"] for node_type, _ in template["chain"]
        if NODE_TYPES[node_type]["skill"]
    ))
    ok, unknown = validate_step_names(steps)
    if not ok:
        # 未知步骤显式失败，不静默删除（§5.1）
        raise CompileError(f"计划包含未知步骤，校验失败：{unknown}")

    # 3) 参数快照（进队前冻结，存 runs.frozen_inputs，含逐项来源）
    snapshot = build_snapshot(task_row, settings)
    _validate_params(capability, snapshot)

    # 4) 创建运行（排队态；模板版本 + 快照指纹随运行留档）
    run_id = new_id()
    _insert_run(
        conn, run_id=run_id,
        fields={
            "task_id": task_id,
            "task_version": int(task_row.get("version") or 1) + 1,
            "template_version": TEMPLATE_VERSION,
            "frozen_inputs": {
                "snapshot": snapshot,
                "snapshot_hash": snapshot_hash(snapshot),
                "capability": capability,
                "steps": steps,
            },
            "status": "queued",
        },
    )

    # 5) 展开节点图：每类型首次出现的实例做前驱解析（同类型多实例时
    #    前驱取上一个同类实例，例如未来 RF 追加轮；阶段 3 模板每类一个）
    now = utcnow_iso()
    node_ids: Dict[str, List[str]] = {}
    last_of_type: Dict[str, str] = {}
    exec_order = 0
    for node_type, pred_types in template["chain"]:
        if node_type not in NODE_TYPES:
            raise CompileError(f"未知节点类型，校验失败：{node_type}")
        node_id = new_id()
        exec_order += 1
        conn.execute(
            "INSERT INTO nodes (id, run_id, node_key, node_type, params,"
            " status, ready_at, exec_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, run_id, node_type, node_type,
                json.dumps({"from_snapshot": True}, ensure_ascii=False),
                "pending",
                now if not pred_types else None,
                exec_order,
            ),
        )
        node_ids.setdefault(node_type, []).append(node_id)
        for pred_type in pred_types:
            pred_id = last_of_type.get(pred_type)
            if pred_id is None:
                raise CompileError(
                    f"节点 {node_type} 的前驱 {pred_type} 不在链上（模板损坏）"
                )
            conn.execute(
                "INSERT INTO node_edges (run_id, predecessor_id, successor_id)"
                " VALUES (?, ?, ?)",
                (run_id, pred_id, node_id),
            )
        last_of_type[node_type] = node_id

    # 6) 推进任务：当前运行指向新运行（乐观锁：比较任务版本）
    update_versioned(
        conn, "tasks", task_id, expected_task_version,
        {"current_run_id": run_id, "summary_status": "queued"},
    )

    # 7) 事件：编译结果可追溯（前端/巡检按 run 查节点清单与快照）
    append_event(
        conn, type="run.compiled",
        user_id=task_row.get("user_id"),
        conversation_id=task_row.get("conversation_id"),
        task_id=task_id, run_id=run_id,
        object_type="run", object_id=run_id,
        payload={
            "capability": capability,
            "template_version": TEMPLATE_VERSION,
            "snapshot_hash": snapshot_hash(snapshot),
            "node_count": exec_order,
            "steps": steps,
        },
    )

    return {
        "run_id": run_id,
        "task_id": task_id,
        "task_version": int(task_row.get("version") or 1) + 1,
        "capability": capability,
        "template_version": TEMPLATE_VERSION,
        "snapshot_hash": snapshot_hash(snapshot),
        "nodes": [{"node_key": t, "node_id": ids[0], "type": t,
                   "skill": NODE_TYPES[t]["skill"], "label": NODE_TYPES[t]["label"]}
                  for t, ids in node_ids.items()],
    }


def compile_task(store, *, task_id: str, expected_task_version: int,
                 settings: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """对外入口：把一个确认后的任务草稿编译为运行 + 节点图（排队态）。"""
    return store.submit_write(
        compile_task_tx, task_id=task_id,
        expected_task_version=expected_task_version,
        settings=settings, timeout=timeout,
    )


# ── 只读查询（任务详情：快照 + 节点清单，供验收与前端查看） ────


def get_run_snapshot(store, run_id: str) -> Optional[Dict[str, Any]]:
    """读取运行的冻结参数快照（含逐项来源与 taken_at，只读短事务）。"""
    def _tx(conn):
        row = conn.execute(
            "SELECT frozen_inputs FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        wrapper = json.loads(row[0])
        return wrapper.get("snapshot") if isinstance(wrapper, dict) else wrapper

    return store.read(_tx)


def get_run_graph(store, run_id: str) -> Optional[List[Dict[str, Any]]]:
    """读取运行的节点清单与依赖顺序（按执行顺序排列，只读短事务）。"""
    def _tx(conn):
        rows = conn.execute(
            "SELECT n.id, n.node_key, n.node_type, n.status, n.exec_order,"
            " (SELECT GROUP_CONCAT(p.node_key, ',') FROM node_edges e"
            "  JOIN nodes p ON p.id = e.predecessor_id"
            "  WHERE e.successor_id = n.id AND e.run_id = n.run_id) AS preds"
            " FROM nodes n WHERE n.run_id = ? ORDER BY n.exec_order",
            (run_id,),
        ).fetchall()
        cols = ["id", "node_key", "type", "status", "exec_order", "preds"]
        return [dict(zip(cols, r)) for r in rows]

    return store.read(_tx)
