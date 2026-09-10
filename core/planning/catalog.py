# -*- coding: utf-8 -*-
"""计划编译 — 能力目录（升级第三阶段）。

依据总体技术方案 §5.1「能力目录不是新的 Skill 列表」：

  能力描述面向用户，Skill 面向算法调用。新增能力记录至少包含：
  允许的目标、必需字段、允许参数、输入产物类型、输出类型、模板版本、
  是否产生文件。

与理解层 `core/agent/understanding/capabilities.py` 的分工：那边回答
「编译前还缺什么信息」，这边回答「确认后编译成什么样的节点图」。
主链技能名沿用 `plan_schema.WORKFLOW_STEPS`（§5.1：不改 7 步主链定义本身）；
数据获取内部的展开只发生在节点图模板里，不改变技能本身。
"""

from typing import Any, Dict, List, Tuple

from core.agent.understanding import operations as ops

# 目录/主链模板版本：模板结构变更时 +1（记录进 runs.template_version）
TEMPLATE_VERSION = "plan-catalog-v1"

# ── 节点类型（§5.2 完整生产模板展开；阶段 3 只编译，不派发） ────
# 命名沿用 §5.2 图中的角色；skill 字段标明落到现有 7 步主链的哪一步，
# 展开型节点（search/select/acquire/prepare/check）阶段 3 全部归入
# data_acquisition 技能的内部展开，技能本身零改动。

NODE_TYPES: Dict[str, Dict[str, str]] = {
    "search_scene":   {"skill": "data_acquisition", "label": "检索场景候选"},
    "select_scene":   {"skill": "data_acquisition", "label": "配对选择或场景确定"},
    "acquire_asset":  {"skill": "data_acquisition", "label": "网络资产获取"},
    "prepare_local":  {"skill": "data_acquisition", "label": "本地定标对齐准备"},
    "data_check":     {"skill": "data_acquisition", "label": "原始包数据检查"},
    "preprocess_split": {"skill": "data_pipeline",  "label": "预处理与空间划分"},
    "prep_check":     {"skill": "data_pipeline",  "label": "预处理数据检查"},
    "ttri":           {"skill": "ttri_compute",   "label": "TTRI 一次拟合与应用"},
    "ttri_check":     {"skill": "ttri_compute",   "label": "TTRI 数据检查"},
    "rf_round":       {"skill": "rf_model",       "label": "RF 轮：训练与测试预测"},
    "train_decision": {"skill": "rf_model",       "label": "Train 决定：结束/再试/选定"},
    "promote_best":   {"skill": "rf_model",       "label": "登记最佳模型引用"},
    "tcr":            {"skill": "tcr_compute",    "label": "TCR 与最终温度"},
    "export":         {"skill": "lst_export",     "label": "导出 GeoTIFF"},
    "closure_eval":   {"skill": "accuracy_eval",  "label": "闭合评价"},
    "gapfill":        {"skill": "postprocess",    "label": "独立填洞产品"},
    "rebuild":        {"skill": "",               "label": "显式重建缺失输入"},
}

# ── 完整 LST 主链模板（§5.2：线性链 + 有界调优回边不在图中建环） ──
# 元素：(节点类型, 前驱类型列表)。首节点前驱为空。
FULL_LST_CHAIN: List[Tuple[str, List[str]]] = [
    ("search_scene",    []),
    ("select_scene",    ["search_scene"]),
    ("acquire_asset",   ["select_scene"]),
    ("prepare_local",   ["acquire_asset"]),
    ("data_check",      ["prepare_local"]),
    ("preprocess_split", ["data_check"]),
    ("prep_check",      ["preprocess_split"]),
    ("ttri",            ["prep_check"]),
    ("ttri_check",      ["ttri"]),
    ("rf_round",        ["ttri_check"]),
    ("train_decision",  ["rf_round"]),
    ("promote_best",    ["train_decision"]),
    ("tcr",             ["promote_best"]),
    ("export",          ["tcr"]),
    ("closure_eval",    ["export"]),
]

# ── 能力目录（§5.1 表格的机器可读版） ─────────────────────────
# 每条：allowed_targets 允许的目标；required_fields 必需字段（对齐理解层
# capabilities.REQUIRED_FIELDS）；allowed_params 允许参数及取值域；
# input_types / output_types 输入产物类型；produces_files 是否产生文件；
# chain 节点图模板（None = 不建计算图）。

CAPABILITY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    ops.CAP_QUERY: {
        "label": "解释与查询",
        "allowed_targets": ("任务", "运行", "产物", "问题"),
        "required_fields": (),
        "allowed_params": {},
        "input_types": ("ledger_objects",),
        "output_types": (),
        "produces_files": False,
        "chain": None,  # 只查询，不建计算图
    },
    ops.CAP_SEARCH: {
        "label": "搜索影像",
        "allowed_targets": ("新任务",),
        "required_fields": ("region", "time"),
        "allowed_params": {"cloud_threshold": (0, 100), "datasets": ("landsat", "sentinel2", "dem")},
        "input_types": (),
        "output_types": ("scene_candidates",),
        "produces_files": False,
        "chain": [("search_scene", [])],
    },
    ops.CAP_DOWNLOAD_SUBSET: {
        "label": "下载指定数据",
        "allowed_targets": ("新任务",),
        "required_fields": ("region", "time", "datasets"),
        "allowed_params": {"cloud_threshold": (0, 100), "datasets": ("landsat", "sentinel2", "dem")},
        "input_types": ("scene_candidates",),
        "output_types": ("raw_assets",),
        "produces_files": True,
        # 搜索/选择 → 指定资产获取 → 子集验证，不补其他传感器
        "chain": [("search_scene", []), ("select_scene", ["search_scene"]),
                  ("acquire_asset", ["select_scene"]),
                  ("data_check", ["acquire_asset"])],
    },
    ops.CAP_PREPROCESS: {
        "label": "数据预处理",
        "allowed_targets": ("新任务", "已有任务"),
        "required_fields": ("region",),
        "allowed_params": {"train_ratio": (0.0, 1.0), "val_ratio": (0.0, 1.0),
                           "test_ratio": (0.0, 1.0), "block_size_px": (1, 256),
                           "guard_buffer_m": (0.0, 1000.0)},
        "input_types": ("raw_assets",),
        "output_types": ("prepared_batches",),
        "produces_files": True,
        "chain": [("preprocess_split", []), ("prep_check", ["preprocess_split"])],
    },
    ops.CAP_TRAIN: {
        "label": "模型训练",
        "allowed_targets": ("新任务", "已有任务"),
        "required_fields": ("region",),
        "allowed_params": {"rf_params": None,  # 科学白名单校验在执行层；此处声明允许存在
                           "tuning_max_rounds": (1, 8)},
        "input_types": ("prepared_batches",),
        "output_types": ("trained_models",),
        "produces_files": True,
        "chain": [("ttri", []), ("ttri_check", ["ttri"]),
                  ("rf_round", ["ttri_check"]), ("train_decision", ["rf_round"]),
                  ("promote_best", ["train_decision"])],
    },
    ops.CAP_FULL_LST: {
        "label": "10 米地表温度完整生产",
        "allowed_targets": ("新任务",),
        "required_fields": ("region", "time", "product_mode"),
        "allowed_params": {"cloud_threshold": (0, 100),
                           "datasets": ("landsat", "sentinel2", "dem"),
                           "product_mode": ("pair", "monthly"),
                           "rf_params": None,
                           "train_ratio": (0.0, 1.0), "val_ratio": (0.0, 1.0),
                           "test_ratio": (0.0, 1.0), "block_size_px": (1, 256),
                           "guard_buffer_m": (0.0, 1000.0),
                           "tcr_mode": ("block_constant", "smooth_recentered"),
                           "tuning_max_rounds": (1, 8)},
        "input_types": (),
        "output_types": ("scene_candidates", "raw_assets", "prepared_batches",
                         "trained_models", "lst_10m_product"),
        "produces_files": True,
        "chain": FULL_LST_CHAIN,
    },
    ops.CAP_GAPFILL: {
        "label": "已有结果空洞填补",
        "allowed_targets": ("已有产物",),
        "required_fields": ("region",),
        "allowed_params": {"max_level": (1, 16)},
        "input_types": ("lst_10m_product",),
        "output_types": ("lst_10m_filled_product",),
        "produces_files": True,
        # 仅填洞节点及独立产物（不覆盖主图）
        "chain": [("gapfill", [])],
    },
}


def validate_step_names(steps) -> Tuple[bool, List[str]]:
    """校验步骤名是否全部在 7 步主链 + 填洞之内（§5.1）。

    未知步骤返回失败清单——调用方必须显式报校验失败，
    不能删掉后继续说计划有效。
    """
    from core.agent.plan_schema import WORKFLOW_STEPS

    known = set(WORKFLOW_STEPS) | {"lst_gapfill"}
    unknown = [s for s in steps if s not in known]
    return (not unknown), unknown
