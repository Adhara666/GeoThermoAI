# GeoThermoAI 知识种子设计稿（`knowledge_seed.json`）

> **用途**：`global_knowledge` 首次播种的数据源，定义"领域常识"有哪些、每条存什么。
> **组装来源**：《GeoThermoAI_公式与原理知识参考.md》+《GeoThermoAI_数据源与卫星参数参考.md》+《GeoThermoAI_记忆系统数据清单.md》K 编号。
> **口径**：不含区域经验；以当前代码实现为准。

---

## 一、文件结构

```jsonc
{
  "schema_version": 1,
  "seeded_at": "2026-08-05T00:00:00",
  "items": [
    { "id": "K01", "topic": "TTRI 公式", "tags": ["ttri", "公式", "地形"],
      "content": "..." },
    // ... 共 K01–K24（可选补充 K30/K31）
  ]
}
```

- 每个 item = 一条可独立检索的知识（`content` 写入 ChromaDB `global_knowledge`，`tags` 进 metadata 供过滤）；
- 播种幂等：Collection `count>0` 跳过。

---

## 二、种子条目总表（K01–K24，无区域经验）

### 公式与原理类（K01–K04）

| id | topic | tags | content 要点 |
|---|---|---|---|
| K01 | TTRI 公式 | `ttri, 公式, 地形` | LST 拟合含截距；TTRI = a·DEM + b·Slope + c·cos(Aspect)（不含截距）；仅 train 拟合一次，系数复用；10m 用完整 30m 约束层 + 统一仿射映射双线性插值 |
| K02 | TCR 机制 | `tcr, 残差, 热约束` | 块聚合消噪 → 30m 真值减块均值得系统性偏差 → 规则网格双线性插值到 10m；LST_final = LST_pred + TCR |
| K03 | 降尺度原理 | `降尺度, 原理, 双阶段` | RF 9 维特征空间高频细节 + TCR 地理空间低频偏差；30m LST + S2 10m 特征 → 10m LST |
| K04 | MB 指标定义 | `指标, MB` | MB = mean(预测 − 参考)，单位 K，正值=预测偏暖 |

### 数据源与卫星参数类（K10–K13）

| id | topic | tags | content 要点 |
|---|---|---|---|
| K10 | Landsat 参数 | `landsat, 卫星, LST` | C2 L2：lwir11（ST_B10）+ QA_PIXEL；重访 16 天；LST(K)=DN×0.00341802+149.0 |
| K11 | Sentinel-2 参数 | `sentinel2, 定标, 波段` | L2A：B02/B03/B04/B08/B11 + SCL；重访 5 天；按景定标 reflectance=(DN+BOA_ADD_OFFSET)/QUANTIFICATION，DN=0 为 NoData |
| K12 | DEM 数据源 | `dem, 地形, 数据源` | Copernicus GLO-30，30m；用于计算 Slope/Aspect/cos(Aspect) |
| K13 | 影像配对规则 | `配对, 云量, 规则` | Landsat 与 S2 时间差 ≤ 2 天；云量阈值默认 30，样本不足可放宽 |

### 调参与解读类（K20–K24）

| id | topic | tags | content 要点 |
|---|---|---|---|
| K20 | 样本量 → n_estimators | `调参, n_estimators` | 样本 >5 万 → 200–500；<1 万 → 100–150（防过拟合） |
| K21 | 地形 → max_depth | `调参, max_depth, 地形` | DEM 标准差 >100m → 30–40；<30m → 15–20 |
| K22 | 温度变异 → min_samples_leaf | `调参, min_samples_leaf` | LST 标准差 >5K → 减到 5 |
| K23 | 植被覆盖 → max_features | `调参, max_features` | NDVI 均值 >0.5 → 可增至 0.7 |
| K24 | 指标解读基准 | `指标, 解读, R2` | 测试集 R²：≥0.85 优秀 / 0.80–0.85 良好 / 0.75–0.80 合格 / <0.75 偏低 |

> 注：原"区域经验（K24）"已按要求移除。

### 可选补充（不在数据清单编号内，建议一并播种）

| id | topic | tags | content 要点 |
|---|---|---|---|
| K30 | 光谱指数公式 | `指数, 公式, NDVI` | NDVI=(NIR−R)/(NIR+R+ε)；NDWI=(G−NIR)/(G+NIR+ε)；NDBI=(SWIR1−NIR)/(SWIR1+NIR+ε) |
| K31 | 地形特征计算 | `地形, Slope, Aspect` | 由 DEM 用 numpy.gradient 求 Slope、Aspect，取 cos(Aspect) 作 TTRI 输入 |

---

## 三、完整 `items` 内容（可直接复制）

```jsonc
{ "items": [
  { "id": "K01", "topic": "TTRI 公式",
    "tags": ["ttri", "公式", "地形"],
    "content": "TTRI（地形热响应指数）刻画地形对地表温度的控制作用。LST 拟合模型为 LST = intercept + a·DEM + b·Slope + c·cos(Aspect)（含截距，仅用训练集拟合一次）；TTRI 标量取地形线性贡献部分，不含截距：TTRI = a·DEM + b·Slope + c·cos(Aspect)。validate/test/完整30m约束层/10m预测格网复用同一组系数做无标签变换；10m 空间化基于完整30m约束层 + 统一仿射映射双线性插值。" },
  { "id": "K02", "topic": "TCR 机制",
    "tags": ["tcr", "残差", "热约束"],
    "content": "TCR（热约束残差）修正跨尺度系统性偏差，分三步：①在30m块内对10m预测取均值，随机误差相互抵消；②30m真实LST减去块均值得到纯净系统性偏差 TCR = LST_true_30m − LST_pred_30m_block；③用规则网格双线性插值连续降尺度到10m。最终 LST_final_10m = LST_pred_10m + TCR_10m。细→粗映射使用仿射逆变换，30m参考为完整30m约束层。" },
  { "id": "K03", "topic": "降尺度原理",
    "tags": ["降尺度", "原理", "双阶段"],
    "content": "LST 降尺度为回归-热约束双阶段框架：随机森林在9维特征空间（R,G,B,NIR,SWIR1,NDVI,NDWI,NDBI,TTRI）建立光谱-地形到LST的非线性映射，负责10m高频细节建模；TCR在2维地理空间补偿跨尺度系统性偏差。整体为 Landsat 30m LST + Sentinel-2 10m 多光谱特征 → 10m LST。" },
  { "id": "K04", "topic": "MB 指标定义",
    "tags": ["指标", "MB"],
    "content": "MB = mean(预测 − 参考)，单位 K，正值表示预测整体偏暖。与 R²/RMSE/MAE 共同构成模型精度指标。" },
  { "id": "K10", "topic": "Landsat 参数",
    "tags": ["landsat", "卫星", "LST"],
    "content": "Landsat 8/9 Collection 2 Level-2：地表温度波段 lwir11（ST_B10，缩放 DN）+ QA_PIXEL；重访周期16天；温度定标 LST(K) = DN × 0.00341802 + 149.0；QA 掩膜剔除云/云影/填充像元。" },
  { "id": "K11", "topic": "Sentinel-2 参数",
    "tags": ["sentinel2", "定标", "波段"],
    "content": "Sentinel-2 Level-2A：多光谱 B02/B03/B04/B08/B11 + SCL；重访周期5天；地表反射率按景定标 reflectance = (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE，DN=0 仍为 NoData；Processing Baseline ≥ 04.00 时 offset=-1000、quantification=10000。" },
  { "id": "K12", "topic": "DEM 数据源",
    "tags": ["dem", "地形", "数据源"],
    "content": "DEM 数据源为 Copernicus GLO-30，30m；高程用于计算 Slope、Aspect、cos(Aspect) 等地形特征，作为 TTRI 回归输入。" },
  { "id": "K13", "topic": "影像配对规则",
    "tags": ["配对", "云量", "规则"],
    "content": "Landsat 与 Sentinel-2 影像配对要求成像时间差 ≤ 2 天；搜索时按 eo:cloud_cover < cloud_threshold 过滤，云量阈值默认 30，训练样本不足时可放宽。" },
  { "id": "K20", "topic": "样本量 → n_estimators",
    "tags": ["调参", "n_estimators"],
    "content": "训练样本数 > 5 万时，n_estimators 可增大到 200–500；样本数 < 1 万时，n_estimators 减小到 100–150 以防过拟合。" },
  { "id": "K21", "topic": "地形 → max_depth",
    "tags": ["调参", "max_depth", "地形"],
    "content": "地形复杂（DEM 标准差 > 100m）时 max_depth 增大到 30–40；地形平坦（DEM 标准差 < 30m）时 max_depth 减小到 15–20。" },
  { "id": "K22", "topic": "温度变异 → min_samples_leaf",
    "tags": ["调参", "min_samples_leaf"],
    "content": "温度变异性大（LST 标准差 > 5K）时，min_samples_leaf 建议减小到 5。" },
  { "id": "K23", "topic": "植被覆盖 → max_features",
    "tags": ["调参", "max_features"],
    "content": "植被覆盖度高（NDVI 均值 > 0.5）时，max_features 可以增大到 0.7。" },
  { "id": "K24", "topic": "指标解读基准",
    "tags": ["指标", "解读", "R2"],
    "content": "LST 降尺度测试集 R² 通常处于 0.75–0.85：≥0.85 为优秀，0.80–0.85 为良好，0.75–0.80 为合格，<0.75 偏低需检查数据质量或调参。" },
  { "id": "K30", "topic": "光谱指数公式",
    "tags": ["指数", "公式", "NDVI"],
    "content": "基于 Sentinel-2 定标反射率：NDVI=(NIR−R)/(NIR+R+ε)；NDWI=(G−NIR)/(G+NIR+ε)；NDBI=(SWIR1−NIR)/(SWIR1+NIR+ε)，ε 防除零。波段对应 R=B4、G=B3、B=B2、NIR=B8、SWIR1=B11。" },
  { "id": "K31", "topic": "地形特征计算",
    "tags": ["地形", "Slope", "Aspect"],
    "content": "由 DEM 用 numpy.gradient 计算：Slope = arctan(√(gx²+gy²))（度）；Aspect = (arctan2(−gx,gy)+360) mod 360（度）；cos(Aspect) 作为 TTRI 回归输入之一。" }
] }
```

---

## 四、播种说明

- **幂等**：启动时若 `global_knowledge` count>0 则跳过，不重复写入；
- **检索**：`search_for_agent` 按查询文本对 `content` 做语义检索，`tags` 供 metadata 过滤；
- **更新**：种子只读；如需修订知识，改本设计稿并在下次重建 Collection 时重播（不热更新）；
- **无区域经验**：本清单不含任何区域/城市特定经验（已按要求移除）。
