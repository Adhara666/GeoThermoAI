# GeoThermoAI 第三方 Skill 开发指南

## 1. 概述

### 1.1 架构设计理念

GeoThermoAI 采用 **插件化 Skill 架构**，将地表温度降尺度流水线拆分为多个独立的、可替换的功能模块（Skill）。每个 Skill 封装一个完整的处理步骤，通过统一的接口契约实现模块间的解耦和协作。

核心设计原则：

- **接口统一**：所有 Skill 继承自 `BaseSkill` 基类，实现相同的属性和方法签名，确保流水线调度器和 AI 智能体可以无差别地调用任何 Skill。
- **分组互替**：同一分组（group）内的 Skill 共享相同的输入/输出 Schema，可以互相替换而无需修改流水线上下游。
- **动态加载**：第三方 Skill 放置在 `skills/` 目录下即可被自动发现和加载，无需修改核心代码。
- **AI 友好**：每个 Skill 通过自然语言描述参数和功能，供 LLM 智能体理解并自动编排执行计划。

### 1.2 第三方 Skill 的作用和优势

| 优势 | 说明 |
|------|------|
| **算法扩展** | 在不修改核心代码的前提下，引入新的机器学习模型（如 XGBoost、LightGBM、神经网络） |
| **领域适配** | 针对特定地理区域或数据源定制预处理逻辑 |
| **社区共享** | 第三方 Skill 可独立发布、版本管理和复用 |
| **零侵入** | 通过目录约定和自动加载机制实现"即插即用" |

---

## 2. 快速开始

### 2.1 创建 Skill 目录结构

在项目根目录的 `skills/` 文件夹下创建你的 Skill 包目录：

```
GeoThermoAI/
├── core/
│   └── skills/
│       ├── base_skill.py          # 基类定义
│       ├── skill_registry.py      # 注册中心
│       └── builtin/               # 内置 Skill
├── skills/                        # 第三方 Skill 目录
│   └── my_xgboost/                # 你的 Skill 包
│       ├── __init__.py            # 必须存在，自动加载的入口
│       └── xgboost_skill.py       # Skill 实现
└── ...
```

> **重要**：每个 Skill 包目录下必须包含 `__init__.py` 文件，否则自动加载机制会跳过该目录。

### 2.2 实现 BaseSkill 接口

创建 `skills/my_xgboost/xgboost_skill.py`：

```python
from typing import Any, Dict, List
from core.skills.base_skill import BaseSkill, SkillParameter, Hyperparameter, SkillResult


class XGBoostSkill(BaseSkill):
    """XGBoost 梯度提升树回归模型训练与测试集预测"""

    @property
    def name(self) -> str:
        return "xgboost_model"

    @property
    def group(self) -> str:
        return "model_train"

    @property
    def description(self) -> str:
        return "使用 XGBoost 梯度提升树训练回归模型（含TTRI特征），对测试集进行预测并输出评估指标。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="train_csv", type="file_path",
                           description="训练集CSV路径（需包含TTRI列）", required=True),
            SkillParameter(name="val_csv", type="file_path",
                           description="验证集CSV路径", required=True),
            SkillParameter(name="test_csv", type="file_path",
                           description="测试集CSV路径", required=True),
            SkillParameter(name="output_dir", type="file_path",
                           description="输出目录路径", required=True),
            SkillParameter(name="n_estimators", type="number",
                           description="提升轮数", required=False, default=300),
            SkillParameter(name="max_depth", type="number",
                           description="树的最大深度", required=False, default=6),
            SkillParameter(name="learning_rate", type="number",
                           description="学习率", required=False, default=0.1),
        ]

    @property
    def hyperparameters(self) -> List[Hyperparameter]:
        return [
            Hyperparameter(name="n_estimators", label="提升轮数", type="number",
                           default=300, min=50, max=1000, step=50,
                           description="XGBoost 提升轮数"),
            Hyperparameter(name="max_depth", label="最大深度", type="number",
                           default=6, min=3, max=15, step=1,
                           description="每棵树的最大深度"),
            Hyperparameter(name="learning_rate", label="学习率", type="number",
                           default=0.1, min=0.01, max=0.3, step=0.01,
                           description="学习率，越小越保守"),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "train_csv": "训练集CSV路径",
            "val_csv": "验证集CSV路径",
            "test_csv": "测试集CSV路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "model_path": "模型文件路径",
            "train_metrics": "训练集评估指标",
            "test_metrics": "测试集评估指标",
            "metrics_path": "指标JSON路径",
            "feature_importance": "特征重要性列表",
        }

    def execute(self, params: Dict[str, Any],
                progress_callback=None, log_callback=None) -> SkillResult:
        # 你的实现逻辑...
        pass
```

### 2.3 注册 Skill

在 `skills/my_xgboost/__init__.py` 中导出 Skill 类：

```python
from .xgboost_skill import XGBoostSkill

__all__ = ["XGBoostSkill"]
```

完成以上步骤后，**无需任何额外注册操作**。GeoThermoAI 的 `SkillRegistry` 会在启动时自动扫描 `skills/` 目录并加载所有 Skill。

---

## 3. BaseSkill 接口详解

### 3.1 必需属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | Skill 唯一标识符，在整个注册中心中不可重复。建议使用小写+下划线命名，如 `"xgboost_model"` |
| `group` | `str` | Skill 分组标识。同组 Skill 可互相替换，流水线按组名调度 |
| `description` | `str` | 自然语言功能描述（1-2句话），供 LLM 智能体理解该 Skill 的功能并生成执行计划 |
| `parameters` | `List[SkillParameter]` | 该 Skill 接受的参数列表。每个参数包含 `name`、`type`、`description`、`required`、`default` 等字段 |
| `input_schema` | `Dict[str, str]` | 输入数据 Schema，键为字段名，值为描述。**同组 Skill 必须定义完全相同的 `input_schema`** |
| `output_schema` | `Dict[str, str]` | 输出数据 Schema，键为字段名，值为描述。**同组 Skill 必须定义完全相同的 `output_schema`** |

### 3.2 必需方法

```python
def execute(self, params: Dict[str, Any],
            progress_callback=None, log_callback=None) -> SkillResult:
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `params` | `Dict[str, Any]` | 调用方传入的参数字典，键与 `parameters` 中定义的 `name` 对应 |
| `progress_callback` | `Callable[[str, float, str], None]` | 进度回调，签名为 `(step_name, percent, message)`，`percent` 范围 0.0~1.0 |
| `log_callback` | `Callable[[str, str], None]` | 日志回调，签名为 `(level, message)`，`level` 通常为 `"INFO"` / `"WARNING"` / `"ERROR"` |

返回值 `SkillResult` 包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 执行是否成功 |
| `message` | `str` | 人类可读的结果摘要 |
| `data` | `Dict[str, Any]` | 结构化输出数据，键应与 `output_schema` 对应 |
| `artifacts` | `List[str]` | 生成的文件路径列表 |

### 3.3 可选属性

```python
@property
def hyperparameters(self) -> List[Hyperparameter]:
    """该 Skill 的超参数列表 - 供 UI 动态渲染参数表单"""
    return []  # 默认为空
```

`Hyperparameter` 用于描述可通过 UI 调节的超参数，支持以下类型：

| type 值 | UI 控件 | 额外字段 |
|---------|---------|----------|
| `"number"` | 数值滑块/输入框 | `min`, `max`, `step` |
| `"select"` | 下拉选择框 | `options`（选项列表） |
| `"boolean"` | 开关 | 无 |

每个 `Hyperparameter` 还包含 `label`（UI 显示文本）和 `description`（tooltip 提示）。

---

## 4. Skill 分组机制

GeoThermoAI 的流水线由多个步骤组成，每个步骤对应一个 Skill 分组。同一分组内的 Skill 处理相同的输入数据并产生相同格式的输出，可以互相替换。

### 4.1 可替换组

以下分组支持通过第三方 Skill 替换内置实现：

| 分组名 | 功能 | 内置 Skill | 输入 | 输出 |
|--------|------|-----------|------|------|
| `data_process` | 数据获取与预处理 | `data_acquisition`, `data_pipeline` | 原始栅格路径 | 训练/验证/测试集 CSV |
| `ttri_compute` | 地形热响应指数计算 | `ttri_compute` | 训练/验证/测试集 CSV | 含 TTRI 列的 CSV + 回归系数 |
| `model_train` | 机器学习模型训练与预测 | `rf_model` | 含 TTRI 的训练/验证/测试集 CSV | 模型文件 + 评估指标 + 特征重要性 |

> **示例**：开发一个 `XGBoostSkill`（`group="model_train"`），当用户指定"使用 XGBoost"时，AI 智能体会自动选择你的 Skill 而非内置的 `rf_model`。

### 4.2 固定组

以下分组由内置 Skill 实现，一般不需要替换（但技术上仍可扩展）：

| 分组名 | 功能 | 内置 Skill | 说明 |
|--------|------|-----------|------|
| `tcr_compute` | 热约束残差修正 | `tcr_compute` | 基于模型预测结果计算 TCR，与具体模型无关 |
| `lst_export` | 最终 LST 计算与 GeoTIFF 导出 | `lst_export` | 纯数学计算和文件格式转换 |
| `accuracy_eval` | 精度评估 | `accuracy_eval` | 空间一致性评估和值域分析 |

### 4.3 辅助组

| 分组名 | 功能 | 内置 Skill | 说明 |
|--------|------|-----------|------|
| `ai_assist` | AI 智能辅助 | `ai_assistant` | 参数推荐、结果诊断、报告生成 |

---

## 5. 接口契约

### 5.1 核心规则

**同组的所有 Skill 必须定义完全相同的 `input_schema` 和 `output_schema`。**

这是保证流水线上下游数据正确传递的基础约束。如果第三方 Skill 的 Schema 与同组内置 Skill 不一致，流水线调度器将无法正确传递数据。

### 5.2 示例：model_train 组的接口契约

所有属于 `model_train` 组的 Skill（如内置的 `rf_model` 和第三方的 `xgboost_model`）必须遵守以下契约：

**input_schema：**

```python
{
    "train_csv": "训练集CSV路径",       # 含 TTRI 列的训练集
    "val_csv": "验证集CSV路径",         # 含 TTRI 列的验证集
    "test_csv": "测试集CSV路径",        # 含 TTRI 列的测试集
    "output_dir": "输出目录",           # 模型文件和结果的输出位置
}
```

**output_schema：**

```python
{
    "model_path": "模型文件路径",       # 训练好的模型文件（.pkl / .joblib 等）
    "train_metrics": "训练集评估指标",  # dict，包含 R2, RMSE, MAE 等
    "test_metrics": "测试集评估指标",   # dict，包含 R2, RMSE, MAE 等
    "metrics_path": "指标JSON路径",     # 评估指标的持久化文件路径
    "feature_importance": "特征重要性列表",  # list，按特征名排序的重要性值
}
```

**params 中可接受的超参数**（通过 `parameters` 属性声明）：

虽然不同模型的超参数不同，但 `execute()` 方法接收的 `params` 字典会同时包含 Schema 字段和超参数字段。你的实现需要从中提取所需参数。

---

## 6. 开发示例：XGBoost Skill

以下是一个完整的第三方 Skill 示例，展示如何开发一个可替换 `rf_model` 的 XGBoost 模型 Skill。

### 6.1 目录结构

```
skills/
└── my_xgboost/
    ├── __init__.py
    └── xgboost_skill.py
```

### 6.2 完整代码

**skills/my_xgboost/xgboost_skill.py**：

```python
"""
XGBoost 梯度提升树 Skill

替换随机森林，使用 XGBoost 进行 LST 降尺度回归：
    - 训练 XGBoost 回归模型
    - 对测试集进行预测
    - 输出评估指标和特征重要性
"""

import os
import json
import time
from typing import Any, Dict, List

from core.skills.base_skill import BaseSkill, SkillParameter, Hyperparameter, SkillResult


class XGBoostSkill(BaseSkill):
    """XGBoost 梯度提升树回归模型训练与测试集预测"""

    @property
    def name(self) -> str:
        return "xgboost_model"

    @property
    def group(self) -> str:
        return "model_train"

    @property
    def description(self) -> str:
        return "使用 XGBoost 梯度提升树训练回归模型（含TTRI特征），对测试集进行预测，输出模型文件、评估指标（R²/RMSE/MAE）和特征重要性排序。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="train_csv",
                type="file_path",
                description="训练集CSV路径（需包含TTRI列）",
                required=True,
            ),
            SkillParameter(
                name="val_csv",
                type="file_path",
                description="验证集CSV路径（需包含TTRI列）",
                required=True,
            ),
            SkillParameter(
                name="test_csv",
                type="file_path",
                description="测试集CSV路径（需包含TTRI列）",
                required=True,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="n_estimators",
                type="number",
                description="提升轮数（树的数量）",
                required=False,
                default=300,
            ),
            SkillParameter(
                name="max_depth",
                type="number",
                description="树的最大深度",
                required=False,
                default=6,
            ),
            SkillParameter(
                name="learning_rate",
                type="number",
                description="学习率（步长收缩）",
                required=False,
                default=0.1,
            ),
            SkillParameter(
                name="subsample",
                type="number",
                description="每棵树的样本采样比例",
                required=False,
                default=0.8,
            ),
            SkillParameter(
                name="colsample_bytree",
                type="number",
                description="每棵树的特征采样比例",
                required=False,
                default=0.8,
            ),
        ]

    @property
    def hyperparameters(self) -> List[Hyperparameter]:
        return [
            Hyperparameter(
                name="n_estimators",
                label="提升轮数",
                type="number",
                default=300,
                min=50,
                max=1000,
                step=50,
                description="XGBoost 提升轮数，越大越稳定但越慢",
            ),
            Hyperparameter(
                name="max_depth",
                label="最大深度",
                type="number",
                default=6,
                min=3,
                max=15,
                step=1,
                description="每棵树的最大深度",
            ),
            Hyperparameter(
                name="learning_rate",
                label="学习率",
                type="number",
                default=0.1,
                min=0.01,
                max=0.3,
                step=0.01,
                description="学习率，越小越保守但需要更多轮数",
            ),
            Hyperparameter(
                name="subsample",
                label="样本采样比例",
                type="number",
                default=0.8,
                min=0.5,
                max=1.0,
                step=0.1,
                description="每棵树的样本采样比例",
            ),
            Hyperparameter(
                name="colsample_bytree",
                label="特征采样比例",
                type="number",
                default=0.8,
                min=0.3,
                max=1.0,
                step=0.1,
                description="每棵树的特征采样比例",
            ),
        ]

    # ── 接口契约：与 rf_model 完全相同的 input_schema / output_schema ──

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "train_csv": "训练集CSV路径",
            "val_csv": "验证集CSV路径",
            "test_csv": "测试集CSV路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "model_path": "模型文件路径",
            "train_metrics": "训练集评估指标",
            "test_metrics": "测试集评估指标",
            "metrics_path": "指标JSON路径",
            "feature_importance": "特征重要性列表",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行 XGBoost 训练和测试集预测。"""
        import numpy as np
        import pandas as pd

        # ── 参数提取 ──────────────────────────────────────────────────
        train_csv = params.get("train_csv", "")
        val_csv = params.get("val_csv", "")
        test_csv = params.get("test_csv", "")
        output_dir = params.get("output_dir", "")

        # 参数校验
        for param_name, val in [
            ("train_csv", train_csv),
            ("val_csv", val_csv),
            ("test_csv", test_csv),
            ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {param_name} 不能为空")

        # 超参数提取
        xgb_params = {}
        for key in ["n_estimators", "max_depth", "learning_rate",
                     "subsample", "colsample_bytree"]:
            if key in params:
                xgb_params[key] = params[key]

        if xgb_params and log_callback:
            log_callback("INFO", f"XGBoost 超参数: {xgb_params}")

        # ── 依赖检查 ──────────────────────────────────────────────────
        try:
            import xgboost as xgb
            from sklearn.metrics import r2_score, mean_squared_error, mean_absolute_error
        except ImportError:
            return SkillResult(
                success=False,
                message="未安装 xgboost，请运行: pip install xgboost",
            )

        # ── 数据加载 ──────────────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "加载训练数据...")

        try:
            df_train = pd.read_csv(train_csv)
            df_val = pd.read_csv(val_csv)
            df_test = pd.read_csv(test_csv)
        except Exception as e:
            return SkillResult(success=False, message=f"数据加载失败: {e}")

        # 确定特征列和目标列
        target_col = "LST"
        exclude_cols = {target_col, "row", "col"}
        feature_cols = [c for c in df_train.columns if c not in exclude_cols]

        X_train = df_train[feature_cols].values
        y_train = df_train[target_col].values
        X_val = df_val[feature_cols].values
        y_val = df_val[target_col].values
        X_test = df_test[feature_cols].values
        y_test = df_test[target_col].values

        if progress_callback:
            progress_callback("xgboost_model", 0.1, "数据加载完成")

        # ── 模型训练 ──────────────────────────────────────────────────
        if log_callback:
            log_callback("INFO", f"开始训练 XGBoost: {len(X_train):,} 训练样本, "
                                 f"{len(X_val):,} 验证样本")

        os.makedirs(output_dir, exist_ok=True)

        default_params = {
            "objective": "reg:squarederror",
            "tree_method": "hist",
            "random_state": 42,
            "n_jobs": -1,
        }
        default_params.update(xgb_params)

        try:
            t_start = time.time()
            model = xgb.XGBRegressor(**default_params)
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                verbose=False,
            )
            train_time = time.time() - t_start
        except Exception as e:
            return SkillResult(success=False, message=f"XGBoost 训练失败: {e}")

        if progress_callback:
            progress_callback("xgboost_model", 0.5, "模型训练完成")

        # ── 保存模型 ──────────────────────────────────────────────────
        import joblib
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        model_path = os.path.join(output_dir, f"xgb_model_{timestamp}.pkl")
        joblib.dump(model, model_path)

        if log_callback:
            log_callback("INFO", f"模型已保存: {model_path}")

        # ── 评估 ─────────────────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始评估...")

        # 训练集评估
        y_train_pred = model.predict(X_train)
        train_metrics = {
            "R2": round(r2_score(y_train, y_train_pred), 6),
            "RMSE": round(float(np.sqrt(mean_squared_error(y_train, y_train_pred))), 6),
            "MAE": round(float(mean_absolute_error(y_train, y_train_pred)), 6),
        }

        # 测试集评估
        y_test_pred = model.predict(X_test)
        test_metrics = {
            "R2": round(r2_score(y_test, y_test_pred), 6),
            "RMSE": round(float(np.sqrt(mean_squared_error(y_test, y_test_pred))), 6),
            "MAE": round(float(mean_absolute_error(y_test, y_test_pred)), 6),
        }

        # 特征重要性
        importances = model.feature_importances_
        feature_importance = sorted(
            zip(feature_cols, importances.tolist()),
            key=lambda x: x[1], reverse=True,
        )

        if progress_callback:
            progress_callback("xgboost_model", 0.9, "评估完成")

        # ── 保存指标 ──────────────────────────────────────────────────
        metrics_data = {
            "model_type": "XGBoost",
            "train_metrics": {"train": train_metrics},
            "test_metrics": test_metrics,
            "feature_importance": feature_importance,
            "features": feature_cols,
            "params": default_params,
            "train_time_seconds": round(train_time, 2),
        }
        metrics_path = os.path.join(output_dir, f"xgb_metrics_{timestamp}.json")
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics_data, f, ensure_ascii=False, indent=2)

        # ── 保存测试集预测结果 ────────────────────────────────────────
        test_results_dir = os.path.join(output_dir, "test")
        os.makedirs(test_results_dir, exist_ok=True)
        df_test_pred = df_test.copy()
        df_test_pred["LST_pred"] = y_test_pred
        test_output_path = os.path.join(test_results_dir, "test_prediction.csv")
        df_test_pred.to_csv(test_output_path, index=False)

        artifacts = [model_path, metrics_path, test_output_path]

        if progress_callback:
            progress_callback("xgboost_model", 1.0, "训练+预测完成")

        result_data = {
            "model_path": model_path,
            "train_metrics": train_metrics,
            "test_metrics": test_metrics,
            "metrics_path": metrics_path,
            "feature_importance": feature_importance,
            "features": feature_cols,
            "params": default_params,
            "train_time_seconds": round(train_time, 2),
        }

        return SkillResult(
            success=True,
            message=(
                f"XGBoost 训练完成: 训练R²={train_metrics['R2']}, "
                f"测试R²={test_metrics['R2']}, "
                f"RMSE={test_metrics['RMSE']}, "
                f"耗时 {train_time:.1f}s"
            ),
            data=result_data,
            artifacts=artifacts,
        )
```

**skills/my_xgboost/__init__.py**：

```python
"""
第三方 Skill: XGBoost 模型训练
"""

from .xgboost_skill import XGBoostSkill

__all__ = ["XGBoostSkill"]
```

---

## 7. 注册与加载

### 7.1 自动加载机制

GeoThermoAI 启动时，`SkillRegistry` 会自动扫描 `skills/` 目录下的所有子目录。加载流程如下：

```
skills/
├── my_xgboost/           # 自动发现
│   ├── __init__.py       # 必须存在 → importlib 加载模块
│   └── xgboost_skill.py  # 扫描模块中的所有类
├── my_preprocess/        # 自动发现
│   ├── __init__.py
│   └── preprocess_skill.py
└── invalid_folder/       # 跳过（无 __init__.py）
```

加载逻辑（来自 `skill_registry.py`）：

```python
def load_third_party_skills(self, skills_dir: str = "skills"):
    """动态加载第三方 Skill 包"""
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return
    for skill_dir in skills_path.iterdir():
        if skill_dir.is_dir() and (skill_dir / "__init__.py").exists():
            try:
                module = importlib.import_module(f"skills.{skill_dir.name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and
                        issubclass(attr, BaseSkill) and attr != BaseSkill):
                        self.register(attr())
            except Exception as e:
                print(f"加载第三方Skill失败 [{skill_dir.name}]: {e}")
```

**关键行为**：

1. 只扫描含 `__init__.py` 的一级子目录
2. 自动查找模块中所有继承自 `BaseSkill` 的类并实例化注册
3. 同一目录下可以包含多个 Skill 类，都会被自动发现
4. 加载失败时打印错误信息但不影响其他 Skill 的加载

### 7.2 手动注册方式

如果需要更精细的控制，也可以在代码中手动注册：

```python
from core.skills.skill_registry import SkillRegistry
from skills.my_xgboost.xgboost_skill import XGBoostSkill

registry = SkillRegistry()

# 手动注册
registry.register(XGBoostSkill())

# 按名称获取
skill = registry.get("xgboost_model")

# 按组获取（可替换 Skill）
model_skills = registry.get_group("model_train")

# 获取优先 Skill（指定名称优先，否则返回组内第一个）
skill = registry.get_by_group("model_train", preferred="xgboost_model")
```

---

## 8. 最佳实践

### 8.1 错误处理

- **参数校验**：在 `execute()` 入口处校验所有必需参数，返回 `SkillResult(success=False, message="...")` 而非抛出异常。
- **依赖检查**：在使用第三方库前通过 `try/except ImportError` 检查是否已安装，给出友好的安装提示。
- **异常捕获**：核心计算逻辑用 `try/except` 包裹，将异常信息包含在 `SkillResult.message` 中。

```python
def execute(self, params, progress_callback=None, log_callback=None):
    # 参数校验
    if not params.get("train_csv"):
        return SkillResult(success=False, message="参数 train_csv 不能为空")

    # 依赖检查
    try:
        import xgboost as xgb
    except ImportError:
        return SkillResult(success=False, message="未安装 xgboost，请运行: pip install xgboost")

    # 核心逻辑
    try:
        result = do_training(...)
    except Exception as e:
        return SkillResult(success=False, message=f"训练失败: {e}")
```

### 8.2 进度回调

进度回调的签名为 `callback(step_name: str, percent: float, message: str)`：

- `step_name`：当前步骤名称，通常使用 Skill 的 `name`
- `percent`：进度百分比，范围 `0.0` ~ `1.0`
- `message`：人类可读的进度描述

**建议**：

- 在关键节点调用进度回调（数据加载、训练开始、评估完成等）
- 多步骤 Skill 按各步骤的耗时比例分配进度区间
- 调用前检查 `progress_callback` 是否为 `None`

```python
if progress_callback:
    progress_callback("xgboost_model", 0.3, "数据加载完成")

# 多步骤进度分配示例
progress_callback("xgboost_model", 0.0 + pct * 0.5, f"[训练] {msg}")   # 前50%
progress_callback("xgboost_model", 0.5 + pct * 0.5, f"[预测] {msg}")   # 后50%
```

### 8.3 日志输出

日志回调的签名为 `callback(level: str, message: str)`：

- `level`：日志级别，使用 `"INFO"` / `"WARNING"` / `"ERROR"`
- `message`：日志内容

**建议**：

- 在步骤开始和结束时输出 INFO 日志
- 遇到可恢复的异常时输出 WARNING 日志
- 调用前检查 `log_callback` 是否为 `None`

```python
if log_callback:
    log_callback("INFO", "开始训练 XGBoost 模型...")
    log_callback("INFO", f"训练完成: R²={r2:.4f}, 耗时 {elapsed:.1f}s")
```

---

## 附录：SkillParameter 字段参考

```python
@dataclass
class SkillParameter:
    name: str               # 参数名，如 "n_estimators"
    type: str               # 类型: "string", "number", "boolean", "file_path"
    description: str        # 自然语言描述
    required: bool = True   # 是否必填
    default: Any = None     # 默认值
    choices: List = []      # 可选值列表（如 ["gpt-4o", "gpt-4o-mini"]）
```

## 附录：Hyperparameter 字段参考

```python
@dataclass
class Hyperparameter:
    name: str               # 参数名
    label: str              # UI 显示标签
    type: str               # "number" | "select" | "boolean"
    default: Any            # 默认值
    min: Any = None         # 数值型最小值
    max: Any = None         # 数值型最大值
    step: Any = None        # 数值型步长
    options: List = []      # type="select" 时的选项列表
    description: str = ""   # 参数说明（tooltip）
```

## 附录：内置 Skill 清单

| Skill 名称 | 分组 | 功能 |
|------------|------|------|
| `data_acquisition` | `data_process` | 从 GEE 下载遥感数据 |
| `data_pipeline` | `data_process` | 数据预处理 + 数据集划分 |
| `ttri_compute` | `ttri_compute` | 地形热响应指数计算 |
| `rf_model` | `model_train` | 随机森林训练与预测 |
| `tcr_compute` | `tcr_compute` | 热约束残差修正 |
| `lst_export` | `lst_export` | LST 最终计算 + GeoTIFF 导出 |
| `accuracy_eval` | `accuracy_eval` | 空间一致性精度评估 |
| `ai_assistant` | `ai_assist` | AI 参数推荐/结果诊断/报告生成 |
