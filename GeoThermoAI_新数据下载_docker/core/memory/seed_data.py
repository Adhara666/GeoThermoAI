"""
领域知识种子数据（`knowledge_seed.json` 的内嵌来源）

对应《GeoThermoAI_知识种子设计稿.md》K01–K31（无区域经验，以当前代码实现为准）。
首次播种时写入 `data/users/{uid}/memory/knowledge_seed.json` 并灌入
ChromaDB `global_knowledge` Collection（幂等：count>0 跳过）。
"""

SEED_SCHEMA_VERSION = 1

SEED_ITEMS = [
    {
        "id": "K01",
        "topic": "TTRI 公式",
        "tags": ["ttri", "公式", "地形", "跨分辨率失真"],
        "content": "TTRI（地形热响应指数）刻画地形对地表温度的控制作用。LST 拟合模型为 LST = intercept + a·DEM + b·Slope + c·cos(Aspect)（含截距，仅用训练集拟合一次）；TTRI 标量取地形线性贡献部分，不含截距：TTRI = a·DEM + b·Slope + c·cos(Aspect)。validate/test/完整30m约束层/10m预测格网复用同一组系数做无标签变换；10m 空间化基于完整30m约束层 + 统一仿射映射双线性插值。",
    },
    {
        "id": "K06",
        "topic": "TTRI 解决的问题（设计动机）",
        "tags": ["ttri", "动机", "跨分辨率失真", "坡向", "热效应空间插值"],
        "content": "TTRI 解决的核心问题是地形变量跨分辨率失真：若把 30m DEM 重采样到 10m 后再计算坡度坡向，坡向作为 0-360° 循环变量插值后物理意义崩塌——一个 30m 像元碎裂为 9 个 10m 子像元，坡向人为平滑或跳变，其误差经 cos(Aspect) 放大为可观的热效应偏差，坡度同样失真（实验证明加入插值后的坡向坡度反而使 RMSE 恶化）；若放弃坡度坡向，又损失地形热效应信息。TTRI 的思路是把 DEM/Slope/Aspect 对 LST 的线性贡献压缩为单通道连续标量场（与 LST 同量纲 K），在热效应空间而非物理空间插值：TTRI_30m 双线性插值到 10m 相当于把「地形对 LST 的热效应贡献」连续映射到 10m 网格，只需一次插值、误差来源单一可控，避免 DEM、Slope、Aspect 三个独立插值误差的叠加。类比：图像处理中先 RGB→亮度-色度空间再插值，在更均匀的感知空间操作效果远优于原始空间直接插值。",
    },
    {
        "id": "K07",
        "topic": "TCR 解决的问题（设计动机）",
        "tags": ["tcr", "动机", "跨尺度系统性偏差", "尺度不变假设"],
        "content": "TCR 解决的核心问题是跨尺度系统性偏差：随机森林等逐像素回归模型采用「粗分辨率建模、细分辨率预测」范式，隐含尺度不变假设（LST 与预测变量的关系在不同分辨率下保持一致），但空间平均效应使模型应用于细分辨率时必然引入系统性偏差——即使粗分辨率测试集 R² 很高，也无法反映降尺度的跨尺度误差，简单残差修正不足以消除。TCR 用 30m 真实 LST（物理真值，完整约束层）对 10m 预测施加块级热约束，包含三项设计机制：①全量约束——使用完整30m约束层全部像元而非稀疏采样，每块偏差都被精确捕获，避免采样偏差；②块级聚合消噪——块内 10m 预测取均值，随机预测误差相互抵消，保留纯净的系统性偏差；③块常数闭合修正——默认将每格残差以整格常数方式加回格内全部有效10m像元（block_constant），精确满足「30m产品格网算术均值闭合」；可选平滑重中心化（smooth_recentered）用连续残差场替代整格常数，但同样需按同一父格重中心化保证格内均值闭合。",
    },
    {
        "id": "K02",
        "topic": "TCR 机制",
        "tags": ["tcr", "残差", "热约束"],
        "content": "TCR（热约束残差）修正跨尺度系统性偏差，以完整30m约束层（30m_constraint_grid.csv，真实30m像元而非稀疏锚点）为参考，细→粗/粗→细映射统一使用 core.grid_mapping 仿射逆变换。核心公式：TCR_30m = LST_true_30m − mean(LST_pred_in_30m_cell)；LST_final_10m = LST_pred_10m + TCR。默认 block_constant 模式：每个30m格内所有有效10m像元加同一残差常数，精确满足「30m产品格网算术均值闭合」，边界可能呈块状；可选 smooth_recentered 模式（实验性）：先对TCR场双线性插值生成连续残差场，再按同一父格重中心化，格内均值闭合同样精确，但不承诺全局连续（30m栅格边缘半个像元内退化为整格常数）。TCR 只谈算术均值闭合，不宣称辐射或能量守恒。",
    },
    {
        "id": "K03",
        "topic": "降尺度原理",
        "tags": ["降尺度", "原理", "双阶段"],
        "content": "LST 降尺度为回归-热约束双阶段框架：随机森林在9维特征空间（R,G,B,NIR,SWIR1,NDVI,NDWI,NDBI,TTRI）建立光谱-地形到LST的非线性映射，负责10m高频细节建模；TCR在2维地理空间补偿跨尺度系统性偏差。整体为 Landsat 30m LST + Sentinel-2 10m 多光谱特征 → 10m LST。两阶段互补的设计动机：RF 逐像素预测的随机误差可被 Bagging 抑制、跨尺度系统性偏差则不可避免，而 TCR 依赖真值无法独立预测；两类误差来源与性质不同，单一模型难以同时最优处理两者，按误差性质分解为特征空间回归（高频细节）+ 地理空间热约束（低频偏差校正）后各司其职，整体更鲁棒。",
    },
    {
        "id": "K04",
        "topic": "MB 指标定义",
        "tags": ["指标", "MB"],
        "content": "MB = mean(预测 − 参考)，单位 K，正值表示预测整体偏暖。与 R²/RMSE/MAE 共同构成模型精度指标。",
    },
    {
        "id": "K05",
        "topic": "数据源",
        "tags": ["数据源", "Planetary Computer", "Copernicus Data Space", "GEE"],
        "content": "本系统数据源为 Microsoft Planetary Computer（STAC API：planetarycomputer.microsoft.com）+ Copernicus Data Space（dataspace.copernicus.eu，国内下载更快）。Landsat 8/9 Collection 2 Level-2（ST_B10 + QA_PIXEL）、Sentinel-2 L2A（B02/B03/B04/B08/B11 + SCL）、Copernicus GLO-30 DEM 均由系统通过 STAC API 自动搜索并下载；Sentinel-2 与 DEM 优先 Copernicus Data Space，失败自动回退 Planetary Computer。不使用 Google Earth Engine（GEE），国内可直连。",
    },
    {
        "id": "K10",
        "topic": "Landsat 参数",
        "tags": ["landsat", "卫星", "LST"],
        "content": "Landsat 8/9 Collection 2 Level-2：地表温度波段 lwir11（ST_B10，缩放 DN）+ QA_PIXEL；重访周期16天；温度定标 LST(K) = DN × 0.00341802 + 149.0；QA 掩膜剔除云/云影/填充像元。",
    },
    {
        "id": "K11",
        "topic": "Sentinel-2 参数",
        "tags": ["sentinel2", "定标", "波段"],
        "content": "Sentinel-2 Level-2A：多光谱 B02/B03/B04/B08/B11 + SCL；重访周期5天；地表反射率按景定标 reflectance = (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE，DN=0 仍为 NoData；Processing Baseline ≥ 04.00 时 offset=-1000、quantification=10000。",
    },
    {
        "id": "K12",
        "topic": "DEM 数据源",
        "tags": ["dem", "地形", "数据源"],
        "content": "DEM 数据源为 Copernicus GLO-30，30m；高程用于计算 Slope、Aspect、cos(Aspect) 等地形特征，作为 TTRI 回归输入。",
    },
    {
        "id": "K13",
        "topic": "影像配对规则",
        "tags": ["配对", "云量", "规则"],
        "content": "Landsat 与 Sentinel-2 影像配对要求成像时间差 ≤ 2 天；搜索时按 eo:cloud_cover < cloud_threshold 过滤，云量阈值默认 30，训练样本不足时可放宽。",
    },
    {
        "id": "K20",
        "topic": "样本量 → n_estimators",
        "tags": ["调参", "n_estimators"],
        "content": "训练样本数 > 5 万时，n_estimators 可增大到 200–500；样本数 < 1 万时，n_estimators 减小到 100–150 以防过拟合。",
    },
    {
        "id": "K21",
        "topic": "地形 → max_depth",
        "tags": ["调参", "max_depth", "地形"],
        "content": "地形复杂（DEM 标准差 > 100m）时 max_depth 增大到 30–40；地形平坦（DEM 标准差 < 30m）时 max_depth 减小到 15–20。",
    },
    {
        "id": "K22",
        "topic": "温度变异 → min_samples_leaf",
        "tags": ["调参", "min_samples_leaf"],
        "content": "温度变异性大（LST 标准差 > 5K）时，min_samples_leaf 建议减小到 5。",
    },
    {
        "id": "K23",
        "topic": "植被覆盖 → max_features",
        "tags": ["调参", "max_features"],
        "content": "植被覆盖度高（NDVI 均值 > 0.5）时，max_features 可以增大到 0.7。",
    },
    {
        "id": "K24",
        "topic": "指标解读基准",
        "tags": ["指标", "解读", "R2"],
        "content": "LST 降尺度测试集 R² 通常处于 0.75–0.85：≥0.85 为优秀，0.80–0.85 为良好，0.75–0.80 为合格，<0.75 偏低需检查数据质量或调参。",
    },
    {
        "id": "K30",
        "topic": "光谱指数公式",
        "tags": ["指数", "公式", "NDVI"],
        "content": "基于 Sentinel-2 定标反射率：NDVI=(NIR−R)/(NIR+R+ε)；NDWI=(G−NIR)/(G+NIR+ε)；NDBI=(SWIR1−NIR)/(SWIR1+NIR+ε)，ε 防除零。波段对应 R=B4、G=B3、B=B2、NIR=B8、SWIR1=B11。",
    },
    {
        "id": "K31",
        "topic": "地形特征计算",
        "tags": ["地形", "Slope", "Aspect"],
        "content": "由 DEM 用 numpy.gradient 计算：Slope = arctan(√(gx²+gy²))（度）；Aspect = (arctan2(−gx,gy)+360) mod 360（度）；cos(Aspect) 作为 TTRI 回归输入之一。",
    },
]


def seed_document() -> dict:
    """组装成 knowledge_seed.json 文件内容。"""
    return {
        "schema_version": SEED_SCHEMA_VERSION,
        "items": SEED_ITEMS,
    }
