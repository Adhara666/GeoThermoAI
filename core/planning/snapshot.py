# -*- coding: utf-8 -*-
"""计划编译 — 参数快照冻结（升级第三阶段）。

依据总体技术方案 §5.3：

  **快照在进入执行队列前建立，不是进程真正开始计算时才建立。**
  排队期间修改设置，也不能改变已提交任务的实际参数。

实现：编译运行时把任务的全部实际参数（科学约束 + 检索条件 + 模型超参数）
一次性"拍照"为带来源的 JSON，存入 `runs.frozen_inputs`，并附快照指纹。
参数来源逐项记录（§4.2 槽位来源优先级，落到快照的三类）：

  user      用户亲口确认的槽位值（含证据消息）
  default   settings.json 等系统默认（绝不标成 user）

之后的调度/执行只允许从快照取值；设置界面怎么改都不影响本运行。
"""

import copy
import hashlib
import json
from typing import Any, Dict

from core.agent.understanding.slotbook import SlotBook

# 快照包含的默认参数键 → settings.json 中的来源路径（. 分隔）
# 与 catalog 中各能力的 allowed_params 对应；不在 allowed_params 里的键
# 不会被编译进节点参数。
DEFAULT_PARAM_PATHS = {
    "cloud_threshold": "data.cloud_threshold",
    "dem_source": "data.dem_source",
    "train_ratio": "processing.train_ratio",
    "val_ratio": "processing.val_ratio",
    "test_ratio": "processing.test_ratio",
    "block_size_px": "processing.block_size_px",
    "guard_buffer_m": "processing.guard_buffer_m",
    "seed": "processing.seed",
    "step2_min_valid_samples": "processing.step2_min_valid_samples",
    "tcr_mode": "processing.tcr_mode",
    "exec_mode": "agent.default_exec_mode",
    "batch_size": "processing.batch_size",
    "chunk_size": "processing.chunk_size",
    "tuning_max_rounds": "agent.tuning_max_rounds",
    "replan_max": "agent.replan_max",
    "rf_params": "model",
}

EXECUTION_DEFAULTS = {
    "cloud_threshold": 30, "dem_source": "copernicus", "train_ratio": .6,
    "val_ratio": .2, "test_ratio": .2, "block_size_px": 30, "guard_buffer_m": 100.0,
    "seed": 42, "step2_min_valid_samples": 750000, "tcr_mode": "block_constant",
    "batch_size": 500000, "chunk_size": 500000, "exec_mode": "approval",
    "tuning_max_rounds": 5, "replan_max": 3,
    "rf_params": {"n_estimators": 200, "max_depth": 25, "min_samples_split": 16,
                  "min_samples_leaf": 8, "max_features": .5, "random_state": 42},
}

# 槽位字段 → 快照参数键
SLOT_PARAM_KEYS = {
    "region": "region",
    "time": "time",
    "product_mode": "product_mode",
    "datasets": "datasets",
}


def snapshot_hash(snapshot: Dict[str, Any]) -> str:
    """快照指纹：规范化 JSON 的 SHA-256（对比快照是否被改动用）。"""
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_snapshot(task_row: Dict[str, Any],
                   settings: Dict[str, Any]) -> Dict[str, Any]:
    """从任务草稿 + 当前系统设置建立冻结快照。

    返回::

        {
          "params": {
             "<key>": {"value": ..., "source": "user"|"default",
                        "evidence": 原文片段（仅 user）},
             ...
          },
          "slot_values": {...原槽位值深拷贝...},
          "taken_at": UTC ISO 时间
        }

    快照建立后不得修改（不可变）；重规划建立替代运行时另建新快照。
    """
    from core.state_kernel import utcnow_iso

    params: Dict[str, Any] = {}

    # 1) 槽位确认值：用户确认 > 默认（§4.2 优先级，快照只留两类来源）
    book = SlotBook(task_row.get("slots") or {})
    for field, param_key in SLOT_PARAM_KEYS.items():
        entry = book.get(field)
        value = entry.get("value") if entry else None
        if value is not None and not book.is_negated(field, value):
            params[param_key] = {
                "value": copy.deepcopy(entry["value"]),
                "source": "user" if book.source(field) == "user" else "default",
                "evidence": str(entry.get("evidence") or ""),
            }

    # 2) 系统默认：settings.json 按路径取值，未被槽位覆盖的键补齐
    for key, path in DEFAULT_PARAM_PATHS.items():
        if key in params:
            continue  # 槽位确认值优先
        node: Any = settings
        found = True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        if found and node is not None:
            params[key] = {"value": copy.deepcopy(node), "source": "default",
                           "evidence": ""}

    for key, value in EXECUTION_DEFAULTS.items():
        if key not in params:
            params[key] = {"value": copy.deepcopy(value), "source": "default", "evidence": ""}
    params["rf_params"]["value"] = {**EXECUTION_DEFAULTS["rf_params"], **params["rf_params"]["value"]}
    return {
        "params": params,
        "slot_values": copy.deepcopy(task_row.get("slots") or {}),
        # 只保存程序绑定的文件引用，不保存凭据，也不接受模型路径。
        "execution": copy.deepcopy(settings.get("_execution") or {}),
        "taken_at": utcnow_iso(),
    }
