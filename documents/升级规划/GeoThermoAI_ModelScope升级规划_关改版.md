# GeoThermoAI / EasyLST → ModelScope Studio 升级规划：最终执行版 v3.1

> **文档用途**：在现有 `GeoThermoAI - 副本/` 与 `documents/` 基础上，给出一份可以按章节、按文件、按验收门槛直接实施的 ModelScope Studio 改造规范。
>
> **本轮范围**：只修订技术文档，不生成完整工程代码；原文件 `技术文档/GeoThermoAI_ModelScope升级规划_v2.md` 保留不覆盖。v3.1 进一步补入 TLC 类、MFGWML 类方法的增量接入契约；这里只规定接口、产物、验证和 UI 兼容边界，不把尚未实现的新算法写成已完成能力。
>
> **代码基线**：`F:/Cursor_Python/geothermalai/GeoThermoAI - 副本/`
>
> **资料基线**：`F:/Cursor_Python/geothermalai/documents/`、`文献/newgeothermal/readers/`、`docs/modelscope/`、`比赛要求/`
>
> **事实核验日期**：2026-07-31（赛事 PDF/海报、ModelScope 本地官方 PDF、在线部署 schema、提交门户公开 OpenAPI、`modelscope_studio` 2.0.2 Python 包及其 `modelscope-studio` 源码与示例均已核对）
>
> **最终定位**：确定性、可审计的 EasyLST 热红外降尺度工作流是科学计算内核；LLM/Agent 负责意图理解、结构化参数建议、流程说明和结果解读，不负责猜测文件路径、绕过数据契约或改变科学指标定义。

---

## 0. 先给结论：原思路保留，但实施顺序必须调整

现有 v2 的主方向是正确的：

- 将 PyWebView 桌面界面迁移到 ModelScope Gradio 创空间；
- 采用类似 `coder_artifacts` 的整页布局，左侧对话/任务编排，右侧展示科学产物；
- 保留 EasyLST 的 TTRI + RF + TCR 方法主线；
- 保留 Skill/Agent 的可扩展架构；
- 先完成 Studio，后续再补 Notebook 复现闭环；
- 利用 ModelScope 的创空间、数据集、模型托管和 API-Inference 资源。

但“核心算法零改动后直接换 UI”不可执行。代码审查证明，当前原型存在几项会直接影响科学正确性或云端运行的阻断问题。最终执行顺序应改为：

```text
冻结现状与建立小型测试夹具
  → 统一格网、产物和任务状态契约
  → 修正 TTRI / TCR / 导出 / 评估的科学与空间问题
  → 建立 fail-fast 的确定性编排器
  → 制作无需外部密钥的小型真实 Demo
  → 搭建左对话 / 右产物 Studio
  → ModelScope 原生 Gradio 部署
  → 托管模型与数据、公开验收并提交
```

### 0.1 已锁定的关键决策

| 决策项 | 最终选择 | 原因 |
|---|---|---|
| 提交形态 | **Studio 优先**；Notebook 后补 | Studio 本身就是赛事允许的独立提交形态；先把交互体验做完整，不以 Notebook 缺失否定路线 |
| 产品内核 | **确定性 DAG/状态机 + 可选 LLM Copilot** | 保持 Agent 价值，同时保证科学计算可复现、可定位、可恢复 |
| UI 骨架 | **模仿 `coder_artifacts` 整页结构** | 使用官方已存在的 Row/Col/Card/Tabs/Modal/Drawer/Tour 交互骨架，业务替换为 LST 降尺度 |
| 页面分栏 | 左侧 1/3 对话与任务；右侧 2/3 产物 | 官方示例已验证 `antd.Col(span=24, md=8/16)`，移动端自动堆叠 |
| Studio 部署 | **普通 Gradio SDK 创空间优先** | 组件兼容、平台集成简单；Docker 仅在 clean build 证明原生依赖无法满足时启用 |
| 平台版本 | `gradio==6.2.0` + `modelscope_studio==2.0.2` | ModelScope 当前 quick-deploy schema 支持 6.2.0；组件 2.0.2 要求 Gradio 6.0–6.8 |
| 运行资源 | 免费 `2vCPU/16GB` 作为基线；GPU/xGPU 只作可选增强 | EasyLST 主链 CPU 可运行；赛事 GPU 权益和账号可用项以实际页面为准 |
| Demo 策略 | **小 AOI 真运行 + 大区域离线处理** | 历史武汉全尺度运行曾产生约 2.06 亿行/224 MB 输出，不适合公共 Studio 同步请求 |
| 内部数据格式 | 栅格窗口 + NPZ/JSON；不再用 10 m 全图 CSV | 控制内存、避免行序依赖，并保留空间格网语义 |
| 记忆系统 | 先做会话/任务 JSON 或 SQLite；Chroma/RAG 延后 | 先解决任务血缘与恢复；避免在提交前增加大依赖和跨用户隐私风险 |
| 方法族扩展 | P0 先落静态注册表、方法运行命名空间和数据驱动 presenter；仅 EasyLST 可运行 | 为 TLC/MFGWML 留稳定接点，同时不挤占首版科学纠错与部署时间 |
| TLC/MFGWML 与多方法 Benchmark | 作为 P2 按 `planned → experimental → available` 增量接入 | 先修正 split、TTRI 泄漏与指标语义；只有验证协议一致的方法运行才允许横向排名 |

### 0.2 本文中的优先级

- **P0**：Studio 上线前必须完成；缺一项就不进入公开部署。
- **P1**：提交版应完成；若外部平台条件受限，必须有明确降级行为。
- **P2**：架构已预留，但不阻断 Studio 首版。
- **发布门禁**：只有通过相应验收后，文档中的“已完成”才可以改为勾选状态。

---

## 1. 赛事要求、评分证据与实际提交形式

### 1.1 赛事直接要求

以下内容以 `比赛要求/GRSS x 魔搭社区 _ GeoAI 开源实践挑战赛启动.pdf` 的文本层和逐页视觉核验、`比赛要求/geoai比赛.png` 为主，并用官方 AP-GARSS 规则页与提交门户补充核对 Eligibility、可核验性、精确截止时区和后续节点。

| 项目 | 官方要求 | 本项目落实方式 |
|---|---|---|
| 赛事定位 | 将已有研究、模型或数据整理为可运行、可检查、可复用的 GeoAI 开源资源；不要求必须发明全新算法 | 将已有 EasyLST/GeoThermoAI 原型改造成可直接体验、可下载、可追溯的 Studio |
| 发布平台 | 作品发布于 ModelScope 并附清晰文档 | Studio、后续 Model/Dataset/Notebook 全部互相链接 |
| 合法形式 | Notebook、Studio、Notebook + Studio 三选一 | 首轮选择 Studio；后续 Notebook 负责复现与解释 |
| Studio 定义 | 可交互网页应用，可用于模型推理、遥感分析、数据浏览/下载、GeoAI 等 | 左侧任务编排，右侧地图、对照、指标、产物和血缘 |
| 队伍 | 每队最多 5 人，每人仅加入 1 队 | 由队长在提交门户维护成员信息 |
| 参赛对象 | 官方 Eligibility 面向相关领域的学生、早期职业研究者和开发者 | 注册前逐人确认资格及“未加入其他队” |
| 可核验性 | 主链依赖的代码、模型和数据必须可由评委独立核验；无法独立核验的专有依赖可能导致不合格 | Demo 主链只使用可追溯、可访问或有合法获取脚本的资源；统一登记外部资产 |
| 截止时间 | PDF 给出 2026-08-14；提交门户进一步明确为 **2026-08-14 24:00（UTC+8）** | 技术冻结应早于门户截止，预留公开访问与重部署缓冲 |
| 后续节点 | 2026-08-21 公布入围；2026-10-11 至 10-14 在青岛举行决赛答辩与颁奖 | D13 冻结的 release、截图、指标和演示脚本同时作为答辩底稿 |
| 论文/摘要 | SS095 是相关活动，不是挑战赛作品的强制前置 | 可选，不纳入 Studio 上线阻断项 |

赛事并未要求必须做 Agent，也没有要求必须复刻某篇论文。GeoThermoAI 的 Agent、工作流和热红外应用都属于允许方向；评审重点是这些能力是否真的可运行、可解释、可复用。

### 1.2 评分标准转成可验收证据

| 维度 | 分值 | 评委关注 | GeoThermoAI 必须提供的证据 |
|---|---:|---|---|
| 科学价值 | 30 | 问题重要性、原创性、地学/遥感相关度 | LST 30 m→10 m 的问题定义；EasyLST 公式与输入输出；“既有 EasyLST 方法 / 原 GeoThermoAI 能力 / 本次新增科学纠错与开源工程贡献 / 第三方资源”贡献边界表；真实小 AOI 结果；独立预测指标与粗尺度一致性指标分开；已知边界 |
| 技术实现 | 20 | 方法严谨性、工程质量、系统设计 | 格网/产物契约；空间 split；只用训练集拟合 TTRI；窗口化推理；fail-fast/resume；状态与错误可审计；测试证据 |
| 开源贡献 | 20 | 公开可访问、许可证、相关资源完整链接 | 公开 Studio 及代码/模型/数据链接；资源卡片、版本、哈希与获取说明；发布收尾时补齐明确许可 |
| 形式完成度 | 20 | 所选提交形式的呈现有效性 | 无需密钥的一键 Demo；清晰进度；输入预览；粗细结果对照；指标解释；下载包；移动端可用；失败可恢复 |
| 社区影响力 | 10 | 下载、Star、Fork 等真实反馈 | 提前公开、可复制 Quick Start、示例截图/GIF、清晰问题反馈入口；不预设或伪造任何社区数据 |

评审体验应形成一条连续证据链：

```text
打开公开 Studio
  → 点击“一键体验”
  → 看到真实工作流逐步执行
  → 查看 30 m / 10 m 对照、指标和数据血缘
  → 下载 GeoTIFF + metrics + manifest
  → 从 README 跳转到代码、模型和数据资源
```

### 1.3 ModelScope 免费资源如何转成具体成果

| 平台资源 | 官方材料能确认的范围 | 本项目的具体用法 |
|---|---|---|
| Studio | 赛事期间提供交互式 Web 应用部署资源；普通账号当前可见免费基线为 2vCPU/16GB | 承载公开 Demo、上传校验、任务状态、地图、指标与产物下载；核心链路按 CPU 设计 |
| Notebook | 新用户 36 小时免费 GPU 与 CPU 算力，用尽后可申请追加 | 后续跑空间 split、baseline/ablation、生成 `metrics.json` 和可复现 Notebook；不把它当首版 Studio 的前置条件 |
| 模型托管 | ModelScope 模型仓库 | 托管 EasyLST 的 `rf_model.joblib`、`bundle_meta.json`、TTRI 系数、method spec、指标和 model card；后续每个可运行方法按独立版本管理 bundle；P0 小模型同时内置于 Studio，Model repo 作为固定 revision 的可复用发布源 |
| 数据集托管 | ModelScope 数据集仓库 | 托管可合法再发布的小型 Demo、manifest、期望输出和 benchmark split；若上游限制再分发，只发布获取脚本与 item 清单 |
| API-Inference | 每日最高 2,000 次，覆盖平台支持的开源模型；具体模型和实际额度会变化 | 仅用于可选的意图解析/结果说明；无 token 或调用失败时退化为确定性表单和模板报告，LST 计算不受影响 |
| xGPU/GPU | 赛事材料称可提供免费 GPU；具体型号、资格、时长需以账号可见资源为准 | 只在确有 GPU 推理/实验需求时申请；RF、GDAL/rasterio 和可视化不为“用满资源”而强行上 GPU |

资源使用原则：GPU 用于适合 GPU 的训练/模型实验；遥感下载、重投影、RF 与报告生成优先 CPU；LLM 使用 API-Inference，避免在 Studio 中常驻大语言模型。

### 1.4 提交门户的精确字段与操作边界

提交入口：

`https://modelscope.cn/studios/GRSS-Student-Chapter-Beijing/APGARSS-GeoAI-Challenge?lang=en_US`

其运行 host 的公开 `/openapi.json` 已于 2026-07-31 核验。表单字段为：

```text
team_name
leader_email
project_name
project_abstract
member1_name / member1_affiliation / member1_modelscope_username
member2_name / member2_affiliation / member2_modelscope_username
...
member5_name / member5_affiliation / member5_modelscope_username
artifact_links
learn_article_link
model_dataset_links
tag_topic_confirmed
csrf_token
```

**当前提交是链接式表单提交。** 公开 OpenAPI 未出现作品文件上传字段；作品本体必须先发布到 ModelScope，再在 `artifact_links`、`learn_article_link`、`model_dataset_links` 中填写真实可访问链接。若截止前门户新增字段，以队长登录后的页面和服务器回显为准，不凭本文自造上传要求。

接口层硬必填字段为 `team_name`、`leader_email`、`member1_name`、`member1_affiliation` 和页面自动管理的 `csrf_token`。**接口允许空值不等于评审材料完整**：项目名、摘要和已实际完成的公开产物链接应填写；尚未产生的可选 Learn/Model/Dataset 链接可暂留空，完成后再更新，禁止伪造占位 URL。最终材料按以下清单准备：

- 项目名；
- 建议控制在 99 words 以内的英文项目摘要；当前门户前端实现允许 100 词，但提示文字写作 “under 100 words”，最终以前台即时校验为准；
- 可公开访问的 Studio/Notebook/代码等 `artifact_links`；
- Learn 文章链接（完成后填写）；
- 模型/数据集资源链接；
- 页面要求的 topic/tag 确认项；
- 队长和成员的姓名、单位、ModelScope 用户名。

成员 2–5 一旦添加，姓名、单位、ModelScope 用户名必须三项成组填写。团队名注册后锁定；页面允许在截止前更新材料，最终以门户显示和服务器保存成功状态为准。`csrf_token` 只属于浏览器会话，禁止写入任何工程文件。

### 1.5 动态信息的发布前复核

以下项目不是赛事 PDF 中的固定事实，不能在代码或 README 中写成永久保证：

- 账号实际可选 GPU/xGPU 规格、时长与付费价格；
- `/mnt/workspace` 容量和创空间休眠时长；
- API-Inference 当前支持的模型 ID、并发与实际额度；
- Gradio/base image 的最新可选值；
- 尚未创建的 GeoThermoAI Model/Dataset/Studio 仓库 ID；
- 2026-08-14 当天门户是否新增服务器端校验文案。

发布负责人须在最终提交前重新打开赛事页、提交门户、创空间设置页和 deploy schema，并把复核日期写进发布记录。

---

## 2. 现有项目真实能力与必须修正的问题

### 2.1 当前已经做成的内容

`GeoThermoAI - 副本/` 不是空壳，已经形成一条有历史运行记录的原型链。可复用能力包括：

| 能力 | 当前实现位置 | 结论 |
|---|---|---|
| Planetary Computer STAC 搜索与影像配对 | `core/skills/builtin/data_acquisition.py` | 可复用搜索、asset 选择和下载思想，需统一阈值、几何和产物契约 |
| Landsat/Sentinel 掩膜与光谱指数 | `core/data_preprocessing.py` | QA/SCL、反射率和指数逻辑可复用，需严格格网校验与窗口化 |
| TTRI 公式原型 | `core/ttri.py` | 公式主线可保留，但拟合/应用方式必须纠错 |
| RF 九特征训练/预测 | `core/rf_model.py` | 模型框架可复用，需空间 split、模型元数据和窗口推理 |
| TCR 跨尺度残差约束 | `core/tcr.py` | 数学定义可保留，空间实现与评估命名必须纠错 |
| GeoTIFF 导出与空间一致性评估 | `core/export_geotiff.py`、`core/evaluation.py` | affine 聚合思路可复用，导出需消除 CSV 行序假设 |
| Skill 注册表与单 Agent | `core/skills/`、`core/agent/` | 扩展框架可复用，科学执行要交给显式状态机 |
| 工作流、影像对选择、结果卡片的交互语义 | `ui/` | 视觉 token 和交互思路可参考，PyWebView bridge 不能直接迁移 |

当前七步业务链为：

```text
data_acquisition
→ data_pipeline
→ ttri_compute
→ rf_model
→ tcr_compute
→ lst_export
→ accuracy_eval
```

### 2.2 影响直接实现的 P0 问题

这些问题不是对路线的否定，而是把原型变为可信 Studio 前必须完成的工程化纠错。

| 问题 | 代码证据 | 影响 | 最终处理 |
|---|---|---|---|
| TTRI 目标泄漏 | `core/ttri.py:84-162` 对 train/val/test 分别用各自 LST 拟合 | 验证/测试标签参与特征生成，R² 不能作为独立泛化证据 | 只在 train 拟合一次；保存系数；同一系数应用 val/test/全场景 |
| 10 m 映射使用 `row/3,col/3` | `core/ttri.py:209-311`、`core/tcr.py:248-271` | 不同格网原点、范围或掩膜下空间对应错误 | 建立统一 `RasterGrid`；所有映射走 affine/CRS/reproject，业务代码禁止再写比例索引 |
| 30 m 样本并非完整格网 | `data_preprocessing.py:535-573` 使用 `step=2` | 稀疏点构网格和 TCR 插值可能出现空洞 | 完整特征保持栅格；仅训练样本抽样落 NPZ |
| 像元随机 split | `core/split_dataset.py:16-142` | 空间自相关导致指标偏乐观 | 主模式改为空间 block/留区/留时相；随机 split 仅保留为调试模式 |
| TCR 后指标语义混淆 | TCR 使用参与约束的 30 m Landsat ST 参考值 | 一致性变好不等于独立预测精度 | `independent_prediction` 与 `coarse_consistency` 两套指标分栏 |
| 导出依赖 CSV 行序 | `core/export_geotiff.py:70-89` | 行被打乱时像元错位；全幅数组内存大 | 依据 row/col/transform 或直接窗口写 GeoTIFF/COG |
| pipeline 路径不一致 | `core/pipeline.py` 期待 `*_with_TTRI.csv`，TTRI 实际原地覆盖 | 现成 `run_full` 无法稳定端到端 | 新建 manifest 驱动的 orchestrator；旧 pipeline 暂列 legacy |
| 评估缺 meta 参数 | `core/pipeline.py:463-476` 与 `evaluation.py:71-74` 不匹配 | 全流程到评估阶段会失败 | 所有 stage 只从 artifact key 取必需输入，并在运行前校验 |
| Skill 失败仍标 completed | `geo_thermo_agent.py:686-704` | 失败后继续，产生级联伪成功 | fail-fast；失败态不可进入下一 stage |
| 全局搜索“最新文件” | `geo_thermo_agent.py:335-456` | 会话/项目之间可能串用产物 | 只允许 `job_id + artifact key + hash` 精确寻址 |
| 自动调参分支事实上不可达 | `geo_thermo_agent.py:557-635` 的 `user_specified` 判断 | 文档宣称与代码事实不符 | 首版删除“已自动调参”表述；后续按空间验证集做受控搜索 |
| 全图 10 m CSV | 历史运行约 2.06 亿行 | 公共 Studio 会超时、爆内存/磁盘 | 全图栅格窗口化；训练样本上限抽样；Studio 小 AOI |

### 2.3 云端 UI 与安全边界

| 当前做法 | 为什么不能直接搬到 Studio | 改造要求 |
|---|---|---|
| `window.pywebview.api` + JS 轮询 | 浏览器中不存在 PyWebView bridge | 改为 Gradio 事件、generator、`gr.State` 和服务端 job state |
| tkinter 本地目录选择 | 容器没有用户本地桌面 | 改为示例按钮、Gradio 上传、成果下载和每会话工作区 |
| `config/settings.json` 存 API Key | 当前文件含明文凭据样式，且 `get_config` 会返回前端 | 撤销旧 key；只从平台 Secret/环境变量读取；页面不回显 |
| 上传任意 Skill ZIP 并动态 import | 公共服务存在 zip-slip/任意代码执行风险 | 提交版关闭；只允许白名单内置模型与受控参数配置 |
| SHP 不读取 `.prj` 就当经纬度 | 投影坐标会生成错误 AOI | 只接受完整 ZIP Shapefile 并读取 CRS，或优先 GeoJSON/bbox；统一转 EPSG:4326 |
| 共享线程字典、无锁状态 | 并发会话可能串状态和文件 | `session_id → job_id` 隔离；每 job 单任务锁；并发数先保守设置 |
| 直接展示绝对路径/内部异常 | 泄露服务器信息且用户无法行动 | UI 只展示相对 artifact 名与结构化错误码/建议 |

旧配置文件中的密钥处理是安全整改，不代表要用许可证或 Notebook 问题否定当前路线。

### 2.4 可复用、需修补、需重写的边界

| 类别 | 内容 |
|---|---|
| 直接复用并修补 | STAC 搜索/asset 选择；QA/SCL 掩膜规则；光谱指数；RF 特征组合；TCR 数学定义；evaluation 的 affine 聚合思路；现有 UI 色彩、进度和影像对选择语义 |
| 必须大改 | TTRI 拟合与栅格生成；TCR 格网实现；空间 split；全图 CSV；导出方式；pipeline 失败传播；Agent 产物寻址；托管上传与会话隔离 |
| 不迁移 | PyWebView、原 HTML/JS bridge、tkinter 目录对话框、公开任意 Skill ZIP、浏览器 API Key 设置 |
| 延后 | ChromaDB/RAG、XGBoost/LightGBM/MLP 全量 Benchmark、UHI/时序/生态扩展、LLM 主导调参 |

准确对外表述应为：

> GeoThermoAI 已形成并跑过数据获取、预处理、TTRI、RF、TCR、GeoTIFF 与评估的原型链；比赛版在保留方法主线的基础上，统一格网与产物契约、消除数据泄漏、改造云端编排并提供可复现的小型 Demo。

不要写“当前仓库已经一键完全复现”或“所有核心算法零改动”。

### 2.5 已有历史结果如何使用

`data/conversations/eb2b3c0d4a32.json` 留有武汉运行记录，证明原型曾处理大场景，但产物未随当前仓库提供，且指标受旧 split/TTRI/TCR 语义影响。可以把它当作性能和工程规模依据，不能直接当最终参赛结论。

| 历史证据 | 可支持的结论 | 不可支持的结论 |
|---|---|---|
| 单轮下载约 14,289.5 秒 | 全场景在线下载太慢，必须提供内置 Demo 和离线批处理 | Studio 能在数分钟内任意区域全流程完成 |
| 10 m 有效预测像元约 6,391 万，CSV 总行约 2.06 亿 | 必须取消全图 CSV 并窗口化 | 当前内存/性能已经适配 2vCPU/16GB |
| GeoTIFF 约 15,477×13,346，约 224 MB | 大成果应分流到 Dataset/离线产物 | 应把完整武汉场景塞入 Studio Git 仓库 |
| 旧 RF test R² 约 0.816 | RF 原型确实训练过 | 在纠正 TTRI 泄漏和空间 split 前宣称独立泛化 R²=0.816 |
| 旧一致性 MAE/RMSE 等 | TCR/评估链有运行记录 | 把约束一致性写成独立预测精度 |

最终页面和报告中的数值必须由新 `metrics.json` 生成，禁止复制旧报告或演示数字硬编码。

---

## 3. 最终产品范围与用户路径

### 3.1 一句话产品定义

**GeoThermoAI / EasyLST Studio 是一个面向热红外遥感的可审计降尺度工作台：用户通过自然语言或结构化表单选择小型 AOI/示例数据，系统执行 TTRI + RF + TCR 的确定性工作流，并在同一页面展示输入、30 m→10 m 对照、独立预测指标、粗尺度一致性、数据血缘和可下载成果。**

这里的“10 m”是输出格网像元间距，不代表获得了真实 10 m 热红外观测。结果仍受 Landsat 热红外原始空间支撑、辅助特征和模型假设约束；项目没有同日真实 10 m LST 标签，所有独立精度都必须按第 6.5 节明确为 **30 m held-out 参考尺度验证**。

首版对外运行方法仍是 EasyLST；产品层从一开始通过 `MethodSpec/MethodAdapter/MethodResult` 解耦。后续 TLC/MFGWML 等方法只替换确定性方法子图和方法卡，不改变上述用户路径、主 UI 骨架与共同科学声明。

### 3.2 三种运行模式

| 模式 | P级 | 输入 | 执行内容 | 对外承诺 |
|---|---:|---|---|---|
| `demo` 一键体验 | P0 | 内置小型真实 AOI + 预训练模型/必要中间数据 | 现场执行窗口化特征读取→RF 推理→TCR→导出→评估 | 无用户密钥、无外网也能完成；不是播放静态图片 |
| `upload` 自带数据 | P1 | 标准 ZIP 数据包 | 校验五类栅格/manifest→预处理→训练或载入模型→完整工作流 | 支持用户自有数据，但严格限制格式、CRS、大小和像元数 |
| `live` 在线小 AOI | P1，可降级 | bbox/GeoJSON/ZIP Shapefile + 日期/云量 | STAC 搜索→候选影像对→用户确认→下载→完整工作流 | 受 Planetary Computer 网络影响；失败不影响 Demo 模式 |

`upload` 的首版标准包固定为：

```text
input_package.zip
├── input_manifest.json
├── aoi.geojson
├── landsat_st_b10_dn.tif
├── landsat_qa_pixel.tif
├── sentinel2_sr_dn.tif
├── sentinel2_scl.tif
└── dem.tif
```

系统不根据文件名猜波段顺序。`input_manifest.json` 的 P0 最低契约如下；`crs/transform/shape/dtype/nodata` 必须同时从 GeoTIFF 读取并与声明交叉验证，示例中的 hash/ID/时间必须替换为真实值：

```json
{
  "schema_version": "1.0",
  "aoi_path": "aoi.geojson",
  "assets": {
    "raw.landsat_st_b10_dn": {
      "path": "landsat_st_b10_dn.tif", "band_names": ["ST_B10"],
      "unit": "DN", "scale": 0.00341802, "offset": 149.0,
      "datetime": "ISO-8601", "item_id": "real-item-id", "sha256": "real-sha256"
    },
    "raw.landsat_qa_pixel": {
      "path": "landsat_qa_pixel.tif", "band_names": ["QA_PIXEL"],
      "unit": "bitfield", "scale": 1.0, "offset": 0.0, "sha256": "real-sha256"
    },
    "raw.sentinel2_sr_dn": {
      "path": "sentinel2_sr_dn.tif", "band_names": ["B02", "B03", "B04", "B08", "B11"],
      "unit": "DN", "scale": 0.0001, "offset": 0.0,
      "datetime": "ISO-8601", "item_id": "real-item-id", "sha256": "real-sha256"
    },
    "raw.sentinel2_scl": {
      "path": "sentinel2_scl.tif", "band_names": ["SCL"],
      "unit": "class_id", "scale": 1.0, "offset": 0.0, "sha256": "real-sha256"
    },
    "raw.dem": {
      "path": "dem.tif", "band_names": ["elevation"],
      "unit": "m", "scale": 1.0, "offset": 0.0,
      "collection": "cop-dem-glo-30|nasadem", "item_id": "real-item-id", "sha256": "real-sha256"
    }
  }
}
```

每个 asset 还须声明 `crs`、6 参数 `transform`、`width/height`、`dtype`、`nodata`；多波段文件的顺序必须与 `band_names` 完全一致，S2 stack 另写 `native_resolution_m_by_band` 和已有重采样历史。manifest 引用只能指向解压后的当前 job 目录，文件数和名称走白名单。缺项、不一致或路径越界时在计算前拒绝，并返回具体修复项。发布时在 `docs/input_contract.md` 将上述结构落成 JSON Schema，并用一个有效包、一个错 band order 包和一个路径越界包做 smoke test。

### 3.3 P0/P1/P2 范围

#### P0：公开 Studio 的硬门槛

- 格网/manifest/artifact 契约；
- TTRI 只拟合训练集；
- 空间 block split；
- affine 驱动的 TTRI/TCR；
- 窗口化 RF 推理与 GeoTIFF 写出；
- 独立预测/约束一致性分栏；
- fail-fast 状态机、取消和 resume 基础；
- 一键 Demo；
- 左对话/右产物 UI；
- ModelScope Gradio 6.2.0 部署；
- 真实 README、输入输出说明和结果下载。

#### P1：提交版应补齐

- 在线小 AOI 或标准上传模式至少完成一个；
- LLM 可选的意图解析与结果解释；
- ModelScope Model/Dataset 资源卡和固定 revision；
- 运行报告、provenance、可复现下载包；
- 公开访问、移动端与双会话隔离验收；
- 后续 Notebook 入口可以先在 README 标为“计划中”，完成后再链接。

#### P2：不阻断首版

- TLC 类方法：以 Guo 等（2022）Three Layers Composition 为已读文献基线，先做论文协议复现，再评估 30→10 m 适配；
- MFGWML 类方法：以 Xu 等（2021）Multi-Factor Geographically Weighted Machine Learning 为已读文献基线，按复合模型 bundle 接入；
- XGBoost/LightGBM/MLP 等基学习器或独立 baseline Benchmark；
- ChromaDB/语义 RAG；
- LLM 受控超参建议；
- UHI、时序、生态响应；
- 青岛/山地等更多 preset；
- COG/STAC/GeoParquet 等更完整 AI-Ready 发布形态。

P2 不是删除，而是必须在 P0 的科学和工程契约稳定后再接入。TLC/MFGWML 的方法卡和禁用态可以随 P0 骨架落库，但在算法、依赖、复现和协议门禁通过前不得在公共 Studio 中显示为“可运行”或“已复现”。

### 3.4 面向评委的 3 分钟体验脚本

1. 打开 Studio，右侧显示空状态和“武汉小区域 Demo”说明。
2. 点击左侧示例卡“生成 10 m LST 并解释结果”。
3. 系统展示数据来源、日期、AOI、预计像元和预训练模型版本。
4. 工作流从 `DATA_READY` 开始，逐步显示预处理、TTRI、RF、TCR、导出、评估的可审计事件。
5. 右侧自动切到地图：在同一色标下切换 Landsat 30 m、RF 10 m、TCR 后 10 m。
6. 指标页分别显示独立预测和 30 m 约束一致性；未计算的指标明确写“未计算”，不互相替代。
7. 血缘页显示 item ID、日期、参数、模型 hash、处理版本。
8. 点击下载，获得 GeoTIFF、preview、metrics、manifest 和日志摘要的 ZIP。

---

## 4. 目标架构：科学工作流与 Agent 解耦

### 4.1 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│ ModelScope Studio / Gradio 6.2.0                             │
│                                                              │
│  左：Chatbot + Sender + Preset     右：Artifact Card          │
│  ─────────────────────────────     ─────────────────────────  │
│  意图/参数/运行/取消/选择影像对       地图/对照/指标/产物/血缘/日志 │
└──────────────────────────┬───────────────────────────────────┘
                           │ structured action
┌──────────────────────────▼───────────────────────────────────┐
│ Studio Event Adapter                                         │
│ session_id ↔ job_id；输入校验；UI update；无业务路径猜测       │
└──────────────────────────┬───────────────────────────────────┘
                           │ artifact key / manifest
┌──────────────────────────▼───────────────────────────────────┐
│ Deterministic Orchestrator                                    │
│ 状态机、stage schema、方法注册表、fail-fast、cancel、resume、hash 校验 │
└──────┬───────────────┬──────────────┬───────────────┬────────┘
       │               │              │               │
   AOI/Acquisition  Preprocess     Method Adapter   Export/Eval
       │               │              │               │
       └─────────────── JobManifest + ArtifactStore ───┘
                           │
                   runs/<job_id>/...

可选 LLM Copilot ──► 只生成/解释 structured action 与结果摘要
                     无 token 时使用规则解析和模板说明
```

### 4.2 Agent 的权限边界

允许：

- 把“用武汉示例做 10 m LST”解析成白名单 action；
- 发现缺少日期、AOI 或模式时向用户提结构化问题；
- 解释每个 stage 的目的、参数和已发生的规则修正；
- 根据 `metrics.json` 生成带事实引用的短结果说明；
- 推荐下一步，例如下载成果、换小 AOI、查看一致性。

禁止：

- 直接拼接或修改绝对文件路径；
- 搜索 output 中的“最新文件”；
- 绕过 manifest 和 schema 调用算法；
- 将失败 stage 标为完成；
- 用自然语言结果覆盖真实 `metrics.json`；
- 执行用户上传的 Python/Skill ZIP；
- 输出或展示隐藏的内部推理链。

页面展示的不是“思考链”，而是**可审计行动日志**：意图摘要、使用参数、规则触发、stage 进度、产物、指标和错误处理。这样既有 Agent 感，也更适合科研复现。

### 4.3 Job 主状态机

不能用一条无条件流水线同时表示“现场训练”和“载入固定模型”。最终实现是共享 job 输入段 + 每个 method_run 独立子图；下面先展开 EasyLST 的两个显式 profile：

```text
job 共享输入段（state.json.stages）
IDLE → VALIDATING
→ [SEARCHING → AWAITING_PAIR_SELECTION → ACQUIRING | DATA_READY]
→ [PREPROCESSING | LOADING_AND_VERIFYING_FEATURES]

EasyLST train profile（state.json.method_runs.<id>.stages）
→ SPLITTING_AND_BUILDING_BASE_SAMPLES
→ PREPARING_TTRI(FIT → BUILD/APPLY)
→ TRAINING_RF

EasyLST pretrained/demo profile（state.json.method_runs.<id>.stages）
→ [LOADING_SPLIT_AND_BUILDING_BASE_SAMPLES（独立评估需要） | SKIPPED（production）]
→ PREPARING_TTRI(LOAD_COEFFICIENTS → BUILD/APPLY)
→ LOADING_RF

该 method_run 的输出段（仍写 method_runs.<id>.stages）
→ INFERENCING → CORRECTING → EVALUATING
→ RENDERING(preview/report) → PACKAGING_METHOD(zip) → METHOD_COMPLETED

仅比较模式（state.json.comparisons.<comparison_id>.stages）
→ COMPARING → PACKAGING_COMPARISON → COMPARISON_COMPLETED

primary 完成且所请求的 comparison 完成 → job COMPLETED
```

Demo 绝不重新拟合 TTRI 或 RF；它加载与 RF hash 匹配的 TTRI 系数和冻结 split，再现场构建 TTRI 栅格、执行窗口推理、TCR、评估、渲染和打包。训练 profile 才会生成两代样本、拟合 TTRI 并训练 RF。

上述状态序列是首个 `easylst_rf_tcr` 方法适配器的确定性 `stage_plan`，不是 UI 对某一种算法的硬编码。`VALIDATING` 阶段先解析本次选中的 method specs，计算所需 source assets/artifact roles 的并集并冻结 `InputPlan`；共享输入段结束后，编排器再执行每个方法自己的白名单 stage。TLC 类可以没有 `TRAINING_RF`，MFGWML 类可以使用多个基学习器、PCA 和 GWR 的复合 stage。不同方法只能通过声明式 capability 影响输入计划，并改变共享输入段之后的方法子图，不能改写 AOI、GridPlan、数据血缘、公共评估器、打包器或前端主骨架。若现有 job 没有新方法必需的源数据/特征，UI 必须提示“新建 job/补齐输入”，不能在原比较运行中静默换数据。

`pretrained+evaluation` 只在模型包携带且 hash 匹配当前数据集的冻结 split 时允许（P0 武汉 Demo 即如此）；任意新 upload/live AOI 若没有匹配 split，不得临时随机切一份后冒充该模型的冻结评估，默认转 `pretrained+production` 并将 independent 标为未计算，或选择 train profile 建立新 evaluation。

异常与控制分支：

- P0 单方法中任一运行态 → `FAILED`：记录 `failed_stage`、错误码、可操作说明，停止后续 stage。
- P2 比较模式中，共享 stage 或 primary method 失败仍令 job `FAILED`；非 primary method 失败只将该 method_run 标为 `FAILED`。确认共享 artifacts/hash 未损坏后可继续后续 method runs；若 primary 完成且 `comparison.json` 成功生成，comparison 状态为 `COMPLETED`、结果写 `partial=true` 和失败理由，job 最终为 `COMPLETED`；否则 job 为 `FAILED`。失败项永不进入排名，这不是忽略错误。
- 取消请求不是新的状态枚举：事件只原子设置 `cancel_requested=true`，当前 job/method/comparison 状态暂时保持 `RUNNING`；每个下载 asset 或栅格 window 边界检查后，将当前作用域及 job 置为 `CANCELLED`。因此不得在 JSON 中写未声明的 `CANCEL_REQUESTED` 状态。
- `resume`：从第一个非 `SUCCEEDED` stage 开始；先复核所有上游 artifact 的 schema 和 SHA256，不通过就回退到其 producer stage。
- `AWAITING_PAIR_SELECTION` 不是阻塞线程等待。系统保存候选和 pending action 后结束本次 Gradio 事件；用户选择后由新事件继续。

每个 stage 的子状态只能是：

```text
PENDING | RUNNING | SUCCEEDED | FAILED | CANCELLED | SKIPPED
```

#### 4.3.1 方法子图的稳定边界

增量方法运行统一表示为：

```text
公共输入/预处理已完成
→ METHOD_VALIDATING
→ METHOD_PREPARING
→ [METHOD_FITTING | METHOD_LOADING | SKIPPED]
→ METHOD_PREDICTING
→ [METHOD_POSTPROCESSING | SKIPPED]
→ METHOD_EVALUATING → METHOD_RENDERING → METHOD_PACKAGING
```

每个 adapter 将内部 stage 映射到上述通用阶段，但不得向编排器返回任意 Python 文件、import 路径或 shell 命令；只能引用代码库内已经注册和测试过的 stage ID。共享 acquisition/preprocessing 每个 job 只执行一次，方法专属的特征选择、模型、系数、中间栅格和输出只能写入自己的 `method_run_id` 命名空间。这样后续新增 TLC/MFGWML 只增加方法 spec、adapter、方法内 stage 和测试，不改变 `app.py`、两栏布局或六个结果 Tab。

### 4.4 会话与持久化

- `gr.State` 只保存浏览器会话需要的轻量信息：服务端生成的 `session_id`、当前 `job_id`、对话消息和 UI 选中项。
- `state.json` 是运行状态、stage 状态和取消标记的唯一真相源；`manifest.json` 只保存输入契约、GridPlan、参数与 artifact registry，不再重复写当前状态。完成 stage 的摘要在最终 `provenance.json` 中快照。

`state.json` 的最低结构固定为：

```json
{
  "schema_version": "1.1",
  "job_id": "uuid",
  "state_revision": 0,
  "status": "IDLE|RUNNING|FAILED|CANCELLED|COMPLETED",
  "current_stage": null,
  "cancel_requested": false,
  "failed_stage": null,
  "error_code": null,
  "method_runs": {
    "server_generated_method_run_id": {
      "status": "PENDING|RUNNING|FAILED|CANCELLED|COMPLETED",
      "current_stage": null,
      "error_code": null,
      "stages": {}
    }
  },
  "comparisons": {},
  "stages": {
    "stage_name": {
      "status": "PENDING|RUNNING|SUCCEEDED|FAILED|CANCELLED|SKIPPED",
      "started_at": null, "finished_at": null, "progress": 0.0
    }
  }
}
```

- `job_id` 必须由服务端生成 UUID；每个 action 都校验当前 session 对 job 的所有权。P0 可规定仅同一会话 resume；若支持刷新/跨会话恢复，必须使用不含路径的高熵签名 resume token，不能允许用户用任意 job ID 读取产物。
- 建立比较后才新增 `state.json.comparisons.<comparison_id>`，其字段固定为 `status/current_stage/error_code/stages`，枚举与 method_run 相同；manifest 只保存比较配置，完成/partial/失败属于 state 与 `comparison.json`。
- `method_run_id` 也由服务端从安全 slug、方法版本和随机后缀生成，不能由浏览器提交路径或 Python entrypoint。`comparison_id` 同样由服务端生成且只引用同 job 的 method runs。`state.json.stages` 只记录共享输入段；方法的评估/渲染/方法打包也属于方法子图，写 `state.json.method_runs.<id>.stages`；最终比较/比较打包写 `state.json.comparisons.<id>.stages`。顶层 `current_stage` 只保存当前活动作用域的完整引用，例如 `method_runs.<id>.METHOD_EVALUATING`。方法/比较配置与 artifact 引用分别写 `manifest.json.method_runs/comparisons`，继续保持状态与不可变血缘分离。
- 所有 state/manifest/artifact registry 更新均使用同目录临时文件、flush/fsync（平台支持时）和原子替换，避免半写状态。
- ModelScope 下的可重建缓存和运行目录放 `/mnt/workspace/geothermoai/`；本地开发默认 `./runtime/`。应用启动时显式创建 `runs/`、`cache/`、`demo_cache/`，执行一次原子写/读/删探针；失败则启动页给出阻断错误。
- `/mnt/workspace` 跨重启持久，但创空间重命名/转移仍可能丢失；最终成果必须同步到 Model/Dataset repo 或由用户下载。
- 用户上传默认属于临时 job，公开服务不默认为其长期保留；Demo 缓存可以持久化。
- 同一 job 只允许一个 `RUNNING` 任务；多方法比较按 method_run 顺序执行，全部完成后才生成 comparison，不在 2vCPU/16GB 基线上并行占用多份模型/栅格内存。首版总并发保守设置，完成实测后再调整。
- 配置 job TTL、单 job/总容量上限和启动/周期清理；下载中的 job 不删除，Demo cache 与用户 job 分目录。删除前必须解析并验证目标仍位于 workdir 的 `runs/` 下。
- 日志对 Secret、带 SAS/query 的签名 URL 和服务器绝对路径做脱敏；provenance 只保留 collection/item/asset 三元组或无签名 canonical URL。

---

## 5. ModelScope 工程目录与旧项目映射

### 5.1 最终 Studio 仓库结构

普通 Gradio 创空间的默认入口必须位于仓库根目录。不要沿用 v2 中 `studio/app.py` 作为默认入口的结构。

```text
GeoThermoAI-ModelScope/
├── app.py                         # 唯一 Studio 薄入口：构建 UI、绑定事件、launch
├── requirements.txt              # Python 依赖；发布版经 clean build 后精确锁定
├── packages.txt                  # 可选；仅在日志证明确需系统包时存在
├── README.md                      # 保留平台生成 YAML；正文为项目卡和 Quick Start
├── .gitignore                     # 排除 runtime/runs/cache/Secret/本地测试输出
├── .gitattributes                 # 仅实际启用 Git LFS 时加入并提交
├── LICENSE                        # 发布收尾项
├── THIRD_PARTY_ASSETS.md          # 外部模型/数据/字体/底图等来源、revision、hash、许可
├── CHANGELOG.md
│
├── geothermoai/                    # 根包布局；无需依赖 IDE/PYTHONPATH 注入
│   ├── __init__.py
│   ├── config.py                   # 环境变量、项目安全阈值、preset
│   ├── contracts.py                # RasterGrid/GridPlan/ArtifactRef/JobManifest/StageRecord/ProtocolSpec
│   ├── grid_ops.py                 # 唯一 affine/CRS/window/reproject 入口
│   ├── job_store.py                # job 目录、原子 state/manifest、artifact lookup
│   ├── method_registry.py           # 静态白名单 MethodSpec/adapter factory；拒绝动态上传代码
│   ├── methods/
│   │   ├── __init__.py
│   │   ├── base.py                  # MethodAdapter/MethodResult 协议
│   │   ├── easylst_rf_tcr.py        # P0：把现有确定性主链接成首个 available adapter
│   │   ├── tlc_guo2022.py           # P2 实现阶段新增；P0 不放假实现
│   │   └── mfgwml_xu2021.py         # P2 实现阶段新增；P0 不放假实现
│   ├── aoi.py                      # bbox/GeoJSON/ZIP SHP 解析、CRS、面积/像元估算
│   ├── acquisition.py              # 活动数据获取后端；候选 pair 与下载
│   ├── preprocessing.py            # 窗口化对齐、掩膜、光谱/地形特征
│   ├── spatial_split.py            # buffered block/留区/留时相
│   ├── ttri.py                     # fit_ttri/apply_ttri/build_ttri_raster
│   ├── rf_model.py                 # model bundle、窗口预测
│   ├── tcr.py                      # canonical-grid 聚合、守恒校正、final10
│   ├── export.py                   # 窗口写 GeoTIFF 与 profile 验证
│   ├── evaluation.py               # independent 与 coarse consistency
│   ├── render.py                   # preview/比较图/feature importance
│   ├── report.py                   # 从 metrics/manifest 生成 Markdown/HTML
│   ├── packaging.py                # 按白名单生成最终 ZIP
│   ├── orchestrator.py             # 确定性 profile/stage 状态机
│   ├── llm_copilot.py              # 可选；无 token 可完全不加载
│   └── studio/
│       ├── ui.py                   # 页面组件树
│       ├── events.py               # Gradio 事件→structured action/orchestrator
│       ├── presenter.py            # manifest/metrics→卡片、图表、地图 HTML
│       └── session.py              # session_id/job_id 绑定
│
├── configs/
│   ├── presets/
│   │   ├── wuhan_demo.json
│   │   └── live_small_aoi.json
│   ├── methods/
│   │   ├── easylst_rf_tcr.json      # P0 完成全部门禁后 available；参数 schema、能力、引用、限制
│   │   ├── tlc_guo2022.json         # P0 可登记 planned 方法卡；无 adapter 时不可运行
│   │   └── mfgwml_xu2021.json       # P0 可登记 planned 方法卡；无 adapter 时不可运行
│   ├── protocols/
│   │   └── wuhan_30to10_holdout_v1.json  # P0 唯一可排名协议；冻结评估与预算语义
│   └── feature_schema.json          # EasyLST P0 schema；方法扩展另引用版本化 feature pool
│
├── demo/
│   └── wuhan_small/
│       ├── demo_manifest.json
│       ├── README.md
│       ├── inputs/                  # 无网运行所需的裁剪基础特征/参考栅格
│       ├── method/
│       │   └── easylst_rf_tcr/      # 受信 MethodSpec、RF/TTRI bundle 与 bundle meta
│       ├── split_map.tif
│       └── optional_dataset_mirror_pointer.json
│
├── assets/
│   ├── logo.png
│   ├── cover.png
│   └── demo_thumbnails/
│
├── scripts/
│   ├── build_demo.py
│   ├── verify_assets.py
│   ├── benchmark_demo.py
│   └── smoke_test.py
│
├── docs/
│   ├── methodology.md
│   ├── contribution_boundary.md          # 既有方法/原型、本次新增、第三方资源的贡献边界
│   ├── input_contract.md
│   ├── deployment.md
│   ├── limitations.md
│   ├── methods/
│   │   ├── easylst_rf_tcr.md
│   │   ├── tlc_guo2022.md                 # 文献基线/复现边界/适配计划，不写成已实现
│   │   └── mfgwml_xu2021.md               # 文献基线/复现边界/适配计划，不写成已实现
│   ├── protocols/
│   │   └── wuhan_30to10_holdout_v1.md      # 人读协议说明；值以对应 JSON/hash 为准
│   └── screenshots/
│
└── tests/
    ├── fixtures/
    │   └── tiny_case/             # 合成 6×6 30m / 18×18 10m，无版权依赖
    ├── test_grid_ops.py
    ├── test_preprocessing_halo.py
    ├── test_ttri_no_leakage.py
    ├── test_tcr_conservation.py
    ├── test_export_order.py
    ├── test_orchestrator.py
    ├── test_method_registry.py
    ├── test_method_namespace.py
    ├── test_protocol_gate.py
    ├── test_comparison_contract.py
    └── test_studio_sessions.py
```

这里明确选择根目录 `geothermoai/` 包布局，根 `app.py` 可直接 `import geothermoai`，不依赖 IDE/PYTHONPATH 注入。clean build 的第一项导入门禁是 `python -c "import geothermoai"`，随后加载两个 JSON preset、方法注册表和 ProtocolSpecs 并验证 hash，再构建 UI。P0 只导入 `availability=available` 的 EasyLST adapter；TLC/MFGWML 的 planned JSON 可以用于方法说明和路线展示，但不得 import 不存在的模块、显示运行按钮或伪造结果。P2 开始某一方法时才创建对应实现文件、专属依赖和测试。只有切换 Docker 时才新增 `.dockerignore`，并排除 `runs/`、cache、测试输出和任何本地 Secret 文件。

### 5.2 旧文件到新模块的迁移表

| 旧文件 | 新位置/处理 | 迁移方式 |
|---|---|---|
| `main.py` | 根 `app.py` | 不迁移 PyWebView；只重建 Gradio 启动入口 |
| `ui/api.py` | `studio/events.py`、`session.py`、`aoi.py` | 拆开业务/状态/上传；删除 webview、tkinter、API Key 回显 |
| `ui/index.html/scripts/styles` | `studio/ui.py` + 少量 CSS | 只复用视觉 token 和交互语义，不搬 JS bridge |
| `core/data_preprocessing.py` | `preprocessing.py` | 保留公式，改为契约/窗口/严格格网 |
| `core/split_dataset.py` | `spatial_split.py` | 随机 split 降级为 debug；新增空间 block |
| `core/ttri.py` | 新 `ttri.py` | 拆 fit/apply/raster；只用 train 系数 |
| `core/rf_model.py` | 新 `rf_model.py` | 接收 NPZ/array 契约；输出 model bundle；窗口预测 |
| `core/tcr.py` | 新 `tcr.py` | 删除 KDTree + `row/3` 主路径；改 affine 格网 |
| `core/lst_final.py` | artifact validator 或删除独立 stage | 最终 LST 由 TCR stage 直接产出 |
| `core/export_geotiff.py` | `export.py` | 窗口写；不依赖 CSV 行序 |
| `core/evaluation.py` | 新 `evaluation.py` | API 直接分 independent/coarse consistency |
| `core/pipeline.py` | `orchestrator.py` | 旧类标 legacy，不再作为 Studio 主链 |
| `core/agent/geo_thermo_agent.py` | `llm_copilot.py` + orchestrator action API | 保留意图和表达层，移除路径猜测和科学状态控制 |
| `core/skills/builtin/data_acquisition_rasterio.py` | 与 GDAL 版本二选一 | 不允许两个同名活动实现长期漂移；按第 10.4 节门禁选择 |

### 5.3 `app.py` 的职责上限

根入口只允许完成：

1. 读取非敏感配置和 Secret 是否存在；
2. 构建 UI；
3. 创建服务对象；
4. 绑定事件；
5. `demo.queue().launch(...)`。

禁止把栅格读取、模型训练、下载、报告生成或长函数直接堆进 `app.py`。这样部署日志和本地测试才能独立定位。

---

## 6. 数据、格网、产物与模型契约

### 6.1 `JobManifest` 最低字段

所有 stage 的唯一数据上下文是 `manifest.json`，而不是某个全局变量或“最新文件”。运行态只以 `state.json` 为准，禁止在两个文件中双写 status。建议用标准库 `dataclass` + 显式校验实现，首版不为契约额外引入 Pydantic。

```json
{
  "schema_version": "1.1",
  "job_id": "uuid",
  "created_at": "ISO-8601 UTC",
  "mode": "demo|upload|live",
  "run_purpose": "evaluation|production",
  "pipeline_profile": "train|pretrained",
  "aoi": {},
  "acquisition": {},
  "input_plan": {"required_source_asset_roles": [], "sha256": "..."},
  "grid_plan": {"grid30": {}, "grid10": {}, "sha256": "..."},
  "parameters": {},
  "method_runs": {
    "easylst_rf_tcr__1_0_0__runid": {
      "method_id": "easylst_rf_tcr",
      "method_version": "1.0.0",
      "availability_at_run": "available",
      "implementation_kind": "native",
      "execution_profile": "pretrained",
      "protocol_id": "wuhan_30to10_holdout_v1",
      "protocol_spec_sha256": "...",
      "namespace": "method_runs/easylst_rf_tcr__1_0_0__runid",
      "input_artifact_keys": [
        "features.lst_30m_K", "features.terrain_30m",
        "features.spectral_30m", "features.spectral_10m", "splits.split_map"
      ],
      "method_parameters": {},
      "method_spec_sha256": "...",
      "method_config_sha256": "...",
      "primary_output_key": "method_runs.easylst_rf_tcr__1_0_0__runid.outputs.primary_lst",
      "metrics_key": "method_runs.easylst_rf_tcr__1_0_0__runid.metrics.primary"
    }
  },
  "comparisons": {},
  "artifacts": {}
}
```

字段要求如下：

| 区块 | 必需字段 |
|---|---|
| `aoi` | `source_type`、`geometry_wgs84`、`bbox_wgs84`、`area_km2` |
| `acquisition` | Landsat/Sentinel/DEM item IDs；两期 datetime；日期差；云量/覆盖阈值；asset keys |
| `input_plan` | 所选 method specs 的 source asset/artifact role 并集、生成版本和 SHA256；运行开始后不可变 |
| `grid_plan.grid30/grid10` | `crs_wkt_or_epsg`、6 参数 transform、width、height、resolution_x/y、bounds、nodata、dtype、nesting |
| `parameters` | 共享 QA/SCL 掩膜、重采样 preset、split/buffer、window、seed 和最小公共样本阈值；不放算法私有参数 |
| `method_runs.<method_run_id>` | `method_id/version`、运行时 availability、`implementation_kind`、execution profile、`protocol_id + protocol_spec_sha256`、namespace、输入 keys、方法参数、spec/config hash、primary output/metrics key；EasyLST 的 TTRI/RF/TCR 参数放这里 |
| `comparisons.<comparison_id>` | server-generated comparison ID、同 job 候选/primary run、ranked/visual-only、共同 protocol ID/hash 或 null、初始拒排原因；完成/partial 结果只写 comparison artifact/state，不回写 manifest |
| `artifacts.<key>` | `relative_path`、`media_type`、`sha256`、`size_bytes`、`producer_stage`、`upstream_keys`；栅格另加 band names/unit/scale/offset/dtype/nodata/grid key |

路径只保存 job 根目录下相对路径；绝对路径不得发送到浏览器或写入可下载 provenance。

#### 6.1.1 `ProtocolSpec`：排名协议不能只靠字符串

`configs/protocols/<protocol_id>.json` 是独立、版本化、可哈希的评估/比较协议，至少冻结：

```text
protocol_id / version / track / run_purpose
coarse_grid_role / target_grid_role / supported_grid_ratio
reference_artifact_role
split_policy / buffer_policy / held_out_policy / constraint_policy
required_source_asset_pool / candidate_feature_pool_id
leakage_rules / reference_access_rules / halo_rules
evaluator_version / metric_definitions / denominators / minimum_sample_rules
tuning_data_boundary / tuning_budget / seed_and_repeat_policy
runtime_measurement_rules
```

创建比较时，`manifest.comparisons.<comparison_id>` 最低字段固定为：

```text
comparison_id / comparison_mode = ranked | visual_only
candidate_method_run_ids[] / primary_method_run_id
common_protocol_id / protocol_spec_sha256
initial_not_ranked_reasons[]
```

spec 通过项目统一 canonical JSON 序列化（UTF-8、key 排序、紧凑分隔符、禁止 NaN）后计算 SHA256。method run、primary metrics 和 comparison 必须同时保存 `protocol_id` 与 `protocol_spec_sha256`；排名门禁比较 hash，而不是只比较可能同名但内容已变的 ID。任何 split/buffer、reference、候选特征池、防泄漏规则、evaluator 或调参预算变化，都必须创建新 protocol version/hash。

`comparison_id` 由服务端生成，候选列表至少含两个 method runs，且必须全部属于同一 `job_id`。若候选的 protocol ID 或 hash 不完全一致，仍可在“对照”Tab 做 `visual_only` 展示，但 manifest 固定写 `comparison_mode=visual_only`、`common_protocol_id=null`、`protocol_spec_sha256=null`、`initial_not_ranked_reasons=["protocol_mismatch"]`；公共 gate 在最终 `comparison.json` 再写逐项拒排理由，绝不生成排名。单方法页面不创建 comparison；只有用户明确比较或 benchmark preset 才建立 `comparisons.<comparison_id>`。

### 6.2 统一栅格与单位

#### 6.2.1 原始量与物理量禁止混名

| Artifact | 存储类型 | 单位/值域 | 规则 |
|---|---|---|---|
| `raw.landsat_st_b10_dn` | 原始整数 | DN | Collection 2 L2 fallback 定标 `K = DN×0.00341802 + 149.0`；优先核对 asset 元数据 |
| `raw.sentinel2_sr_dn` | 原始整数 | DN | `reflectance = DN/10000` |
| QA / SCL | uint16 / uint8 | bit field / class ID | 绝不做连续插值 |
| `features.lst_30m_K` | float32 | K | 只能由 ST_B10 DN 定标后生成；TCR/评估禁止直接读 raw DN |
| Sentinel 反射率特征 | float32 | `[0,1]` | 无效像元走 mask；外部 NoData `-9999` |
| DEM / Slope / Aspect | float32 | m / degree / degree | 外部 NoData `-9999` |
| TTRI / TCR / 各方法 primary LST | float32 | K | 外部 NoData `-9999`；内存可用 NaN；目标 grid 写在 artifact metadata |
| valid/split/constraint mask | uint8 | 布尔或枚举 | 0 的语义在 schema 中明确 |

科学计算和 GeoTIFF 始终以 K 保存。UI 可以额外显示 °C，但必须明确写 `°C = K - 273.15`，不能改变下载数据单位。

#### 6.2.2 `GridPlan` 的唯一生成算法

P0 小 AOI 不能让五个输入各自决定目标格网。固定算法如下：

1. bbox/geometry 先转 WGS84；按 AOI 质心确定 UTM zone，北半球用 EPSG:326xx、南半球用 EPSG:327xx。
2. P0 拒绝跨反经线、跨多个 UTM zone 或超项目像元上限的 AOI；upload 若必须使用其他投影，只能由受测 preset 显式指定 projected CRS。
3. 将 AOI bounds 投影到目标 CRS；以该 CRS 的 `(0,0)` 为 30 m anchor，执行 `left/bottom=floor(coord/30)×30`、`right/top=ceil(coord/30)×30`，向外 snap。
4. `grid30` 使用 snap 后 bounds、30 m 像元和 north-up transform；`grid10` 与它共享 CRS、左上角和 bounds，分辨率 10 m，`width/height` 严格为 grid30 的 3 倍。
5. 所有 source→canonical warp 必须显式传同一 `dst_crs/dst_transform/width/height`，不能只传 resolution 后让库自行推断边界。
6. GridPlan 的算法版本、两个 grid、AOI mask 和 SHA256 写入 manifest；模型元数据记录该版本和预处理 preset hash。

`RasterGrid/GridPlan` 至少实现并测试：

- 像元中心 row/col ↔ map coordinate；
- bounds、shape、resolution 和 CRS 一致性；
- window 迭代；
- source grid→canonical grid 的显式 reproject/resample；
- grid30/grid10 同 CRS、同 origin、严格 3×嵌套检查；
- north-up 检查。若首版不支持旋转/错切 transform，应显式拒绝，而不是默默错误计算。

### 6.3 任务目录与固定 artifact key

```text
runs/<job_id>/
├── manifest.json
├── state.json
├── inputs/
│   ├── aoi.geojson
│   └── pair_candidates.json
├── raw/
│   ├── landsat_st_b10_dn.tif
│   ├── landsat_qa_pixel.tif
│   ├── sentinel2_sr_dn.tif
│   ├── sentinel2_scl.tif
│   └── dem.tif
├── features/
│   ├── lst_30m_K.tif
│   ├── terrain_30m.tif
│   ├── spectral_30m.tif
│   ├── spectral_10m.tif
│   ├── valid_30m.tif
│   └── valid_10m.tif
├── splits/
│   ├── split_map.tif
│   └── constraint_mask.tif
├── samples/
│   ├── base_train.npz
│   ├── base_val.npz
│   └── base_test.npz
├── method_runs/
│   └── <method_run_id>/
│       ├── method_spec.json              # 当次解析后的冻结方法卡
│       ├── samples/                      # 方法专属样本；TLC 类可为空
│       │   ├── model_train.npz
│       │   ├── model_val.npz
│       │   └── model_test.npz
│       ├── bundle/                       # none/parameters/single_model/composite 均可
│       │   ├── ttri_coefficients.json    # 下列三项仅为 EasyLST 示例
│       │   ├── rf_model.joblib
│       │   └── bundle_meta.json
│       ├── intermediates/                # TTRI、原始预测、TCR、PCA/GWR/残差等方法私有产物
│       │   ├── ttri_30m.tif              # EasyLST 示例
│       │   ├── ttri_10m.tif
│       │   ├── lst_raw_10m_K.tif
│       │   ├── holdout_prediction.npz
│       │   ├── tcr_30m_K.tif
│       │   └── tcr_10m_K.tif
│       ├── outputs/
│       │   └── primary_lst_K.tif         # UI 通过 role 读取，不假设算法名称
│       ├── metrics/
│       │   └── primary.json
│       ├── previews/
│       │   ├── preview.png
│       │   └── method_specific/          # 可选；只展示已声明产物
│       ├── provenance.json
│       ├── report.md
│       └── geothermoai_<method_run_id>.zip
├── comparisons/
│   └── <comparison_id>/
│       ├── comparison.json               # protocol ID/hash、gate 决定、partial、排名/拒排理由
│       ├── metrics_table.json
│       ├── preview.png
│       └── geothermoai_comparison_<comparison_id>.zip
└── logs/
    └── job.log
```

调用方只能请求如 `raw.landsat_st_b10_dn`、`features.lst_30m_K`、`samples.base_train`、`method_runs.<run_id>.bundle.rf_model`（EasyLST 示例）、`method_runs.<run_id>.outputs.primary_lst` 这样的 artifact key。`JobStore` 将 key 解析为经过验证的相对路径。方法运行不得写全局 `models/` 或 `outputs/`，因此第二种方法不会覆盖第一种方法；P0 迁移期间如必须兼容旧 key，只能在 artifact registry 中建立带 `alias_of` 的只读别名，不能复制文件或搜索“latest”。

### 6.4 EasyLST 训练样本与 bundle

split 与 TTRI 之间必须保留两代压缩 NPZ，禁止原地覆盖：

```text
base_train/base_val/base_test.npz
  X_spectral       float32 [n, 8]
  terrain          float32 [n, 3]  # DEM_m, Slope_deg, cosAspect
  y_K              float32 [n]
  row30 / col30    int32   [n]
  block_id         int32   [n]

method_runs/<method_run_id>/samples/model_train/model_val/model_test.npz
  X_model          float32 [n, 9]  # X_spectral + TTRI
  y_K / row30 / col30 / block_id  # 与对应 base artifact 一致
```

split stage 只写共享 base NPZ；EasyLST 的 TTRI stage 只从 `base_train.terrain + base_train.y_K` 拟合，再用同一系数为三份 base 样本追加 TTRI，写入该 EasyLST method namespace 的 model NPZ。两代 artifact 分别记录 upstream hash。EasyLST 训练从 `spectral_30m + TTRI30` 采样，推理从 `spectral_10m + TTRI10` 读取。

特征顺序不得靠数组位置口头约定，唯一来源是 EasyLST `bundle_meta.json.feature_names`。首版八个光谱特征和一个 TTRI 与现有代码一致：

```text
R, G, B, NIR, SWIR1, NDVI, NDWI, NDBI, TTRI
```

EasyLST P0 的映射固定为 `R=B04, G=B03, B=B02, NIR=B08, SWIR1=B11`。当前代码没有实际使用 B12/SWIR2，EasyLST 的 README、报告和 UI 不得写成十特征或声称包含 B12。30 m/10 m 两份 spectral raster 使用同一 band order 和 feature schema hash。未来 MFGWML 类若按文献实现，可在自己的版本化 feature pool 中声明 SWIR2、更多指数、地形或变换特征；这不等于修改 EasyLST 的九维 schema，也不得在这些输入尚未获取、校验和实现时宣称已经支持。

`ttri_coefficients.json` 至少包含：

```text
formula_version
fit_split_id
intercept
coef_dem
coef_slope
coef_cos_aspect
n_samples
fit_metrics
training_artifact_sha256
terrain_schema_sha256
```

EasyLST `bundle_meta.json` 至少包含：

```text
model_type
feature_names / feature_units
python_version / sklearn_version
hyperparameters
random_seed
split_id
ttri_coeff_sha256
training_data_sha256
preprocessing_schema_sha256
grid_plan_schema_version
training_grid_plan_instance_sha256
grid_contract_sha256
mask_resampling_preset_sha256
model_file_sha256
created_at
```

加载模型前逐项比较 feature schema、sklearn 兼容版本、`grid_contract_sha256`、预处理兼容规格和 TTRI 系数 hash；不一致时拒绝推理并提示重新训练/选择匹配模型。`training_grid_plan_instance_sha256` 只用于训练血缘，包含 AOI-specific CRS zone/bounds/shape，跨 AOI 不要求相等；`grid_contract_sha256` 只包含模型真正依赖的 schema version、米制 north-up、30/10 m 分辨率、严格嵌套、单位、特征/mask/resampling 版本。固定 Demo 可进一步要求完整 GridPlan instance hash 相等。`joblib.load` 具有 pickle 同等执行风险：公共服务只加载团队内置或固定 ModelScope revision、SHA256 匹配的受信模型；upload ZIP 不允许携带或覆盖 joblib，P0 不支持用户自定义模型。后续 composite bundle 中任何可执行反序列化格式遵守同一规则；每个文件、bundle manifest 和 loader 版本都要校验，不能因方法换成 MFGWML 就放宽。

正式 preset 至少设置 `min_train_blocks=4`、`min_val_blocks=1`、`min_test_blocks=1`、`min_ttri_samples=100`、`min_rf_train_samples=500`、`min_eval_samples=30`；tiny fixture 用专用更小阈值。TTRI 的设计矩阵必须全部有限且 `rank([1, DEM, Slope, cosAspect])=4`，否则 fail-fast。实际阈值可经实验修订，但必须版本化，不得静默放宽。

### 6.5 指标契约

项目没有真实 10 m LST 标签。“独立预测”固定指：在未进入 TTRI 拟合、RF 调参和 TCR constraint 的 held-out 30 m cells 上，将指定 10 m 预测按同一 `Agg` 回聚合到 grid30，与 `features.lst_30m_K` 比较。它是独立的粗尺度 hold-out 验证，不是 10 m 真实温度精度。

每个方法运行的 `method_runs/<method_run_id>/metrics/primary.json` 固定分成以下区块并写清方法、协议和评估对象。本文其他章节为简洁而写的 `metrics.json`，均指当前 primary method run 的这个文件，不代表全局可被覆盖的单例：

```json
{
  "schema_version": "1.1",
  "method_run_id": "easylst_rf_tcr__1_0_0__runid",
  "method_id": "easylst_rf_tcr",
  "method_version": "1.0.0",
  "protocol_id": "wuhan_30to10_holdout_v1",
  "protocol_spec_sha256": "...",
  "ranking_eligible": true,
  "not_ranked_reasons": [],
  "protocol_gate_version": "1.0",
  "protocol_checked_at": "ISO-8601 UTC",
  "independent_prediction": {
    "definition": "30m coarse held-out validation; not 10m ground truth accuracy",
    "computed": true,
    "evaluation_grid": "grid30",
    "reference_artifact_key": "features.lst_30m_K",
    "split_id": "...",
    "constraint_mask_id": "...",
    "rf_30m_holdout": {
      "prediction_artifact_key": "method_runs.easylst_rf_tcr__1_0_0__runid.intermediates.holdout_prediction", "n": 0, "coverage": 0.0,
      "R2": null, "MB_K": null, "MAE_K": null, "RMSE_K": null
    },
    "downscaled_10_to_30_holdout": {
      "prediction_artifact_key": "method_runs.easylst_rf_tcr__1_0_0__runid.intermediates.lst_raw_10m", "n": 0, "coverage": 0.0,
      "R2": null, "MB_K": null, "MAE_K": null, "RMSE_K": null
    }
  },
  "coarse_consistency": {
    "definition": "constraint-cell 10m-to-30m conservation",
    "computed": true,
    "prediction_artifact_key": "method_runs.easylst_rf_tcr__1_0_0__runid.outputs.primary_lst",
    "reference_artifact_key": "features.lst_30m_K",
    "constraint_mask_id": "...",
    "n": 0,
    "coverage": 0.0,
    "valid_fraction": 0.0,
    "MB_K": null,
    "MAE_K": null,
    "RMSE_K": null
  },
  "distribution": {
    "artifact_key": "method_runs.easylst_rf_tcr__1_0_0__runid.outputs.primary_lst",
    "valid_mask_key": "features.valid_10m",
    "min_K": null,
    "p01_K": null,
    "median_K": null,
    "p99_K": null,
    "max_K": null,
    "nodata_fraction": null
  },
  "runtime": {"wall_time_s": null, "peak_ram_mb": null},
  "method_specific": {}
}
```

分母固定如下：independent 的 `n` 是成功匹配的有效 held-out 粗像元数，`coverage=n/held-out 中有效参考粗像元总数`；coarse 的 `n` 是参与约束且能有效回聚合的粗像元数，`coverage=n/constraint mask 中有效参考粗像元总数`，`valid_fraction` 是这些粗像元内“有效细像元数/理论细像元数”的总体比例。R² 在 `n<2`、低于 preset 最小评估样本或参考方差近零时写 null，并记录 `not_computed_reason`。

若 held-out 块参与了任一方法的拟合、特征选择、PCA/GWR 融合、调参或约束后处理，或 production run 使用全部有效 30 m cells 做约束，则 `independent_prediction.computed=false`，UI 显示“本次未进行独立验证”。若需要展示先前独立结果，必须引用冻结的 evaluation run/bundle/split ID，不得把一致性值复制过去。`method_specific` 只能保存方法特有的诊断量，不能混入跨方法排名列。

### 6.6 每个 stage 的唯一输入/输出

下表把共享 stage 与 EasyLST 首个 adapter 的具体 stage 同时列出；未来方法只能用第 7.9 节的 adapter 契约替换方法子图，不能改变共享 stage 的语义。`evaluate/render/package` 虽复用公共函数，但每次调用都属于具体 method_run 并写其嵌套状态；多方法完成后的 `compare/package_comparison` 属于 comparison 状态，不复用某个方法的 stage key。

| Stage | 输入 | 输出 | 失败条件示例 |
|---|---|---|---|
| `validate_aoi` | bbox/GeoJSON/ZIP SHP | `inputs/aoi.geojson` + manifest.aoi | CRS 缺失、几何无效、面积/像元超限 |
| `resolve_methods_and_inputs` | method IDs/versions + registry + protocol specs + AOI/mode | 冻结 method/protocol specs 与 hashes、server-generated run IDs、InputPlan、stage plans | planned/依赖缺失/协议冲突/hash 或输入角色无法满足 |
| `search_pairs` | AOI、日期、云量 | `pair_candidates.json`，状态等待选择 | 无候选、覆盖不足、网络失败 |
| `acquire` | 已选 pair | `raw/*.tif` + acquisition 血缘 | asset 缺失、日期/波段/覆盖不符 |
| `preprocess` | raw + GridPlan + preset | LST_K、terrain30、spectral30/10、mask | grid/CRS/shape/单位不一致 |
| `split` | spectral30/terrain/LST/mask + buffered block 参数 | split/constraint mask + base NPZ | 任一 split/buffer 样本不足 |
| `load_split_samples` | 冻结 split/config/hash + spectral30/terrain/LST/mask | 经验证的 split/constraint mask + base NPZ | split/grid/model hash 不匹配 |
| `prepare_ttri_fit` | base_train + terrain rasters | 当前 method namespace 内的系数、TTRI30/TTRI10、model NPZ | val/test label 被误用、矩阵秩不足 |
| `prepare_ttri_load` | 受信系数 + terrain + 可选 base样本 | 当前 method namespace 内经 hash 验证的 TTRI30/10；有 base 时另写 model NPZ | 系数/model/grid schema 不匹配 |
| `train_rf` | 当前 method namespace 的 model_train/model_val | namespace 内 joblib + bundle meta + val 指标 | feature schema 不一致 |
| `load_rf` | model bundle | 已验证模型对象 | 版本/hash/schema 不匹配 |
| `infer` | model + 10 m feature windows；evaluation 时另含 model_test | namespace 内 raw 10 m + 可选 holdout prediction | 窗口失败、取消 |
| `tcr` | EasyLST raw10 + LST_K30 + constraint mask + canonical grids | namespace 内 TCR 中间量 + `outputs.primary_lst` | 子像元有效率不足、非嵌套格网 |
| `evaluate` | adapter 的 primary prediction + held-out/constraint 定义 | namespace 内 primary metrics + provenance stage snapshot | 评估对象、protocol 或分母不明确 |
| `render` | adapter 声明的 rasters + metrics + manifest | namespace 内 preview/对比图/可选诊断/report | 产物不可读、数值来源缺失 |
| `package` | 当前 method run 白名单 artifact keys | 方法 ZIP；比较模式另生成 comparison ZIP | 缺必需产物；ZIP 自身被递归加入 |
| `compare` | 同 job method results + ProtocolSpecs + gate decisions | `comparisons/<id>/comparison.json` + metrics table/preview | 跨 job；ranked 时 protocol ID/hash 不同；primary 未完成 |
| `package_comparison` | comparison 白名单 + 各方法下载引用 | comparison ZIP | 包含其他 job/未声明文件；递归包含自身 |

任何 stage 只接受 artifact key 和明确参数，不接受“某个可能存在的 CSV 路径”。

---

## 7. 科学计算改造：按文件直接施工

### 7.1 P0-1：AOI 与数据入口

新建 `geothermoai/aoi.py`，完成：

1. bbox 必须按 `[west, south, east, north]` 验证，范围限于 WGS84 合法经纬度。
2. P0 GeoJSON 按 RFC 7946 只接受 WGS84 经/纬度；发现 legacy `crs` 且不是 WGS84 时明确拒绝并提示转为 WGS84，不能猜测。
3. Shapefile 只接受 ZIP，检查 `.shp/.shx/.dbf/.prj`；禁止只上传 `.shp`。
4. 解压时校验每个目标路径仍在 job 临时目录内，限制文件数和展开总字节，防止路径穿越/压缩炸弹。
5. 使用源 CRS 读取并重投影为 EPSG:4326；修复无效几何或明确拒绝。
6. 再投影到预计目标 CRS 后计算 area/10 m pixel count，不能直接按经纬度 degree 估面积。
7. 运行前显示预计面积、像元、模式和是否需要联网，用户确认后才继续。

项目初始安全阈值：

- Demo 目标约 `512×512` 个 10 m 像元（约 5.12 km×5.12 km）；
- 在线/上传硬上限初始设为 `1024×1024` 个 10 m 像元；
- 这些是**项目安全配置，不是 ModelScope 官方限制**；上线后必须依据 2vCPU/16GB 实测调整；
- 上传字节阈值通过 `GEOTHERMO_MAX_UPLOAD_MB` 配置，最终值由部署基准锁定。

### 7.2 P0-2：数据获取与候选影像对

保留 Landsat/Sentinel 主 collection，并修正现代码的 DEM fallback：

```text
landsat-c2-l2
sentinel-2-l2a
cop-dem-glo-30  -> asset `data`（首选）
nasadem         -> asset `elevation`（可选 fallback）
```

Planetary Computer 没有名为 `srtm` 的 collection；现代码把 `srtm` 与统一 asset `data` 绑定会必然失败。所谓 SRTM 只能作为 NASADEM 的数据来源描述，不能再作为 collection ID。collection→asset 映射必须是受测配置；每个活动 DEM 后端至少用一个真实 item 做 asset schema 集成测试。

施工要求：

1. 统一代码、注释和 UI 中的 `cloud_threshold`、`coverage_threshold`；不得出现注释 20%、执行回退 50% 等漂移。
2. bbox 与多边形都要计算真实 AOI 覆盖率；不能将 bbox 覆盖直接写成 1.0。
3. 候选表至少显示 item ID、日期、日期差、云量、AOI 覆盖和数据源。
4. 用户选择前状态为 `AWAITING_PAIR_SELECTION`；选择后将完整 candidate 写 manifest。
5. 下载后逐 asset 验证可读、波段数、dtype、NoData、bounds、CRS 和数据日期。
6. 每个 STAC item ID、asset key、canonical unsigned URL（若可公开）、etag/size（若能得到）、下载时间和 hash 写入 provenance；带 SAS/query/token 的签名 href 不落盘、不进日志。
7. 并发下载是可配置优化，不宣称固定 40–60% 收益。初始 `MAX_DOWNLOAD_WORKERS=2`，记录实际时延、失败和重试后再调。
8. Demo 模式不得依赖 Planetary Computer 在线可用；live 失败必须给出切换 Demo/上传模式的按钮。

### 7.3 P0-3：窗口化预处理

重构现有 `core/data_preprocessing.py` 时保留定标和指数，但所有输入都必须 warp 到第 6.2 节唯一 GridPlan，不能分别按 shape 或 resolution 推断目标。P0 重采样/定标表固定如下：

| 输入/产物 | 目标 | P0 规则 |
|---|---|---|
| Landsat ST_B10 DN | grid30 | 先处理 fill/无效值，再用连续型 bilinear warp；按元数据或 fallback `DN×0.00341802+149.0 K` 定标 |
| Landsat QA_PIXEL | grid30 | nearest；默认剔除 bits 0–4（fill、dilated cloud、cirrus、cloud、cloud shadow） |
| S2 B02/B03/B04/B08 | grid10 | `/10000` 后按连续反射率 bilinear warp |
| S2 B11/SWIR1（原生 20 m） | grid10 | `/10000` 后 bilinear；报告必须注明这是插值得到的 10 m SWIR1 |
| S2 各连续反射率 | grid30 | 从各源 band 直接以 average warp 到 grid30，不聚合已经算好的指数 |
| S2 SCL | grid10 | nearest；默认有效类 4/5/6，其余剔除 |
| DEM | grid30 | bilinear；保留 m |
| TTRI30→TTRI10 | grid10 | bilinear连续场 |
| QA/SCL/valid/split/constraint mask | 对应格网 | nearest；valid30 对细格有效率另按明确阈值聚合 |
| TCR30→TCR10 | grid10 | 第 7.7 节的粗像元内常数复制，不用 bilinear |

连续波段分别落到 grid30/grid10 后，再在各自格网上计算 NDVI/NDWI/NDBI；因此输出 `spectral_30m.tif` 和 `spectral_10m.tif`，band order/schema 相同。所有 QA bits、SCL 类别、定标和 resampling 规则写入版本化 preset 与 manifest，修改即改变 preset hash。

坡度/坡向只在 canonical grid30 上计算，使用 transform 中的真实 x/y spacing，删除硬编码 30 m。窗口化地形导数必须在每个 512 窗口外读取至少 1 个 grid30 像元 halo，计算后裁回中心；NoData 邻域规则固定，并用跨窗口 fixture 与整图结果比较，避免接缝。

全图特征保持 GeoTIFF/窗口数组，不写 10 m 全图 CSV。训练样本从空间块分层抽样，默认最多 200,000 条、固定 seed；在线公共 Studio 默认载入预训练模型。默认窗口 `512×512`，边缘窗口按实际大小处理，每个窗口边界检查取消标记。

初始内存估算要写入 benchmark：12 个 float32 的 512² 基础数组约 12 MiB，但必须给 GDAL/rasterio、mask、拷贝、模型和 Python 留出实测余量，不能只用理论数组值当峰值 RAM。

### 7.4 P0-4：空间拆分

用 `spatial_split.py` 替代主路径中的逐行随机拆分：

1. 正式模式使用连续 held-out 区域或 buffered spatial blocks；不能只把相邻小 block 随机分组后就称为独立。
2. block size/buffer 用物理距离表达，并依据经验变程或受审查 preset 确定；train 与 test 至少隔一个配置化 buffer，buffer 样本不参与拟合、调参、TCR 约束或指标。
3. 用固定 seed 分配 block，输出 `split_map.tif`（invalid/train/val/test/buffer）和三份 `base_*.npz`；页面/报告显示各类 block 数、像元数、物理尺寸和覆盖。
4. 在生成 artifact 前执行第 6.4 节最小 block/样本门禁；tiny fixture 使用固定、非共线 terrain 与专用 split，不能依赖随机碰巧通过。
5. 若做跨区/跨时相实验，则 held-out 区域或时相完全不进入训练。
6. 旧随机 60/20/20 只保留 `debug_random` 模式，页面不得用它生成正式“独立泛化”结论。

### 7.5 P0-5：TTRI 纠错

现有公式保留：

```text
LST = intercept + a·DEM + b·Slope + c·cos(Aspect)
TTRI = a·DEM + b·Slope + c·cos(Aspect)
```

实现拆成三个显式函数：

```text
fit_ttri(base_train) -> ttri_coefficients.json
apply_ttri(base_samples, coefficients) -> model_samples
build_ttri_raster(terrain_rasters, coefficients, grid30, grid10)
  -> ttri_30m.tif + ttri_10m.tif
```

硬约束：

- 只允许 `base_train.y_K` 进入 `fit_ttri`，设计矩阵必须有限、rank=4 且满足最小样本门禁；
- base_val/base_test 只能作为 `apply_ttri` 的地形输入，修改它们的 LST 标签不能改变 TTRI；
- TTRI30 从完整地形格网生成，不从 `step=2` 稀疏 CSV 拼网格；
- TTRI30→TTRI10 使用地理 transform 和目标 grid 的 bilinear reproject；
- Aspect 以 degree 存储，固定用 `cosAspect = cos(deg2rad(Aspect_deg))`；平坦/NoData 像元规则写入 terrain preset；
- 系数、截距、样本数、fit split/hash 和训练拟合指标写 JSON。

### 7.6 P0-6：RF 模型包与窗口推理

保留 scikit-learn Random Forest 作为首版唯一主模型。施工要求：

1. 训练只读取当前 EasyLST method namespace 内契约化的 `model_train/model_val.npz`；test 不参与调参。
2. 模型参数来自 preset，并完整写 `bundle_meta.json`。
3. `n_jobs` 不能默认 `-1`；按 2vCPU 基线设置可配置上限。
4. 预测从 10 m 特征窗口读取，校验 feature order、mask 和模型 schema。
5. 直接按窗口写当前 namespace 的 `intermediates/lst_raw_10m_K.tif`，不先生成全场景 CSV。
6. 每个窗口结束检查取消状态并更新 stage progress。
7. 训练和加载模型是互斥分支：Demo 默认加载固定模型、匹配的 TTRI 系数和冻结 split，绝不现场重拟合；upload/live 可按配置选择轻量重训。

旧文档中 RF `n_estimators` 存在 200/400 不一致。最终值不靠文案拍定：选择一个 preset 值，连同 seed、split 和模型 hash 写进 bundle meta；所有指标自动读取这一版本。

### 7.7 P0-7：TCR 格网实现与指标边界

保留残差约束思想，但把“参考值”“约束格”和守恒算子写死。P0 只支持第 6.2 节严格嵌套的 canonical grids：

```text
R_30m(j) = LST_ref_30m(j) - Agg_valid(LST_RF_10m within coarse cell j)
TCR_10m(i) = R_30m(parent_grid30_cell(i))
LST_final_10m(i) = LST_RF_10m(i) + TCR_10m(i)
```

改造要求：

- `LST_ref_30m` 是 Landsat ST 参考，不称“真实 10 m ground truth”；
- 使用 GridPlan/affine 找 parent cell 并做带有效权重的 average，删除业务代码中的 `row/3` 和 KDTree 主路径；严格 3×是契约校验，不是到处手写索引比例的理由；
- 对每个粗像元统计有效 10 m 子像元数/比例；低于 `min_valid_fraction` 时该粗像元不参与约束；
- TCR30 是带 constraint mask 的 30 m 栅格；TCR10 在约束粗像元内复制常数残差，保证回聚合守恒。P0 不使用 bilinear TCR，也不做无限最近邻补洞；未约束区的 TCR artifact 写 NoData，合成 final 时 correction 按 0 处理，因此 evaluation run 中保持 RF 原值，并由 constraint mask 明确标记；
- 当前 namespace 的 `outputs/primary_lst_K.tif` 由 TCR stage 直接写出；旧 `lst_final.py` 只保留验证职责；
- 对参与 TCR 的 30 m 单元，以非均匀残差、NoData、低有效率和边界用例验证 `abs(Agg(final10)-LST_ref30)<1e-4 K`；
- 若未来要求平滑 TCR，必须先实现 area-overlap conservative operator 或插值后逐粗像元再归一化，并另立版本；普通 bilinear reproject 不得声称严格守恒。

`run_purpose` 决定 constraint mask：

- `evaluation`：TTRI 只用 train，RF 调参只用 val，TCR constraint 默认只允许 train；test 永不参与。独立指标默认评估 EasyLST `intermediates.lst_raw_10m` 回聚合后的 test 结果；若要评估其他 artifact，必须单列 method_run/artifact key 和无泄漏证明。
- `production`：冻结 evaluation/model 后，可用全部有效 30 m cells 生成最终 TCR 产品；当前 run 的 `independent_prediction.computed=false`，只报告 coarse consistency。页面若展示先前独立指标，必须标出所引用的 evaluation run/model/split。
- manifest 必须写 `constraint_split_ids`、`constraint_mask` hash、`run_purpose`、`min_valid_fraction` 和聚合算子版本。

### 7.8 P0-8：导出、预览和报告

- `INFERENCING` 按窗口写 EasyLST raw10，`CORRECTING` 写该 namespace 的 TCR30/TCR10/primary LST；两者都检查窗口 row/col/bounds，禁止依赖 CSV 当前行序。
- 输出 profile 明确 CRS、transform、width、height、dtype、compression、nodata。
- COG 是 P2 增强；若首版还未做有效 COG 校验，文件名和文案只写 GeoTIFF，不冒充 COG。
- `EVALUATING` 先生成 metrics/provenance；`RENDERING` 再生成 preview、对比图、feature importance 和 report；`PACKAGING` 最后生成 ZIP。resume 分别判断这三个 stage，不能再合并成 export/evaluate。
- preview 从降采样数组生成，最大边控制在约 1024 像素，不为预览读完整 2 亿像元。
- 30 m/10 m 对照使用同一有效区和同一色标；计算使用 K，UI 可切换 °C。
- `report.md` 的全部数值来自 `metrics.json` 和 manifest；LLM 只能增加被标记的解释段，不能改数值。
- 方法 ZIP 按 method_run artifact-key 白名单打包，至少包含 primary GeoTIFF、preview、metrics、method spec、manifest/provenance、报告和版本信息；排除 ZIP 自身、其他方法、临时文件、raw 大文件和日志中的敏感字段。

### 7.9 P2 方法族增量接入与异构 Benchmark

v2 中“遍历注册组并执行所有模型”的接口过窄：TLC 类可以是没有全局训练模型的影像滤波/合成流程，MFGWML 类则是多个基学习器、PCA 与空间回归构成的复合 bundle，二者都不能被强行伪装成一个 `fit → joblib → predict` 的单模型。最终采用“静态方法注册表 + 方法适配器 + 协议门禁 + 方法运行命名空间”，而不是让 Agent 或用户动态上传插件代码。

#### 7.9.1 已读方法的准确指代与本项目边界

| 方法族 | 本地文献依据 | 能确认的论文任务 | 本项目中的首个接入目标 | 当前状态 |
|---|---|---|---|---|
| EasyLST | 现有代码、创新报告和开发文档 | TTRI + RF + TCR 的 30→10 m 工作流 | `easylst_rf_tcr@1.0.0`，作为 adapter 契约的第一个真实实现 | P0 `available`（完成本规划纠错后） |
| TLC | Guo, Hu & Schlink, 2022, *A new nonlinear method for downscaling land surface temperature by integrating guided and Gaussian filtering*, DOI `10.1016/j.rse.2022.112915` | Three Layers Composition；以二维三次卷积插值（cubic convolution interpolation）、引导滤波和 Gaussian 低通从粗 LST 与细 predictor/LULC 信息组织大尺度、细节和边界层；论文主验证含 300→30 m，不能直接当作本项目 30→10 m 证据 | 先做 `tlc_guo2022_paper` 论文协议复现；通过后另建 `tlc_like_30to10` 适配版本 | P2 `planned` |
| MFGWML | Xu et al., 2021, *Spatial Downscaling of Land Surface Temperature Based on a Multi-Factor Geographically Weighted Machine Learning Model*, DOI `10.3390/rs13061186` | 多因子；XGBoost、MARS、BRR 三个基学习器预测，经 PCA 和 GWR 融合；产品路径可为 30→10 m，论文定量验证还包含升尺度后再降尺度 schemes | 先冻结论文协议和复合 bundle，再做 `mfgwml_xu2021_paper`；若更换特征、库或验证协议，另建 adaptation ID | P2 `planned` |

命名是科学声明的一部分：只有逐项核对输入、公式、参数、尺度、实现与验证协议后，才能使用 `*_paper` / `paper_reproduction`；借鉴三层分解、局部融合或更换依赖的版本必须写 `*_like_*` / `adaptation`。不得将论文中的 R²/RMSE 抄成 GeoThermoAI 新运行结果，也不得把 TLC 误展开成其他同名缩写。

#### 7.9.2 `MethodSpec` 静态注册表

`configs/methods/<method_id>.json` 是可校验的方法 spec，`geothermoai/method_registry.py` 只加载仓库内白名单。最低字段固定为：

```text
method_id / display_name / family / version
implementation_kind = native | paper_reproduction | adaptation
availability = planned | experimental | available
citation[] / source_revision / source_code_license
supported_protocol_ids[] / supported_grid_ratios[]
required_source_asset_roles[] / required_artifact_roles[] / optional_artifact_roles[]
allowed_feature_pool_id / parameter_schema / default_parameters
trainable / stochastic / random_seed_policy
bundle_kind = none | parameters | single_model | composite
reference_access_policy / spatial_influence_radius_or_halo
primary_output_role / optional_output_roles[]
postprocess_policy = native | none | separate_ablation
runtime_dependencies[] / resource_profile / limitations[]
adapter_factory_id (planned 时必须为 null)
```

运行时冻结副本保存为 `method_spec.json`，UI 可把它显示成“方法卡”；它是算法能力/血缘卡，不等同于 ModelScope 模型仓库的 Model Card。若该方法发布 bundle，后者仍在 Model repo README 中单独维护并反向链接 method ID/version。

约束如下：

- `planned`：只有经过核对的方法卡，`adapter_factory_id=null`，方法选择器禁用，不导入 adapter，不引入运行依赖；
- `experimental`：实现和测试已存在，只在开发 preset/环境变量下可选，公共 Demo 默认不可选；
- `available`：输入、命名空间、科学回归、资源和 UI 验收全部通过，才进入公共方法选择器；
- `adapter_factory_id` 只能指向代码内预注册 factory；配置、上传 ZIP、LLM 输出都不能携带 module path、entrypoint 或任意 Python；
- `source_code_license` 只描述实际复用的第三方代码。若团队依据论文自行实现，应记录论文引用、实现 commit 和差异，不能杜撰“官方代码”或许可证；
- 方法的可用状态由服务端根据 spec、依赖探针和 protocol 共同决定，浏览器不能把禁用项改为可运行。

#### 7.9.3 `MethodAdapter` 外层契约

以下是实施时必须遵守的接口签名规范，不是本轮要生成的算法代码：

```text
describe() -> MethodSpec
validate_inputs(common_context, method_config) -> ValidationReport
build_stage_plan(common_context, output_namespace) -> list[registered_stage_id]
collect_result(output_namespace) -> MethodResult
```

`common_context` 只暴露 manifest、GridPlan 和 artifact roles，不暴露可任意拼接的服务器路径。`build_stage_plan` 只能返回白名单 stage ID。单模型 adapter 内部可继续使用 `fit/predict` 小接口；`bundle_kind=none|parameters` 的 TLC 类可以跳过训练模型，`bundle_kind=composite` 的 MFGWML 类可以在自己的 namespace 保存多个基学习器、PCA 和空间融合参数。

`MethodResult` 至少包含：

```text
method_run_id / method_id / method_version / protocol_id
primary_output_key / raw_output_key(optional)
metrics_key / method_spec_key / provenance_key / report_key
displayable_layers[] / diagnostic_artifact_keys[]
```

adapter 不得自报排名资格。公共 protocol gate 根据冻结 ProtocolSpec、manifest、artifact hashes 和 metrics 另行生成：

```text
ProtocolDecision:
  method_run_id / protocol_id / protocol_spec_sha256
  ranking_eligible / not_ranked_reasons[] / checked_at / gate_version
```

gate 将决定写入 method primary metrics 与 `comparison.json`；presenter 只读取该决定，忽略 adapter 返回的任何同名字段。UI、报告和打包器只消费这些 role/key，不判断文件名里是否有 `rf`、`tcr`、`tlc` 或 `gwr`。每个 adapter 必须在启动计算前完成 `required_artifact_roles`、单位、CRS、GridPlan、分辨率比例、mask、特征池和依赖检查；缺一项即 fail-fast，不允许运行到一半才猜默认值。

方法选择发生在 acquisition 前：orchestrator 对所选 specs 的 `required_source_asset_roles` 求并集，生成冻结 `InputPlan`；acquisition 只按这个计划取数，preprocess 生成共同 artifact pool，方法私有派生特征再在各自 `METHOD_PREPARING` 中生成。运行开始后不能通过对话临时追加波段或修改 InputPlan；确需添加方法时，如果现有 artifacts 完全满足可新建 method run，否则复制参数并创建新 job。这样 MFGWML 将来需要 SWIR2 等输入时有明确施工入口，同时不会让 EasyLST 首版多下载无用资产。

#### 7.9.4 两类后续 adapter 的输入与产物要求

| 项目 | TLC 类 | MFGWML 类 |
|---|---|---|
| 适配器类型 | image-to-image / filtering-composition；可无训练 bundle | spatial ensemble learner；复合 bundle |
| 最低输入角色 | 粗分辨率 LST、细分辨率 predictor、有效区/LULC 映射、配准 GridPlan | 粗 LST、粗/细成对候选特征、坐标/格网、split/mask |
| 方法私有配置 | predictor 与地类映射、滤波/匹配/合成参数及其论文出处 | 候选 feature pool、特征选择边界、各基学习器、PCA、GWR kernel/bandwidth 及库版本 |
| bundle | `none` 或版本化参数文件 | 三个基学习器 + scaler/feature selectors + PCA 模型/跨尺度应用策略 + GWR 参数/β 系数图 + coarse residual 栅格的 `composite`；逐项记录 hash |
| 原始输出 | TLC 合成 DLST | 细尺度 GWR 融合后执行原生 residual correction 的 DLST |
| 必备诊断 | 三层/掩膜/有效区等已实现且声明的中间量；不得由 UI 猜名称 | 入选特征、粗/细三子模型预测、PCA 版本/应用策略与 PC1/PC2、GWR β/带宽、`residual_coarse`、`residual_high_resampled` 和适用网格；后者是 coarse residual 的 NN 结果，不是用 10 m 真值重拟合 |
| 首个验证门禁 | 先按论文尺度构造 synthetic degradation/reference；再测试 30→10 adaptation | 论文 reproduction 与 30→10 产品运行分开记录；没有 10 m 真值时仍遵守本项目指标边界 |

当前 EasyLST 九维输入不等于 MFGWML 的完整候选因子集；MFGWML spec 只有在 SWIR2/所需指数与地形/变换特征真正进入 acquisition、preprocess 和 schema 后才能通过输入检查。TLC 也不能默认复用 EasyLST 的全部 Sentinel 特征：必须在方法 spec 中明确 predictor/LULC 选择和质量要求。具体公式、窗口、权重、带宽、库替代关系由各自的论文复现任务逐项落地，本轮文档不伪造完整新算法实现。

MFGWML 可由论文正文明确确认的最低链路是：XGBoost/MARS/BRR 三个基学习器预测 → PCA 保留 PC1/PC2 → GWR 空间融合；30 m 的 GWR slopes/intercept 与 coarse residual 按 nearest-neighbor 到 10 m，最终包含 residual correction。论文正文对“PCA 在哪个尺度拟合、跨尺度是否复用同一变换”的叙述不够无歧义，因此不能把一种解释冒充论文原文。

本项目为获得确定性实现，预设的**工程复现规约**是：粗尺度三预测拟合并冻结 PCA；细尺度特征进入同一组已拟合基学习器得到三张细预测，再应用冻结 PCA 得到细 PC，不插值粗 PC；随后把 GWR β 图与 `residual_coarse` 最近邻为 `residual_high_resampled`，做 GWR 融合和 residual correction。该规约及全部 upstream hashes 必须进入 composite bundle manifest。只有作者源码、补充材料或可定位的原文证据确认这一跨尺度 PCA 规则后，method ID 才能升为 `paper_reproduction`；确认前只能标 `adaptation/experimental`，并将 PCA policy 作为显式差异。无论采用何种经证据确认的 policy，都不得无记录地重拟合 PCA、用不存在的 10 m LST 拟合 residual，或省略论文明确的 residual correction。

#### 7.9.5 公平比较由 `protocol_id` 决定

“同一特征列”不适用于异构方法族；公平约束应是同一**可用数据与候选特征池**，允许方法按公开规则选择不同子集。只有以下项目完全一致的 method runs 才能进入同一排名：

- `protocol_id + protocol_spec_sha256`、run purpose、AOI/scene/date、粗/细 GridPlan 与 reference artifact；
- 同一 split、held-out/constraint mask、有效像元分母和公共 evaluator 版本；
- 同一可用数据源、候选 feature pool 和防泄漏规则；实际 feature names/count、筛选 split 与选择规则必须展示；
- 同一调参数据边界和预算规则；随机方法固定 seeds/重复统计，无随机性的流程写 `N/A`；
- 同时报 R²/RMSE/MAE/MB、coverage、wall time、峰值 RAM以及失败或未计算原因；
- 任一 held-out 数据参与特征选择、模型/PCA/GWR 拟合、调参或约束时，该 run 自动 `ranking_eligible=false`。

“无需训练”不等于“没有泄漏”。TLC 类或任何滤波/图像合成方法若在生成 held-out 区域时直接读取该区域的粗 LST，或通过邻域窗口间接读取它，就不能把结果标成独立 held-out 预测。每个 MethodSpec 必须声明 `reference_access_policy` 和空间影响半径/halo；公共评估器据此扩大 buffer。若方法无法在遮蔽 held-out reference 后产生有效预测，它只能进入 `paper_native_reproduction` 或 `production` 轨，不能为了凑排名改变独立验证定义。MFGWML 的特征选择、PCA、GWR 带宽/系数拟合也全部受同一禁令约束。

结果展示分三轨，禁止跨轨混排：

1. `common_benchmark`：相同 protocol 的当前项目运行，可以排名；
2. `paper_native_reproduction`：各论文原生尺度/数据协议，只能在各自方法卡中展示复现偏差，不能跨论文排名；
3. `production`：没有独立细尺度标签时展示产物、分布和 coarse consistency，不进入精度榜。

TCR 是 EasyLST 当前端到端方法的一部分，TLC 的三层合成和 MFGWML 的空间融合也是各自方法的一部分。不得给某个方法暗中追加另一个方法的后处理后仍沿用原 method ID。若要研究“共同 TCR 后处理”，必须建立新的 adaptation/ablation method ID，同时保留 raw 与 corrected 输出，在同一 protocol 下成对报告。

#### 7.9.6 增量实施顺序与单方法完成门禁

| 阶段 | 只做什么 | 通过条件 |
|---|---|---|
| P0-A | 实现 registry、namespace、MethodResult 和数据驱动 presenter；只注册 EasyLST 为 available | 单方法体验与现主链一致；UI 无 RF 文件名依赖；上传不能注册代码 |
| P0-B | 加入 TLC/MFGWML 的 planned 方法卡 | 名称、DOI、输入、协议和限制可查；选项禁用且原因明确；不增加运行依赖 |
| P2-A | 选择一个方法做 `paper_reproduction` | 全文公式/参数逐项映射；最小 fixture、论文协议回归和差异报告通过 |
| P2-B | 如需 30→10，另建 adaptation ID | 新数据/feature schema、尺度适用性、postprocess 和验证限制显式记录 |
| P2-C | 加入公共 protocol Benchmark | 至少两个 eligible run；namespace 不覆盖；同协议门禁、资源实测和比较 ZIP 通过 |

每新增一个方法，必须依次完成：唯一 method ID/版本 → 引用与实现边界 → 输入 capability → 依赖 clean build → namespace 写入 → bundle/hash → tiny/科学回归 → 公共 evaluator → protocol eligibility → 六 Tab/ZIP 验收 → `experimental` 升为 `available`。任何一步未过，就保持 planned/experimental，README 和 Studio 不写“已支持”。没有通过 P0 科学验收前，不开展多方法排名。

---

## 8. Agent、调参和记忆系统的最终实现方式

### 8.1 Structured Action 契约

LLM 或规则解析器只输出白名单结构：

```json
{
  "intent": "run_demo|run_upload|search_live|select_pair|resume|cancel|explain|download",
  "job_id": "optional",
  "method_id": "easylst_rf_tcr",
  "params": {},
  "requires_confirmation": false
}
```

`events.py` 校验 intent、参数类型和范围后才调用 orchestrator。`method_id` 只接受 registry 中当次可用的 ID；planned/experimental 未开放方法返回结构化禁用原因。比较操作只接受当前 session 已拥有的 `method_run_id`，并再次执行 protocol gate。未知字段拒绝；自然语言永远不能作为 shell、Python、路径或模型参数直接执行。

### 8.2 四个表达阶段保留，但改成证据驱动

| 阶段 | Agent 作用 | 数据来源 |
|---|---|---|
| 规划 | 识别 demo/upload/live、已注册 method；补问 AOI、日期、是否训练 | 用户输入 + preset/method schema |
| 参数说明 | 解释云量、空间 split 和当前方法的已验证参数；planned 方法只说明不可运行原因 | 已验证配置 + MethodSpec + methodology |
| 诊断 | 把结构化 error/metrics 转为可操作建议 | stage error + manifest + metrics |
| 解读 | 总结两类指标、可用诊断、protocol/排名边界 | method metrics + bundle meta；不读取虚构样本数 |

UI 可显示：

```text
[意图] 使用武汉小区域 Demo，载入固定模型 v1
[参数] 10 m grid=512×512，window=512，单位=K
[阶段] EasyLST 推理完成，产物 method_runs.<run_id>.intermediates.lst_raw_10m
[规则] 12 个粗像元有效子像元不足，未进入 TCR 约束
[结果] 已生成独立预测指标和粗尺度一致性指标
```

这比暴露“思考链”更清楚，也能被评委复查。

### 8.3 LLM 接口与无密钥降级

ModelScope 官方 coder 示例验证了 OpenAI-compatible 方式：

```text
base_url = https://api-inference.modelscope.cn/v1
secret   = MODELSCOPE_ACCESS_TOKEN（项目自定义的 Secret 名）
model    = GEOTHERMO_LLM_MODEL_ID（部署时从当前支持列表选择）
```

注意：`MODELSCOPE_ACCESS_TOKEN` 不是平台自动注入变量，必须由空间所有者在 Secret 中创建。模型 ID 和额度会变，禁止在技术文档中假装某个尚未验证的模型永久可用。

降级顺序：

1. 有 token 且当前模型健康：LLM 解析/解释；
2. LLM 超时、限流或 JSON 无效：规则解析 + 模板解释；
3. 完全无 token：按钮/表单照常运行全部科学链路；对话提示“智能解释未启用”，不影响 Demo。

### 8.4 超参数策略

首版不采用“LLM 直接决定并重训”的方式。正确顺序是：

1. 预设安全参数范围；
2. 只在 train/val 上做受控候选或 Randomized/Grid Search；
3. 用空间 val 指标选择；
4. test 只做一次最终评估；
5. LLM 可以解释搜索结果或提出候选，但规则验证、资源预算和验证指标拥有最终决定权；
6. 任何自动重训均设置最大轮数、最大样本、wall-time 和参数边界。

因此 v2 的 LLM tuning 代码保留为 P2 设计素材，不作为当前已实现能力。

### 8.5 记忆系统分期

#### P0：任务状态与会话历史

- `manifest.json`、`state.json`、job 日志；
- 浏览器会话内对话；
- 只按 job/session 精确加载，不跨用户默认共享。

#### P1：结构化实验记忆

- JSON 或 SQLite 存 `region_type/season/feature_set/model_params/split/metrics/hash`；
- 只推荐已经成功且数据契约相容的实验；
- 用户上传内容默认不进入全局记忆。

#### P2：受控 RAG

- 只为经过审核的 methodology/FAQ 建全局只读知识库；
- 项目/用户知识分 collection，并有删除/容量/隐私策略；
- 只有在 2vCPU/16GB 冷启动、磁盘和依赖基准通过后才引入 ChromaDB/embedding；
- 不在容器启动时无说明下载 `all-MiniLM-L6-v2`；若使用任何外部模型，必须列入 `THIRD_PARTY_ASSETS.md` 并锁 revision/hash。

---

## 9. Studio 前端：按 `coder_artifacts` 骨架实现 GeoThermoAI

### 9.1 只复用整页骨架，不做组件橱窗

参考文件：

`docs/modelscope/02-组件库/modelscope-studio/docs/layout_templates/coder_artifacts/demos/app.py`

确认可用的骨架是：

```text
gr.Blocks
└── ms.Application
    └── antdx.XProvider
        └── ms.AutoLoading
            └── antd.Row
                ├── antd.Col(span=24, md=8)   左侧工作区
                └── antd.Col(span=24, md=16)  右侧产物区
```

`coder_artifacts` 的 Row/Col/Card/Tabs 骨架原本使用 `antd.ConfigProvider`；本项目因使用 Chatbot/Sender，按官方 Chatbot 模板改用 `antdx.XProvider`。两者是 provider 选择，不必同时嵌套。准确 import 为：

```python
import gradio as gr
import modelscope_studio.components.antd as antd
import modelscope_studio.components.antdx as antdx
import modelscope_studio.components.base as ms
import modelscope_studio.components.pro as pro
```

ModelScope Studio 组件负责布局、卡片、Tabs、Modal、Drawer、Tour 和视觉；文件、Plot、JSON、HTML 地图及 Python 数据绑定优先使用原生 Gradio。`pro.Chatbot` 使用其官方 dict-message 契约，不当作原生 `gr.Chatbot` 的 tuple 历史；运行时将 `antdx.Sender` 更新为 `loading=True` 后才使用其 cancel 语义。

### 9.2 页面线框

```text
┌─────────────────────────────────────────────────────────────────────┐
│ GeoThermoAI · EasyLST                         job status · method/protocol │
├──────────────────────┬──────────────────────────────────────────────┤
│ 左：任务与对话        │ 右：Artifacts                              │
│                      │                                              │
│ Welcome / 3种模式     │ [地图] [对照] [指标] [产物] [血缘] [日志]  │
│ ┌──────────────────┐ │ ┌──────────────────────────────────────────┐ │
│ │ Chatbot messages │ │ │  Empty / Loading / Render / Error       │ │
│ │ 审计事件、询问、诊断│ │ │  Folium map / plots / cards / files   │ │
│ └──────────────────┘ │ └──────────────────────────────────────────┘ │
│ 快捷卡：武汉Demo      │                                              │
│       上传标准包      │  extra: 下载结果 · 方法卡 · 使用导览         │
│       在线小AOI       │                                              │
│ Sender + Run/Cancel  │                                              │
├──────────────────────┴──────────────────────────────────────────────┤
│ 数据源 · 模型revision · 单位K/°C · 隐私/缓存提示 · 版本             │
└─────────────────────────────────────────────────────────────────────┘
```

宽屏左/右比例约 1:2；小屏 `span=24` 自动上下排列。不要再加 v2 中独立 25% 会话列表 + 45% 对话 + 30% 可视化的三栏结构，因为窄屏和评委演示中右侧地图过小，也不符合指定的 coder_artifacts 骨架。

### 9.3 组件树与职责

#### 顶部

- `antd.Flex`：Logo、标题、由当前 `MethodSpec.display_name + version` 生成的方法标签；EasyLST 首版可显示 `TTRI + RF + TCR`，但组件本身不得硬编码该字符串；
- `antd.Tag`/状态文本：Demo/Upload/Live、当前 job、stage、protocol 和方法 availability；
- 不显示服务器路径、token 或内部 host。

#### 左侧

- `pro.Chatbot`：展示用户消息、Agent 说明、工具/阶段审计事件；
- `antdx.Sender`：自然语言任务输入；支持 submit/cancel；
- 三个 `antd.Card` 示例：一键 Demo、上传标准包、在线小 AOI；点击只填充/触发白名单 action；
- 原生 Gradio 上传控件：GeoJSON/ZIP/标准包；
- 参数抽屉 `antd.Drawer`：方法/比较模式、日期、云量、split、是否载入 bundle 等高级项；方法选项完全来自静态 registry，planned、依赖缺失或 protocol 不兼容项禁用并显示原因；
- `antd.Modal`：影像对候选表和用户确认；
- `antd.Tour`：四步使用导览。

#### 右侧 Artifact Card

使用 `antd.Card(title="科学产物")`，在其 `extra` slot 放：

- 下载当前结果；
- 打开当前方法卡；
- 使用导览。

Card body 内是无 tab bar 切换或正常 `antd.Tabs`，固定六页：

| Tab | 内容 | 主要组件 |
|---|---|---|
| 地图 | 共同 coarse/reference + 所选 method run 的 primary layer；仅加入 adapter 声明可展示的中间层 | `gr.HTML` 承载受控 Folium iframe |
| 对照 | 单方法粗/细/中间量，或同 protocol 多方法 gallery/差值图；统一色标 | `gr.Image`/`gr.Gallery`/`gr.Plot` |
| 指标 | 每行为一个 method run；公共指标、coverage/time/RAM/排名资格；方法特有指标只进展开详情 | Antd Statistic + `gr.Dataframe` 或 Markdown |
| 产物 | 按 method namespace 分组的名称、格式、单位、大小、SHA256、下载 | `gr.File`/`DownloadButton`；不用 JS Blob |
| 血缘 | AOI、item ID、日期、方法卡、protocol、feature subset、bundle/split/hash、软件版本 | 只读 `gr.JSON`/Markdown |
| 日志 | 按 method_run_id 过滤/聚合的 stage 时间线、进度、错误码、恢复建议 | `antd.Timeline`/Markdown |

右侧内容状态沿用 coder_artifacts 的 `empty/loading/render/error` 思路：

- `empty`：方法简介、输入要求和示例缩略图；
- `loading`：当前 stage、百分比和预计下一步，不展示虚构 ETA；
- `render`：完成的 tabs；
- `error`：失败 stage、错误码、操作建议、resume/改用 Demo 按钮。

主 UI 骨架在后续增量接入中保持冻结：仍是 `md=8/16` 左对话/右产物、仍是上述六个语义 Tab，不新增第三栏、第七个顶层 Tab 或每种方法一套页面。`presenter` 固定输出 `method_options`、`selected_run_ids`、`layers`、`comparison_items`、`metric_rows`、`artifact_groups`、`lineage`、`log_rows`、`method_card`，再更新启动时已声明的组件；Gradio 回调过程中不动态创建/销毁 Tabs。

参数 schema 只负责校验、默认值和选项，不是运行时组件工厂。应用启动时读取静态 registry，对当次部署中所有 `available` 方法按代码内“字段类型→已验证组件”白名单预声明参数控件；planned 方法不创建参数控件。`select_method()` 回调只能更新既有组件的 `visible/value/choices/interactive`，不得实例化新 Gradio/ModelScope Studio 组件、import 新 adapter 或执行 schema 内代码。方法专属诊断也只更新既有 Tab 的预声明容器/折叠区。

`metric_rows` 中的 `ranking_eligible/not_ranked_reasons` 只能来自公共 `ProtocolDecision`；即使 adapter 或方法私有 metrics 写了同名字段，presenter 也不读取。

### 9.4 事件绑定

| 触发 | 服务端函数 | 更新目标 |
|---|---|---|
| 示例卡 click | `select_preset()` | Sender、参数摘要、模式 |
| 方法 change | `select_method()` | 仅更新预声明参数控件的 visible/value/choices/interactive，以及方法卡、输入校验摘要；不创建组件、不重建页面骨架 |
| 比较选择 change | `select_method_runs()` | 对照/指标/产物/血缘视图；protocol 不同则禁用排名并说明 |
| Sender submit / Run | `handle_action()` generator | Chatbot、Sender loading、job state、右侧状态、进度 |
| Sender cancel | `request_cancel()` + Gradio `cancels=[run_event]` | cancel flag、按钮、消息；计算在窗口边界真正停止 |
| 上传 change | `validate_upload()` | 输入预览、错误、Run 可用性 |
| pair 确认 | `select_pair_and_resume()` | manifest、Chatbot、状态机 |
| Resume | `resume_job()` | 从第一个未成功 stage 继续 |
| 下载 | 原生文件输出 | 当前 method run ZIP；比较模式为 comparison ZIP；不在浏览器拼二进制 |
| Tour close/finish | coder 示例同款 open/close handler | Tour 状态 |

长任务通过 generator `yield` UI 更新；不要恢复旧桌面端“后台线程 + 浏览器轮询共享字典”的主实现。

### 9.5 地图实现规范

v2 的 Folium 示例缺少 CRS 转换、色标和 `colored_band` 定义，不能直接复制。正确实现要求：

1. 从 artifact profile 读取 CRS、transform 和 bounds；
2. 用 `rasterio.warp.transform_bounds` 转到 EPSG:4326；
3. 按最大显示边长读取/重采样，不加载全幅；
4. 对共同有效区计算统一 P01/P99 或固定业务色标；三层共享 vmin/vmax；
5. 将 K 转色彩 RGBA，透明区域对应 NoData；
6. Folium `ImageOverlay` 使用 WGS84 bounds；
7. 加底图、LayerControl、单位明确的 colorbar、透明度；
8. 生成完整 HTML 后，沿用官方 coder 示例的受控 iframe 思路展示；只嵌入服务端生成的受信任 HTML，不回显用户 HTML/JS；
9. GeoTIFF/ZIP 下载必须走 Gradio File，不用 coder 示例的 `data:` URI/JS Blob；那只适合小 HTML。

首版地图必须支持图层切换、缩放和平移。点击取温度可列为 P1；没有可靠的地图坐标→栅格索引服务前不写“已支持点击取值”。

### 9.6 视觉规范

建议主题不模仿常见紫色 AI 聊天页，采用热红外/地学视觉：

| Token | 建议 |
|---|---|
| 主色 | 深海军蓝 `#123A5A` 或 `#164E63` |
| 强调 | 热橙 `#E76F51`；只用于运行/温度/警告关键点 |
| 背景 | 暖灰白 `#F6F7F9` |
| 成功 | 青绿 `#2A9D8F` |
| 危险 | 清晰红色，配文字/图标，不只靠颜色 |
| 地图色带 | 科学可读的 `inferno`/`turbo` 等，固定同一场景范围并标单位 |

页面要求：

- 标题、卡片、空状态、加载态和错误态视觉一致；
- 地图/图表不裁切，移动端可横向滚动指标表；
- 所有按钮有禁用/运行/失败状态；
- 图标带文本或 tooltip；
- 对比图写清来源、时间、分辨率和单位；
- 不把模型思考动画当科学进度。

### 9.7 启动方式

普通 Gradio 创空间按官方 coder 示例的已验证形式：

```text
demo.queue().launch(css=css, ssr_mode=False)
```

Docker 模式才需要显式监听 `0.0.0.0:7860`。不要在普通 Gradio 路径中自行启动第二个 FastAPI/Flask 服务。

---

## 10. ModelScope 部署、依赖与资源组织

### 10.1 主路线：普通 Gradio SDK 创空间

创建时明确选择：

```text
sdk_type:      gradio
sdk_version:   6.2.0
base_image:    ubuntu22.04-py311-torch2.9.1-modelscope1.35.0
resource_configuration: platform/2v-cpu-16g-mem
```

上述值于 2026-07-31 从官方 deploy schema 核验。部署前再次读取：

`https://modelscope.cn/api/v1/studios/deploy_schema.json`

PyPI 包 `modelscope_studio==2.0.2` 的官方依赖是 `gradio>=6.0,<=6.8.0`。平台默认 Gradio 版本可能高于这个区间，因此必须显式选择/锁定 6.2.0，不能“使用最新默认值”。

### 10.2 `requirements.txt` 的生成规则

平台硬锁：

```text
gradio==6.2.0
modelscope_studio==2.0.2
```

其余地学依赖不能把 v2 的“新增依赖汇总”原样抄进去。正确流程是：

1. 从 `GeoThermoAI - 副本` 的真实 import 审计得到最小包集；
2. 移除 PyWebView/tkinter 桌面依赖；
3. 只在启用相应功能时加入 Folium、OpenAI client、ModelScope Hub；
4. 在与平台相同的 Python 3.11 clean environment 安装；
5. 运行 `python -c "import geothermoai"`，加载两个 JSON preset、全部 method specs 与 ProtocolSpecs 并验证 hash；仅实例化 `available` adapter，再构建 UI；
6. 对每个 available 方法运行依赖 probe、tiny fixture 和 Demo smoke test；
7. 记录 `pip check`、版本和 build log；
8. 将通过的精确版本写入发布版 `requirements.txt`。

最小包类别来自现有真实代码：

```text
numpy / pandas / scipy / scikit-learn / joblib
rasterio
geopandas / shapely / pyshp（仅矢量上传路径需要）
pystac-client / pystac / planetary-computer（仅 live 模式需要）
Pillow / matplotlib（预览与图表）
folium（交互地图）
requests
openai（仅启用 API-Inference Copilot 时）
```

若运行期从 ModelScope Hub 拉资源，再加已经核验的 `modelscope-hub==0.1.8`；否则不为“显得使用平台”增加无用依赖。

`planned` TLC/MFGWML 方法卡不得把假定依赖提前塞进 P0 `requirements.txt`。方法进入 experimental 时，先在独立 clean environment 核对论文实现所需库、Python 3.11 可用性、许可和数值等价性，再把实际 import 审计结果加入候选锁文件；升为 available 前必须在 ModelScope 原生环境重跑整套测试。若论文流程原本使用另一语言或不同库，不得声称 Python 替代实现天然等价。单个扩展方法的依赖问题也不会自动触发 Docker：仍按第 10.6 节的 clean-build 门禁决定；若方法超出公共 Studio CPU/RAM 预算，则保留离线/Notebook 运行和只读结果包导入，不让它拖垮 EasyLST 公共 Demo。

不要未经验证直接加入 pip `gdal`。`osgeo` Python binding 必须与系统 GDAL 精确匹配，这是是否改 Docker/切 rasterio 获取后端的决策点。

### 10.3 `packages.txt` 的边界

`packages.txt` 是可选文件，只写 Debian/Ubuntu 包名，一行一个，不写 `apt install` 命令。平台先装 packages，再装 requirements。

v2 预写：

```text
gdal-bin
libgdal-dev
libproj-dev
```

不能把这三行当作已验证方案，因为安装系统库不等于 Python `osgeo` 一定可 import。执行以下门禁：

- 若 ModelScope 活动路径改用并通过等价性测试的 rasterio acquisition，且 clean build 无系统缺包，则不创建 `packages.txt`；
- 若保留 GDAL acquisition，先在原生镜像验证 `gdal-config`/Python binding 精确兼容；
- 只有 build/run log 证明缺少哪个系统包时才写入；
- 若两轮原生 clean build 都因不可控 GDAL/系统二进制问题失败，切到 Docker 后备，不继续盲加包。

### 10.4 两个数据获取后端的选择门禁

当前仓库同时有 `data_acquisition.py`（GDAL/osgeo）和 `data_acquisition_rasterio.py`，长期同时注册会漂移。实施时二选一：

#### 路径 A：原生 Gradio + rasterio acquisition（推荐先试）

- 将 GDAL 版本的阈值、候选、血缘和验证修正同步到 rasterio 实现；
- 用同一小 AOI 对比两个后端的 CRS、bounds、transform、shape、波段、统计和 hash/容差；
- tiny/live 小样例全链通过后，只注册 rasterio 后端；GDAL 版本标 legacy/deprecated；
- 优点是减少 `osgeo` 安装风险，适合普通 Gradio 创空间。

#### 路径 B：Docker + 已跑过的 GDAL acquisition

- 若 rasterio parity 不通过，或科学链必须使用 osgeo，使用 Docker 固定 GDAL/Python 环境；
- UI 和 orchestrator 不变，只替换 acquisition adapter；
- Docker 的额外账号条件必须先满足。

不得在运行时“哪个 import 成功就随机选哪个”；活动后端写进 build 信息和 manifest。

### 10.5 `ms_deploy.json` 是否需要

- 网页自定义创建 + Git 推送：**不需要** `ms_deploy.json`。
- 只有选择官方“快速创建并部署”上传整个文件夹时，才在根目录加入它。

当前可用的 Gradio quick-create 配置：

```json
{
  "$schema": "https://modelscope.cn/api/v1/studios/deploy_schema.json",
  "sdk_type": "gradio",
  "sdk_version": "6.2.0",
  "resource_configuration": "platform/2v-cpu-16g-mem",
  "base_image": "ubuntu22.04-py311-torch2.9.1-modelscope1.35.0"
}
```

禁止把 access token、LLM key 或其他 Secret 写入 `environment_variables` 后提交。Secret 在创空间创建后通过设置页或受控 CLI 添加。

### 10.6 Docker 后备方案

切换条件满足任一项即可：

- 普通 Gradio clean build 两轮均因不可控 GDAL/系统库失败；
- 必须保留 `osgeo` 且平台系统版本无法匹配；
- 未来确实需要 Gradio 之外的多服务/反向代理/自定义系统栈。

Docker 官方边界：

- 根目录必须有 `Dockerfile`；
- 服务监听 `0.0.0.0:7860`；
- 8080 被平台保留，不能使用；
- 不在后端自定义使用 `Authorization`、`X-modelscope-*`、`X-studio-*` 请求头；
- Docker 创空间需完成平台要求的阿里云绑定/实名认证；
- `/mnt/workspace` 和 Secret 只在运行期可用，Docker build 阶段不可用；
- UI 美观程度与是否 Docker 无关，仍使用同一套 ModelScope Studio 组件。

对应 quick-create 配置为：

```json
{
  "$schema": "https://modelscope.cn/api/v1/studios/deploy_schema.json",
  "sdk_type": "docker",
  "resource_configuration": "platform/2v-cpu-16g-mem",
  "port": 7860
}
```

Docker 配置中不写 `sdk_version` 或 `base_image`；镜像由根 Dockerfile 决定。

### 10.7 README 与创空间卡片

ModelScope 创建空间后会生成带 YAML 的 README。实施要求：

- 保留平台生成的 YAML 结构；
- 只修改已由官方卡片文档确认的 `domain/tags/datasets/models/deployspec/license` 字段；`models`/`datasets` 填 `owner/repo` ID，正文再写可点击 URL，未使用字段不自造；
- 默认入口是根 `app.py`，只有确有需要才通过 `deployspec.entry_file` 改；本项目不改；
- 不自造 `modelscope.yaml`、`space.yaml` 等平台未要求文件；
- README 正文包含：一句话介绍、动画/截图、一键 Demo、输入契约、输出契约、方法、指标定义、模型/数据链接、资源限制、隐私、已知问题和引用。

尚未创建的 Model/Dataset ID 必须写成“待创建占位符”，在发布门禁前替换；不得提前假装 URL 已存在。

### 10.8 Studio、Model、Dataset、Notebook 的分工

```text
Studio repo  → 代码、UI、小静态资产、无网 P0 所需的小 Demo 输入/模型；Dataset 另作镜像
Model repo   → 版本化 method bundle（EasyLST 为 RF joblib + TTRI 系数）、feature schema、bundle meta、指标、模型卡
Dataset repo → 可发布 Demo 栅格、manifest、golden output、benchmark split、数据卡
Notebook     → 训练、空间评估、baseline/ablation、从数据到模型的复现
```

大场景和大模型权重不塞进 Studio Git。Studio README 与 Model/Dataset 卡片互相链接，revision/tag 固定在 manifest 中。

### 10.9 ModelScope 数据集上传与下载

官方数据集文档中的旧 `modelscope` CLI 仍可用；为避免团队混用两套命令，发布工作站统一采用当前已核验的 `modelscope-hub==0.1.8`。只有 Studio 运行期确实调用 Hub SDK 时才把该包加入 Studio `requirements.txt`。

安装、登录与上传：

```text
python -m pip install modelscope-hub==0.1.8
ms-hub login
ms-hub upload <owner>/<dataset> <local_folder> --repo-type dataset --revision master --commit-message "publish GeoThermoAI demo v1"
```

下载完整或指定文件：

```text
ms-hub download <owner>/<dataset> --repo-type dataset --revision <tag-or-commit> --local-dir ./demo_cache
ms-hub download <owner>/<dataset> README.md demo_manifest.json --repo-type dataset --revision <tag-or-commit> --local-dir ./demo_cache
```

Python SDK 明确从 `modelscope_hub` 导入：`from modelscope_hub import HubApi`，再使用 `upload_folder(..., repo_type='dataset')` / `upload_file`。下载/上传时：

- 固定 revision；
- 完成后验证 SHA256；
- 私有资源 token 只来自 Secret；
- Dataset 技术限制以官方 upload 文档为准：单文件、文件数、网页上传等有具体边界；赛事材料中的“托管资源”宣传语不替代技术上传限制。

### 10.10 ModelScope Hub 与 Git

Studio 源码同步最稳妥的方式：使用空间“空间内容”页提供的 Git 地址，clone 后 commit/push 到平台远端当前默认分支（通常为 `master`，以空间内容页或 `git remote show origin` 为准），再上线/重启；不要 force push。

当前独立 Hub 工具经核验为 `modelscope-hub==0.1.8`，命令名 `ms-hub`。如团队选择使用，可用：

```text
ms-hub login
ms-hub create <owner>/<repo> --repo-type studio --sdk-type gradio
ms-hub deploy <owner>/<repo> --repo-type studio
ms-hub logs <owner>/<repo> --log-type build --keyword ERROR --page-size 100
ms-hub logs <owner>/<repo> --log-type run --keyword ERROR --page-size 100
ms-hub secret add <owner>/<repo> <KEY> <VALUE>
```

`ms-hub upload` 当前只支持 model/dataset，不写成 Studio 上传命令。Studio 文件用 Git/网页上传。

### 10.11 运行环境变量

| 变量 | 类型 | 默认/说明 |
|---|---|---|
| `GEOTHERMO_WORKDIR` | 普通变量 | ModelScope 设 `/mnt/workspace/geothermoai` |
| `GEOTHERMO_MAX_PIXELS` | 普通变量 | 初始 `1048576`，基准后锁定 |
| `GEOTHERMO_MAX_UPLOAD_MB` | 普通变量 | 项目阈值，基准后锁定，不冒充平台限制 |
| `GEOTHERMO_MAX_DOWNLOAD_WORKERS` | 普通变量 | 初始 2 |
| `GEOTHERMO_ENABLE_LIVE` | 普通变量 | `0/1`；外网未验收时设 0 |
| `GEOTHERMO_LLM_MODEL_ID` | 普通变量 | 从部署时当前 API 支持列表选择；可空 |
| `MODELSCOPE_ACCESS_TOKEN` | **Secret** | 仅需要 Hub/API-Inference 时设置；页面不回显 |

配置或更新 Secret 后重启创空间使其生效。

启动 Gate：`config/job_store` 必须创建 `GEOTHERMO_WORKDIR/runs`、`cache`、`demo_cache` 并做原子写/读/删探针；失败时在接受任务前阻断。运维参数另含 job TTL、单 job/总容量上限和清理周期，均作为项目配置，不冒充平台固定限制。

---

## 11. 小型 Demo、外部资产与离线大场景

### 11.1 `wuhan_small` Demo 最低内容

```text
demo_manifest.json
inputs/aoi.geojson
inputs/features/lst_30m_K.tif
inputs/features/terrain_30m.tif
inputs/features/spectral_30m.tif
inputs/features/spectral_10m.tif
inputs/features/valid_30m.tif + valid_10m.tif
method/easylst_rf_tcr/method_spec.json
method/easylst_rf_tcr/bundle/rf_model.joblib
method/easylst_rf_tcr/bundle/bundle_meta.json
method/easylst_rf_tcr/bundle/ttri_coefficients.json
split_map.tif 或可确定性重建它的 split config + hash
golden outputs（仅测试用，可选）
README.md
```

`demo_manifest.json` 必须写：

- 精确 bbox/geometry、CRS、像元大小；
- Landsat/Sentinel 日期、collection/item ID、asset key；
- DEM item/source；
- 获取时间、处理脚本版本/commit；
- 每个文件大小与 SHA256；
- 来源、引用和是否允许随仓库再分发；
- method ID/version、spec/config hash、`availability=available`、protocol 和 primary output role；
- bundle 特征、split、训练数据 hash、sklearn 版本；
- golden output 的生成版本。

Demo 压缩包目标控制在约 100 MB 以内只是项目目标，不是平台官方限制。Dataset repo 可作镜像和大文件分发，但无网 P0 所需的最小输入、受信 RF、TTRI 系数和冻结 split 必须随 Studio repo 或 Docker 镜像实际内置，不能只留远程指针。在线底图不可用时，地图退化为本地 preview/无底图图层，计算和结果查看仍可完成。

### 11.2 Demo 不能作弊成静态播放

允许预置：耗时下载、训练完成的模型、对齐后的输入。

必须现场执行同一套真实核心函数：

```text
读取/校验基础特征与冻结 split
→ 加载并校验 TTRI 系数，现场构建 TTRI30/10
→ 加载并校验 RF → 窗口推理
→ TCR 守恒校正 → metrics → preview/report → ZIP
```

默认 `wuhan_demo.json` 固定为 `pipeline_profile=pretrained`、`run_purpose=evaluation`，用冻结 split 展示当前 run 的独立粗尺度 hold-out 与 train constraint consistency，绝不重拟合 TTRI/RF。另设 production 按钮时必须启动新 job；它可全场约束，但当前 independent 标为未计算并链接冻结 evaluation 结果。golden output 只用于回归测试，不直接当本次运行结果返回。

### 11.3 离线完整路径

大武汉/更多城市采用同一个 orchestrator 的 batch profile：

- 提高 AOI 上限，但仍窗口化；
- 可在本地或 Notebook 进行下载、训练和全场景推理；
- 每个 stage 产物与 manifest 同构；
- 完成后将模型、精简 Demo、指标、preview、必要数据同步到 ModelScope；
- Studio 只浏览大场景结果或运行小 AOI，不在单次公共请求中重做数小时下载和 2 亿行计算。

### 11.4 外部下载说明制度

以后从网上或 ModelScope 下载任何模型、数据集、底图或其他文件，都在 `THIRD_PARTY_ASSETS.md` 逐项记录：

| 字段 | 要求 |
|---|---|
| 名称/用途 | 例如武汉 Demo 的 Landsat LST 输入 |
| 来源 | ModelScope owner/ID 或上游官方 URL |
| 精确版本 | tag/commit/revision/item ID/日期 |
| 文件信息 | 文件名、size、SHA256；可得时写 etag |
| 获取方式 | SDK/CLI/Git/脚本命令 |
| 缓存 | `/mnt/workspace/geothermoai/cache/...` 或本地路径 |
| 许可/引用 | 原始条款链接与署名；不确定时不再分发原文件 |
| 随项目分发 | yes/no；若 no，提供获取脚本/manifest |

本轮文档审查只读取了用户已提供文件和公开网页/schema，没有下载新的模型或外部数据集到工程目录。

TLC/MFGWML 本轮只引用本地已保存的论文阅读材料与 DOI，没有下载算法模型、源码或新数据。后续若为了复现下载作者源码、R/Python 包、测试影像、预训练 bundle 或论文补充材料，除上述字段外还要记录其对应 `method_id`、论文复现/适配用途、是否进入公共 Studio、与原论文实现的差异；出版社 PDF 不随仓库重新分发，除非其许可明确允许。

---

## 12. 文件级实施路线：从现在到公开提交

当前日期为 2026-07-31，门户截止为 2026-08-14 24:00（UTC+8）。v2 的 4–6 周规划不适合当前窗口；下面按依赖关系压缩为 14 天执行计划。日期是管理建议，技术门禁不能因赶进度跳过。

### 12.1 任务看板

| 日程 | 工作 | 具体文件 | 当日交付 | Gate |
|---|---|---|---|---|
| D0 | 冻结原型与安全清理 | 旧仓库只读 tag/副本；撤销旧明文 API Key | 基线清单、风险清单、目标仓库 | 不覆盖原 v2/原型；无有效密钥进新仓库 |
| D1 | 契约与 tiny fixture | `contracts.py`、`grid_ops.py`、`job_store.py`、`method_registry.py`、`methods/base.py`、`configs/protocols/`、`tests/fixtures/tiny_case` | manifest/state 分工、GridPlan、ProtocolSpec hash、method/comparison namespace/roles；6×6/18×18 canonical grids | grid round-trip/nesting、原子写、registry/namespace/protocol gate 和 workdir 探针通过 |
| D2 | AOI/数据输入 | `aoi.py`、acquisition adapter | bbox/WGS84 GeoJSON/ZIP SHP、upload schema、候选 pair | CRS/面积/像元/zip 安全；DEM collection→asset 真 item 测试通过 |
| D3 | 预处理与空间 split | `preprocessing.py`、`spatial_split.py` | LST_K、spectral30/10、terrain/mask、buffered split、base NPZ | 无全图 CSV；halo 无接缝；split/buffer 不泄漏 |
| D4 | TTRI 纠错 | `ttri.py` | train 系数、TTRI30/10、model NPZ | 改 val/test y 不改变 TTRI；设计矩阵 rank=4 |
| D5 | RF/TCR/栅格导出纠错 | `rf_model.py`、`tcr.py`、`export.py` | 窗口 RF、constraint mask、TCR30/10、final10 | 严格嵌套非均匀残差 fixture 守恒通过 |
| D6 | EasyLST adapter、评估、渲染、打包与 orchestrator | `methods/easylst_rf_tcr.py`、`evaluation.py`、`render.py`、`report.py`、`packaging.py`、`orchestrator.py` | EasyLST MethodResult、两类指标、两 profile、fail-fast/resume、方法 ZIP | 失败不越 stage；指标不混用；方法 namespace 不写全局输出；ZIP 不递归包含自身 |
| D7 | 制作真实小 Demo | `scripts/build_demo.py`、`demo/wuhan_small` | 内置 Demo input/model/TTRI/frozen split/golden | 无网、无 token、pretrained profile 全链通过 |
| D8 | 左对话/右产物 UI | `studio/ui.py`、`events.py`、`presenter.py`、`app.py` | coder_artifacts 骨架业务化；动态方法卡/presenter；TLC/MFGWML planned 禁用态 | 空/载入/成功/失败/取消五态可见；主两栏和六 Tab 不随方法变化 |
| D9 | 本地 clean build | `requirements.txt`、测试 | Python 3.11 最小依赖锁 | 包导入、JSON preset/method/protocol specs、hash 与 available adapter probe、`pip check`、unit/integration/smoke 全过 |
| D10 | ModelScope 原生部署 | Studio Git、README、Secret | 私有空间运行，build/run log 无错误 | workdir 探针与隐身窗口 Demo 全链通过 |
| D11 | Model/Dataset 分流 | 模型卡、数据卡、资产清单 | 固定 revision 的 Model/Dataset 资源 | Studio/README 链接真实公开可读 |
| D12 | 形式和科学 QA | 地图、报告、输入输出/贡献边界文档 | 截图/GIF、runtime benchmark、limitations | 指标来自 JSON；两会话不串；下载可开；不把 runtime benchmark 写成多方法排名 |
| D13 | 发布冻结与正常最终保存 | 版本 tag、公开空间、提交门户 | Release candidate；表单最新版本保存成功 | 不再加 P2 大功能；外部访问和服务器回显复核 |
| D14 | 最终复核/应急补交 | 提交门户与全部公开链接 | 截图、表单文本副本、链接清单 | 只作复核和故障缓冲，早于 24:00 UTC+8 完成 |

如某个 P1 功能延迟，优先保证：Demo 真运行、科学纠错、结果可下载、公开稳定。不要为了在截止前抢做 TLC/MFGWML、Chroma、LightGBM 或复杂 UHI 而破坏 P0；首版完成的是扩展契约和 planned 方法卡，不是两套新算法。

### 12.2 每阶段的完成定义

#### Stage A：契约完成

- 业务代码不再出现 `row/3`；
- 所有输出都有 artifact key、producer、upstream hash；
- 两个 job 即使输入同名也不共享路径；
- 两个 method runs 即使处理同一输入也只写各自 namespace；registry 拒绝重复 ID、动态 entrypoint 和 planned adapter 实例化；
- ProtocolSpec 可稳定哈希；method/comparison 同时校验 ID+hash；跨 protocol 只能 visual-only；method/comparison 状态作用域互不覆盖；
- state/manifest 职责不重叠，断电式中断后可读取；job/session 所有权检查通过。

#### Stage B：科学内核完成

- TTRI 只用 base_train，输出独立 model NPZ；
- buffered 空间 split 与 constraint mask 可视化；
- TCR 只在严格嵌套 canonical grids 上按常数残差守恒；
- independent/coarse 两类指标分开；
- 同一 tiny input 的输出可重复，hash/容差符合测试。

#### Stage C：Demo 完成

- 一次按钮可跑完；
- 无外网、无 LLM token；
- P0 input/model/TTRI/split 实际内置，不依赖首次 Hub 下载；
- 不返回 golden output 冒充新计算；
- 产物 ZIP 可解压，GeoTIFF profile 与 manifest 相同。

#### Stage D：Studio 完成

- 左对话/右产物同屏；
- 方法标签、参数、图层、指标、产物和方法卡由 `MethodViewModel` 更新，主两栏与六 Tab 固定；planned/依赖缺失方法不可运行；
- 两个浏览器会话不串 job；
- 取消最多完成当前窗口，不启动下一 stage；
- 错误信息对用户可操作；
- 密钥、绝对路径、内部 traceback 不出现在 UI。

#### Stage E：ModelScope 完成

- 明确使用 `gradio==6.2.0` / `modelscope_studio==2.0.2`；
- build/run log 留档；
- 空间公开且无需登录体验；
- Model/Dataset/README 链接互通；
- 门户字段保存成功。

### 12.3 首版发布后的方法增量看板

下面是后续迭代顺序，不并入 D0–D14，也不代表本轮已经实现算法：

| 里程碑 | 文件范围 | 交付 | Gate |
|---|---|---|---|
| M0 扩展壳验收 | registry/base adapter/namespace/presenter/tests | EasyLST 通过新接口；TLC/MFGWML planned 方法卡 | 单方法结果与重构前科学容差一致，UI 骨架截图回归一致 |
| M1 TLC 论文协议复现 | `methods/tlc_guo2022.py`、独立 config/fixture/method doc | `paper_reproduction` 结果与差异报告 | 论文尺度、输入、参数、参考与指标逐项核对；不能用 30→10 代替原协议 |
| M2 TLC 30→10 适配 | 新 adaptation method ID/config | 跨传感器/30→10 实验包 | 不沿用 paper ID；质量、尺度效应、coarse consistency 和限制报告齐全 |
| M3 MFGWML 协议核查与复现 | `methods/mfgwml_xu2021.py`、扩展 feature pool、复合 bundle | 三基学习器/PCA policy/GWR β/coarse residual 的可审计 bundle | 先以作者源码/补充材料核实跨尺度 PCA；未确认则仅 adaptation/experimental；所选 policy、β 与 `residual_high_resampled`、原生残差校正、全 bundle hash、内存/耗时通过 |
| M4 公共 Benchmark | protocol/comparison config、comparison package/tests | 同 protocol 对照表与 ZIP | 至少两条 eligible runs；不同 protocol 自动拒绝排名；无产物覆盖 |

若 M1/M3 的复现资料、源码许可或依赖尚不明确，允许保持 planned 并先完善方法卡，不允许靠猜测补齐。每个里程碑只增加对应 adapter、config、文档和测试，`app.py` 与第 9 节主组件树不改。

---

## 13. 测试与验收规范

### 13.1 单元测试

| 测试 | 输入 | 通过标准 |
|---|---|---|
| TTRI 无泄漏/秩 | 固定 base_train，任意改写 base_val/base_test LST；另给秩亏矩阵 | val/test TTRI 逐元素不变；秩亏输入 fail-fast |
| Grid round-trip/plan | 不同原点的 source grids + 6×6/18×18 canonical grids | source pixel/map round-trip；canonical 同 CRS/origin、严格3×；不依赖裸比例索引 |
| TCR 守恒 | 空间非均匀残差、NoData、低 valid fraction、边界粗像元 | 约束格上 `abs(Agg(final10)-LST_ref30)<1e-4 K`；不合格格不伪装约束 |
| Terrain halo | 跨多个窗口的 DEM 与整图基准 | 非全图外边界处 slope/aspect 与整图结果在容差内、无窗口接缝 |
| 定标/重采样 | 固定 DN、QA/SCL 和 B11 fixture | K/反射率、类别 mask、30/10 特征与 preset 完全一致 |
| 导出行序 | 同一带 row/col 的样本随机打乱 | 输出像元完全一致 |
| AOI CRS | WGS84 GeoJSON、投影 Shapefile、非 WGS84 legacy GeoJSON | 前两者得到预期 WGS84 bounds；后者按 P0 契约明确拒绝 |
| QA/SCL | 固定 bit/class fixture | mask 与规则完全一致 |
| Model schema | 特征顺序/hash 错配 | 加载被拒绝并返回明确错误 |
| Model trust | upload ZIP 含 joblib 或固定模型 hash 错误 | 上传被拒绝；不调用 `joblib.load` |
| Method registry | duplicate ID、planned spec、依赖缺失、上传包伪造 adapter/module path | 启动校验报明确错误；planned/缺依赖项禁用；上传永不能注册代码 |
| Method namespace | 同一 job 运行两个 fake adapters（一个 trainless、一个 composite） | 只写各自 `method_runs/<id>`；bundle kind 均可表达；任一文件/hash 不覆盖 |
| Protocol gate | 同 ID/不同 spec hash、不同 split、相同 hash/不同 feature subset、adapter 伪造 eligible | 前两类拒排并给理由；相同候选池/选择边界允许不同子集并披露；adapter 自报资格被忽略 |
| Comparison scope/state | 同 job 同协议、同 job 跨协议、跨 job 三组候选；两个方法都执行 evaluate/render/package | 仅首组可 ranked；跨协议只 visual-only 且 common protocol/hash 为 null；跨 job 拒绝；方法 stage 和 comparison stage 不互相覆盖 |
| Cancel state | 在 shared/method/comparison 窗口中分别发取消 | 先仅 `cancel_requested=true`，边界后相关作用域和 job 为 CANCELLED；JSON 从不出现 `CANCEL_REQUESTED` |
| 原子 state | 中断/并发读写模拟 | 不出现半 JSON；旧完整状态仍可读 |
| Package whitelist | 已有全部产物且目录含旧 ZIP/临时文件 | 新 ZIP 只含白名单且不包含自身/Secret |

### 13.2 tiny 全链集成测试

使用无版权的合成数据：约 6×6 个 30 m 像元，对应 18×18 个 10 m 像元。

必须生成：

```text
manifest/state
split/constraint map + shared base train/val/test NPZ
resolved EasyLST method spec + method_run namespace
EasyLST model train/val/test NPZ + TTRI coefficients + TTRI30/10
EasyLST RF model + bundle meta
raw10 + TCR30/10 + primary LST
method metrics + preview + provenance + method ZIP
```

通过条件：

- 无网络；
- 项目 CI 目标 ≤60 秒；
- 任一步故障立即停止；
- 修复/恢复后从正确 stage 继续；
- buffer/test block 不进入 TTRI/调参/TCR constraint；
- primary output、metrics 和 ZIP 都能从 `method_run_id` 唯一解析，不依赖全局 latest；
- 完成产物全部可打开，profile 与 manifest 一致；
- 同一 release 资产用 SHA256 验完整性，跨 GDAL/rasterio 版本的科学回归比较 CRS/transform/shape/nodata 与数值容差，不要求压缩 GeoTIFF 文件字节完全相同。

≤60 秒是项目 tiny fixture 目标，不代表平台 SLA。

### 13.3 Demo 性能验收

Demo `512²` 的冷启动、端到端 wall time、峰值 RAM、输出大小必须在真实 ModelScope `2vCPU/16GB` 上测量并写入：

`benchmark.json`

文档和 README 使用“目标 / 实测”两栏：

| 指标 | 设计目标 | ModelScope 实测 |
|---|---|---|
| 冷启动 | 部署后填写 | 待测，不提前编数字 |
| Demo 端到端耗时 | 用户可接受，部署后锁定 | 待测 |
| 峰值 RAM | `<16GB` 且留平台余量 | 待测 |
| 输出 ZIP | 可下载、可解压 | 待测 |
| live 成功率/耗时 | 可选增强 | 待测 |

若实测超预算，依次缩小 AOI、减少并存数组、降低训练样本/树数、预置训练模型；不能先删科学校验。

### 13.4 Studio 验收

- [ ] 首次打开有清楚的空状态和一键 Demo；
- [ ] 无用户 API Key 也能跑完整 Demo；
- [ ] 首版 registry 只有 EasyLST 为 available；TLC/MFGWML 显示 planned/禁用和具体原因，不出现假运行按钮；
- [ ] 切换方法卡或比较选择时仍为同一两栏/六 Tab 组件树，不创建方法专页、第三栏或第七 Tab；
- [ ] 运行期间按钮状态正确，重复提交被阻止；
- [ ] 两个并发浏览器会话的 job 目录、消息和产物互不串；
- [ ] 上传错误扩展名、坏 CRS、超像元、zip traversal 都在计算前被拒绝；
- [ ] live 网络失败可回到 Demo，不让页面整体崩溃；
- [ ] 取消在当前窗口后生效，resume 产物与未取消基准一致；
- [ ] 所选共同/方法图层同色标、单位明确、NoData 透明；未声明中间量不显示；
- [ ] 独立指标和一致性指标是两张不同卡；
- [ ] 不同 protocol 的 method runs 不进入同一排名，UI 显示 `not_ranked_reasons`；
- [ ] 所有下载链接有效；
- [ ] UI 不显示 token、绝对路径、内部 traceback；
- [ ] 桌面、平板、手机宽度无严重裁切；
- [ ] 公开/隐身窗口可访问。

### 13.5 科学报告验收

- [ ] 方法公式与真实代码一致；
- [ ] EasyLST 当前特征确实为九维且没有 B12；扩展方法使用自己的 feature pool/version，不改写该事实；
- [ ] 每份报告含 method ID/version、implementation kind、引用/实现来源、protocol、bundle/hash 和排名资格；
- [ ] source item、日期、AOI、split、seed、参数、hash 完整；
- [ ] 所有指标从 `metrics.json` 读取；
- [ ] 报告样本数、coverage、matched count、valid fraction 完整；
- [ ] 旧原型指标只放历史说明，不混入新结果；
- [ ] 使用 TCR 约束的区域不被标为独立测试；
- [ ] 已知限制含原始热红外信息分辨率、同期无云、跨季节/跨区泛化和 live 网络依赖。

### 13.6 评分证据回归表

| 评分项 | 发布前证据 | 验收文件/页面 |
|---|---|---|
| 科学价值 | 问题、贡献边界、方法、真实 Demo、两类指标、限制 | contribution boundary、methodology、metrics、Studio 指标/地图 |
| 技术实现 | grid/manifest、空间 split、窗口化、fail-fast、测试 | tests、manifest、state/log、architecture |
| 开源贡献 | 公开链接、资源卡、版本/hash/来源 | README、Model/Dataset card、assets 清单 |
| 形式完成度 | 一键体验、进度、对照、下载、响应式 | 公开 Studio、截图/GIF、smoke test |
| 社区影响力 | 真实访问/下载/反馈入口 | 平台真实数据；不在技术文档造数 |

---

## 14. 从创建创空间到比赛提交：逐步操作

### 14.1 创建开发空间

0. 队长在 D0–D1 先登录提交门户并完成表单演练；团队名最终确认后尽早注册以验证权限，成员逐人确认资格且未加入其他队。链接材料可在截止前持续更新，最终只以服务器最新回显为准；若队名尚未确认，只演练页面，不执行会锁定队名的注册。
1. 登录 ModelScope。
2. 进入“创建创空间”→“编程式创建”→“自定义创建”。
3. 填写英文名、中文名、描述；开发期可设私有。
4. 选择 Gradio SDK，显式选择 6.2.0。
5. 选择经 deploy schema 再次核实的 Python 3.11 base image。
6. 选择免费 `platform/2v-cpu-16g-mem`；不要未经团队授权选择付费资源。
7. 创建后保留平台生成 README YAML。

如果选择“快速创建并部署”，才使用第 10.5 节 `ms_deploy.json` 并上传整个目标工程文件夹。

### 14.2 推送工程

1. 安装 Git 和 Git LFS。
2. 从空间“空间内容”页复制其实际 Git URL。
3. clone 远端初始化仓库，不在包含其他项目的 workspace 根目录直接 `git init`。
4. 将目标工程复制到该独立仓库。
5. 大文件按平台提示使用 LFS；完整大场景分流 Dataset repo。
6. 本地运行 unit/integration/smoke。
7. `git add`、有意义的 commit、push 到远端当前默认分支（通常为 `master`，以空间内容页/`git remote show origin` 为准）。
8. 不使用 force push。

官方文档展示的 token URL 形式可以工作，但团队应优先使用凭据管理器/平台提供流程，避免 token 留在命令历史或日志。

### 14.3 配置与上线

1. 在 Settings 添加普通变量和 Secret；不把 token 写到代码、README、`ms_deploy.json`。
2. 点击“立即发布/上线”。
3. 检查 build log：依赖、系统包、Gradio/Studio 版本。
4. 检查 run log：入口、Demo asset、端口、首次请求。
5. 变量或 Secret 更新后重启空间。
6. 使用隐身窗口跑完“一键 Demo→地图→指标→下载”。
7. 若原生路径满足验收，不启用 Docker；只有满足第 10.6 节条件才切换。

### 14.4 发布 Model/Dataset 资源

1. 创建真实的 Model repo 和 Dataset repo。
2. 根据第 10.8 节分流文件。
3. 写卡片：来源、用途、输入输出、版本、指标定义、限制和引用。
4. 使用 CLI/SDK/Git 上传，锁 release tag/commit。
5. 从全新目录按文档下载一次，验证 size/hash 和可读性。
6. 将真实 URL/revision 回填 Studio README 和 demo manifest。
7. 未创建的占位符清零后才过发布门禁。

### 14.5 公开发布

1. 将 Studio 从私有改为公开。
2. 确认 Model/Dataset 也按提交需要公开。
3. 无登录访问 Studio、README、模型卡、数据卡和下载。
4. 固定 release commit/tag，记录 URL、commit、部署时间和截图。
5. 页面上只宣传已经通过验收的能力；P2 写 Roadmap。

### 14.6 填写比赛门户

由队长登录提交入口，按第 1.4 节填写：

1. 团队名：注册前最终确认，因为提交后锁定。
2. 队长邮箱和 member1 信息。
3. 项目名。
4. 英文摘要：建议 99 words 以内；最终以前台即时校验为准。
5. 成员 2–5：若填写，三项成组。
6. `artifact_links`：至少公开 Studio；后续 Notebook/代码按真实完成情况补。
7. `learn_article_link`：真实 Learn 文章 URL；未完成前不要伪造。
8. `model_dataset_links`：真实 Model/Dataset URL 和必要说明。
9. 页面要求的 tag/topic 确认。
10. 点击保存/提交，确认成功回显；再次打开验证最新内容仍在。

截止前可以修订并重新提交；仅服务器保存的最新版本进入评审。保留：提交成功截图、最终表单文本副本、全部 URL、release commit/tag、UTC+8 时间。最终以队长登录后的服务器回显为准。

---

## 15. 对 v2 的逐项修订说明

| v2 原设计 | 审查结果 | 最终修订 |
|---|---|---|
| 核心算法零改动 | 不成立；存在 TTRI 泄漏、格网映射、TCR/导出/评估问题 | 方法主线保留，先完成 P0 科学纠错 |
| split 后直接得到九特征 NPZ | TTRI 尚未拟合且 NPZ 缺地形变量，阶段顺序不成立 | base NPZ（8光谱+3地形）→只用 base_train 拟合 TTRI→model NPZ（九特征） |
| Demo 与训练共用单一路径 | 固定 RF 若重拟合 TTRI 会 hash 不匹配 | train/pretrained 两个显式 profile；Demo 只加载匹配系数/模型/split |
| raw 名称直接叫 LST/反射率 | 现 acquisition 实际仍是 DN，容易漏定标或双定标 | raw DN 与 `features.lst_30m_K`/reflectance 物理量分名、分 artifact key |
| TCR 双线性插值同时宣称严格守恒 | 数学上不能保证回聚合等于参考值 | canonical 30/10 严格嵌套，P0 粗像元内常数残差；平滑守恒另立版本 |
| `srtm` + asset `data` fallback | Planetary Computer 无 `srtm` collection | 改为 `nasadem/elevation`；首选 `cop-dem-glo-30/data`，映射做真 item 测试 |
| export 后再 evaluate | ZIP 无法包含尚未生成的 metrics/report | CORRECTING→EVALUATING→RENDERING→PACKAGING，分别可恢复 |
| `studio/app.py` | ModelScope 默认入口不在子目录 | 根目录 `app.py`；业务放根包 `geothermoai/`，避免未安装 src-layout |
| 三栏 UI | 与用户指定 coder_artifacts 骨架不符，右侧过窄 | 改成官方 `md=8/16` 左对话/右产物两栏 |
| `antdx.Bubble.List` 含思考链 | 隐藏推理不适合科学审计 | 用 pro.Chatbot/Sender 展示行动日志、参数、规则、产物和错误 |
| Folium 代码片段 | 缺 CRS、色标、变量和大图控制 | WGS84 bounds、降采样 RGBA、统一色标、受控 iframe |
| 运行时输入 API Key | 公共页面存在泄露/记录风险 | 空间所有者用 Secret；无 token 降级 |
| Chroma + sentence-transformers P0 | 增加冷启动、依赖、存储和隐私复杂度 | P0 JSON/state，P1 SQLite 实验，P2 受控 RAG |
| 先做 XGB/LGBM/MLP Benchmark | 当前 split/特征泄漏会让比较失真 | P2 在公平模型接口和空间 split 后实施 |
| “多模型”只抽象为单一 `fit/predict` | 无法容纳无全局训练的 TLC 类或复合 bundle 的 MFGWML 类；输出和 UI 仍硬编码 RF | 增加静态 MethodRegistry、adapter、method/comparison namespace、ProtocolSpec ID+hash gate 和固定六 Tab 的数据驱动 presenter |
| TLC/MFGWML 直接作为已支持选项 | 本轮没有实现算法，且 TLC 论文主验证与 MFGWML/本项目协议不同 | P0 只放准确方法卡和禁用态；P2 按论文复现→adaptation→共同协议比较逐项升档 |
| LLM 主导调参 | 旧自动调参分支不可达，且 test/预算边界不清 | 受控 train/val 搜索；LLM 只提议/解释 |
| 断点靠搜索最新文件 | 会串项目与会话 | manifest + artifact key + hash 的 resume |
| 并发下载固定 3 且节省 40–60% | 收益未实测，可能受远端限流 | 初始 2，可配置；以 benchmark 为准 |
| `packages.txt` 固定三包 | 不保证 Python osgeo 兼容 | clean build 证据驱动；rasterio 优先试，Docker 条件后备 |
| Gradio `>=6.0,<=6.8` | 范围正确但平台默认可能越界 | 精确锁 `gradio==6.2.0` + `modelscope_studio==2.0.2` |
| Docker 未决 | 不应为美观或想当然使用 | 普通 Gradio 主线；只有明确依赖门禁失败才 Docker |
| 实验报告由 LLM 生成 | 可能产生数字幻觉 | 数值/表格全由 metrics/manifest，LLM 仅可选解释 |
| `uhi_analysis/time_series/eco_response` | 当前没有可靠多期/边界链路 | P2 Roadmap，不写已支持 |

保留不变的核心思想：EasyLST 作为参考科学工作流，GeoThermoAI 提供受控、可审计的方法适配与可视化，Agent 在最需要自然语言灵活性的环节增强，科学计算保持确定性。这里的“可扩展”是仓库内静态白名单和测试门禁，不是公共服务动态执行第三方代码。

---

## 16. 发布前最终清单

### 16.1 科学与工程

- [ ] raw DN 与 K/reflectance 物理量 artifact 分离；没有漏定标或双定标。
- [ ] split 输出 base NPZ，TTRI 只用 base_train，随后另写 model NPZ；无泄漏/秩门禁通过。
- [ ] GridPlan 唯一生成，canonical 30/10 同 CRS/origin 严格 3×；代码无业务 `row/3`。
- [ ] 主指标使用 buffered spatial split；随机 split 只标 debug。
- [ ] evaluation/production constraint mask 分开；独立粗尺度 hold-out 与 coarse consistency 不混用。
- [ ] TCR 常数残差在非均匀/NoData fixture 上通过守恒。
- [ ] 不产生 10 m 全图 CSV；推理/导出窗口化。
- [ ] state/manifest 单一职责、artifact/hash 完整；无 global latest。
- [ ] MethodSpec/MethodResult、ProtocolSpec 稳定 hash、method/comparison namespace/state 和公共 protocol gate 落地；两个 fake adapters 不覆盖产物，adapter 不能自授排名资格。
- [ ] 公共 registry 只把通过门禁的 adapter 标 available；TLC/MFGWML 首版保持 planned，不引入假依赖/假输出。
- [ ] fail-fast、cancel、分 stage resume、job/session 所有权和两会话隔离通过。
- [ ] 只加载固定 revision/hash 的受信 joblib；upload 不接受模型二进制。
- [ ] Demo 内置 P0 input/model/TTRI/split，无网、无用户 token 真运行。

### 16.2 UI

- [ ] coder_artifacts 的整页两栏骨架，而非组件橱窗。
- [ ] 方法扩展仍复用两栏与六 Tab；方法选择/比较只更新既有组件的数据。
- [ ] 空、加载、成功、失败、取消状态完整。
- [ ] 地图/对照/指标/产物/血缘/日志六页完整。
- [ ] 地图同色标、单位、日期、分辨率明确。
- [ ] 下载使用 Gradio File；二进制不走 JS Blob。
- [ ] 移动端、隐身窗口、错误上传均验收。

### 16.3 ModelScope 与提交

- [ ] `gradio==6.2.0` / `modelscope_studio==2.0.2` 精确锁定。
- [ ] 根包 `geothermoai` 可导入，两个 JSON preset、method/protocol specs 可加载且 hash 稳定，workdir 原子探针通过。
- [ ] build/run log 无阻断；2vCPU/16GB benchmark 有实测。
- [ ] Secret 不在代码、日志、浏览器响应和 Git 历史。
- [ ] Studio/Model/Dataset 真实公开 URL 与 revision 已回填。
- [ ] README 无虚构 ID、虚构指标或未实现能力。
- [ ] 发布收尾补齐必要许可证/资源许可说明，不以此推翻当前路线。
- [ ] 所有接口硬必填字段及已实际完成的可选材料均保存成功；摘要按门户即时限制；团队信息完整；未完成的可选链接不伪造。
- [ ] 截止前从外部网络最后复核全部链接。

---

## 17. 事实来源与禁止幻想清单

### 17.1 本地主要依据

| 主题 | 文件 |
|---|---|
| 赛事规则/评分/资源 | `比赛要求/GRSS x 魔搭社区 _ GeoAI 开源实践挑战赛启动.pdf`、`比赛要求/geoai比赛.png` |
| 原升级思路 | `技术文档/GeoThermoAI_ModelScope升级规划_v2.md` |
| 当前代码 | `GeoThermoAI - 副本/` 全部业务 Python、UI JS/CSS/HTML、配置与历史对话 |
| 项目方法/历史文档 | `documents/开发参考/GeoThermoAI_开发文档.md`、`documents/测绘国赛PPT和报告/`、原 EasyLST DOCX/PPTX |
| TLC 已读文献材料 | `文献/newgeothermal/readers/04_TLC_Guo2022/逐字精讲.md`、`文献/newgeothermal/readers/_extract/TLC_print.txt`；Guo et al. 2022，DOI `10.1016/j.rse.2022.112915` |
| MFGWML 已读文献材料 | `文献/newgeothermal/readers/05_MFGWML_Xu2021/逐字精讲.md`、`技术文档/GeoThermoAI技术路线.md`；Xu et al. 2021，DOI `10.3390/rs13061186` |
| ModelScope 引用范围 | `docs/modelscope/引用指南.md` |
| 创空间创建/入口/依赖/持久化 | `docs/modelscope/魔搭操作文档/创空间/创空间创建与搭建.pdf` |
| quick deploy/schema | `docs/modelscope/魔搭操作文档/创空间/快速创建并部署.pdf` |
| Docker | `docs/modelscope/魔搭操作文档/创空间/Docker创空间介绍.pdf` |
| 资源规格 | `docs/modelscope/魔搭操作文档/创空间/创空间资源规格 .pdf` |
| 数据集上传/下载 | `docs/modelscope/魔搭操作文档/数据集/数据集的上传 .pdf`、`数据集的下载 .pdf` |
| Studio 组件版本/API | `docs/modelscope/02-组件库/modelscope-studio/README-zh_CN.md`、`pyproject.toml`、`.wiki/zh`、`docs` |
| 指定 UI 骨架 | `docs/modelscope/02-组件库/modelscope-studio/docs/layout_templates/coder_artifacts/demos/app.py` |

### 17.2 在线官方依据（2026-07-31 核验）

- 赛事页：`https://modelscope.cn/events/263`
- AP-GARSS 规则页：`https://www.apgarss.com/website/GeoAI-Challenge`
- 提交入口：`https://modelscope.cn/studios/GRSS-Student-Chapter-Beijing/APGARSS-GeoAI-Challenge`
- 提交应用公开 OpenAPI：`https://grss-student-chapter-beijing-apgarss-geoai-challen.ms.show/openapi.json`
- deploy schema：`https://modelscope.cn/api/v1/studios/deploy_schema.json`
- 数据集下载：`https://modelscope.cn/docs/datasets/download`
- 数据集上传：`https://modelscope.cn/docs/datasets/upload`
- 创空间创建：`https://modelscope.cn/docs/studios/create`
- 快速创建：`https://modelscope.cn/docs/studios/quick-create`
- Docker：`https://modelscope.cn/docs/studios/docker`
- API-Inference：`https://modelscope.cn/docs/model-service/API-Inference/intro`
- ModelScope Studio 源码/示例：`https://github.com/modelscope/modelscope-studio`
- ModelScope Hub：`https://github.com/modelscope/modelscope_hub`
- Planetary Computer Copernicus DEM collection/asset：`https://planetarycomputer.microsoft.com/api/stac/v1/collections/cop-dem-glo-30`
- Planetary Computer NASADEM collection/asset：`https://planetarycomputer.microsoft.com/api/stac/v1/collections/nasadem`

### 17.3 明确禁止写入实现或 README 的内容

- 未查询 schema 就自造 SDK/base image/hardware 值；
- 自造 ModelScope 不支持的配置文件或 API；
- 将 `ms_deploy.json` 说成所有 Git 部署必需；
- 将 pip `gdal` 与系统 GDAL 未验证组合写成“必定可用”；
- 假设平台自动注入 `MODELSCOPE_ACCESS_TOKEN`；
- 假设某个 API model ID 永久可用；
- 把赛事“最高 2,000 次”写成无条件固定额度；
- 把 `/mnt/workspace` 写成永久可靠的唯一存储；
- 把旧日志或报告数字硬编码成新 Demo 结果；
- 把 TCR 约束一致性包装成独立精度；
- 把 10 m 输出格网包装成真实 10 m 热红外观测/ground truth accuracy；
- 把不存在的 Planetary Computer `srtm` 写成 collection，或假设所有 DEM asset 都叫 `data`；
- 用普通 bilinear TCR 后声称逐粗像元严格守恒；
- 从用户上传包或未锁 revision/hash 的远端加载 joblib/pickle；
- 把无网 P0 Demo 的关键 input/model/split 只写成远程指针；
- 把计划中的 Notebook、RAG、XGBoost、UHI/时序写成已实现。
- 把 TLC 误写成其他全称，或在没有论文协议复现时声称 TLC 已验证 30→10 m；
- 把 MFGWML 简化成单一 XGBoost/GWR，或用 EasyLST 当前九特征声称已经 1:1 复现；
- 将 TLC/MFGWML 的论文指标当作当前武汉运行结果，或把不同 protocol 的 method runs 混排；
- 给某一方法暗中追加 TCR/其他后处理却不更换 method ID 和 implementation kind；
- 让上传包、LLM 或配置文件注册/执行任意 adapter、module path、entrypoint 或 shell command。

---

## 18. 最终 Definition of Done

当且仅当以下条件同时满足，才能称为“可照着实现并完成的 GeoThermoAI ModelScope Studio 提交版”：

1. 新工程按第 5 节组织，根 `app.py` 在 `gradio==6.2.0` / `modelscope_studio==2.0.2` 下可导入并构建。
2. 第 6 节数据/格网/产物/指标契约均有代码校验和测试。
3. 第 7 节科学纠错全部通过，不再依赖目标泄漏、`row/3` 或全图 CSV。
4. Demo 在无用户密钥、无外网的情况下执行真实核心链并生成全套产物。
5. 第 9 节左对话/右产物页面完成，六类产物可查看和下载。
6. 失败、取消、恢复和并发隔离通过验收，页面不泄露 Secret/内部路径。
7. ModelScope 私有验收后公开，真实 Model/Dataset/README 链接互通。
8. 所有页面数值来自当前 release 的 `metrics.json`/manifest，来源与版本可追溯。
9. 门户硬必填字段与已完成材料链接均保存成功，外部访问验证通过，并留存 release/最终提交证据。
10. EasyLST 已通过统一 MethodAdapter 运行；方法运行使用独立 namespace；ProtocolSpec ID/hash 与 method/comparison 状态作用域通过测试；TLC/MFGWML 可按第 7.9/12.3 节增量接入而无需修改主两栏/六 Tab UI；未实现方法只显示 planned 状态。

这套方案不是推倒重来：它保留 GeoThermoAI/EasyLST 的业务价值、方法主线、Skill/Agent 思路和已有交互经验；修订的重点是把原型中的隐式假设变成明确契约，把“能跑过一次”升级为“能公开稳定运行、能解释、能复核、能迁移”。

---

*文档版本：v3.1 最终执行版｜原 v2 保留｜v3.1 增补 TLC/MFGWML 方法族扩展契约与固定 UI 兼容设计｜最后核验：2026-07-31*
