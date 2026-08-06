# GeoThermoAI 公式与原理知识参考（EasyLST）

> **定位**：作为记忆系统 `global_knowledge` 领域知识的**权威来源**（对应《GeoThermoAI_记忆系统数据清单.md》K01–K04 等条目）。
> **口径**：以当前代码（`GeoThermoAI_新数据下载_docker`）实现为准；与《原版算法参考.docx》有出入处，本文档按当前代码表述。
> **涉及代码**：`core/ttri.py`、`core/tcr.py`、`core/rf_model.py`、`core/data_preprocessing.py`、`core/evaluation.py`。

---

## 一、总体框架：回归-热约束双阶段（降尺度原理）

LST 降尺度将任务按误差性质拆成两个互补阶段：

1. **回归阶段（特征空间）**：随机森林在 **9 维特征空间**中建立光谱-地形到 LST 的非线性映射，负责 10m 尺度的高频细节建模；
2. **热约束阶段（地理空间）**：TCR 利用 30m 真实 LST 对 10m 预测做块级热约束，在 **2 维地理空间**补偿跨尺度系统性偏差。

```
Landsat 30m LST（真值，DN 定标为 K）
      + Sentinel-2 10m 多光谱（R, G, B, NIR, SWIR1）+ DEM
                    │
       数据预处理：定标 / 对齐 / 云掩膜 / 指数(NDVI,NDWI,NDBI) / 地形(Slope,Aspect)
                    │
       30m 采样样本 ──► RF 训练（9 特征 + TTRI → LST）──► 10m 预测 LST_pred
                    │                                        │
                    │                              TCR 热约束修正（30m 真值）
                    │                                        ▼
                    └────────────► 最终 10m LST_final = LST_pred + TCR
```

---

## 二、LST 辐射定标（Landsat ST）

Landsat 8/9 Collection 2 Level-2 地表温度产品（`lwir11`/`ST_B10`）为缩放后 DN，转温度：

$$
\text{LST} = \text{DN} \times 0.00341802 + 149.0 \quad \text{(K)}
$$

> 代码位置：`core/data_preprocessing.py`（`LST_SCALE = 0.00341802`，`LST_OFFSET_K = 149.0`）；转换在预处理构建训练/约束层时完成。

---

## 三、光谱指数（Sentinel-2）

基于 Sentinel-2 L2A 定标后的地表反射率（R/G/B/NIR/SWIR1），加 ε 防除零：

$$
NDVI = \frac{NIR - R}{NIR + R + \varepsilon}
$$

$$
NDWI = \frac{G - NIR}{G + NIR + \varepsilon}
$$

$$
NDBI = \frac{SWIR1 - NIR}{SWIR1 + NIR + \varepsilon}
$$

> 波段对应：R=B4(Red)、G=B3(Green)、B=B2(Blue)、NIR=B8、SWIR1=B11。
> 代码位置：`core/data_preprocessing.py::_compute_indices`。

---

## 四、地形特征（Slope / Aspect / cos(Aspect)）

由 DEM 用 `numpy.gradient` 计算（dx = dy = 30m）：

$$
g_x = \frac{\partial \mathrm{DEM}}{\partial x},\quad g_y = \frac{\partial \mathrm{DEM}}{\partial y}
$$

$$
\mathrm{Slope} = \arctan\left(\sqrt{g_x^2 + g_y^2}\right) \quad \text{(度)}
$$

$$
\mathrm{Aspect} = \left(\arctan2(-g_x,\ g_y) + 360\right) \bmod 360 \quad \text{(度)}
$$

地形特征 `cos(Aspect)` 作为 TTRI 回归输入之一。

> 代码位置：`core/data_preprocessing.py::_terrain_features`。

---

## 五、TTRI（地形热响应指数）

### 5.1 动机

LST 降尺度需要的是"地形对 LST 的热效应在 10m 尺度的**连续空间表达**"，而非"精确的 10m 地形变量"。30m DEM 直接插值到 10m 后再算坡度/坡向，其物理意义会崩塌；TTRI 先把地形变量对 LST 的**线性贡献压缩为与 LST 同量纲的单通道标量**，再对该标量场做双线性插值。

### 5.2 LST 的拟合（含截距）

仅用 **train 分割**，以 30m 定标 LST（K）为目标，对地形特征做**多元线性回归（最小二乘，含截距项）**：

$$
\mathrm{LST} = \mathrm{intercept} + a \cdot \mathrm{DEM} + b \cdot \mathrm{Slope} + c \cdot \cos(\mathrm{Aspect})
$$

- **设计矩阵含常数列**：A = [1, DEM, Slope, cos(Aspect)]，用 `np.linalg.lstsq` 求解系数；
- **截距 intercept 承担 LST 的整体基线水平**（地形均质区域的背景温度），a / b / c 刻画地形对 LST 的相对贡献；
- 拟合时用**含截距模型**计算预测值、R² 及秩/条件数诊断（秩亏或 condition number > 1e8 时拒绝生成系数）；
- 系数（intercept + a / b / c）固定保存到 `ttri_coefficients.json`。

### 5.3 TTRI 标量（不含截距）

$$
\mathrm{TTRI} = a \cdot \mathrm{DEM} + b \cdot \mathrm{Slope} + c \cdot \cos(\mathrm{Aspect})
$$

- TTRI 只取地形特征的**线性贡献部分，不含截距**——尽管拟合 LST 时带截距；
- **原因**：截距是全局常数，不携带任何空间信息，在 10m 网格上也是同一常量，插值无意义；TTRI 表达"地形驱动的相对热效应"，与 LST 同量纲但**基准无关**；
- 因此 validate / test / 完整 30m 约束层 / 10m 预测格网的 TTRI 全部由 a / b / c 计算，**不含截距**；截距仅作为拟合诊断信息记录，不参与空间化。

> 代码位置：`core/ttri.py::_fit_regression_diagnostics`（拟合含截距）、`compute_ttri_for_constraint_grid` / `compute_ttri_predict`（空间化只用 a/b/c，不含截距）。

### 5.4 关键约定

- 系数固定保存 `ttri_coefficients.json`；validate / test / 完整 30m 约束层 / 10m 预测格网**复用同一组系数**做无标签变换；
- 拟合含**秩与条件数诊断**：秩亏（rank < 参数数）或病态（condition number > 1e8）时拒绝生成系数；
- 10m 空间化：基于完整 30m 约束层 + **统一仿射映射双线性插值**，而非稀疏网格假设。

---

## 六、随机森林降尺度模型

### 6.1 特征与目标

$$
\text{特征(9维)} = \{R,\ G,\ B,\ NIR,\ SWIR1,\ NDVI,\ NDWI,\ NDBI,\ TTRI\},\quad \text{目标} = LST
$$

> 代码位置：`core/rf_model.py::FEATURE_COLS`、`TARGET_COL = "LST"`。

### 6.2 精度指标

$$
R^2 = 1 - \frac{\sum (y - \hat{y})^2}{\sum (y - \bar{y})^2}
$$

$$
\mathrm{RMSE} = \sqrt{\frac{1}{N}\sum (y - \hat{y})^2},\qquad \mathrm{MAE} = \frac{1}{N}\sum |y - \hat{y}|,\qquad \mathrm{MB} = \frac{1}{N}\sum (\hat{y} - y)
$$

> MB 正值表示预测整体偏暖；训练/验证/测试各自独立计算。

### 6.3 数据集划分与默认超参数

- 按**空间块 + guard buffer** 划分（避免空间自相关），默认 60% / 20% / 20%；
- 默认超参数（可被用户设置 / LLM 推荐覆盖）：

| 参数 | 默认值 |
|---|---|
| n_estimators | 200 |
| max_depth | 25 |
| min_samples_split | 16 |
| min_samples_leaf | 8 |
| max_features | 0.5 |
| random_state | 42 |

---

## 七、TCR（热约束残差）

### 7.1 动机

逐像素回归的预测误差含**随机误差**（Bagging 可抑制）与**系统性偏差**（跨尺度预测不可避免）。TCR 用 30m 真实 LST 这一物理真值对 10m 预测施加块级热约束。

### 7.2 三步机制

1. **块级聚合消噪**：对落入每个 30m 块内的 10m 预测取均值，随机误差相互抵消；
2. **求系统性偏差**：30m 真值减去块均值，得到纯净的系统性偏差场；
3. **规则网格双线性插值**：偏差场位于 30m 规则网格节点上，双线性权重天然连续，平滑降尺度到 10m。

### 7.3 公式

$$
\mathrm{LST\_pred}_{30m}^{(j)} = \frac{1}{N_j} \sum_{k \in \mathrm{Block}_j} \mathrm{LST\_pred}_{10m}(k)
$$

$$
\mathrm{TCR}_{30m}^{(j)} = \mathrm{LST\_true}_{30m}^{(j)} - \mathrm{LST\_pred}_{30m}^{(j)}
$$

$$
\mathrm{LST\_final}_{10m} = \mathrm{LST\_pred}_{10m} + \mathrm{TCR}_{10m}^{(\text{双线性插值})}
$$

> 代码位置：`core/tcr.py::compute_tcr`；细→粗映射使用 `core/grid_mapping` 的**仿射逆变换**；30m 参考为**完整 30m 约束层**（非抽样 CSV）。模式：`block_constant`（默认）/ `smooth_recentered`（实验性）。

---

## 八、空间一致性评估（10m 聚合闭合）

将 10m 结果按统一仿射映射聚合回 30m 格，与 30m 真值对比，验证降尺度是否保持空间分布规律：

$$
\mathrm{LST10m\_agg}^{(j)} = \frac{1}{N_j} \sum_{k \in \mathrm{Block}_j} \mathrm{LST\_final}_{10m}(k)
$$

$$
\mathrm{MB} = \frac{1}{N}\sum\left(\mathrm{LST10m\_agg} - \mathrm{LST30m}\right),\quad
\mathrm{MAE} = \frac{1}{N}\sum\left|\mathrm{LST10m\_agg} - \mathrm{LST30m}\right|
$$

$$
\mathrm{RMSE} = \sqrt{\frac{1}{N}\sum\left(\mathrm{LST10m\_agg} - \mathrm{LST30m}\right)^2}
$$

另报告**值域低/高端差**：`low_end_difference_K`（10m 全量最小值 − 30m 最小值）、`high_end_difference_K`（10m 全量最大值 − 30m 最大值），用于检查值域是否被压缩/拉伸。

> 代码位置：`core/evaluation.py::evaluate_coarse_constraint_closure`。

---

## 九、记忆系统应用

本文档条目与《GeoThermoAI_记忆系统数据清单.md》领域知识对应：

| 数据清单编号 | 本文档章节 | 建议入记忆的要点 |
|---|---|---|
| K01（TTRI 公式） | 第五节 | LST 拟合含截距；TTRI = a·DEM + b·Slope + c·cos(Aspect) 为地形线性贡献部分，**不含截距**，仅 train 拟合一次 |
| K02（TCR 机制） | 第七节 | 块聚合消噪 → 真值减块均值 → 规则网格双线性插值 |
| K03（降尺度原理） | 第一节 | RF 特征空间高频细节 + TCR 地理空间低频偏差 双阶段 |
| K04（MB 定义） | 6.2 | MB = mean(预测 − 参考)，正值=偏暖 |
| K13（配对规则） | — | Landsat/S2 时间差 ≤ 2 天、云量阈值规则（见数据清单） |

> 建议以本文档第五、七、一节内容为基础，组装为 `global_knowledge` 的 RAG 种子段落（每段一个主题，便于语义检索命中）。
