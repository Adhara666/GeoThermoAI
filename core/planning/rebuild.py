# -*- coding: utf-8 -*-
"""计划编译 — 缺输入时的显式重建（升级第三阶段）。

依据总体技术方案 §5.5：

  托管执行模式下，Skill 不再在内部调用 `ensure_stage_inputs()` 自行启动
  上游。适配器先进行输入预检，返回"缺少什么、为什么缺、可从哪份已登记
  输入重建"。编译器按原 `stage_rebuild.py` 的依赖关系加入显式重建节点，
  保存使用的原参数、原划分与 TTRI 系数；完成后再唤醒消费节点。

权限边界（原样沿用）：
  已授权继续完整生产时，恢复必要中间文件属于原目标范围；只有"做预处理"
  却连原始数据也没有时，**不能静默扩成下载加全流程**——此时返回权限
  失败，由理解层追问用户。

依赖关系取自 core/stage_rebuild.py 的有向链：
    data_pipeline → ttri_compute → rf_model
                            ↘ tcr_compute → lst_export → accuracy_eval
                            ↘ accuracy_eval
"""

import json
from typing import Any, Dict, List

from core.state_kernel.store import append_event, new_id, utcnow_iso

# 技能级依赖（与 stage_rebuild.ensure_stage_inputs 的重建链一致）：
# key 的输入缺失时，需按 value 链从上游重建。
REBUILD_EDGES: Dict[str, List[str]] = {
    "data_pipeline": [],
    "ttri_compute": ["data_pipeline"],
    "rf_model": ["ttri_compute"],
    "tcr_compute": ["ttri_compute"],
    "lst_export": ["tcr_compute"],
    "accuracy_eval": ["lst_export", "tcr_compute"],
}


class RebuildPermissionError(Exception):
    """重建超出授权范围（缺原始输入且目标不包含获取上游）。"""


def rebuild_chain(target_skill: str,
                  available_inputs: Dict[str, bool]) -> List[str]:
    """返回重建 target_skill 所需的缺失上游技能序列（按依赖顺序）。

    available_inputs：各技能的登记输入是否存在（产物解析器/输入预检提供）。
    只在依赖链内补齐；链外（如 data_acquisition）不在此补。
    """
    missing: List[str] = []
    seen = set()

    def visit(skill: str):
        if skill in seen:
            return
        seen.add(skill)
        if available_inputs.get(skill):
            return  # 该环节输入已登记，无需重建
        for upstream in REBUILD_EDGES.get(skill, []):
            visit(upstream)
        missing.append(skill)

    if available_inputs.get(target_skill):
        return []
    for upstream in REBUILD_EDGES.get(target_skill, []):
        visit(upstream)
    if not available_inputs.get(target_skill):
        missing.append(target_skill)
    return missing


def insert_rebuild_nodes_tx(
    conn,
    *,
    run_id: str,
    consumer_node_id: str,
    target_skill: str,
    available_inputs: Dict[str, bool],
    original_params: Dict[str, Any],
) -> List[str]:
    """在既有节点图里为消费节点显式插入重建节点链（§5.5）。

    在打开的写事务内执行。重建节点沿用 rebuild 类型，params 保存
    原参数/原划分/TTRI 系数引用（来自快照，不由模型生成）；
    依赖边：… → rebuild_1 → … → rebuild_n → 消费节点（顶替原前驱）。
    返回新建节点 id 列表（按执行顺序）。
    """
    if target_skill not in REBUILD_EDGES:
        raise RebuildPermissionError(
            f"技能 {target_skill} 不在可重建清单内（权限边界）"
        )
    chain = rebuild_chain(target_skill, available_inputs)
    if not chain:
        return []
    # 链首若是 data_pipeline 而其输入（raw）缺失，属于"连原始数据也没有"
    if chain[0] == "data_pipeline" and not available_inputs.get("data_pipeline") \
            and "raw_assets" not in (original_params.get("granted_inputs") or {}):
        raise RebuildPermissionError(
            "缺少原始输入且本任务未授权数据获取："
            "不允许静默扩成下载加全流程，需用户明确授权"
        )

    now = utcnow_iso()
    created: List[str] = []
    prev_id = None
    for i, skill in enumerate(chain):
        node_id = new_id()
        conn.execute(
            "INSERT INTO nodes (id, run_id, node_key, node_type, params,"
            " status, exec_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, run_id, f"rebuild_{skill}_{i + 1}", "rebuild",
                json.dumps(
                    {
                        "rebuild_skill": skill,
                        "original_params": original_params.get("snapshot", {}),
                        "source": "stage_rebuild_chain",
                    },
                    ensure_ascii=False, sort_keys=True,
                ),
                "pending", now,
            ),
        )
        if prev_id is not None:
            conn.execute(
                "INSERT INTO node_edges (run_id, predecessor_id, successor_id)"
                " VALUES (?, ?, ?)",
                (run_id, prev_id, node_id),
            )
        prev_id = node_id
        created.append(node_id)

    if created:
        # 消费节点改为依赖最后一个重建节点（顶替其原前驱边）
        conn.execute(
            "DELETE FROM node_edges WHERE successor_id = ? AND run_id = ?",
            (consumer_node_id, run_id),
        )
        conn.execute(
            "INSERT INTO node_edges (run_id, predecessor_id, successor_id)"
            " VALUES (?, ?, ?)",
            (run_id, prev_id, consumer_node_id),
        )
        append_event(
            conn, type="run.rebuild_inserted", run_id=run_id,
            object_type="node", object_id=created[0],
            payload={"target_skill": target_skill,
                     "rebuild_nodes": created},
        )
    return created


def insert_rebuild_nodes(store, *, run_id: str, consumer_node_id: str,
                         target_skill: str, available_inputs: Dict[str, bool],
                         original_params: Dict[str, Any],
                         timeout: float = 30.0) -> List[str]:
    """对外入口（事务包装）。"""
    return store.submit_write(
        insert_rebuild_nodes_tx, run_id=run_id,
        consumer_node_id=consumer_node_id, target_skill=target_skill,
        available_inputs=available_inputs, original_params=original_params,
        timeout=timeout,
    )
