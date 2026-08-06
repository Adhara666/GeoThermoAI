# GeoThermoAI 实验记录 Schema 设计稿（`experiments.json`）

> **用途**：定义一次实验在 `experiments.json` 中的完整结构——精确查询层的数据规范。
> **口径**：字段名与当前代码输出一一对应（`_execute_plan` 收尾聚合时零转换）；一次实验一条记录；按项目一份文件。
> **关联**：《GeoThermoAI_记忆系统数据清单.md》2.1/2.2；本文件同时是 ChromaDB 段落组装的字段来源。

---

## 一、记录结构总览（成功实验示例）

```jsonc
{
  "schema_version": 1,
  "experiment_id": "exp_9f2c3a4b_001",          // project_id 前 8 位 + 序号
  "conv_id": "ab12cd34",
  "project_id": "9f2c3a4b...",
  "region": "武汉市_市.geojson",
  "date_range": ["2024-07-01", "2024-07-31"],
  "pair": {
    "landsat_date": "2024-07-21", "landsat_satellite": "L9",
    "landsat_cloud_cover": 12.5, "landsat_coverage": 95.2,
    "sentinel2_date": "2024-07-22", "sentinel2_cloud_cover": 8.3,
    "sentinel2_coverage": 97.8, "time_diff_days": 1
  },
  "status": "success",                            // success / failed
  "timestamp": "2026-08-05 10:00:00",

  "data_features": {
    "train_samples": 45678, "val_samples": 15226, "test_samples": 15226,
    "dem_std": 120.0, "dem_range": 620.0,
    "ndvi_mean": 0.35, "ndvi_std": 0.18,
    "lst_range": 17.0, "lst_std": 4.2,
    "feature_stats": {
      "NDVI": {"mean": 0.35, "std": 0.18, "min": -0.2, "max": 0.9},
      "DEM":  {"mean": 45.0, "std": 120.0, "min": -20.0, "max": 600.0},
      "LST":  {"mean": 312.0, "std": 4.2, "min": 298.5, "max": 315.5}
    }
  },

  "ttri": {
    "coefficients": [0.012, -0.5, 0.3],          // [a(DEM), b(Slope), c(cosAspect)]
    "intercept": 310.0,
    "r2": 0.55,
    "predict_valid_10m": 70000000,
    "out_of_grid": 1200
  },

  "model": "rf",
  "params": {
    "n_estimators": 300, "max_depth": 35,
    "min_samples_split": 16, "min_samples_leaf": 8,
    "max_features": 0.5, "random_state": 42
  },
  "metrics": {
    "train": {"R2": 0.90, "RMSE": 1.50, "MAE": 1.10, "MB": 0.00},
    "val":   {"R2": 0.82, "RMSE": 2.10, "MAE": 1.55, "MB": -0.02},
    "test":  {"R2": 0.87, "RMSE": 1.23, "MAE": 0.91, "MB": 0.12}
  },
  "feature_importance": [
    {"feature": "NDVI", "importance": 0.28},
    {"feature": "DEM", "importance": 0.23},
    {"feature": "NIR", "importance": 0.10},
    {"feature": "B", "importance": 0.09},
    {"feature": "G", "importance": 0.08},
    {"feature": "R", "importance": 0.07},
    {"feature": "SWIR1", "importance": 0.06},
    {"feature": "NDWI", "importance": 0.05},
    {"feature": "NDBI", "importance": 0.04}
  ],

  "independent_prediction": {
    "n_samples": 388869,
    "R2": 0.82, "RMSE_K": 1.41, "MAE_K": 1.05, "MB_K": 0.08,
    "split_method": "spatial_block_guard",
    "guard_buffer_m": 100.0, "block_size_px": 10
  },

  "tcr": {
    "mode": "block_constant",
    "mean_K": -0.05, "std_K": 0.30, "n_valid_blocks": 150000
  },

  "lst_export": {
    "image_size": {"height": 15530, "width": 14047},
    "stats": {"min": 298.5, "max": 348.5, "valid_percent": 45.6},
    "file_size_mb": 820.0
  },

  "closure": {
    "n_matched_cells": 373240,
    "metrics": {"MB_K": 0.05, "MAE_K": 0.40, "RMSE_K": 0.50, "R2": 0.995},
    "value_range": {"low_end_difference_K": -0.45, "high_end_difference_K": -0.58}
  },

  "train_time_seconds": 183.2
}
```

---

## 二、字段明细表

### 2.1 元信息（必填）

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `schema_version` | int | 固定 1 | 常量 |
| `experiment_id` | string | `exp_{project_id[:8]}_{序号}` | 运行时生成 |
| `conv_id` | string | 来源对话（级联删除依据） | 运行时状态 |
| `project_id` | string | 所属项目 | `_projects.json` |
| `region` | string | 研究区文件/标识 | 计划参数 |
| `date_range` | [string, string] | 起止日期 | 计划参数 |
| `pair` | object | 所选影像配对 | data_acquisition 结果 |
| `status` | string | `success` / `failed` / `paused`（见第三节判定） | 流程收尾/暂停 |
| `timestamp` | string | 完成时间 | 运行时 |

### 2.2 数据质量（data_pipeline 成功即有）

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `data_features.train_samples / val_samples / test_samples` | int | 各集样本数 | `_collect_data_features()` |
| `data_features.dem_std / dem_range` | float | 地形复杂度 | 同上 |
| `data_features.ndvi_mean / ndvi_std` | float | 植被覆盖 | 同上 |
| `data_features.lst_range / lst_std` | float | 温度变异 | 同上 |
| `data_features.feature_stats` | object | 各特征 mean/std/min/max | 同上 |

### 2.3 模型与精度（rf_model 成功即有）

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `model` | string | 模型名（如 `rf`） | 计划参数 |
| `params` | object | 生效超参数 | rf_model 结果 |
| `metrics.train / val / test` | object | `{R2, RMSE, MAE, MB}` | `core/rf_model.py` |
| `feature_importance` | array | `[{feature, importance}]`，各特征对 LST 的贡献度（与特征列一一对应） | rf_model 结果 |
| `independent_prediction` | object | `n_samples` + `{R2, RMSE_K, MAE_K, MB_K}` + 划分信息 | `evaluate_independent_prediction` |
| `train_time_seconds` | float | 训练耗时 | rf_model 结果 |

> 注：`feature_importance` 为**浮点列表**（[{feature, importance}]），是重要的实验数据——既随结构化字段存入 `experiments.json`（上表，供跨实验归因对比），也参与 RAG 段落组装（"特征重要性：NDVI(0.28) > DEM(0.23)"）供语义检索命中。

### 2.4 残差与影像（后续阶段成功即有）

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `ttri` | object | coefficients / intercept / r2 / predict_valid_10m / out_of_grid | `core/ttri.py` |
| `tcr` | object | mode / mean_K / std_K / n_valid_blocks | `core/tcr.py` |
| `lst_export` | object | image_size / stats / file_size_mb | `export_geotiff` |
| `closure` | object | n_matched_cells / metrics / value_range | `evaluate_coarse_constraint_closure` |

---

## 三、失败实验的记录约定

### 3.1 失败从哪来：Skill 层保证"必有结论"

已核实 8 个内置 Skill（data_acquisition / data_pipeline / ttri_compute / rf_model / tcr_compute / lst_export / accuracy_eval / ai_assistant）的 `execute()` **在所有路径**（成功、参数校验失败、依赖缺失、内部异常）都返回 `SkillResult(success: bool, message: str)`，不会裸抛异常或漏返回。因此每个 Skill 必然报告"本次执行是否完成或失败"，Agent 层无需补兜底，直接读 `result.success` 即可。

### 3.2 聚合判定：一次实验的整体成功/失败

**判定位置**：`_execute_plan` 收尾处（升级方案 3.2 的 `auto_save_experiment` 钩子）。现状代码不聚合各步骤结果，需新增：

```
任一必需步骤 result.success == False 或抛异常   →  status = "failed"
    failure_stage   = 该步骤的 skill 名
    failure_message = result.message 或异常文本
全部必需步骤 success == True 且未暂停           →  status = "success"
```

**边界约定**：

- **提前终止**：data_acquisition 失败/无配对会提前 `return`（后续步骤未执行），必须在 return 前补记一条 `failed`（`failure_stage="data_acquisition"`），否则半途实验丢失；
- **普通 Skill 失败不中断**：现状 `_execute_plan` 对非 data_acquisition 的 `success=False` 只记录不中断、继续跑后续步骤——聚合时按各步骤的 `result.success` 逐段判定"已完成/未执行"，与整体 `status` 解耦；
- **暂停记为 `paused`**：`PAUSE_MARKER`（等待用户选择配对/输入）表示实验**未完成**，此时写 `status="paused"` 记录已执行阶段，恢复跑完后**覆盖**为 success / failed，避免把交互暂停误记为失败、也不丢已执行信息；
- **失败记录**：`status = "failed"` 并追加：

```jsonc
{ "failure_stage": "rf_model", "failure_message": "模型训练失败: ..." }
```

- **已完成阶段**的字段照常记录（如 data_pipeline 成功 → 保留 `data_features`）；**未执行阶段**的字段**省略**（不写 null 占位）；
- 至少保留：`conv_id / project_id / region / date_range / status / timestamp / failure_stage / failure_message`——支撑"上次为什么失败"类检索。

---

## 四、多次实验 / 多配对约定

- **同一项目多次实验**：`experiments.json` 为数组，每次实验 `append` 一条，互不覆盖；
- **同一次全流程多配对**：每对生成一条记录（`pair` 字段区分），`experiment_id` 序号递增；
- **查询**：`get_best(region, model)` 取该区域/模型 R² 最高；`get_recent(n)` 取最近 n 条；`delete_by_conv(conv_id)` 删除某对话全部记录。

---

## 五、与 ChromaDB 的对应（双写口径）

| 层 | 内容 | metadata |
|---|---|---|
| `experiments.json` | 上述结构化字段 | —（JSON 文件本身） |
| ChromaDB `project_{id}` | 由结构化字段组装的**自然语言段落**（含 region/日期/指标/特征重要性/配对），见数据清单 2.3 示例 | `{conv_id, region, model, r2, date}` |

> 组装规则：一次实验一条段落；`r2` 取 `metrics.test.R2`；`date` 取 `date_range[0]`。删除对话时两处按 `conv_id` 同步删除。
