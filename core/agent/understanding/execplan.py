# -*- coding: utf-8 -*-
"""理解层 → 既有执行链的接缝（升级第二阶段：理解层）。

阶段 2 **不编译节点图**（那是阶段 3 的能力目录与编译器）。本模块只做一件事：
把「程序已经决定好的任务草稿」翻译成现有执行链认得的 plan 形状，
让模型建议与真实执行值**逐项对得上**（§5.3「计划摘要、保存参数和实际
调用值必须一致」）。

这样做的关键收益：执行链不再自己去猜地区、猜时间、猜要跑哪几步——
这些都由理解层校验过了，执行链只负责按已确认的参数干活。
"""

from typing import Any, Dict, List, Optional

from core.agent.understanding import capabilities, operations as ops
from core.agent.understanding.slotbook import SlotBook

# 步骤原因（中文，气泡里展示；与 reflection.planner_rules 的默认说明同义）
_STEP_REASON = {
    "data_acquisition": "检索并获取满足条件的卫星影像",
    "data_pipeline": "定标、掩膜、对齐并划分训练数据",
    "ttri_compute": "拟合并应用地形热响应指数",
    "rf_model": "训练随机森林降尺度模型",
    "tcr_compute": "按真实父格回加残差做热约束修正",
    "lst_export": "导出 10 米地表温度产品",
    "accuracy_eval": "做粗尺度闭合评价",
    "lst_gapfill": "对已有产品做空洞填补",
}


class ResolvedTask:
    """交给执行链的「已确认任务」。执行链据此直接开工，不再重新理解。"""

    def __init__(self, row: Dict[str, Any]):
        self.task_id = str(row.get("task_id") or row.get("id") or "")
        self.version = int(row.get("version") or 1)
        self.capability = str(row.get("capability") or "")
        self.label = str(row.get("label") or "")
        self.book = SlotBook(row.get("slots") or {})

    # ── 槽位读数 ────────────────────────────────────────────────

    @property
    def region_name(self) -> str:
        return str(self.book.value(ops.F_REGION) or "")

    @property
    def study_area_file(self) -> str:
        return str((self.book.get(ops.F_REGION).get("detail") or {}).get("path") or "")

    @property
    def time_range(self) -> Dict[str, str]:
        value = self.book.value(ops.F_TIME)
        if not isinstance(value, dict):
            return {"start": "", "end": ""}
        return {"start": str(value.get("start") or ""),
                "end": str(value.get("end") or "")}

    @property
    def product_mode(self) -> str:
        mode = str(self.book.value(ops.F_PRODUCT_MODE) or "")
        return mode if mode in ops.ALL_PRODUCT_MODES else ""

    @property
    def datasets(self) -> List[str]:
        value = self.book.value(ops.F_DATASETS)
        return list(value) if isinstance(value, (list, tuple)) else []

    @property
    def legacy_intent(self) -> str:
        return capabilities.legacy_intent(self.capability)

    # ── 计划 ────────────────────────────────────────────────────

    def goal(self) -> str:
        when = ""
        tr = self.time_range
        if tr["start"] and tr["end"]:
            when = (f"{tr['start'][:4]} 年 {int(tr['start'][5:7])} 月"
                    if tr["start"][:7] == tr["end"][:7] and tr["start"] != tr["end"]
                    else (tr["start"] if tr["start"] == tr["end"]
                          else f"{tr['start']} 到 {tr['end']}"))
        target = self.region_name or "所选研究区"
        return f"{capabilities.label(self.capability)}：{target} {when}".strip()

    def steps(self) -> List[Dict[str, Any]]:
        tr = self.time_range
        out: List[Dict[str, Any]] = []
        for name in capabilities.legacy_steps(self.capability):
            params: Dict[str, Any] = {}
            if name == "data_acquisition":
                params = {"region": self.study_area_file,
                          "start_date": tr["start"], "end_date": tr["end"]}
                mode = self.product_mode
                if mode:
                    params["composite"] = mode
                if self.datasets:
                    params["datasets"] = list(self.datasets)
                if self.capability == ops.CAP_SEARCH:
                    params["search_only"] = True
            out.append({"skill": name, "params": params,
                        "reason": _STEP_REASON.get(name, "")})
        return out

    def to_plan(self, *, constraints: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
        """执行链认得的 plan 形状（`plan_schema.parse` 会补齐其余字段）。"""
        tr = self.time_range
        return {
            "intent": self.legacy_intent,
            "goal": self.goal(),
            "region": {"name": self.region_name,
                       "study_area_file": self.study_area_file},
            "time_range": {"start": tr["start"], "end": tr["end"]},
            "constraints": dict(constraints or {}),
            "steps": self.steps(),
            "memory_refs": [],
            "task_id": self.task_id,
            "task_version": self.version,
        }

    def to_summary(self) -> Dict[str, Any]:
        """任务卡片用的小结（不含路径与英文技能名）。"""
        tr = self.time_range
        return {
            "task_id": self.task_id, "version": self.version,
            "label": self.label,
            "capability": capabilities.label(self.capability),
            "region": self.region_name,
            "time_start": tr["start"], "time_end": tr["end"],
            "product_mode": {"pair": "配对模式",
                             "monthly": "月度合成模式"}.get(self.product_mode, ""),
            "datasets": self.datasets,
        }
