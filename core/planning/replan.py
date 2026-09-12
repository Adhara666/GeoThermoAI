# -*- coding: utf-8 -*-
"""计划编译 — 修改、重规划与运行版本（升级第三阶段）。

依据总体技术方案 §5.4：

  | 变化                                     | 身份处理                             |
  | 用户改日期/边界/产品，或接受扩大范围       | 任务版本增加，新运行；旧运行停止派发 |
  | 只改排队优先级                            | 科学快照不变，更新调度信息           |

  重规划次数、已排除候选和调优消耗**持久记录**；替代运行继承本任务版本
  已消耗的重规划额度，不因换运行编号、重启或重试清零。

额度持久化的落点：`tasks.accumulated` JSON（阶段 1 建表预留的累计记录列），
形如::

    {"replan_used": 1, "tuning_rounds_used": 2, "tuning_continuity": {"ai_rounds": 0}}

额度上限（replan_max=3、tuning_max_rounds=5/硬上限 8）沿用当前配置解析器
默认值，在编译运行前按实际配置冻结进快照，不借本模块改阈值（§6.2）。
"""

import json
from typing import Any, Dict, Optional

from core.planning.compiler import (
    _insert_run,
    _patch_run,
    _settings_get as _compiler_settings_get,
    _validate_params,
)
from core.planning.snapshot import build_snapshot, snapshot_hash
from core.state_kernel.store import (
    append_event,
    new_id,
    update_versioned,
)

# runs 表无 version 列，对 runs 的写入用 _insert_run/_patch_run（见 compiler.py）

# 触发"新任务版本 + 新运行"的科学身份字段（改它们 = 科学输入变了）
SCIENTIFIC_IDENTITY_FIELDS = ("region", "time", "product_mode", "datasets")

# 触发"替代运行"（同任务版本，重新固定选择记录）的变化类型
RESUPERSE_FIELDS = ("product_mode",)


class ReplanBudgetExhausted(Exception):
    """重规划额度已用尽（上限按运行前冻结配置）。"""


def load_accumulated(accumulated_raw: Any) -> Dict[str, Any]:
    """解析 tasks.accumulated（历史留档兼容：空/坏值视为零额度消耗）。"""
    if isinstance(accumulated_raw, dict):
        return dict(accumulated_raw)
    if isinstance(accumulated_raw, str) and accumulated_raw:
        try:
            return dict(json.loads(accumulated_raw))
        except (ValueError, TypeError):
            pass
    return {}


def replan_budget(accumulated: Any) -> Dict[str, int]:
    """读取持久额度：已消耗的重规划次数与调优轮数（重启/换运行不归零）。

    接受 dict 或 JSON 字符串（tasks.accumulated 列的原生存储形态）。
    """
    accumulated = load_accumulated(accumulated)
    return {
        "replan_used": int(accumulated.get("replan_used") or 0),
        "tuning_rounds_used": int(accumulated.get("tuning_rounds_used") or 0),
        "ai_rounds": int((accumulated.get("tuning_continuity") or {})
                         .get("ai_rounds") or 0),
    }


def consume_replan_budget_tx(conn, *, task_id: str,
                             expected_task_version: int,
                             replan_max: int) -> int:
    """消耗一次重规划额度（原子：额度检查 + 版本推进在同一短事务）。

    返回消耗后的累计额度。额度耗尽抛 ReplanBudgetExhausted，
    绝不静默放行（§5.4：重规划次数持久记录）。
    """
    from core.state_kernel.store import StaleVersionError

    row = conn.execute(
        "SELECT version, accumulated FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    current_version, accumulated_raw = int(row[0]), row[1]
    if current_version != int(expected_task_version):
        raise StaleVersionError(
            f"tasks({task_id}) 版本不匹配：期望 {expected_task_version}，"
            f"当前 {current_version}"
        )
    acc = load_accumulated(accumulated_raw)
    budget = replan_budget(acc)
    if budget["replan_used"] >= int(replan_max):
        raise ReplanBudgetExhausted(
            f"重规划额度已用尽（{budget['replan_used']}/{replan_max}），"
            f"需要用户确认新任务而不是继续重规划"
        )
    acc["replan_used"] = budget["replan_used"] + 1
    conn.execute(
        "UPDATE tasks SET accumulated = ? WHERE id = ?",
        (json.dumps(acc, ensure_ascii=False, sort_keys=True), task_id),
    )
    append_event(
        conn, type="task.replan_consumed", task_id=task_id,
        object_type="task", object_id=task_id,
        object_version=current_version + 1,
        payload={"replan_used": acc["replan_used"], "replan_max": replan_max},
    )
    return acc["replan_used"]


def start_superseding_run_tx(conn, *, task_id: str,
                             expected_task_version: int,
                             settings: Dict[str, Any],
                             replan_max: int,
                             reason: str,
                             stop_old_run: bool = True,
                             project_dir: Optional[str] = None,
                             run_label: Optional[str] = None) -> Dict[str, Any]:
    """建立替代运行（§5.4）：同任务版本、新快照、旧运行留档并停止派发。

    - 消耗一次重规划额度（持久累计，不因换运行清零）；
    - 新运行建立**新的**参数快照（旧快照不可变，绝不覆盖）；
    - 旧运行标记 superseded_by + cancel_requested（真正的执行停摆在阶段 4，
      本阶段只落身份标记）。
    """
    from core.state_kernel import tasks as tasks_tx

    task_row = tasks_tx.load_task(conn, task_id)
    if task_row is None:
        raise KeyError(f"任务不存在：{task_id}")

    # 额度：先消耗（内部做版本比对；注意本函数不再改任务版本——
    # 替代运行不增加任务版本，与"用户改日期/边界/产品=新版本"区分）
    consume_replan_budget_tx(
        conn, task_id=task_id, expected_task_version=expected_task_version,
        replan_max=replan_max,
    )

    old_run_id = task_row.get("current_run_id") or ""
    old_run = None
    if old_run_id:
        old_run = conn.execute(
            "SELECT id FROM runs WHERE id = ?", (old_run_id,)
        ).fetchone()

    # 新快照：以最新草稿重新拍照（§5.3：替代运行另建新快照）
    snapshot = build_snapshot(task_row, settings)
    _validate_params(str(task_row.get("capability") or ""), snapshot)
    new_run_id = new_id()
    _insert_run(
        conn, run_id=new_run_id,
        fields={
            "task_id": task_id,
            "task_version": int(task_row.get("version") or 1) + 1,
            "template_version": "plan-catalog-v1",
            "frozen_inputs": {
                "snapshot": snapshot,
                "snapshot_hash": snapshot_hash(snapshot),
                "capability": task_row.get("capability"),
                "supersedes": old_run_id,
                "reason": reason,
            },
            "status": "queued",
            **({"project_dir": project_dir} if project_dir else {}),
            **({"run_label": run_label} if run_label else {}),
        },
    )

    # 替代运行同样必须有可执行节点图，不能只有一条 queued 运行记录。
    from core.planning.catalog import CAPABILITY_TEMPLATES
    last = {}
    chain = CAPABILITY_TEMPLATES[task_row["capability"]]["chain"] or []
    for order, (kind, predecessors) in enumerate(chain, 1):
        nid = new_id()
        conn.execute("INSERT INTO nodes(id,run_id,node_key,node_type,params,status,exec_order) VALUES(?,?,?,?,?,'pending',?)",
                     (nid, new_run_id, kind, kind, json.dumps({"from_snapshot": True}), order))
        for predecessor in predecessors:
            conn.execute("INSERT INTO node_edges VALUES(?,?,?)", (new_run_id, last[predecessor], nid))
        last[kind] = nid

    if old_run is not None:
        _patch_run(conn, old_run_id,
                   {"superseded_by": new_run_id, "cancel_requested": 1})

    update_versioned(
        conn, "tasks", task_id, expected_task_version,
        {"current_run_id": new_run_id, "summary_status": "queued"},
    )
    append_event(
        conn, type="run.superseded", task_id=task_id, run_id=new_run_id,
        object_type="run", object_id=new_run_id,
        payload={"old_run_id": old_run_id, "reason": reason,
                 "snapshot_hash": snapshot_hash(snapshot)},
    )
    return {"run_id": new_run_id, "old_run_id": old_run_id,
            "snapshot_hash": snapshot_hash(snapshot)}


def start_superseding_run(store, *, task_id: str, expected_task_version: int,
                          settings: Dict[str, Any], replan_max: int,
                          reason: str, timeout: float = 30.0) -> Dict[str, Any]:
    """对外入口（事务包装）。"""
    return store.submit_write(
        start_superseding_run_tx, task_id=task_id,
        expected_task_version=expected_task_version, settings=settings,
        replan_max=replan_max, reason=reason, timeout=timeout,
    )


def apply_field_change_tx(conn, *, task_id: str,
                          expected_task_version: int,
                          field_patches: Dict[str, Any],
                          settings: Dict[str, Any]) -> Dict[str, Any]:
    """「用户改日期/边界/产品方式」的原子处理（§5.4 第 3 行）。

    在同一短事务内：任务版本 +1（新身份）→ 消耗重规划额度 → 建替代运行
    与新快照 → 旧运行停止派发标记 → 事件。科学身份字段的补丁直接应用，
    是否需要澄清由理解层在更上游处理；本函数是程序决定之后的落地。
    """
    from core.state_kernel import tasks as tasks_tx

    task_row = tasks_tx.load_task(conn, task_id)
    if task_row is None:
        raise KeyError(f"任务不存在：{task_id}")

    unknown = [f for f in field_patches if f not in SCIENTIFIC_IDENTITY_FIELDS]
    if unknown:
        raise ValueError(f"以下字段不属于科学身份，不触发重规划：{unknown}")

    # 1) 任务版本 +1（新身份）：科学身份字段在 tasks.slots JSON 内，
    #    合并进现有槽位后用第二阶段的草稿补丁原子操作落库
    slots = dict(task_row.get("slots") or {})
    fields = dict(slots.get("fields") or {})
    for field, entry in field_patches.items():
        fields[field] = dict(entry or {})
    slots["fields"] = fields
    new_version = tasks_tx.patch_task(
        conn, task_id, expected_task_version, slots=slots,
        event_type="task.field_changed",
        event_payload={"fields": sorted(field_patches)},
    )

    # 2) 消耗重规划额度（持久累计）
    replan_max = int(_settings_get(settings, "agent.replan_max") or 3)
    acc_raw = conn.execute(
        "SELECT accumulated FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0]
    acc = load_accumulated(acc_raw)
    budget = replan_budget(acc)
    if budget["replan_used"] >= replan_max:
        raise ReplanBudgetExhausted(
            f"重规划额度已用尽（{budget['replan_used']}/{replan_max}）"
        )
    acc["replan_used"] = budget["replan_used"] + 1
    conn.execute(
        "UPDATE tasks SET accumulated = ? WHERE id = ?",
        (json.dumps(acc, ensure_ascii=False, sort_keys=True), task_id),
    )

    # 3) 替代运行（不再重复消耗额度，直接建新快照 + 旧运行停派发标记）
    old_run_id = task_row.get("current_run_id") or ""
    snapshot = build_snapshot(tasks_tx.load_task(conn, task_id), settings)
    new_run_id = new_id()
    _insert_run(
        conn, run_id=new_run_id,
        fields={
            "task_id": task_id,
            "task_version": new_version,
            "template_version": "plan-catalog-v1",
            "frozen_inputs": {
                "snapshot": snapshot,
                "snapshot_hash": snapshot_hash(snapshot),
                "capability": task_row.get("capability"),
                "supersedes": old_run_id,
                "reason": f"field_change: {sorted(field_patches)}",
            },
            "status": "queued",
        },
    )
    old_run = conn.execute(
        "SELECT id FROM runs WHERE id = ?", (old_run_id,)
    ).fetchone() if old_run_id else None
    if old_run is not None:
        _patch_run(conn, old_run_id,
                   {"superseded_by": new_run_id, "cancel_requested": 1})
    update_versioned(
        conn, "tasks", task_id, new_version,
        {"current_run_id": new_run_id, "summary_status": "queued"},
    )
    final_version = int(conn.execute(
        "SELECT version FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0])
    append_event(
        conn, type="task.field_changed", task_id=task_id, run_id=new_run_id,
        object_type="task", object_id=task_id, object_version=new_version,
        payload={"fields": sorted(field_patches), "old_run_id": old_run_id,
                 "new_run_id": new_run_id, "replan_used": acc["replan_used"]},
    )
    return {"task_version": final_version, "run_id": new_run_id,
            "old_run_id": old_run_id, "replan_used": acc["replan_used"],
            "snapshot_hash": snapshot_hash(snapshot)}


def apply_field_change(store, *, task_id: str, expected_task_version: int,
                       field_patches: Dict[str, Any],
                       settings: Dict[str, Any],
                       timeout: float = 30.0) -> Dict[str, Any]:
    """对外入口（事务包装）。"""
    return store.submit_write(
        apply_field_change_tx, task_id=task_id,
        expected_task_version=expected_task_version,
        field_patches=field_patches, settings=settings, timeout=timeout,
    )


def _settings_get(settings: Dict[str, Any], path: str) -> Any:
    """本模块局部设置取值（与 compiler._settings_get 同义，供额度上限读取）。"""
    return _compiler_settings_get(settings, path)
