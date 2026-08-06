# GeoThermoAI 数据源与卫星参数知识参考

> **定位**：作为记忆系统 `global_knowledge` 领域知识的权威来源（对应《GeoThermoAI_记忆系统数据清单.md》K10–K13 条目）。
> **口径**：以当前代码（`GeoThermoAI_新数据下载_docker`）实现为准；与《原版算法参考.docx》有出入处，按当前代码表述。
> **涉及代码**：`core/skills/builtin/data_acquisition.py`、`core/skills/builtin/sentinel2_calibration.py`、`core/data_preprocessing.py`、`config/settings.json`。

---

## 一、数据源总览

| 数据 | 产品 | 关键波段 | 输出文件（项目 raw/） | 分辨率 |
|---|---|---|---|---|
| 热红外遥感 | Landsat 8/9 Collection 2 Level-2 | `lwir11`（地表温度 ST）+ `qa_pixel` | `landsat_lst.tif`、`landsat_qa_pixel.tif` | 30m |
| 多光谱遥感 | Sentinel-2 Level-2A（大气校正地表反射率） | B02、B03、B04、B08、B11 + SCL | `sentinel2_bands.tif`（5 波段）、`sentinel2_scl.tif` | 10m（SCL 20m） |
| 地形数据 | Copernicus DEM GLO-30 | 高程（DEM） | `dem.tif` | 30m |

---

## 二、获取架构：双数据源与回退

| 平台 | 用途 | 认证方式 | 特点 |
|---|---|---|---|
| **Planetary Computer**（微软） | Landsat / Sentinel-2 / DEM 主数据源 | STAC API + SAS 令牌签名（`planetary-computer` 库） | 无需注册，免费 |
| **Copernicus Data Space**（CDSE，欧空局） | Sentinel-2 / DEM **加速下载**（国内访问更快） | STAC + OAuth Bearer token；DEM 走 S3 SigV4 签名（Bearer 会 403） | 需 `settings.json::data_space` 配置凭据 |

**回退策略**：

```
Sentinel-2 / DEM → 优先 Copernicus Data Space → 失败/未配置 → 回退 Planetary Computer
Landsat         → 仅 Planetary Computer
DEM             → Copernicus DEM GLO-30（`settings.json::data.dem_source` = copernicus）
```

> 关键点：CDSE 下载 DEM（CCM 贡献任务数据）仅支持 S3 SigV4 签名，Bearer token 实测 403；因此未配置 S3 密钥时 DEM 自动走 Planetary Computer。

---

## 三、Landsat 8/9（热红外）

- **STAC 集合**：`landsat-c2-l2`（Planetary Computer）
- **下载波段**：`lwir11`（= ST_B10，地表温度，DN 缩放值）+ `qa_pixel`（质量评估）
- **分辨率**：下载输出 30m
- **重访周期**：16 天
- **温度定标**（在预处理构建训练/约束层时完成）：

$$
\mathrm{LST}\ (K) = \mathrm{DN} \times 0.00341802 + 149.0
$$

- **QA 云掩膜**（`data_preprocessing._landsat_qa_mask`）：QA 位掩码剔除云/云影/填充像元（cloud bits = bits 1–4，fill = bit 0）。

---

## 四、Sentinel-2（多光谱）

- **STAC 集合**：CDSE 与 Planetary Computer 均为 `sentinel-2-l2a`
- **下载波段**：B02、B03、B04、B08、B11（多光谱）+ SCL（场景分类）
- **`sentinel2_bands.tif` 波段顺序**（与下载一致，10m 输出；B11 原生 20m 重采样到 10m）：

| 波段序号 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| 通道 | B02（蓝） | B03（绿） | B04（红） | B08（近红外） | B11（短波红外） |
| 特征列 | R | G | B | NIR | SWIR1 |

- **重访周期**：5 天
- **按景辐射定标**（`sentinel2_calibration.py`，L2A 地表反射率）：

$$
\mathrm{reflectance} = \frac{\mathrm{DN} + \mathrm{BOA\_ADD\_OFFSET}}{\mathrm{BOA\_QUANTIFICATION\_VALUE}}
$$

  - DN = 0 仍为 NoData（不参与校正）；
  - Processing Baseline ≥ 04.00 → 每波段 `BOA_ADD_OFFSET = -1000`、`QUANTIFICATION = 10000`；更旧 baseline → offset = 0；
  - 参数**按景解析**（优先读 MTD_MSIL2A.xml，失败回退 baseline 规则，来源标注在 `source` 字段），定标记录写入 `sentinel2_provenance.json`。

- **SCL 有效类别**（联合掩膜用）：`[4, 5, 6]`（植被、裸土、水体）。

---

## 五、DEM（地形）

- **数据源**：Copernicus DEM GLO-30（`settings.json::data.dem_source` = `copernicus`）
- **STAC 集合**：Planetary Computer `cop-dem-glo-30`；CDSE `cop-dem-glo-30-dged-cog`
- **分辨率**：30m；高程用于计算 Slope / Aspect / cos(Aspect)（见公式与原理参考文档第四节）

---

## 六、质量控制

- **配对规则**（`data_acquisition._build_pairs`）：Landsat 与 Sentinel-2 按成像日期配对，**时间差 ≤ 2 天**；L8 只与 L8 拼接、L9 只与 L9 拼接；每组 mosaic 覆盖度 ≥ 70% 才算合格；
- **云量阈值**：搜索时按 `eo:cloud_cover < cloud_threshold` 过滤——有效默认 **30**（`settings.json::data.cloud_threshold` 与 Skill 默认一致）；样本不足时可放宽；
- **联合掩膜**：预处理用 `landsat_qa_mask & sentinel_scl_mask` 确定有效像元。

---

## 七、参数与配置（`settings.json`）

| 配置 | 键 | 默认 | 说明 |
|---|---|---|---|
| 云量阈值 | `data.cloud_threshold` | 30 | 影像搜索过滤阈值（可放宽） |
| DEM 数据源 | `data.dem_source` | copernicus | copernicus（Copernicus GLO-30） |
| 默认输出目录 | `data.default_output_dir` | 空 | 由后端按用户自动分配 workspace |
| CDSE 凭据 | `data_space` | — | username/password/client_id/client_secret/s3_key/s3_secret（**敏感，不入记忆**） |

---

## 八、记忆系统应用

本文档条目与《GeoThermoAI_记忆系统数据清单.md》领域知识对应：

| 数据清单编号 | 本文档章节 | 建议入记忆的要点 |
|---|---|---|
| K10（Landsat 参数） | 第三节 | C2 L2，lwir11 + qa_pixel，重访 16 天，LST = DN×0.00341802+149.0 |
| K11（Sentinel-2 参数） | 第四节 | L2A，B02/B03/B04/B08/B11 + SCL，重访 5 天，按景 BOA_ADD_OFFSET 定标 |
| K12（DEM 数据源） | 第五节 | Copernicus GLO-30，30m |
| K13（影像配对规则） | 第六节 | 时间差 ≤ 2 天；云量阈值默认 30，可放宽 |

> 建议以第三、四、五、六节内容组装为 `global_knowledge` 的 RAG 种子段落（每段一个主题：Landsat、Sentinel-2、DEM、云量与掩膜）。
