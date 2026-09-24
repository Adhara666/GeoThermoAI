**[English](README.md)** | **[简体中文](README.zh-CN.md)**

<div align="center">
  <img src="image/README/1786697477692.png" alt="GeoThermoAI" width="760">
  <h1>GeoThermoAI</h1>
  <p><b>一句话生成 10 米高分辨率地表温度产品</b> —— 多角色智能体驱动的地表温度空间降尺度系统</p>
  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License"></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white" alt="Python"></a>
    <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker"></a>
    <a href="https://modelscope.cn/studios/Adhara/GeoThermoAI"><img src="https://img.shields.io/badge/Online-Demo-FF6A00.svg" alt="Demo"></a>
  </p>
</div>

用自然语言描述需求（例如"对武汉市开展 2024 年 7 月的地表温度降尺度"），GeoThermoAI 会自动完成**数据检索与下载、预处理、地形热响应计算、随机森林建模、热约束校正、精度评估与产品导出**，产出可直接进入 GIS 分析的 **10m 高分辨率地表温度 GeoTIFF**——无需遥感操作经验。

**用户获得什么**：一张道路、水体和建筑纹理清晰的 10m 地表温度图层；可在地图上逐像元查询开尔文温度；可下载 GeoTIFF 与全部过程数据；可对已有结果继续追问（"这个点多少度""这是哪里"）。

**我们坚持什么**：定标、掩膜、空间划分、模型训练、热约束校正和精度计算全部由确定性遥感算法（GDAL / rasterio / scikit-learn）完成，Agent 负责理解目标、组织任务、检查数据和解释结果——**数值可信、过程可审计**。

## ✨ 核心亮点

| 能力 | 说明 |
|---|---|
| **自然语言驱动** | Chat 提问 / Work 执行，一句话描述目标即启动全流程 |
| **多角色智能体** | Planner / Data / Train / Evaluation 四类角色分工协作，关键计算仍由确定性算法完成 |
| **Chat / Work 双模式** | 只读问答与完整执行边界清晰，互不越界 |
| **「由我批准」/「完全执行」** | 可选在关键节点（影像配对、超参寻优等）人工审批，或全自动连续执行 |
| **配对 / 月度合成双策略** | 面向真实时刻精细分析，或面向长时序月度热环境监测 |
| **无空洞结果自动续跑** | 创建任务时说"无空洞"，主流程完成后自动执行空洞填补，一份报告交付 |
| **地图交互测温** | 30m / 10m 图层像元级开尔文温度查询、光标锁定、透明度调节 |
| **项目级记忆** | 结构化 + RAG 双轨记忆，跨对话延续任务、复用历史经验 |
| **结果智能问答** | 对已有结果自然语言提问：地点识别（"这是哪里"）、区域/地图选点温度统计 |

## 🚀 快速开始

```bash
docker build -t geothermoai-image .
docker volume create geothermoai_data
docker run -d --name geothermoai -p 7860:7860 \
  --memory=16g --cpus=16 \
  -v geothermoai_data:/app/data \
  geothermoai-image
```

打开 **http://localhost:7860** 后：

1. 在右侧工作面板完成 **[API设置 → 数据源（可选）→ 测试 → 研究区]** 四步配置（LLM 推荐 DeepSeek V4 Flash；数据源不配置也可直接使用，系统自动回退 Microsoft Planetary Computer）；
2. 新建项目与对话，选择 **Work 模式**；
3. 发送一句话：`对武汉市开展 2024 年 7 月的地表温度降尺度，用配对模式`；
4. 在任务面板跟踪进度，完成后在地图中查看结果、在下载面板获取 GeoTIFF。

更多入口：

- 在线体验（ModelScope 创空间）：<https://modelscope.cn/studios/Adhara/GeoThermoAI>
- 操作演示（B 站，武汉 2024 年 7 月配对模式全流程）：<https://www.bilibili.com/video/BV1L8gP6vEBC/>
- 示例数据与产品（武汉 2024-07-22）：<https://modelscope.cn/datasets/Adhara/Wuhan_10m_LST_20240722>

> 说明：容器内 `/app/data` 是账号、对话、研究区、任务台账与记忆的持久根，必须挂载。部署变量表与迁移说明见 [部署配置说明](docs/部署配置说明_第七阶段.md)。

## 📸 功能速览

<div align="center"><img src="image/README/1786613710042.png" alt="1786613710042"></div>
<div align="center"><b>Chat / Work 双模式：只读问答与完整执行边界清晰</b></div>

<div align="center"><img src="image/README/1786613837347.png" alt="1786613837347"></div>
<div align="center"><b>由我批准 / 完全执行：关键节点是否等待人工确认</b></div>

<div align="center"><img src="image/README/1786634168921.png" alt="1786634168921"></div>
<div align="center"><b>当需求缺少关键信息（如处理方式）时，系统主动提问</b></div>

<div align="center"><img src="image/README/1786599051833.png" alt="1786599051833" width="560"></div>
<div align="center"><b>地图像元级温度查询：10m 结果与 30m 参考均可逐像元读取</b></div>

<div align="center"><img src="image/README/1786635056513.png" alt="1786635056513" width="560"></div>
<div align="center"><b>精度面板：测试区精度、样本数与温度值域一目了然</b></div>

## 🧠 工作原理

系统在算法前增加了一层任务控制，把用户目标拆分为**规划、数据、训练、评价**四类职责，由智能体编排执行；所有关键计算仍交给确定性遥感算法完成。完整流程为：

```
数据获取 → 数据预处理 → 地形热响应（TTRI）→ 随机森林建模 → 热约束校正（TCR）→ 导出 → 精度评估
                                                                            └ 可选：空洞填补（无空洞产品）
```

- 数据来源：Microsoft Planetary Computer（Landsat 8/9 L2）与 Copernicus Data Space（Sentinel-2 L2A、GLO-30 DEM），自动搜索、下载、缓存，失败自动回退；
- 温度来源：Landsat 30m 地表温度提供粗尺度热参考，Sentinel-2 提供 10m 空间细节，随机森林 + 热约束残差校正输出 10m 温度估计（保持 30m 父格均值约束）；
- 结果问答：数值统计由程序层完成，语言模型只做措辞组织，**程序算数、模型说话**。

技术细节见 [算法原理](docs/算法原理.md)；使用与配置步骤见 [功能与使用指南](docs/功能与使用指南.md)。

## 📖 文档导航

| 文档 | 内容 |
|---|---|
| [docs/功能与使用指南.md](docs/功能与使用指南.md) | 配置步骤、模式说明、地图与精度面板、武汉案例 |
| [docs/算法原理.md](docs/算法原理.md) | 定标/掩膜、光谱与地形特征、TTRI、随机森林、热约束校正、精度指标 |
| [docs/背景与动机.md](docs/背景与动机.md) | 从遥感影像到可用温度产品的四个断点 |
| [docs/部署配置说明_第七阶段.md](docs/部署配置说明_第七阶段.md) | 部署变量表与迁移说明 |

## 🛰️ 效果示例

<div align="center"><img src="image/README/1786634710546.png" alt="1786634710546"></div>
<div align="center"><b>武汉市 2024-07-22：10m 地表温度产品（左）与 30m Landsat 参考（右）对比</b></div>

与 30m 参考相比，10m 产品清晰呈现了街道、路网与建筑地块间的温度细节。本案例数据与产品已上传 [ModelScope 数据集](https://modelscope.cn/datasets/Adhara/Wuhan_10m_LST_20240722) 供查阅。

> 特别说明：降尺度结果不是独立获取的 10m 热红外观测。测试区精度评价的是模型对留出 30m 温度样本的预测能力，粗尺度闭合评价的是结果回聚合后与参考均值的匹配程度，二者均不能替代独立的 10m 精度验证。

## 📄 算法灵感

本项目算法来源于两位开发者的热红外遥感课程设计，主要灵感来自以下论文与在线教程：

1. Kolláth, J., Kolláth, M., & Šveda, P. (2022). Combining Landsat 8 and Sentinel-2 data in Google Earth Engine to derive higher resolution land surface temperature maps in urban environment. *Remote Sensing*, 14(16), 4076. doi: 10.3390/rs14164076
2. Yuan, W., Hu, S., Zhan, C., Wang, G., & Luo, Y. (2025). Machine learning land surface temperature downscaling method based on Landsat 9 and Sentinel-2 satellite feature interaction. *Geo-spatial Information Science*, In Press. doi: 10.1080/10095020.2025.2598526
3. Bahi, H., Bounoua, L., Sabri, A., Bannari, A., Malah, A., & Rhinane, H. (2025). A new thermal fusion method to downscale land surface temperature to finer spatial resolution using Sentinel-MSI and Landsat-OLI/TIRS imagery. *Remote Sensing Applications: Society and Environment*, 37, 101519. doi: 10.1016/j.rsase.2025.101519
4. Andriambololonaharisoamalala, R. R., Helmholz, P., Bulatov, D., Ivanova, I., Song, Y., Soon, S., & Jones, E. (2025). Downscaling of urban land surface temperatures using geospatial machine learning with Landsat 8/9 and Sentinel-2 imagery. *Remote Sensing*, 17(14), 2392. doi: 10.3390/rs17142392
5. <https://mp.weixin.qq.com/s/OXwHyvUK3zEe0AoqTw6_rA>

## 💬 参与交流

GeoThermoAI 由两位来自中国地质大学（武汉）遥感科学与技术专业 2023 级的本科生共同开发。我们诚挚期待与热红外遥感领域的同行深入交流、广泛合作，共同打磨底层算法。

发现 bug 或有建议，欢迎提交 Issue 或邮件联系：

- jiyinuo@cug.edu.cn
- 3509851915@qq.com

## ⚖️ 开源许可

GeoThermoAI 采用「开源社区版 + 商业授权」双许可模式：全部源代码以 **GNU Affero General Public License v3.0（AGPL-3.0）** 授权，商业使用场景需另行购买商业授权，详情见 [NOTICE](NOTICE)。
