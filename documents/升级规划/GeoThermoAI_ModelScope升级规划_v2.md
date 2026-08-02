# GeoThermoAI → ModelScope Studio 升级规划 v2

> **目标**: 将 GeoThermoAI 从桌面 PyWebView 应用迁移至 ModelScope Studio（Gradio + Ant Design X），完成从"LST 降尺度单任务工具"到"LLM-Orchestrated Workflow 驱动的热红外遥感 GeoAI Agent 平台"的升级。
>
> **架构定位**: LLM-Orchestrated Workflow — LLM 在最需要灵活性的地方（理解意图、推荐参数、分析结果）做智能增强，核心科学计算保持确定性工作流，确保可复现性和可调试性。
>
> 基于 v1 规划 + 多轮技术讨论的最终整合版。

---

## 一、现状概要

### 1.1 当前技术栈

| 层级 | 技术 | 备注 |
|---|---|---|
| UI 容器 | PyWebView（桌面原生窗口） | 迁移目标：废弃 |
| 前端 | 原生 HTML + CSS + JS | 迁移目标：废弃 |
| API 桥 | `window.pywebview.api.xxx()` | 迁移目标：改为 Gradio 事件 |
| AI 引擎 | OpenAI 兼容 API（DeepSeek） | 保留，配置方式改为运行时输入 |
| Agent | 单 Agent + JSON 计划 + Skill 编排 | 保留并增强 |
| Skill 系统 | 注册表模式，8 个内置 Skill | 保留，扩展更多模型 Skill |
| 核心算法 | scikit-learn RF / rasterio / GDAL | 保留，零改动 |
| 数据源 | Microsoft Planetary Computer STAC | 保留 |

### 1.2 当前内置 Skill

| Group | Skill 名称 | 说明 |
|---|---|---|
| `data_process` | `data_acquisition`, `data_pipeline` | 数据获取与预处理 |
| `ttri_compute` | `ttri_compute` | 地形热响应指数计算 |
| `model_train_predict` | `rf_model`（唯一） | 模型训练与预测 |
| `tcr_compute` | `tcr_compute` | 热约束残差修正 |
| `lst_export` | `lst_export` | LST 最终结果导出 |
| `accuracy_eval` | `accuracy_eval` | 空间一致性评估 |
| `ai_assistant` | `ai_assistant` | 纯 LLM 对话交互 |

---

## 二、技术选型总览

### 2.1 前端

| 组件 | 选型 | 版本要求 | 说明 |
|---|---|---|---|
| 应用框架 | **Gradio Blocks** | ≥ 6.0.0, ≤ 6.8.0（ModelScope 约束） | 主 UI 容器，三栏布局（25%/45%/30%） |
| 对话组件 | **gr.Chatbot** + **gr.Textbox** | Gradio 内置 | 气泡式对话（用户/AI 分左右）。注：原计划的 antdx.Bubble/Sender 是 React/npm 库，非 pip 包，Gradio 中对应的标准组件即 gr.Chatbot |
| 思考链展示 | **`<details>/<summary>` HTML** + 自定义 CSS | Gradio 内置渲染 | AI 思考过程以可折叠小字区置于气泡正文上方，默认折叠（DeepSeek“已深度思考”样式） |
| 侧边栏 | **gr.Accordion** + **gr.Radio** | Gradio 内置 | 项目→对话两级结构：项目可折叠，对话隶属项目，体现记忆隔离 |
| API 设置面板 | **gr.Dropdown / gr.Textbox / gr.Number** | Gradio 内置 | 兼容 OpenAI / Anthropic 双格式，真实流式调用 LLM |
| 交互地图 | **Folium** + `LayerControl` | ≥ 0.16.0 | 多图层交互式地图（LST/NDVI/DEM/真彩色等），每层可独立勾选/取消、调不透明度，类似 GEE 图层管理器，需将 UTM 坐标转为 WGS84 经纬度 |
| 可视化图表 | **Matplotlib** | 已有依赖 | Benchmark 柱状图、UHI 图、时序图（配置中文字体） |
| 进度/状态 | Gradio 原生 `gr.Progress` + `gr.Dataframe` | 内置 | 工作流 7 步进度状态表 |
| LLM 调用 | **requests**（SSE 流式） | 已有依赖 | OpenAI `/chat/completions` 与 Anthropic `/v1/messages` 双格式流式请求 |

### 2.2 后端

| 组件 | 选型 | 说明 |
|---|---|---|
| 核心语言 | Python 3.10+ | 已有 |
| AI 引擎 | OpenAI 兼容 API（DeepSeek / Kimi / OpenAI） | 运行时传入 API Key，不硬编码 |
| 遥感处理 | GDAL + rasterio + numpy | 已有，保留 |
| 机器学习 | scikit-learn → 扩展 xgboost, lightgbm, catboost, extra_trees | 5 个模型：RF / XGBoost / LightGBM / CatBoost / ExtraTrees |
| **向量数据库** | **ChromaDB**（PersistentClient） | 本地持久化，零运维，每个项目一个 Collection + 全局共享 Collection |
| **文本嵌入** | **sentence-transformers/all-MiniLM-L6-v2** | ~80MB，本地离线运行，无需 GPU |
| 记忆管理 | 自定义 `MemoryManager` + ChromaDB + JSON | 双重查询：RAG 语义检索（global_knowledge + project经验）+ JSON 精确查询（实验记录/偏好），LLM 自主融合 |
| 数据生命周期 | `MemoryManager` 级联删除 | 删除对话或项目时同步清理 JSON 文件 + ChromaDB Collection，前端弹框确认 |
| **并发下载** | **concurrent.futures.ThreadPoolExecutor** | max_workers=3，IO 密集型 |
| 可视化 | 自定义 `LayerVisualizer`（`core/visualization.py`） | Folium 多图层管理，色带渲染，UTM→WGS84 坐标转换 |
| 报告生成 | Jinja2（可选） | 实验报告模板 |
| 系统依赖 | gdal-bin, libgdal-dev, libproj-dev | 通过 ModelScope `packages.txt` 安装 |

### 2.3 架构总览

```text
┌─────────────────────────────────────────────────────────┐
│                 ModelScope Studio 容器                    │
│  ┌───────────────────────────────────────────────────┐  │
│  │              Gradio Blocks (UI 层)                 │  │
│  │  ┌─────────────┐  ┌──────────┐  ┌──────────────┐ │  │
│  │  │ 对话面板      │  │ 工作面板  │  │ 可视化面板    │ │  │
│  │  │ (antdx)     │  │(参数表单)│  │ (folium地图) │ │  │
│  │  └──────┬──────┘  └────┬─────┘  └──────┬───────┘ │  │
│  └─────────┼──────────────┼───────────────┼─────────┘  │
│            │              │               │            │
│  ┌─────────▼──────────────▼───────────────▼─────────┐  │
│  │              LLM Agent 引擎                       │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │  GeoThermoAgent                             │  │  │
│  │  │  ├─ _get_phase_prompt()    ← 动态角色切换   │  │  │
│  │  │  │  │    ├ 规划专家 │ 调参专家 │ 诊断专家 │   │  │  │
│  │  │  ├─ _build_system_prompt()  ← 注入记忆      │  │  │
│  │  │  ├─ _execute_plan()        ← "思考链"输出   │  │  │
│  │  │  ├─ _llm_decide_tuning()   ← LLM智能调参   │  │  │
│  │  │  ├─ _rule_safeguard()      ← 规则兜底       │  │  │
│  │  │  ├─ _check_exceptions()    ← 主动提问       │  │  │
│  │  │  ├─ _analyze_result()      ← 结果解读       │  │  │
│  │  │  └─ check_step_ready()     ← 断点检查       │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  └──────────────────────┬───────────────────────────┘  │
│                         │                              │
│  ┌──────────────────────▼───────────────────────────┐  │
│  │              Skill 注册表 + 执行引擎              │  │
│  │  data_process │ model_train_predict(5模型) │ tcr │  │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │  ThreadPoolExecutor (并发下载)              │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │  check_step_ready()   ← 断点依赖检查        │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  └──────────────────────┬───────────────────────────┘  │
│                         │                              │
│  ┌──────────────────────▼───────────────────────────┐  │
│  │         记忆系统（Memory / core/memory/）         │  │
│  │  ┌────────────────┐ ┌──────────────────────────┐ │  │
│  │  │ ChromaDB(RAG)  │ │ JSON (结构化实验记录)     │ │  │
│  │  │ 项目A Collection│ │ experiments.json         │ │  │
│  │  │ 项目B Collection│ │ preferences.json         │ │  │
│  │  │ global_knowledge│ │ (项目隔离)               │ │  │
│  │  │ ← 双重查询注入  │ │ ← 精确数值查询           │ │  │
│  │  └────────────────┘ └──────────────────────────┘ │  │
│  │  数据生命周期：delete_conversation / delete_project │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │        可视化模块（core/visualization.py）          │  │
│  │  LayerVisualizer / Folium多图层 / UTM→WGS84坐标   │  │
│  │  GEE风格图层控制器 / 色带渲染                      │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │           核心算法模块（零改动）                    │  │
│  │  rf_model.py / ttri.py / tcr.py / lst_final.py   │  │
│  │  export_geotiff.py / evaluation.py / ...          │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## 三、升级维度（按优先级排序）

### 3.1 [P0] 最小可行迁移 — ModelScope Studio UI

将 PyWebView 替换为 Gradio，核心算法零改动。

#### UI 布局

```
Gradio Blocks
├── gr.Row (header + Logo + 标题 + 技术标识)
├── gr.Row (main content)
│   ├── gr.Column (侧边栏 - 项目管理, 25%)
│   │   ├── gr.Button("＋ 新建项目")
│   │   ├── gr.Accordion("新建项目", 默认隐藏)
│   │   │   ├── gr.Textbox (项目名称输入)
│   │   │   └── gr.Button ("创建" / "取消")
│   │   ├── gr.Dropdown (选择项目 - 动态填充)
│   │   ├── gr.Radio (对话列表 - 动态填充)
│   │   ├── gr.Button("＋ 新建对话")
│   │   ├── gr.Accordion("新建对话", 默认隐藏)
│   │   │   ├── gr.Textbox (对话标题输入)
│   │   │   └── gr.Button ("创建" / "取消")
│   │   ├── gr.Textbox (项目目录路径)
│   │   ├── gr.Button ("保存目录")
│   │   └── gr.File (上传研究区域 GeoJSON/Shapefile)
│   ├── gr.Column (对话面板, 45%)
│   │   ├── gr.Markdown (当前对话标题 + 当前模型展示名称)
│   │   ├── gr.Chatbot (气泡对话，用户/AI 分左右)
│   │   │   └── AI 气泡内含 <details> 可折叠思考链
│   │   ├── gr.Textbox (输入框，placeholder 显示当前模型)
│   │   └── gr.Row (上传研究区 / 选择目录 / 清空对话)
│   └── gr.Column (工作面板, 30%)
│       ├── gr.Tabs
│       │   ├── Tab: 🔑 API 设置 (OpenAI/Anthropic 双格式)
│       │   ├── Tab: ⚙️ 参数设置 (机器学习方法 + 超参数)
│       │   ├── Tab: 🌍 地图浏览 (folium 多图层)
│       │   ├── Tab: 精度评估 (指标表格)
│       │   ├── Tab: 实验对比 (Benchmark 图表)
│       │   └── Tab: 工作流进度 (7 步状态表)
└── gr.Row (状态栏)
```

#### 侧边栏：动态项目管理

移除预置示例项目，改为完全动态的项目管理模式：

- **新建项目**：点击按钮展开隐藏输入区，输入项目名称后创建，项目添加到下拉列表
- **选择项目**：`gr.Dropdown` 动态填充已创建的项目列表
- **对话管理**：选中项目后，`gr.Radio` 显示该项目的对话列表；支持新建对话
- **项目目录**：`gr.Textbox` 输入路径，"保存目录"按钮持久化到项目状态
- **研究区域**：`gr.File` 上传 GeoJSON/Shapefile 文件
- **初始状态**：项目列表为空，对话列表为空，提示用户"请先新建项目"

#### 对话区：气泡 + 可折叠思考链

- **气泡形式**：`gr.Chatbot` 默认将用户消息置右、AI 消息置左，形成分侧气泡
- **思考链分离**：AI 的思考过程不混入正文，而是用 `<details><summary>💭 已深度思考（Xs）</summary>…</details>` 置于气泡正文上方，默认折叠、点击展开（DeepSeek 风格）
- **思考链来源**：优先取流式响应中的 `reasoning_content`（DeepSeek 等模型真实思考）；若无则展示模拟思考过程
- **历史净化**：回传 API 时用正则剥离 `<details>` 块，避免思考链重复进入上下文

#### API 设置面板（OpenAI / Anthropic 双格式）

参考旧版“工作面板-LLM API设置”，字段清单如下：

| 字段 | 组件 | 说明 |
|---|---|---|
| API 格式 | `gr.Dropdown` | OpenAI Chat Completions / Anthropic Messages，切换时联动 URL 提示与占位符 |
| 自定义请求地址 | `gr.Textbox` | OpenAI 补 `/chat/completions`；Anthropic 补 `/v1/messages` |
| 模型 ID * | `gr.Textbox` | 必填 |
| API 密钥 * | `gr.Textbox(password)` | 必填，不明文展示 |
| 模型展示名称 | `gr.Textbox` | 默认等于模型 ID，允许修改 |
| 上下文窗口-输入/输出 | `gr.Number` ×2 | **不设默认值**，留空由用户填写 |

**明确不设计的功能**：模型提供商预设列表、工具调用轮次配置、多模态开关。

**实时显示模型名**：保存后在对话区输入框上方与 placeholder 中同步显示“当前模型：{展示名称}”。

**真实流式调用**：填写 Base URL / API Key / 模型 ID 并保存后，发送消息即真实调用 LLM：
- OpenAI 格式：`POST {base}/chat/completions`，`Authorization: Bearer <key>`，SSE 流式
- Anthropic 格式：`POST {base}/v1/messages`，`x-api-key: <key>` + `anthropic-version`，system 单独传递，SSE 流式

#### 参数设置（机器学习方法 + 超参数）

独立 Tab 用于选择模型方法与调整超参数：

| 字段 | 组件 | 说明 |
|---|---|---|
| 机器学习方法 | `gr.Dropdown` | Random Forest / XGBoost / LightGBM / CatBoost / Extra Trees |
| n_estimators | `gr.Slider` (50–1000) | 决策树数量，默认 200 |
| max_depth | `gr.Slider` (5–50) | 最大深度，默认 25 |
| min_samples_split | `gr.Slider` (2–50) | 最小分裂样本数，默认 10 |
| min_samples_leaf | `gr.Slider` (1–20) | 叶节点最小样本数，默认 5 |

**后端开发注意事项**：
- 当用户只使用一种机器学习方法时，"实验对比" Tab 只展示该方法的结果（不显示多模型对比图表）
- 后端 `BenchmarkSkill` 应根据 `model_train_predict` 组中实际注册的 Skill 数量动态调整输出格式
- 单模型时输出标准指标表（R²/RMSE/MAE/特征重要性）；多模型时输出对比柱状图 + 表格

#### 需要处理的系统依赖（`packages.txt`）

```text
gdal-bin
libgdal-dev
libproj-dev
```

#### 需要修改的文件

| 文件 | 改动 | 难度 |
|---|---|---|
| `main.py` | 替换 `webview.create_window` → `gr.Blocks().launch()` | 高 |
| `ui/api.py` | 保留核心逻辑，适配 Gradio 事件 | 中 |
| `ui/index.html` | 废弃，功能由 Gradio 组件替代 | — |
| `ui/scripts/`, `ui/styles/` | 废弃 | — |
| `config/settings.json` | 移除硬编码的 API Key | 低 |

---

### 3.2 [P0] Benchmark 对比系统

**目标**：将 `model_train_predict` 组从单一 `rf_model` 扩展到多模型，增加 Benchmark Skill 做横向对比。

#### 新增模型 Skill

| Skill 名称 | 依赖 | 说明 |
|---|---|---|
| `xgboost_model` | `xgboost` | 梯度提升标杆，复杂地形通常优于 RF |
| `lightgbm_model` | `lightgbm` | 速度快，适合大样本场景 |
| `catboost_model` | `catboost` | 默认参数最鲁棒，不调参也表现稳定，适合中小数据集 |
| `extra_trees_model` | `sklearn.ensemble` | 零额外依赖，RF 极端随机化变体，方差更低、训练更快 |

#### Benchmark Skill

```python
class BenchmarkSkill(BaseSkill):
    """遍历 model_train_predict 组所有 Skill，汇总指标生成对比报告"""
    group = "benchmark"
    
    def execute(self, params):
        models = registry.get_group("model_train_predict")
        results = []
        for model_skill in models:
            result = model_skill.execute(params)
            results.append(result.data["test_metrics"])
        # 生成对比柱状图 + Markdown 表格
        return SkillResult(data={"comparison": results, "chart": chart_path})
```

#### 新增文件

- `core/xgboost_model.py`
- `core/lightgbm_model.py`
- `core/catboost_model.py`
- `core/extra_trees_model.py`
- `core/skills/builtin/benchmark.py`

#### 核心算法不受影响

`rf_model.py`、`ttri.py`、`tcr.py`、`lst_final.py`、`export_geotiff.py`、`evaluation.py` 等**均无需修改**。

---

### 3.3 [P0] 记忆系统（RAG + 结构化混合存储）

#### 为什么需要两种存储？

| 场景 | 需要 | 用谁 | 原因 |
|---|---|---|---|
| "查武汉上次RF的R²" | 精确数值 | **JSON** | 精确查询，不需要理解语义 |
| "上次武汉为什么效果不好？" | 上下文理解 | **RAG** | 需要搜索含"云量高""样本不足"等描述的记录 |
| "TTRI 公式是什么？" | 领域知识 | **RAG** | 非结构化知识，关键词难覆盖 |
| "用户偏好的云量阈值" | 简单键值 | **JSON** | 读写各一次，没必要向量化 |

**核心原则**：
- 能精确匹配的 → JSON（快、稳、易调试）
- 需要理解语义的 → RAG（灵活、能模糊匹配）
- 两者互补，而不是互相替代

#### RAG 在整个 Agent 生命周期中负责什么

```
┌──────────────────────────────────────────────────────────┐
│                    RAG 负责的场景                        │
│                                                          │
│ ① Agent 规划时 ← 检索项目历史经验，辅助决策               │
│    输入："武汉 地形复杂 LST"                               │
│    输出："2024-07 武汉 RF 使用 max_depth=35 取得 R²=0.89"│
│                                                          │
│ ② 用户问答时 ← 检索领域知识，增强回答质量                 │
│    输入："TTRI 和 DEM 的关系"                             │
│    输出："TTRI = a·DEM + b·Slope + c·cos(Aspect)"       │
│                                                          │
│ ③ 结果分析时 ← 检索相似案例，辅助解读                     │
│    输入："R²=0.72 丘陵地形 RF 模型"                      │
│    输出："类似案例曾通过增大 n_estimators 提升到 0.85"    │
│                                                          │
│ ④ 新项目启动时 ← 从全局知识库获取经验                     │
│    输入："武汉 LST 降尺度 注意事项"                       │
│    输出："武汉夏季云量高，建议 cloud_threshold=50"        │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│                RAG 不负责的场景（交给 JSON）              │
│                                                          │
│ ✗ 精确数值查询（"最高R²是多少"）→ JSON排序              │
│ ✗ 用户偏好读写（"我的API Key"）→ JSON键值对             │
│ ✗ 对话历史加载（"上一条消息"）→ JSON按ID加载            │
└──────────────────────────────────────────────────────────┘
```

### 存储架构全景

整个记忆系统由**两种存储介质 + 三种数据类型**组成，每种数据存在特定的位置：

```
                          GeoThermoAI 记忆系统
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
    JSON 文件                ChromaDB                  运行时内存
  （文件系统）             （向量数据库）                （Python变量）
        │                         │                         │
  ┌─────┴─────────┐    ┌──────────┴──────────┐        ┌────┴────┐
  │ 对话消息       │    │ global_knowledge    │        │当前对话 │
  │ conv_001.json │    │ Collection          │        │消息列表 │
  │ conv_002.json │    │                     │        └─────────┘
  ├────────────────┤    │ · 领域通识          │
  │ 实验记录       │    │ · 算法专识          │
  │ experiments   │    │ （跨项目共享）       │
  │ .json         │    └──────────────────────┘
  ├────────────────┤    ┌──────────────────────┐
  │ 用户偏好       │    │ project_{id}        │
  │ preferences   │    │ Collection          │
  │ .json         │    │                     │
  └────────────────┘    │ · 项目历史经验       │
                         │ · 实验描述性记录     │
                         │ （项目内隔离）       │
                         └──────────────────────┘
```

#### 组件关系说明

| 概念 | 本质 | 类比 |
|---|---|---|
| **ChromaDB** | 一个向量数据库软件（类似 SQLite） | 数据库管理系统 |
| **Collection** | ChromaDB 里的一张"表" | 数据表 |
| **`global_knowledge`** | 一个 Collection，存全局共享知识 | 共享词典 |
| **`project_{id}`** | 一个 Collection，每个项目独立一个 | 项目笔记本 |
| **JSON 文件** | 文件系统上的文本文件 | Excel表格 + Word文档 |
| **RAG** | 一个流程：存 → 转向量 → 语义搜索 → 注入 prompt | 查询方法 |

#### 每类数据存在哪里、怎么查

| 数据类别 | 存储介质 | 具体位置 | 查询方式 | 共享范围 |
|---|---|---|---|---|
| **对话消息** | JSON 文件 | `data/projects/{项目}/conversations/{对话}.json` | 按 conv_id 精确加载 | 该对话内 |
| **实验记录**（结构化：R²、参数等） | JSON 文件 | `data/projects/{项目}/memory/experiments.json` | 精确排序/过滤（`get_best()`） | 项目内共享 |
| **用户偏好**（键值对） | JSON 文件 | `data/projects/{项目}/memory/preferences.json` | 按 key 精确读取 | 项目内共享 |
| **领域通识**（Landsat参数、遥感原理） | ChromaDB | `global_knowledge` Collection | RAG 语义检索 | **所有项目共享** |
| **算法专识**（TTRI公式、TCR机制） | ChromaDB | `global_knowledge` Collection | RAG 语义检索 | **所有项目共享** |
| **项目经验**（描述性：什么参数效果好） | ChromaDB | `project_{项目id}` Collection | RAG 语义检索 | 项目内隔离 |

#### 层级关系一句话

> JSON 是**文件**，ChromaDB 是**数据库**，Collection 是**表**，RAG 是**查法**，`global_knowledge` 是一张**共享表**，`project_{id}` 是给每个项目单独开的**隔离表**。

#### 核心设计原则：不决策，全查

Agent 不需要判断"这次该用 RAG 还是 JSON"。**每次构建 prompt 时两条路都走，LLM 自己挑有用的。**

```
用户说"帮我看看武汉 RF 的情况"
    ↓
┌─ RAG 语义检索 ─────────────────────┐
│  输入：用户原始文本"武汉 RF"        │
│  输出：语义相似的自然语言段落         │
│  "武汉 2024-07 RF：R²=0.87，       │
│   max_depth=35，特征 NDVI 最重要"   │
└───────────────────────────────────┘
    +
┌─ JSON 精确查询 ────────────────────┐
│  输入：提取字段 (region="武汉",     │
│         model="RF")               │
│  输出：结构化数据                   │
│  {"R2":0.87, "n_estimators":300}  │
└───────────────────────────────────┘
    ↓
合并注入 system prompt
    ↓
LLM 自行选择使用哪些信息
```

#### 这样设计的好处

| 问题 | 后果 |
|---|---|
| RAG 没查到？ | JSON 可能查到了，兜底 |
| JSON 没查到？ | RAG 可能查到了，兜底 |
| 两边数据矛盾？ | LLM 自己能发现并处理 |
| 以后加新存储？ | Agent 代码不用改，再加一条路就行 |

**不需要在 Agent 代码中写任何 if/else 路由逻辑。**

#### 实现：_enrich_with_memory()

#### RAG 的完整数据流

```
① 存什么进 RAG？

Agent 运行时自动写入：

  每次实验完成后 → auto_save_to_rag()
  ──→ 写入项目 Collection
  ──→ 文档内容示例：
      "武汉 2024-07 RF 实验：R²=0.87，RMSE=1.23K，
       n_estimators=300，max_depth=35，训练样本45,678，
       地形复杂(DEMσ=120m)，植被覆盖中等(NDVI=0.35)，
       温度范围298-315K。特征重要性：NDVI(0.28) > DEM(0.23) > NIR"
  ──→ metadata: {region, model, r2, date}

  系统初始化时 → 预置领域知识到全局 Collection：
  ──→ "Landsat 8 热红外波段：Band 10 (LWIR1) 100m分辨率，
       中心波长10.8μm，温度反演范围 200-400K"
  ──→ "TTRI（地形热响应指数）公式：
       TTRI = a·DEM + b·Slope + c·cos(Aspect)"
  ──→ "Sentinel-2 波段：B2=Blue(10m), B3=Green(10m), 
       B4=Red(10m), B8=NIR(10m), B11=SWIR1(20m)"

② 怎么从 RAG 查？

  Agent 构建 system prompt 时 → search_rag()
  ──→ 输入：用户指令（"武汉 RF"）
  ──→ 查询项目 Collection + 全局 Collection
  ──→ 各取 top-3 结果，共 6 条
  ──→ 格式化为上下文注入 prompt

③ 注入后的效果
```

#### RAG 注入的 system prompt 示例

```
## 当前项目历史经验（RAG 检索自 project_wuhan_2024）
- 2024-07-16：RF，n_estimators=300, max_depth=35，
  R²=0.87，特征重要性: NDVI(0.28) > DEM(0.23)
- 2024-07-16：XGBoost，n_estimators=200, max_depth=12，
  R²=0.89（待改进：训练时间较长）

## 领域知识参考（RAG 检索自 global_knowledge）
- 武汉属于亚热带季风气候，夏季云量高，建议 cloud_threshold=50
- 复杂地形区域建议使用深树（max_depth≥30）捕捉地形细节

## Agent 基于这些信息做出的决策
→ 用户未指定模型 → 根据历史，RF 和 XGBoost 在武汉效果接近
→ 但用户上次表达过对 XGBoost 训练时间的不满
→ 本次默认选择 RF，参数参考历史最优
```

#### ChromaDB 实现细节

```python
# core/memory/rag_store.py
import chromadb
from chromadb.utils import embedding_functions
import json


class ChromaRAG:
    """基于 ChromaDB 的语义检索层"""
    
    def __init__(self, persist_dir: str):
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2",  # 轻量，~80MB
        )
        # 两个全局 Collection
        self._ensure_global_knowledge()
    
    # ── Collection 管理 ──
    
    def project_collection(self, project_id: str):
        """每个项目一个 Collection，天然隔离"""
        return self.client.get_or_create_collection(
            name=f"project_{project_id}",
            embedding_function=self.embed_fn,
        )
    
    def global_collection(self):
        """全局知识库（所有项目共享）"""
        return self.client.get_or_create_collection(
            name="global_knowledge",
            embedding_function=self.embed_fn,
        )
    
    # ── 写入 ──
    
    def save_experience(self, project_id: str, exp_data: dict):
        """实验结束后自动保存经验到 RAG"""
        doc = self._format_experience_doc(exp_data)
        doc_id = f"exp_{project_id}_{exp_data['id']}"
        collection = self.project_collection(project_id)
        
        # 检查是否已存在（防止重复写入）
        existing = collection.get(ids=[doc_id])
        if existing and existing["ids"]:
            collection.update(ids=[doc_id], documents=[doc])
        else:
            collection.add(
                documents=[doc],
                ids=[doc_id],
                metadatas=[{
                    "type": "experiment",
                    "region": exp_data.get("region", ""),
                    "model": exp_data.get("model", ""),
                    "r2": exp_data.get("metrics", {}).get("R2", 0),
                }],
            )
    
    def save_knowledge(self, content: str, tags: list):
        """添加领域知识到全局知识库"""
        collection = self.global_collection()
        doc_id = f"knowledge_{hash(content)}"
        existing = collection.get(ids=[doc_id])
        if not existing or not existing["ids"]:
            collection.add(
                documents=[content],
                ids=[doc_id],
                metadatas=[{"type": "knowledge", "tags": tags}],
            )
    
    # ── 检索 ──
    
    def search_for_agent(self, project_id: str, query: str, n: int = 3) -> dict:
        """Agent 构建 prompt 时调用：同时检索项目经验 + 全局知识"""
        results = {
            "project_experiences": [],
            "global_knowledge": [],
        }
        
        # 查项目经验
        proj_col = self.project_collection(project_id)
        proj_result = proj_col.query(
            query_texts=[query],
            n_results=n,
            where={"type": "experiment"},
        )
        if proj_result["documents"] and proj_result["documents"][0]:
            results["project_experiences"] = [
                {"content": doc, "metadata": meta}
                for doc, meta in zip(
                    proj_result["documents"][0],
                    proj_result["metadatas"][0],
                )
            ]
        
        # 查全局知识
        global_col = self.global_collection()
        global_result = global_col.query(
            query_texts=[query],
            n_results=n,
        )
        if global_result["documents"] and global_result["documents"][0]:
            results["global_knowledge"] = global_result["documents"][0]
        
        return results
    
    # ── 文档格式化 ──
    
    @staticmethod
    def _format_experience_doc(exp: dict) -> str:
        """将实验记录格式化为 RAG 可检索的自然语言段落"""
        metrics = exp.get("metrics", {})
        features = exp.get("feature_importance", [])
        feat_str = ", ".join(
            f"{f['feature']}({f['importance']:.2f})"
            for f in features[:3]
        ) if features else "无特征重要性数据"
        
        # 附加调优轨迹（如果有）
        tuning = exp.get("tuning_trail", [])
        tuning_str = ""
        if tuning:
            rounds = []
            for t in tuning:
                r2 = t.get("metrics", {}).get("test_r2", "?")
                rounds.append(f"第{t.get('round')}轮:R²={r2}")
            tuning_str = f"，调优轨迹：{' → '.join(rounds)}"
        
        return (
            f"{exp.get('region', '未知')} {exp.get('date', '')} "
            f"{exp.get('model', '未知模型')} 实验："
            f"R²={metrics.get('R2', 'N/A')}, "
            f"RMSE={metrics.get('RMSE', 'N/A')}K, "
            f"MAE={metrics.get('MAE', 'N/A')}K, "
            f"参数={json.dumps(exp.get('params', {}), ensure_ascii=False)}, "
            f"训练样本={exp.get('train_samples', 'N/A')}, "
            f"特征重要性前三：{feat_str}"
            f"{tuning_str}"
        )

    # ── 知识库初始化 ──
    
    def _ensure_global_knowledge(self):
        """首次启动时填充预置知识"""
        collection = self.global_collection()
        if collection.count() > 0:
            return  # 已有数据，跳过
        
        knowledge_base = [
            # 公式
            {
                "content": "TTRI（地形热响应指数）公式：TTRI = a·DEM + b·Slope + c·cos(Aspect)，"
                           "其中DEM为数字高程模型，Slope为坡度，Aspect为坡向。"
                           "TTRI捕捉地形对地表温度的控制作用。",
                "tags": ["ttri", "公式", "地形"],
            },
            {
                "content": "TCR（热约束残差）公式：TCR_30m = LST_true_30m - LST_pred_30m_block，"
                           "用于修正模型在30m尺度的系统偏差，然后将残差空间化到10m。",
                "tags": ["tcr", "公式", "残差"],
            },
            # 卫星参数
            {
                "content": "Landsat 8/9 Collection 2 Level-2 产品：ST_B10（Band 10, LWIR1）"
                           "100m分辨率，重访周期16天。辐射定标：LST = DN × 0.00341802 + 149.0 (K)。",
                "tags": ["landsat", "波段", "LST"],
            },
            # 处理经验
            {
                "content": "影像配对规则：Landsat 与 Sentinel-2 的时间差 ≤ 2天。"
                           "云覆盖阈值默认20%，当训练样本不足时可放宽至50%。",
                "tags": ["配对", "云量"],
            },
            {
                "content": "武汉夏季（6-8月）云量通常30-50%，建议 cloud_threshold=50。"
                           "武汉地形以平原为主，长江沿岸有少量丘陵，DEM标准差通常<50m。",
                "tags": ["武汉", "经验", "参数"],
            },
        ]
        
        for i, kb in enumerate(knowledge_base):
            collection.add(
                documents=[kb["content"]],
                ids=[f"knowledge_{i}"],
                metadatas=[{"type": "knowledge", "tags": kb["tags"]}],
            )
```

#### JSON 层（结构化实验记录）

RAG 不擅长精确查询，这部分交给 JSON：

```python
# core/memory/experiment_log.py
import json
import os
import time
from typing import List, Optional

class ExperimentLog:
    """结构化实验记录（JSON 精确查询层）"""
    
    def __init__(self, project_dir: str):
        self.path = os.path.join(project_dir, "memory", "experiments.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self._save([])
    
    def add(self, region, model, params, metrics):
        records = self._load()
        records.append({
            "id": len(records) + 1,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "region": region,
            "model": model,
            "params": params,
            "metrics": metrics,
        })
        self._save(records)
    
    def get_best(self, region: str = None, model: str = None, metric: str = "R2"):
        """精确查询：该区域/模型的最佳结果"""
        records = self._load()
        filtered = records
        if region:
            filtered = [r for r in filtered if r["region"] == region]
        if model:
            filtered = [r for r in filtered if r["model"] == model]
        if not filtered:
            return None
        return max(filtered, key=lambda r: r.get("metrics", {}).get(metric, 0))
    
    def get_recent(self, n: int = 5) -> List[dict]:
        """精确查询：最近 n 次实验"""
        return self._load()[-n:]
    
    def _load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _save(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
```

#### 两层协同的实现

```python
# Agent 的 system prompt 构建时
class GeoThermoAgent:
    def _enrich_with_memory(self, prompt: str, project_id: str, user_input: str) -> str:
        rag = ChromaRAG(persist_dir="data/memory/chromadb")
        exp_log = ExperimentLog(project_dir=f"data/projects/{project_id}")
        
        memory_context = ""
        
        # 1. JSON 精确查询：直接拿到最佳指标
        best = exp_log.get_best(
            region=self._guess_region(user_input),
        )
        if best:
            memory_context += (
                f"## 历史最佳实验\n"
                f"{best['region']} | {best['model']} | "
                f"R²={best['metrics'].get('R2', 'N/A')} | "
                f"参数={best['params']}\n\n"
            )
        
        # 2. RAG 语义检索：补充上下文经验
        rag_results = rag.search_for_agent(project_id, user_input)
        if rag_results["project_experiences"]:
            memory_context += "## 项目历史经验（RAG）\n"
            for exp in rag_results["project_experiences"]:
                memory_context += f"- {exp['content']}\n"
        if rag_results["global_knowledge"]:
            memory_context += "## 领域知识参考\n"
            for doc in rag_results["global_knowledge"]:
                memory_context += f"- {doc}\n"
        
        if memory_context:
            prompt = prompt.replace(
                "当前软件状态:",
                f"## 记忆上下文\n{memory_context}\n\n当前软件状态:",
            )
        return prompt
```

#### 数据删除与级联清理

删除对话或项目时，该关联的所有记忆必须一并清除，避免遗留残留数据。

##### 删除类型与清理范围

| 删除操作 | 需要清理的内容 |
|---|---|
| **删除单条对话** | 对话 JSON 文件 + 对话在 RAG 中的相关记录 |
| **删除整个项目** | 项目目录（对话 + JSON 记忆）+ 项目的 RAG Collection |

##### 实现

```python
# core/memory/memory_manager.py
class MemoryManager:
    
    def delete_conversation(self, project_id: str, conv_id: str):
        """删除单条对话及其关联记忆"""
        # 1. 删除对话 JSON 文件
        conv_path = f"data/projects/{project_id}/conversations/{conv_id}.json"
        if os.path.isfile(conv_path):
            os.remove(conv_path)
        
        # 2. 删除 RAG 中该对话的记录
        #    每条 RAG 记录在 metadata 中标记了来源对话
        proj_col = self.rag.project_collection(project_id)
        proj_col.delete(where={"source_conv": conv_id})
        
        return {"success": True, "message": f"对话 {conv_id} 及其记忆已删除"}
    
    def delete_project(self, project_id: str):
        """删除整个项目及其所有记忆"""
        # 1. 删除项目目录（含所有对话 JSON + experiments.json + preferences.json）
        project_dir = f"data/projects/{project_id}"
        if os.path.isdir(project_dir):
            import shutil
            shutil.rmtree(project_dir)
        
        # 2. 删除 ChromaDB 中的项目 Collection
        try:
            self.rag.client.delete_collection(f"project_{project_id}")
        except ValueError:
            pass  # Collection 不存在时忽略
        
        return {"success": True, "message": f"项目 {project_id} 及其所有记忆已永久删除"}
```

##### 前端提示

删除前必须弹框确认，说明**删除的影响范围**：

```text
删除对话"武汉7月测试"？
┌─────────────────────────────────────────────┐
│  🗑️ 删除对话                               │
│                                              │
│  将删除：                                    │
│  · 该对话的所有消息（共 23 条）              │
│  · 该对话中产生的实验记录（共 2 条）         │
│                                              │
│  ⚠️ 这些记录也会从 RAG 记忆库中移除，        │
│    后续Agent将无法参考本次实验的经验。       │
│                                              │
│           [取消]    [确认删除]               │
└─────────────────────────────────────────────┘

---

删除项目"武汉_2024"？
┌─────────────────────────────────────────────┐
│  🗑️ 删除项目                               │
│                                              │
│  将删除：                                    │
│  · 项目内所有对话（共 5 个）                │
│  · 所有实验记录（共 12 条）                 │
│  · 所有用户偏好设置                         │
│  · RAG 记忆库中该项目全部经验               │
│  · 项目原始数据和处理结果（可选）            │
│                                              │
│  ⚠️ 此操作不可撤销！                        │
│                                              │
│           [取消]    [确认删除]               │
└─────────────────────────────────────────────┘
```

##### Agent 侧的确认逻辑

Agent 收到删除指令时，主动向用户确认：

```text
用户："把武汉2024这个项目删掉"
Agent："⚠️ 确认删除项目「武汉_2024」？
        将删除 5 个对话、12 条实验记录以及 RAG 记忆库中
        该项目的全部经验。此操作不可撤销。
        请输入项目名称「武汉_2024」确认："

用户："武汉_2024"
Agent："✅ 项目已永久删除"
```

##### API 层实现（Gradio 前后端对接）

```python
# ui/api.py
class GeoThermoAPI:
    
    def delete_conversation(self, conv_id: str) -> dict:
        """前端调用的删除对话接口"""
        project_id = self._get_current_project_id()
        
        # 1. 获取删除信息（用于前端弹框）
        conv_info = self._get_conv_preview(conv_id)
        msg_count = len(conv_info.get("messages", []))
        
        # 2. 前端先弹确认框
        # 此处返回预览信息，由前端弹框确认后再次调用
        return {
            "preview": {
                "conv_id": conv_id,
                "title": conv_info.get("title", "未命名"),
                "message_count": msg_count,
                "experiment_count": self._count_conv_experiments(conv_id),
            }
        }
    
    def confirm_delete_conversation(self, conv_id: str) -> dict:
        """用户确认后的实际删除"""
        project_id = self._get_current_project_id()
        result = self.memory_manager.delete_conversation(project_id, conv_id)
        return result
    
    def delete_project(self, project_id: str) -> dict:
        """前端调用的删除项目接口"""
        conv_count = self._count_project_conversations(project_id)
        exp_count = self._count_project_experiments(project_id)
        
        return {
            "preview": {
                "project_id": project_id,
                "conversation_count": conv_count,
                "experiment_count": exp_count,
            }
        }
    
    def confirm_delete_project(self, project_id: str) -> dict:
        """用户确认后的实际删除"""
        result = self.memory_manager.delete_project(project_id)
        return result
```

#### 新依赖

```text
chromadb>=0.5.0
sentence-transformers>=2.2.0
```

---

### 3.4 [P1] 并发下载优化

当前 5 个下载任务串行执行，改为并发执行。

#### 实现方案

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def _download_all(self, ...):
    tasks = [
        ("landsat_lst", self._download_composite, {...}),
        ("landsat_qa", self._download_composite, {...}),
        ("sentinel2", self._download_composite, {...}),
        ("sentinel2_scl", self._download_composite, {...}),
        ("dem", self._download_composite, {...}),
    ]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fn, **kw): name for name, fn, kw in tasks}
        for future in as_completed(futures):
            name = futures[future]
            future.result()  # 异常会在此抛出
```

#### 注意

- 联合云掩膜（`_landsat_qa_mask(qa) & _sentinel_scl_mask(scl)`）决定了预处理仍需等全部文件就绪
- 瓶颈在 IO（网络下载），不在 CPU
- 预估收益：下载时间节省 40~60%

#### 改动位置

只改 `core/skills/builtin/data_acquisition.py` 中的 `execute()`，约 50 行。

---

### 3.5 [P1] Agent 表达层改进："更像Agent"

#### 3.5.1 执行中的"思考链"

```python
def _execute_plan(self, ...):
    # 执行前：展示推理过程
    _emit("📋 分析指令...\n")
    if not user_specified_model and memory.has_history(region):
        best = memory.get_best_for_region(region)
        _emit(f"→ 检测到 {region} 的历史记录，上次 {best['model']} R²={best['R2']}\n")
        _emit(f"→ {'沿用上次参数' if best['R2'] > 0.85 else '建议尝试新模型'}\n")
    
    # 每步执行前：解释为什么做这一步
    for step in steps:
        _emit(f"\n[{i}/{total}] {skill_name}: {step['reason']}\n")
```

#### 3.5.2 主动提问（异常时给选项）

```python
def _check_exceptions(self, skill_name, result, ...):
    if skill_name == "rf_model":
        r2 = result.data.get("test_metrics", {}).get("R2", 1.0)
        if r2 < 0.6:
            _emit(f"⚠️ R²={r2:.2f}，偏低\n")
            _emit("🔍 可能原因：① 训练样本不足 ② 地形复杂但参数保守\n")
            _emit("🤔 请选择：\n")
            _emit("   A. 自动调参重试（增大 n_estimators/max_depth）\n")
            _emit("   B. 换 XGBoost 试试\n")
            _emit("   C. 忽略，继续后续流程\n")
            # 暂停等待用户选择
```

#### 3.5.3 结果自然语言总结（含特征重要性分析）

```python
def _analyze_result(self, result_data: dict) -> str:
    """LLM 对结果做解读，包含特征重要性分析和物理意义解释"""
    
    features = result_data.get("feature_importance", [])
    feat_str = "\n".join(
        f"  {i+1}. {f['feature']} — 贡献度 {f['importance']:.3f}"
        for i, f in enumerate(features[:5])
    ) if features else "  无特征重要性数据"
    
    prompt = f"""你是遥感领域科学家，对以下实验结果进行专业解读。

## 实验概况
- 区域: {result_data.get('region', '未知')}
- 模型: {result_data.get('model_name', '未知')}
- 训练样本: {result_data.get('train_samples', 'N/A'):,}

## 精度指标
- R²: {result_data.get('r2', 'N/A')}
- RMSE: {result_data.get('rmse', 'N/A')}K
- MAE: {result_data.get('mae', 'N/A')}K

## 特征重要性排序
{feat_str}

## 要求
请输出以下三部分（用 --- 分隔）：

1. **效果评价**：1-2句话。对比同类研究（LST降尺度R²通常0.75-0.85），
   判断当前结果属于优秀/良好/合格/偏低。

2. **归因分析**：2-3句话。结合特征重要性排序，
   指出最重要的2-3个特征及其物理意义。
   例如："NDVI(0.28)贡献最大，说明植被覆盖对地表温度有最强控制..."
   结合区域特征给出合理解释。

3. **改进建议**：1句话，具体、可操作。
   例如："地形复杂区域可尝试XGBoost"而非"可以换个模型试试"。
"""
    return self.assistant._call_api(
        [{"role": "system", "content": prompt}],
        temperature=0.3, max_tokens=1024,
    )
```

#### 3.5.4 执行后主动建议下一步

```python
# pipeline 完成后
suggestions = []
if "uhi_analysis" in available_skills:
    suggestions.append("🔥 做城市热岛分析")
if memory.has_experiment("北京"):
    suggestions.append("📈 和北京结果做对比")
suggestions.append("💬 问我关于结果的任何问题")

_emit(f"\n✅ {region} LST 降尺度完成！\n")
_emit("下一步建议：\n" + "\n".join(f"{s}" for s in suggestions))
```

### 3.5.5 角色动态切换（Phase-Aware Prompting）

Agent 在不同执行阶段切换不同的专业角色 prompt，实现"单Agent多角色"。

```python
def _get_phase_prompt(self, phase: str) -> str:
    prompts = {
        "planning": """你是GeoThermoAI的规划专家，负责分析用户需求并编排Skill。
规则：
- 全流程执行必须包含 data_acquisition → ... → accuracy_eval 全部7步
- 用户指定模型（如"用XGBoost"）→ 选对应Skill
- 用户未指定 → 默认用记忆中的历史最佳模型
- 从记忆注入历史参考信息，辅助决策""",
        
        "tuning": """你是遥感机器学习调优专家，负责分析训练结果并决定是否调优。
关注指标：
- R²、RMSE、MAE
- 过拟合检测（训练R² - 测试R² > 0.20）
- 地形复杂度（DEM标准差）与模型容量的匹配
- 样本量与 n_estimators 的匹配""",
        
        "diagnosis": """你是结果诊断专家，负责检查执行结果中的异常。
检查项：
- 训练样本数是否充足（<5,000 则警告）
- 精度是否达标（R²<0.6 则提示）
- 值域偏差是否通过（>5K 则警告）""",
        
        "interpretation": """你是遥感领域科学家，负责用自然语言解释实验结果。
要求：
- 1-2句话总结结果质量
- 指出贡献最大的特征及其物理意义
- 给出一个具体的改进建议""",
    }
    return prompts.get(phase, "")
```

#### 执行中的角色切换时机

```
用户指令 → 规划专家（生成计划）
    ↓
执行数据获取 → 思考链表达（解释每一步）
    ↓
执行模型训练 → 调参专家（分析指标+决策调优）
    ↓
执行后续步骤 → 诊断专家（监测异常+主动提问）
    ↓
全部完成 → 解读专家（总结结果+建议下一步）
```

#### 改动量

主要在 `core/agent/geo_thermo_agent.py`，新增 ~250 行，不涉及其他文件。

---

### 3.6 [P1] LLM主导+规则兜底的超参数寻优

替换简单规则驱动，改为 LLM 分析数据特征后决策 + 7条规则兜底。

#### 三层决策架构

```text
训练完成 → 收集指标 + 数据特征 + 调优历史
    ↓
┌─ LLM 决策层（灵活）─────────────────────────┐
│  分析数据特征 → 推理 → 输出决策+理由+新参数  │
│  {"action": "accept|adjust|stop",            │
│   "new_params": {"n_estimators": 500}}      │
└──────────────────┬──────────────────────────┘
                   ↓
┌─ 规则兜底层（可靠）─────────────────────────┐
│  规则1: R²<0.6 → 强制调优（覆盖LLM的accept）  │
│  规则2: R²≥0.88 → 禁止调优（覆盖LLM的adjust） │
│  规则3: 过拟合差>0.20 → 干预调优方向          │
│  规则4: 参数越界 → 截断到安全范围             │
│  规则5: 连续两轮提升<0.01 → 强制停止（已收敛）    │
│  规则6: 连续两轮下降 → 强制停止（走势恶化）    │
│  规则7: 调优达8轮 → 强制停止，取RMSE最低轮次 │
└──────────────────┬──────────────────────────┘
                   ↓
┌─ 最终执行层 ─────────────────┐
│  放行LLM决策 或 执行规则修正后的参数  │
└──────────────────────────────┘
```

#### LLM 决策 Prompt（含特征重要性）

```python
def _llm_decide_tuning(self, context: dict) -> dict:
    # 格式化特征重要性
    features = context.get('feature_importance', [])
    feat_str = "\n".join(
        f"  {f['feature']}: {f['importance']:.4f}"
        for f in features[:5]
    ) if features else "  无数据"
    
    model = context['model_name']
    
    prompt = f"""你是一个遥感机器学习调优专家。

## 模型
{model}

## 数据特征
- 地形: {'复杂山地' if context['dem_std'] > 100 else '丘陵' if context['dem_std'] > 50 else '平原'}
- 植被: {'高覆盖' if context['ndvi_mean'] > 0.5 else '中等' if context['ndvi_mean'] > 0.2 else '低覆盖'}
- LST标准差: {context['lst_std']:.1f}K → 温度变异性{'大' if context['lst_std'] > 5 else '中' if context['lst_std'] > 2 else '小'}

## 当前参数与指标
- 参数: {json.dumps(context['current_params'])}
- 训练R²: {context['train_r2']:.4f} | 测试R²: {context['test_r2']:.4f}
- RMSE: {context['rmse']:.3f}K

## 特征重要性排序
{feat_str}

## 调优参考（结合特征重要性）
- 如果 DEM 排前 2 → 地形控制强，建议检查 max_depth 是否足以捕捉地形细节
- 如果 NDVI 排前 2 → 植被强相关，不同植被覆盖度可能需要不同参数策略
- 如果某特征的贡献度极低(<0.01) → 可考虑该特征是否多余，但本系统特征固定为9个

## 历史调优
{json.dumps(context['tuning_history']) if context['tuning_history'] else '（首次）'}

## 决策规则
- accept: 测试R² ≥ 0.80 或 连续两次提升 < 0.01
- adjust: 测试R² < 0.75，有明确调优方向
- stop: 已调优3次，或R²反而下降

返回严格JSON：
{{"action": "accept|adjust|stop", "reason": "...", "new_params": {{...}}}}
"""
    return self._parse_json_response(self.assistant._call_api(
        [{"role": "system", "content": prompt}],
        temperature=0.1, max_tokens=1024,
    ))
```

#### 规则兜底层

```python
def _rule_safeguard(self, llm_decision: dict, context: dict) -> dict:
    test_r2 = context['test_r2']
    train_r2 = context['train_r2']
    
    # 规则1: R²<0.6 强制调优
    if test_r2 < 0.6 and llm_decision.get("action") != "adjust":
        llm_decision["action"] = "adjust"
        llm_decision["reason"] = f"[规则] R²={test_r2:.2f}<0.6，强制调优"
    
    # 规则2: R²≥0.88 禁止调优
    if test_r2 >= 0.88 and llm_decision.get("action") == "adjust":
        llm_decision["action"] = "accept"
        llm_decision["reason"] = f"[规则] R²={test_r2:.2f}≥0.88，停止调优"
    
    # 规则3: 过拟合检测（训练R² >> 测试R²）
    if (train_r2 - test_r2) > 0.20:
        llm_decision["reason"] += " | [规则] 检测到过拟合，已限制模型容量"
        # 降低max_depth，增大min_samples_leaf
        p = dict(context['current_params'])
        p['max_depth'] = max(5, p.get('max_depth', 25) - 10)
        llm_decision['new_params'] = p
    
    # 规则4: 参数越界截断
    if llm_decision.get("new_params"):
        for k, v in llm_decision['new_params'].items():
            if k == 'n_estimators':
                llm_decision['new_params'][k] = max(10, min(2000, v))
            elif k == 'max_depth':
                llm_decision['new_params'][k] = max(1, min(100, v))
    
    # 规则5: 收敛检测（连续两轮提升极小）
    if len(context.get('tuning_history', [])) >= 2:
        history = context['tuning_history']
        r2_values = [h['metrics'].get('test_r2', 0) for h in history]
        r2_values.append(test_r2)
        if len(r2_values) >= 3:
            improvements = [r2_values[i] - r2_values[i-1] for i in range(1, len(r2_values))]
            if all(imp < 0.01 for imp in improvements[-2:]):
                llm_decision["action"] = "accept"
                llm_decision["reason"] = (
                    f"[规则触发了] 连续两轮提升 < 0.01，"
                    f"调优已收敛于 R²={test_r2:.4f}")
                llm_decision["new_params"] = None
                return llm_decision
    
    # 规则6: 走势恶化（连续两轮下降）
    if len(context.get('tuning_history', [])) >= 2:
        history = context['tuning_history']
        r2_values = [h['metrics'].get('test_r2', 0) for h in history]
        r2_values.append(test_r2)
        if len(r2_values) >= 3:
            changes = [r2_values[i] - r2_values[i-1] for i in range(1, len(r2_values))]
            if all(c < 0 for c in changes[-2:]):
                llm_decision["action"] = "accept"
                llm_decision["reason"] = (
                    f"[规则触发了] 连续两轮下降，"
                    f"走势恶化，取最优 R²={max(r2_values):.4f}")
                llm_decision["new_params"] = None
                return llm_decision
    
    return llm_decision
```

#### 调优循环（含 8 轮上限 + RMSE 最低选取）

```python
def _execute_training_with_tuning(self, skill, step, data_features, on_token):
    """模型训练 + LLM调优循环，最多8轮调优（含首次共9轮）"""
    
    MAX_TUNING_ROUNDS = 8       # 最多调优 8 次
    tuning_history = []
    best_by_rmse = None          # (rmse, result_data, round_num)
    
    def _emit(text):
        if on_token:
            on_token(text)
    
    for tuning_round in range(MAX_TUNING_ROUNDS + 1):  # 首次 + 8次调优
        if tuning_round == 0:
            _emit(f"\n  📊 首次训练\n")
        else:
            _emit(f"\n  📊 第 {tuning_round} 轮调优\n")
        
        # 执行训练
        result = skill.execute(step["params"], ...)
        if not result.success:
            _emit(f"  ❌ 训练失败\n")
            break
        
        test_r2 = result.data["test_metrics"].get("R2", 0)
        test_rmse = result.data["test_metrics"].get("RMSE", 999)
        
        # 记录本轮（用于后续规则判断用R²，选最佳用RMSE）
        tuning_history.append({
            "round": tuning_round,
            "params": dict(step["params"]),
            "metrics": {"test_r2": test_r2, "rmse": test_rmse},
        })
        
        # 更新 RMSE 最低的结果
        if best_by_rmse is None or test_rmse < best_by_rmse[0]:
            best_by_rmse = (test_rmse, result, tuning_round)
            _emit(f"  ⭐ 当前 RMSE 最低: {test_rmse:.3f}K（第{tuning_round}轮）\n")
        
        # 规则7: 已达最大调优轮数
        if tuning_round >= MAX_TUNING_ROUNDS:
            _emit(f"  ⏹️ 已达最大调优轮数（{MAX_TUNING_ROUNDS}轮），")
            _emit(f"选取 RMSE 最低的第 {best_by_rmse[2]} 轮（RMSE={best_by_rmse[0]:.3f}K）\n")
            return best_by_rmse[1]
        
        # 准备 LLM 决策上下文
        context = {
            "model_name": skill.name,
            "dem_std": data_features.get("dem_std", 0),
            "ndvi_mean": data_features.get("ndvi_mean", 0),
            "lst_std": data_features.get("lst_std", 0),
            "current_params": dict(step["params"]),
            "train_r2": result.data["train_metrics"].get("R2", 0),
            "test_r2": test_r2,
            "rmse": test_rmse,
            "tuning_history": tuning_history,
        }
        
        # LLM 决策 → 规则兜底
        _emit(f"  🤔 LLM 评估中...\n")
        llm_decision = self._llm_decide_tuning(context)
        safe_decision = self._rule_safeguard(llm_decision, context)
        
        _emit(f"  📋 {safe_decision['reason']}\n")
        
        if safe_decision["action"] in ("accept", "stop"):
            _emit(f"  ✅ 停止调优\n")
            break
        
        # 应用新参数继续
        step["params"].update(safe_decision["new_params"])
        _emit(f"  🔄 新参数: {safe_decision['new_params']}\n")
    
    # 返回 RMSE 最低的结果
    _emit(f"  🏆 取 RMSE 最低的第 {best_by_rmse[2]} 轮\n")
    return best_by_rmse[1] if best_by_rmse else result

| 维度 | 简单规则（原P1） | **LLM驱动+规则兜底（升级后）** |
|---|---|---|
| 调参策略 | 固定增量（n_estimators+100） | **根据数据特征+历史智能推荐** |
| 终止条件 | 硬阈值R²≥0.6 | **综合判断收敛情况** |
| 对新模型的适应 | 每个模型写规则 | **零额外工作** |
| 安全性 | ✅ 确定性强 | **✅ 5条规则确保不会出界** |
| 智能程度 | ❌ 死板 | **✅ 灵活且有依据** |

#### 改动

只改 `core/agent/geo_thermo_agent.py`，新增 `_llm_decide_tuning()`、`_rule_safeguard()`，约 120 行。

---

### 3.7 [P1] 交互式地图可视化（多图层 + GEE 风格图层控制）

当前 LST 结果以静态 JPEG 缩略图展示。升级为**多图层交互式地图**，所有中间产物都可像 GEE 一样勾选/取消勾选。

#### 可视化数据源

| 阶段 | 图层 | 文件 | 色带 | 说明 |
|---|---|---|---|---|
| 下载 | Landsat LST | `raw/landsat_lst.tif` | RdYlBu_r | 30m 原始地表温度 |
| 下载 | Sentinel-2 真彩色 | `raw/sentinel2_bands.tif` (B4,B3,B2) | 真彩色合成 | 10m 自然色图像 |
| 下载 | Sentinel-2 NIR | `raw/sentinel2_bands.tif` (B8) | 灰度 | 近红外波段 |
| 下载 | Sentinel-2 SWIR1 | `raw/sentinel2_bands.tif` (B11) | 灰度 | 短波红外 |
| 下载 | DEM | `raw/dem.tif` | terrain | 数字高程模型 |
| 预处理 | NDVI | 计算自 Sentinel | RdYlGn | 植被指数 (绿=高, 红=低) |
| 预处理 | NDWI | 计算自 Sentinel | Blues | 水体指数 (蓝=水) |
| 预处理 | NDBI | 计算自 Sentinel | RdYlBu_r | 建成区指数 (红=建成区) |
| 最终 | **LST 10m 结果** | `results/rf_10m_lst_final.tif` | RdYlBu_r | 降尺度后的10m地表温度 |

#### 坐标转换说明

所有 GeoTIFF 都是 **UTM 投影**（由 `data_acquisition` 根据研究区中心经度计算），Folium 地图使用 **WGS84 经纬度**。叠加前必须转换。

```
GeoTIFF (UTM, EPSG:326XX) ──→ rasterio.warp.transform() ──→ Folium (WGS84, EPSG:4326)
  每个图层独立转换              转为WGS84经纬度                   叠加为 ImageOverlay
```

#### GEE 风格图层控制器

```
┌──────────────────────────────────────┐
│  🌍 图层管理器           ───  ✕     │
│                                      │
│  ☑ 底图: OpenStreetMap               │
│  ○ 底图: 卫星影像                     │
│  ─────────────────────────            │
│  ☑ LST 10m 结果         ▆▆ 70%      │
│  ☐ NDVI                 ▆▆ 50%      │  ← 每个图层可独立
│  ☑ NDWI                 ▆▆ 50%      │     勾选/不透明度
│  ☐ NDBI                 ▆▆ 50%      │
│  ☑ Sentinel-2 真彩色    ▆▆ 100%     │
│  ☐ DEM 地形                       │
│  ☐ Landsat LST 30m      ▆▆ 70%      │
│  ─────────────────────────            │
│  [+] 添加自定义图层                   │
└──────────────────────────────────────┘
```

#### 核心实现

```python
# core/visualization.py
import os
import json
from typing import List, Dict
import folium
from folium.raster_layers import ImageOverlay
import rasterio
from rasterio.warp import transform
import numpy as np
from matplotlib import cm


class LayerVisualizer:
    """管理所有可用于 Folium 地图的数据图层"""
    
    # 图层定义：名称、文件、色带、不透明度、顺序
    LAYER_DEFS = [
        {
            "id": "landsat_lst",
            "label": "Landsat LST 30m",
            "file": "raw/landsat_lst.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "group": "raw",
            "visible": False,  # 默认隐藏
        },
        {
            "id": "sentinel_rgb",
            "label": "Sentinel-2 真彩色",
            "file": "raw/sentinel2_bands.tif",
            "bands": [4, 3, 2],  # R,G,B 多波段
            "opacity": 0.8,
            "group": "raw",
            "visible": False,
        },
        {
            "id": "dem",
            "label": "DEM 地形",
            "file": "raw/dem.tif",
            "band": 1,
            "colormap": "terrain",
            "opacity": 0.6,
            "group": "raw",
            "visible": False,
        },
        {
            "id": "ndvi",
            "label": "NDVI 植被指数",
            "file": None,  # 实时计算
            "colormap": "RdYlGn",
            "opacity": 0.5,
            "group": "indices",
            "visible": False,
        },
        {
            "id": "lst_10m",
            "label": "🌟 LST 10m 结果",
            "file": "results/rf_10m_lst_final.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "group": "result",
            "visible": True,  # 默认显示
        },
    ]
    
    @staticmethod
    def _read_band(tif_path: str, band: int = 1, colormap: str = None):
        """读取单个波段，返回颜色渲染后的 RGBA 数组 + WGS84 边界"""
        with rasterio.open(tif_path) as src:
            arr = src.read(band).astype(np.float32)
            nodata = src.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            
            left, bottom, right, top = src.bounds
            src_crs = src.crs
            lons, lats = transform(src_crs, 'EPSG:4326',
                                   [left, right, right, left],
                                   [bottom, bottom, top, top])
            bounds_wgs84 = [[float(min(lats)), float(min(lons))],
                            [float(max(lats)), float(max(lons))]]
        
        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            return None, bounds_wgs84
        
        vmin, vmax = float(np.nanmin(valid)), float(np.nanmax(valid))
        normed = np.clip((arr - vmin) / (vmax - vmin + 1e-10), 0, 1)
        
        if colormap:
            cmap = getattr(cm, colormap)
            colored = (cmap(normed)[:, :, :3] * 255).astype(np.uint8)
            return colored, bounds_wgs84
        return normed, bounds_wgs84
    
    @staticmethod
    def _read_rgb(tif_path: str, bands: List[int], scale_factor: float = 1/10000):
        """读取多波段生成 RGB 真彩色"""
        with rasterio.open(tif_path) as src:
            rgb = []
            for b in bands:
                arr = src.read(b).astype(np.float32) * scale_factor
                arr = np.clip(arr, 0, 1)
                rgb.append(arr)
            
            left, bottom, right, top = src.bounds
            src_crs = src.crs
            lons, lats = transform(src_crs, 'EPSG:4326',
                                   [left, right, right, left],
                                   [bottom, bottom, top, top])
            bounds_wgs84 = [[float(min(lats)), float(min(lons))],
                            [float(max(lats)), float(max(lons))]]
        
        colored = (np.stack(rgb, axis=-1) * 255).astype(np.uint8)
        return colored, bounds_wgs84
    
    def build_map(self, project_dir: str) -> str:
        """生成包含所有可用图层的 Folium 地图 HTML"""
        m = folium.Map(control_scale=True)
        first_layer = True
        
        for layer_def in self.LAYER_DEFS:
            file_path = os.path.join(project_dir, layer_def["file"])
            if not os.path.isfile(file_path):
                continue  # 文件不存在时跳过该图层
            
            try:
                if "bands" in layer_def:
                    # 多波段 RGB
                    img, bounds = self._read_rgb(
                        file_path, layer_def["bands"])
                else:
                    # 单波段 + 色带
                    img, bounds = self._read_band(
                        file_path, layer_def.get("band", 1),
                        layer_def.get("colormap"))
                
                if img is None:
                    continue
                
                if first_layer:
                    # 用第一层的中心设置地图初始位置
                    center_lat = (bounds[0][0] + bounds[1][0]) / 2
                    center_lon = (bounds[0][1] + bounds[1][1]) / 2
                    m.location = [center_lat, center_lon]
                    first_layer = False
                
                ImageOverlay(
                    image=img,
                    bounds=bounds,
                    opacity=layer_def["opacity"],
                    name=layer_def["label"],
                    show=layer_def.get("visible", True),
                ).add_to(m)
            except Exception:
                continue  # 加载失败的图层静默跳过
        
        # 添加底图选项
        folium.TileLayer('OpenStreetMap', name='街道地图').add_to(m)
        folium.TileLayer(
            'https://server.arcgisonline.com/ArcGIS/rest/services/'
            'World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri', name='卫星影像'
        ).add_to(m)
        
        # GEE 风格图层控制器
        folium.LayerControl(collapsed=False).add_to(m)
        
        return m._repr_html_()
```

#### 与 Agent 的集成

每完成一个步骤，动态添加新图层到地图（而不是等全部完成再生成）：

```python
# 在 Agent 执行完成后，动态更新可视化面板
# api.py 中
class GeoThermoAPI:
    def get_map_layers(self, conv_id: str) -> dict:
        """获取当前项目可用的所有图层信息"""
        state = self._get_conv_state(conv_id)
        project_dir = state["project_dir"]
        if not project_dir:
            return {"layers": []}
        
        visualizer = LayerVisualizer()
        available = []
        for layer_def in visualizer.LAYER_DEFS:
            file_path = os.path.join(project_dir, layer_def["file"])
            available.append({
                "id": layer_def["id"],
                "label": layer_def["label"],
                "visible": layer_def.get("visible", False),
                "available": os.path.isfile(file_path),
            })
        return {"layers": available}
    
    def get_lst_map(self, conv_id: str) -> str:
        """生成完整的 Folium 地图 HTML"""
        state = self._get_conv_state(conv_id)
        if not state["project_dir"]:
            return "<p>请先选择项目目录</p>"
        visualizer = LayerVisualizer()
        return visualizer.build_map(state["project_dir"])
```

#### 用户视角的体验

```
Agent 执行过程中，可视化面板实时更新：

[1/7] data_acquisition 完成
  → 地图上新增图层：Landsat LST、Sentinel-2、DEM（默认隐藏）
  → 用户可手动勾选查看

[2/7] data_pipeline 完成  
  → 地图上新增图层：NDVI、NDWI、NDBI（默认隐藏）
  → 用户查看植被分布，确认数据质量

[7/7] accuracy_eval 完成
  → 地图上新增图层：LST 10m 结果（默认显示）
  → 用户可叠加 NDVI 和 LST 对比分析
```

#### 新依赖

`folium>=0.16.0` （已有，不变）

#### 改动量

| 文件 | 改动 | 行数 |
|---|---|---|
| 新建 `core/visualization.py` | `LayerVisualizer` 类 | ~200 |
| 改 `ui/api.py` | 新增 `get_map_layers()`、`get_lst_map()` | ~30 |

---

### 3.8 [P2] 拓展遥感任务 Skill

| Skill 名称 | Group | 输入 | 输出 |
|---|---|---|---|
| `uhi_analysis` | `post_analysis` | LST GeoTIFF + 城市边界 | 热岛强度等级图 + 统计报告 |
| `time_series` | `post_analysis` | 多期 LST 结果 | 温度变化趋势图 |
| `eco_response` | `post_analysis` | LST + NDVI/LULC | LST-NDVI 关系散点图 |

---

### 3.9 [P2] 一键体验模式 + 可复现报告

- **一键体验**：预置武汉 demo 数据，用户打开即用，15 秒看到完整流程
- **可复现报告**：每次完整执行后自动生成 Markdown 实验报告

---

### 3.10 [P1] 断点执行

支持用户从指定步骤继续执行，并在执行前自动检查前置数据是否齐全。

#### Skill 依赖声明

每个 Skill 声明自己需要哪些前置文件：

```python
class BaseSkill(ABC):
    @property
    def dependencies(self) -> List[Dict]:
        """
        声明该 Skill 执行前需要存在哪些文件。
        返回：[{"param": "train_csv", "produced_by": "data_pipeline"}, ...]
        """
        return []

class RFModelSkill(BaseSkill):
    @property
    def dependencies(self):
        return [
            {"param": "train_csv", "produced_by": "data_pipeline"},
            {"param": "val_csv",   "produced_by": "data_pipeline"},
            {"param": "test_csv",  "produced_by": "data_pipeline"},
        ]
```

#### 断点检查 + 用户友好提示

```python
def check_step_ready(self, skill_name: str, params: dict) -> dict:
    """
    检查指定 Skill 是否可以执行。
    返回：{"ready": True/False, "missing": [...], "suggestion": "..."}
    """
    skill = self.registry.get(skill_name)
    if not skill:
        return {"ready": False, "error": f"Skill {skill_name} 不存在"}
    
    missing = []
    for dep in skill.dependencies:
        file_path = params.get(dep["param"], "")
        if not file_path or not os.path.isfile(file_path):
            missing.append({
                "file": dep["param"],
                "produced_by": dep["produced_by"],
            })
    
    if missing:
        producers = " → ".join(set(m["produced_by"] for m in missing))
        return {
            "ready": False,
            "missing": missing,
            "suggestion": f"缺少输入文件，由 {producers} 步骤产生。建议从更早的步骤开始。",
        }
    return {"ready": True}
```

#### 用户交互示例

```text
用户: "从 ttricompute 继续跑"

Agent: 正在检查 ttri_compute 的前置条件...
✅ data_pipeline 的输出文件齐全
✅ ttri_compute 可以执行

用户: "从 rf_model 开始"

Agent: 正在检查 rf_model 的前置条件...
❌ train_with_TTRI.csv — 由 ttri_compute 产生（未找到）
❌ 建议：先执行 ttri_compute，或从更早的步骤开始
```

#### 与现有路径机制的配合

当前 `_normalize_plan_paths()` 会自动在 output 目录搜索已有文件，断点检查与之天然配合：
- 文件存在 → `check_step_ready` 通过 → 直接执行
- 文件不存在 → 提示用户从缺失步骤开始

#### 改动量

| 文件 | 改动 | 行数 |
|---|---|---|
| `core/skills/base_skill.py` | 新增 `dependencies` 属性 | +15 |
| `core/skills/builtin/*.py` | 7个Skill实现依赖声明 | +70 |
 `core/agent/geo_thermo_agent.py` | 新增 `check_step_ready()`、`_resume_from()` | +60 |

---

### 3.11 [P2] 执行模式选择：自动执行 vs 由我批准

提供两种执行模式，用户可在启动任务前选择。

#### 模式对比

| 模式 | 决策方式 | 适用场景 |
|---|---|---|
| **⚡ 完全执行** | Agent 自动决策全部步骤 | 有经验用户、批量处理、已知区域重复实验 |
| **✅ 由我批准** | 关键节点暂停等待用户确认 | 入门用户、新区域首次实验、教学演示 |

#### "完全执行"模式详解

此模式下用户只需上传研究区，其余全部由 Agent 自动完成。

##### 多配对顺序处理

搜索到多组影像配对时，**不选择、不跳过，全部顺序执行**：

```
搜索到 3 组影像配对
    ↓
┌─ pair1 ─────────────────────────────┐
│  下载 → 预处理 → TTRI → RF → TCR → │
│  LST → 精度评估 → 结果存入 RAG      │
└─────────────────────────────────────┘
    ↓
┌─ pair2 ─────────────────────────────┐
│  下载 → 预处理 → TTRI → RF → TCR → │
│  LST → 精度评估 → 结果存入 RAG      │
└─────────────────────────────────────┘
    ↓
┌─ pair3 ─────────────────────────────┐
│  ...                                │
└─────────────────────────────────────┘
    ↓
全部完成后，汇总所有配对的精度对比
```

##### 项目目录结构

每个配对在项目文件夹下建独立子目录：

```
data/projects/武汉_2024/
├── pair1/                       ← 命名规则：pair + 递增数字，由代码硬编码
│   ├── raw/                       ← 原始下载数据
│   ├── processed/                 ← 预处理结果
│   └── results/                   ← 模型和 LST 结果
│       ├── rf_10m_lst_final.tif
│       └── accuracy.json
│
├── pair2/
│   ├── raw/
│   ├── processed/
│   └── results/
│
├── memory/                        ← 项目记忆（共享）
│   ├── experiments.json
│   └── preferences.json
│
└── comparison_report.md           ← 全部配对的汇总对比
```

##### 路径硬编码（防 LLM 幻觉）

沿用现有 `SKILL_PATHS` 机制，每个 paired 子目录内部的路径完全由代码控制：

```python
# 在 Agent 中，配对文件夹名完全由代码硬编码，LLM 不参与
pairs = search_result.get("image_pairs", [])

for pair_index, pair in enumerate(pairs):
    pair_folder = f"pair{pair_index + 1}"       # ← pair1, pair2, pair3，LLM 看不见
    pair_dir = f"{project_dir}/{pair_folder}"
    
    SKILL_PATHS_PAIR = {
        "data_acquisition": {"output_dir": pair_dir + "/raw"},
        "data_pipeline": {
            "output_dir": pair_dir + "/processed",
            "landsat_path": pair_dir + "/raw/landsat_lst.tif",
            "sentinel2_path": pair_dir + "/raw/sentinel2_bands.tif",
            "qa_path": pair_dir + "/raw/landsat_qa_pixel.tif",
            "scl_path": pair_dir + "/raw/sentinel2_scl.tif",
            "dem_path": pair_dir + "/raw/dem.tif",
        },
        "rf_model": {"output_dir": pair_dir + "/results"},
        # ... 其他步骤同理
    }
    
    # Agent 执行时强制覆盖 LLM 生成的所有路径
    self._normalize_plan_paths(plan, base_dir=pair_dir)
```

##### 前端配对信息面板

完全执行模式下，前端需要一个实时更新的信息面板，展示当前在处理哪个配对：

```
┌─────────────────────────────────────────────┐
│  ⚡ 完全执行中                              │
│                                              │
│  当前配对 2/3                                │
│  ┌─────────────────────────────────────────┐│
│  │ 📅 Landsat: 2024-07-17 (L9)            ││
│  │ ☁️ 云量: 12.5%                         ││
│  │ 📊 覆盖度: 95.2%                       ││
│  │ 🛰️ Sentinel: 2024-07-18               ││
│  │ ☁️ 云量: 8.3%                          ││
│  │ 📊 覆盖度: 97.8%                       ││
│  │ ⏱️ 时间差: 1天                         ││
│  ├─────────────────────────────────────────┤│
│  │ 进度: ████████░░ 80%                    ││
│  │ 当前步骤: rf_model 训练                  ││
│  │ R² (测试): 0.87 ← 实时更新              ││
│  └─────────────────────────────────────────┘│
│                                              │
│  已完成: 1/3 配对                            │
│  ✅ pair1: R²=0.85, RMSE=1.23K              │
│  ⏳ pair2: 进行中...                        │
│  ⏳ pair3: 等待中                           │
└─────────────────────────────────────────────┘
```

该面板数据由 Agent 通过 `workflow_callback` 推送到前端：

```python
# Agent 执行时推送的配对信息
workflow_callback({
    "type": "pair_progress",
    "pair_index": 1,           # 0-based
    "pair_total": 3,
    "pair_info": {
        "landsat_date": "2024-07-17",
        "landsat_satellite": "L9",
        "landsat_cloud": 12.5,
        "landsat_coverage": 95.2,
        "sentinel_date": "2024-07-18",
        "sentinel_cloud": 8.3,
        "sentinel_coverage": 97.8,
        "time_diff_days": 1,
    },
    "current_step": "rf_model",
    "step_progress": 0.8,
    "completed_pairs": [
        {"index": 0, "r2": 0.85, "rmse": 1.23},
    ],
})
```

##### 全部完成后

所有配对处理完毕后，Agent 自动执行：

1. **汇总对比** — 所有配对的精度指标整理为 Markdown 表格
2. **存入记忆** — 每对实验结果写入 RAG + JSON
3. **LLM 总结** — 调用解读专家角色，给出"哪组配对效果最好、为什么"的分析
4. **前端展示** — 汇总面板显示所有配对的精度排行

```
用户启动全流程
    ↓
[1/7] data_acquisition
    ├── 找到 3 组影像配对 → 用户选择用哪一组（已有）
    └── 下载完成 → ✅ 自动通过
    ↓
[2/7] data_pipeline    → ✅ 自动通过（确定性步骤）
[3/7] ttri_compute      → ✅ 自动通过（确定性步骤）
    ↓
[4/7] rf_model 训练完成
    ├── 展示：R²=0.72, RMSE=1.45K
    ├── 特征重要性：NDVI(0.28) > DEM(0.23) > NIR(0.10)
    ├── 历史参考：该区域上次 RF 跑出 R²=0.87
    └── 🤔 用户决定：
        [ ] 接受结果，继续下一步
        [🔘] 进入超参数寻优 ← 默认推荐
        [ ] 放弃本次，以后再说
    ↓
[5/7] tcr_compute → [6/7] lst_export → [7/7] accuracy_eval
    ↓
全部完成
    ├── 展示：R²=0.87, RMSE=1.23K
    └── 🤔 下一步：
        [ ] 到这里就可以了
        [🔘] 和我上次北京的结果做对比
        [ ] 做城市热岛分析
        [ ] 生成实验报告
```

#### 审批节点清单

| 节点 | 触发条件 | 用户决定什么 | 审批形式 |
|---|---|---|---|
| **影像配对选择** | 搜索到多组配对 | 选择哪一组下载 | 表格选择（已有） |
| **云量过高** | 云量 > 50% | 接受 / 放宽阈值 / 换时间 | 选择框 |
| **训练样本不足** | 有效样本 < 5,000 | 继续 / 回退数据获取 | 选择框 |
| **模型训练完成** | 首次训练结束 | 接受 / 进入调优 / 放弃 | 选择框 + 指标展示 |
| **调优轮次结果** | 每轮调优完成 | 接受本轮 / 继续下一轮 | 选择框 |
| **全部完成** | pipeline 结束 | 选择下一步操作 | 选择框 + 建议列表 |

#### 实现方案

```python
class GeoThermoAgent:
    
    def __init__(self, ...):
        self.exec_mode = "auto"  # "auto" 或 "approval"
    
    def set_exec_mode(self, mode: str):
        """由前端用户在启动前设置"""
        self.exec_mode = mode
    
    def _need_user_approval(self, node: str) -> bool:
        """判断当前节点是否需要用户审批"""
        if self.exec_mode == "auto":
            return False  # 完全执行，全部自动
        
        # "由我批准"模式下，以下节点需要审批
        approval_nodes = [
            "pair_selection",      # 影像配对
            "high_cloud",          # 云量过高
            "insufficient_data",   # 样本不足
            "tuning_decision",     # 是否调优
            "tuning_round",        # 每轮调优结果
            "pipeline_done",       # 全部完成
        ]
        return node in approval_nodes
    
    def _pause_for_approval(self, node: str, context: dict, on_token, pause_callback):
        """暂停等待用户审批，通过 pause_callback 返回用户选择"""
        if not pause_callback or not self._need_user_approval(node):
            return {"approved": True, "auto": True}  # 自动通过
        
        return pause_callback({
            "type": "approval",
            "node": node,
            "data": context,
        })
```

#### 与 AI 聊天气泡的集成

在"由我批准"模式下，审批信息以 AI 聊天气泡的形式展示，内置选择框：

```text
┌─────────────────────────────────────────────┐
│  🤖 GeoThermoAI                             │
│                                              │
│  📊 模型训练完成                             │
│                                              │
│  RF 训练结果：                               │
│  · R² = 0.72                                 │
│  · RMSE = 1.45K                              │
│  · 特征重要性：NDVI(0.28) > DEM(0.23)        │
│                                              │
│  建议进入超参数寻优。                         │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │ ○ 接受结果，继续下一步                  │  │
│  │ ● 进入超参数寻优（推荐）                │  │
│  │ ○ 放弃本次训练                         │  │
│  │                                        │  │
│  │       [确认]                           │  │
│  └────────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

#### 与前端 UI 的配合

界面需要新增两个元素：

```
对话输入框上方，新增模式切换：

[⚡ 完全执行]  [✅ 由我批准]     ← 类似 IDE 的运行/调试模式切换

当前选择处于激活状态，用户随时可以切换。
```

#### 改动量

| 文件 | 改动 | 行数 |
|---|---|---|
| `core/agent/geo_thermo_agent.py` | 新增 `exec_mode`、`_need_user_approval()`、`_pause_for_approval()` | ~80 |
| `ui/api.py` | 新增 `set_exec_mode()` 接口 | ~10 |
| 前端（Gradio） | 新增模式切换组件 | ~30 |

#### 与现有 pause_callback 的关系

当前已有 `pause_callback` 用于影像配对选择，"由我批准"模式在此基础上扩展：

```python
# 当前：仅配对被拦截
pause_callback → 只有 select_pair 一种类型

# 升级后：多个审批节点
pause_callback → {
  "type": "approval",
  "node": "tuning_decision",
  "data": {r2, rmse, features, ...}
}
```

前端（Gradio JS）统一处理 `approval` 类型，根据 `node` 字段渲染不同的选择框气泡。

---

## 四、实施路线图

### Phase 1: 最小可行迁移（~1-2 周）

```
核心算法全复用 → Gradio UI 骨架 → 并发下载 → 一键全流程 → 验证通过
```

| 工作项 | 涉及文件 |
|---|---|
| Gradio UI 骨架（对话 + 工作流面板） | 新建 `app.py`，改造 `ui/api.py` |
| 并发下载 | 改 `data_acquisition.py`（+50行） |
| API Key 运行时配置 | 改 `settings.json` 移除硬编码 |
| 系统依赖处理 | 新建 `packages.txt` |

### Phase 2: 核心 Agent 能力（~2-3 周）

```
记忆系统 → Benchmark → LLM调参 → 断点执行 → 表达层
```

| 工作项 | 涉及文件 | 预估行数 |
|---|---|---|
| ChromaDB 记忆系统 | 新建 `core/memory/` 模块 | ~200 |
| Benchmark 多模型 | 新建 xgboost/lightgbm/catboost/extra_trees + benchmark Skill | ~800 |
| LLM主导+规则兜底调参 | 改 `geo_thermo_agent.py` | ~120 |
| 断点执行（依赖声明+检查） | 改 `base_skill.py` + 7个Skill + agent | ~145 |
| Agent 表达层（角色切换+思考链+主动提问） | 改 `geo_thermo_agent.py` | ~250 |

### Phase 3: 高级功能打磨（~1 周）

```
交互式地图 → 结果总结 → 拓展 Skill → 一键体验 → 报告生成
```

| 工作项 | 涉及文件 |
|---|---|
| Folium 交互式地图 | 新建 `core/visualization.py` |
| 结果自然语言总结 | 改 `geo_thermo_agent.py` |
| UHI/时序/生态分析 Skill | 新建 3 个 Skill 文件 |
| 一键体验 + 实验报告 | 新建 `core/report_generator.py` |

---

## 五、新依赖汇总

```text
# requirements.txt（新增部分）
gradio>=6.0.0,<=6.8.0
folium>=0.16.0
chromadb>=0.5.0
sentence-transformers>=2.2.0
xgboost>=2.0.0
lightgbm>=4.0.0
catboost>=1.2.0
matplotlib>=3.7.0
Jinja2>=3.1.0

# ModelScope packages.txt
gdal-bin
libgdal-dev
libproj-dev
```

---

## 六、风险与注意事项

1. **Planetary Computer 网络**: ModelScope 在中国大陆部署可能需要网络配置
2. **ModelScope Gradio 版本约束**: `>= 6.0.0` 且 `<= 6.8.0`，部分新特性可能受限
3. **sentence-transformers 首次下载**: `all-MiniLM-L6-v2` 约 80MB，需在容器启动时预下载
4. **ChromaDB 存储大小**: 嵌入向量随项目数线性增长，建议定期清理
5. **并发下载带宽**: max_workers=3 为推荐值，实际可根据网络环境调整
6. **核心算法零改动**: 所有核心算法模块不受升级影响
7. **LLM调参稳定性**: LLM 可能返回无效 JSON 或错误参数，规则兜底是必需的安全网
8. **断点执行文件路径一致性**: 断点恢复依赖 `_normalize_plan_paths()` 正确搜索已有文件，需确保路径兜底逻辑覆盖所有场景
