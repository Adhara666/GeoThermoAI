"""
可复用工作流经验

`memory/projects/{project_id}/workflows.json`：一条记录 = 一次「靠谱」的完整流程。
规划 Agent 处理新任务时若检索到同区域的可复用工作流，会沿用其最终参数与云量阈值。

写入条件（三个都满足才写，这是「靠谱」的定义）：
1. 整体状态为 success；
2. 评估通过（报告由系统组装 + LLM 定性短句，始终完整，视为通过）；
3. 测试集 R² ≥ WORKFLOW_MIN_R2（0.75，K24 的合格下限）。
"""

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_lock = threading.Lock()

WORKFLOW_SCHEMA_VERSION = 1

# 工作流写回的精度门槛（K24 合格线）
WORKFLOW_MIN_R2 = 0.75


def should_write(*, status: str, eval_passed: bool, test_r2: Any) -> bool:
    """三个写入条件的唯一判据。"""
    if status != "success" or not eval_passed:
        return False
    try:
        return float(test_r2) >= WORKFLOW_MIN_R2
    except (TypeError, ValueError):
        return False


def new_workflow_id(project_id: str = "") -> str:
    suffix = uuid.uuid4().hex[:6]
    return f"wf_{(project_id or 'p')[:8]}_{int(time.time())}{suffix}"


def build_record(*, project_id: str, experiment_id: str, conv_id: str, region: str,
                 date_range: Optional[List[str]] = None, exec_mode: str = "",
                 pair: Optional[dict] = None, final_params: Optional[dict] = None,
                 tuning_trace: Optional[List[dict]] = None,
                 metrics: Optional[dict] = None,
                 approval_choices: Optional[dict] = None,
                 verdict: str = "good") -> Dict[str, Any]:
    """组装一条工作流记录（形状）。"""
    trace = list(tuning_trace or [])
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "workflow_id": new_workflow_id(project_id),
        "experiment_id": experiment_id,
        "conv_id": conv_id,
        "region": region,
        "date_range": list(date_range or ["", ""]),
        "exec_mode": exec_mode,
        "pair": dict(pair or {}),
        "final_params": dict(final_params or {}),
        "tuning_rounds": len(trace),
        "tuning_trace": trace,
        "metrics": dict(metrics or {}),
        "approval_choices": dict(approval_choices or {}),
        "verdict": verdict,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def record_to_paragraph(record: Dict[str, Any]) -> str:
    """把工作流记录组装为自然语言段落（写入 ChromaDB 供语义检索）。"""
    parts = []
    date_range = record.get("date_range") or ["", ""]
    parts.append(f"{date_range[0]} {record.get('region', '?')} 可复用成功流程"
                 f"（{record.get('verdict', 'good')}）")
    pair = record.get("pair") or {}
    if pair:
        parts.append(f"选用影像 {pair.get('landsat_date', '?')} 与 "
                     f"{pair.get('sentinel2_date', '?')}，相差 "
                     f"{pair.get('time_diff_days', '?')} 天，"
                     f"由{'用户' if pair.get('selected_by') == 'user' else '系统'}选定")
    params = record.get("final_params") or {}
    if params:
        parts.append(f"最终参数 {json.dumps(params, ensure_ascii=False)}")
    if record.get("tuning_rounds"):
        parts.append(f"调优 {record['tuning_rounds']} 轮")
    metrics = record.get("metrics") or {}
    if metrics:
        parts.append(f"测试集 R²={metrics.get('test_r2')}, RMSE={metrics.get('rmse')}K, "
                     f"闭合 MB={metrics.get('closure_mb')}K")
    return "；".join(p for p in parts if p)


class WorkflowExperience:
    """workflows.json 读写封装（每项目一份）。"""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning(f"[memory] 工作流经验读取失败（按空处理）: {e}")
            return []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, records)

    def add(self, record: Dict[str, Any]) -> None:
        with _lock:
            records = self._load()
            records.append(record)
            self._save(records)

    def all(self) -> List[Dict[str, Any]]:
        return self._load()

    def delete_by_conv(self, conv_id: str) -> int:
        """级联删除：某对话产生的工作流经验一并清掉。"""
        with _lock:
            records = self._load()
            kept = [r for r in records if r.get("conv_id") != conv_id]
            removed = len(records) - len(kept)
            if removed:
                self._save(kept)
            return removed

    def find_for_region(self, region: str) -> Optional[Dict[str, Any]]:
        """取同区域中测试集 R² 最高的一条可复用流程。"""
        best = None
        best_r2 = -1.0
        for record in self._load():
            if region and region not in str(record.get("region", "")):
                continue
            try:
                r2 = float((record.get("metrics") or {}).get("test_r2"))
            except (TypeError, ValueError):
                continue
            if r2 > best_r2:
                best, best_r2 = record, r2
        return best
