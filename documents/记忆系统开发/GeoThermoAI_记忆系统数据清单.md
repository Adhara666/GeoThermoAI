# GeoThermoAI 记忆系统数据清单（领域知识 + 实验指标）

> **用途**：作为记忆系统（`core/memory`）的**数据规范**——「要播种哪些领域知识」和「每次实验要记录哪些指标」。
> **字段名**与当前代码输出一一对应，入库时可直接取用，无需新计算。
> **关联文档**：《GeoThermoAI_记忆系统升级方案.md》（同目录）。

---

## 一、领域知识清单（→ `global_knowledge` 播种）

> 播种来源：① 现有 `_build_system_prompt()` 里硬编码的领域知识（原样迁出）；② v2 规划文档预置的领域知识；③ Agent 调参 prompt 里的规则。共分三类。

### 1.1 公式与原理类（确定性知识）

| 编号 | 条目 | 内容要点 | 来源 |
|---|---|---|---|
| K01 | TTRI 公式 | TTRI = a·DEM + b·Slope + c·cos(Aspect)，刻画地形对地表温度的控制作用 | `_build_system_prompt` 硬编码 |
| K02 | TCR 机制 | 热约束残差修正：TCR = LST_true_30m − LST_pred_30m_block，将 30m 系统偏差空间化到 10m | 同上 |
| K03 | 降尺度原理 | Landsat 30m LST + Sentinel-2 10m 多光谱特征 → 10m LST | 同上 |
| K04 | MB 指标定义 | MB = mean(预测 − 参考)，单位 K，正值表示预测整体偏暖 | `evaluation.py` 输出说明 |

### 1.2 数据源与卫星参数类

| 编号 | 条目 | 内容要点 | 来源 |
|---|---|---|---|
| K10 | Landsat 参数 | Landsat 8/9 Collection 2 Level-2：地表温度 ST + QA_PIXEL；重访周期 16 天 | `_build_system_prompt` 硬编码 |
| K11 | Sentinel-2 参数 | S2 Level-2A 多光谱 + SCL；重访周期 5 天；光谱波段需按景应用 BOA_ADD_OFFSET（quantification 10000）定标 | 硬编码 + `sentinel2_calibration.py` |
| K12 | DEM 数据源 | Copernicus GLO-30 | `data_acquisition.py` 参数 |
| K13 | 影像配对规则 | Landsat 与 Sentinel-2 时间差 ≤ 2 天；云量阈值默认 30（配置可调，样本不足可放宽） | `_build_system_prompt` + settings |

### 1.3 调参与经验类（启发式知识）

| 编号 | 条目 | 内容要点 | 来源 |
|---|---|---|---|
| K20 | 样本量 → n_estimators | 样本 > 5 万 → 200–500；样本 < 1 万 → 100–150（防过拟合） | `_build_tuning_prompt()` |
| K21 | 地形 → max_depth | DEM 标准差 > 100m → max_depth 30–40；< 30m → 15–20 | 同上 |
| K22 | 温度变异 → min_samples_leaf | LST 标准差 > 5K → min_samples_leaf 减到 5 | 同上 |
| K23 | 植被覆盖 → max_features | NDVI 均值 > 0.5 → max_features 可增至 0.7 | 同上 |
| K24 | 指标解读基准 | LST 降尺度 R² 通常 0.75–0.85：优秀 / 良好 / 合格 / 偏低 | v2 规划（`_analyze_result` 提示词） |

> **说明**：1.1、1.2 为确定性种子，**只读**；1.3 为启发式经验，既作初始种子，也可在实验后由偏好/实验记录更新（弱规则，仅供 LLM 参考，不参与硬逻辑）。

---

## 二、实验指标清单（→ `experiments.json` + ChromaDB `project_{id}`）

### 2.1 每次实验的统一元信息

| 字段 | 说明 | 来源 |
|---|---|---|
| `conv_id` | 来源对话（删除级联依据） | 运行时状态 |
| `project_id` | 所属项目（隔离依据） | `_projects.json` 补 id 后 |
| `region` | 研究区（研究区文件名 / bbox） | data_acquisition 参数 |
| `date_range` | 起止日期 | 计划参数 |
| `pair` | 所选影像配对（日期/云量/覆盖率） | data_acquisition 结果 |
| `status` | 成功 / 失败 | 流程收尾状态 |
| `timestamp` | 完成时间 | 运行时 |

### 2.2 各阶段指标（按数据来源）

| 阶段 | 指标字段 | 来源代码 |
|---|---|---|
| **data_acquisition**（配对） | `landsat_date / satellite / cloud_cover / coverage`、`sentinel2_date / cloud_cover / coverage`、`time_diff_days` | `_ask_user_to_select_pair()` 读取的 pair 字段 |
| **data_pipeline**（数据特征） | `train_rows`（有效样本）、`constraint_rows`、`predict_valid_pixels`；`dem_std / dem_range / ndvi_mean / ndvi_std / lst_range / lst_std`、各特征 mean/std/min/max、`train/val/test_samples` | `_collect_data_features()` |
| **ttri_compute** | `coefficients`（[a, b, c]）、`intercept`、拟合 `r2`、10m 有效行 `total_valid / out_of_grid` | `core/ttri.py` |
| **rf_model** | 训练/验证指标 `{R2, RMSE, MAE, MB}`、测试指标 `{R2, RMSE, MAE, MB}`、`feature_importance`（[{feature, importance}]）、生效 `params`、`train_time_seconds` | `core/rf_model.py` |
| **rf_model 附加**（独立预测协议） | `n_samples`、`{R2, RMSE_K, MAE_K, MB_K}`、`split_method / guard_buffer_m / block_size_px` | `core/evaluation.py::evaluate_independent_prediction` |
| **tcr_compute** | `tcr_statistics {mean, std, n_valid_blocks}`、`validity`、`mode` | `core/tcr.py` |
| **lst_export** | 影像 `image_size`、`stats {min, max, valid_percent}`、`file_size_mb` | `core/export_geotiff.py` |
| **accuracy_eval**（粗尺度闭合协议） | `closure.metrics {MB_K, MAE_K, RMSE_K, R2}`、`n_matched_cells`、`value_range {low_end_difference_K, high_end_difference_K}` | `core/evaluation.py::evaluate_coarse_constraint_closure` |

### 2.3 两份存储的分工

| 存储 | 放什么 | 形态 |
|---|---|---|
| `experiments.json` | 2.1 + 2.2 的**结构化字段** | 每条实验一条 JSON 记录，支持 `get_best(region, model)` / `get_recent(n)` / `delete_by_conv(conv_id)` |
| ChromaDB `project_{id}` | 2.1 + 2.2 汇总成**一段自然语言** | 语义检索用，metadata 带 `{conv_id, region, model, r2, date}` |

**RAG 段落示例**（由结构化字段自动组装）：

```
武汉 2024-07 RF 实验：R²=0.87，RMSE=1.23K，MAE=0.91K，MB=+0.12K，
n_estimators=300, max_depth=35，训练样本 45,678，地形复杂(DEMσ=120m)，
植被中等(NDVI=0.35)，温度范围 298–315K。
特征重要性：NDVI(0.28) > DEM(0.23) > NIR(0.10)。
独立预测 R²=0.82；粗尺度闭合 MB=+0.05K，MAE=0.40K。
配对：Landsat 2024-07-17 (L9, 云12.5%) + Sentinel 2024-07-18 (云8.3%)。
```

---

## 三、取舍建议：哪些"中间指标"入记忆、哪些不入

| 类型 | 是否入记忆 | 原因 |
|---|---|---|
| 实验级指标（2.2 各阶段关键指标） | ✅ 入 | 跨会话可查、可对比、可调参参考 |
| 数据特征（dem_std / ndvi_mean / lst_std / 样本数） | ✅ 入 | 是"效果为什么好/差"的核心解释变量 |
| 特征重要性 | ✅ 入 | 物理意义解读与归因分析的依据 |
| 配对信息（日期/云量/覆盖率） | ✅ 入 | 判断数据质量、时间差影响 |
| 过程日志、下载进度、临时文件统计 | ❌ 不入 | 噪声大、无跨会话价值 |
| 对话原文 | ❌ 不入 | 成本高、隐私风险（见升级方案 3.5） |
| 阶段级中间 CSV 统计（如逐块统计） | ❌ 不入 | 粒度太细，实验级聚合即可 |

---

## 四、扩展指引

- **领域知识**是开放集：新增数据源、区域经验、调参经验随时追加为种子条目（编号续排），播种逻辑幂等（`count>0` 跳过）。
- **实验指标**是封闭集：以 2.2 表为准，未来新增模型（XGBoost 等）只需复用同结构指标字段，无需改记忆层。
- 字段命名与代码保持一致的直接好处：`_execute_plan` 收尾聚合时**零转换**即可入库。
