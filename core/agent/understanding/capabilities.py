# -*- coding: utf-8 -*-
"""理解层 — 能力的「编译前最低必要信息」表（升级第二阶段：理解层）。

依据总体技术方案 §4.2 表「目标 / 编译前最低必要信息 / 仍需程序判定的事项」
与最终版 §4.1 表「用户目标 / 必须明确的信息 / 实际执行到哪里」。

本阶段**不编译节点图**（那是阶段 3 的 `task_compiler`），这里只回答一个
问题：「这个目标现在还缺什么，需不需要追问」。能力到旧执行链的映射也
放在这里，作为阶段 2 与既有 7 步主链之间唯一的接缝。
"""

from typing import Dict, List, Tuple

from core.agent.understanding import operations as ops

# 每种能力编译前的必需字段
REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    ops.CAP_QUERY: (),
    ops.CAP_SEARCH: (ops.F_REGION, ops.F_TIME),
    ops.CAP_DOWNLOAD_SUBSET: (ops.F_REGION, ops.F_TIME, ops.F_DATASETS),
    ops.CAP_PREPROCESS: (ops.F_REGION,),
    ops.CAP_TRAIN: (ops.F_REGION,),
    ops.CAP_FULL_LST: (ops.F_REGION, ops.F_TIME, ops.F_PRODUCT_MODE),
    ops.CAP_GAPFILL: (ops.F_REGION,),
}

# 能力的中文/英文名（气泡/任务名文案用；不出现英文能力名）
LABELS: Dict[str, str] = {
    ops.CAP_QUERY: "查询与解释",
    ops.CAP_SEARCH: "搜索影像",
    ops.CAP_DOWNLOAD_SUBSET: "下载指定数据",
    ops.CAP_PREPROCESS: "数据预处理",
    ops.CAP_TRAIN: "模型训练",
    ops.CAP_FULL_LST: "10 米地表温度完整生产",
    ops.CAP_GAPFILL: "已有结果空洞填补",
}

LABELS_EN: Dict[str, str] = {
    ops.CAP_QUERY: "Query & explanation",
    ops.CAP_SEARCH: "Imagery search",
    ops.CAP_DOWNLOAD_SUBSET: "Data download",
    ops.CAP_PREPROCESS: "Data preprocessing",
    ops.CAP_TRAIN: "Model training",
    ops.CAP_FULL_LST: "10 m LST full production",
    ops.CAP_GAPFILL: "Gap-filling for existing results",
}

# 能力 → 旧执行链的意图（阶段 2 只做映射，不改执行链本身）
LEGACY_INTENT: Dict[str, str] = {
    ops.CAP_QUERY: "qa",
    ops.CAP_SEARCH: "partial",
    ops.CAP_DOWNLOAD_SUBSET: "partial",
    ops.CAP_PREPROCESS: "partial",
    ops.CAP_TRAIN: "partial",
    ops.CAP_FULL_LST: "task",
    ops.CAP_GAPFILL: "postprocess",
}

# 能力 → 旧 7 步主链里实际要跑的技能子集（顺序沿用 plan_schema.WORKFLOW_STEPS）
LEGACY_STEPS: Dict[str, Tuple[str, ...]] = {
    ops.CAP_SEARCH: ("data_acquisition",),
    ops.CAP_DOWNLOAD_SUBSET: ("data_acquisition",),
    ops.CAP_PREPROCESS: ("data_pipeline",),
    ops.CAP_TRAIN: ("data_pipeline", "ttri_compute", "rf_model"),
    ops.CAP_FULL_LST: ("data_acquisition", "data_pipeline", "ttri_compute",
                       "rf_model", "tcr_compute", "lst_export", "accuracy_eval"),
    ops.CAP_GAPFILL: ("lst_gapfill",),
}

# 只在整月时间范围下才需要追问「配对 / 月度合成」（§4.4「整月的配对/月度
# 语义前移到草稿校验」）；非整月一律按配对模式，不追问
MODE_ONLY_FOR_WHOLE_MONTH = (ops.CAP_FULL_LST,)


def required_fields(capability: str) -> Tuple[str, ...]:
    return REQUIRED_FIELDS.get(capability, ())


def label(capability: str, lang: str = "zh") -> str:
    table = LABELS if lang != "en" else LABELS_EN
    return table.get(capability, capability)


def legacy_intent(capability: str) -> str:
    return LEGACY_INTENT.get(capability, "task")


def legacy_steps(capability: str) -> List[str]:
    return list(LEGACY_STEPS.get(capability, ()))


def is_production(capability: str) -> bool:
    """是否会真正动数据（Chat 模式禁止的那类）。"""
    return capability not in ("", ops.CAP_QUERY)
