# GeoThermoAI 记忆系统升级方案

> **依据**：《GeoThermoAI_ModelScope升级规划_v2.md》第 3.3 节「记忆系统（RAG + 结构化混合存储）」。
>
> **对象**：当前 `GeoThermoAI_新数据下载_docker`（Vue3 + FastAPI、多用户隔离、ModelScope Docker 部署）。
>
> **目标**：在不动现有对话存储、核心算法与前端框架的前提下，为软件加上记忆能力——**记住历史实验、引用领域知识、感知用户偏好、删除时级联清理**。

---

## 一、现状：当前代码里与"记忆"相关的实现

### 1.1 数据与存储现状

| 数据 | 存放位置 | 说明 |
|---|---|---|
| 用户数据根 | `data/users/{uid}/`（`WORKSPACE_ROOT` 可指向大容量盘） | 按用户隔离 |
| 项目列表 | `data/users/{uid}/conversations/_projects.json` | `[{name, dir}]`，**以名称标识项目**（无稳定 id） |
| 对话消息 | `data/users/{uid}/conversations/{conv_id}.json` | 平铺存储：`{id, project, title, messages, project_dir, ...}` |
| 用户配置 | `data/users/{uid}/settings.json` | cloud_threshold、dem_source、模型参数、API 设置 |
| 研究区 | `data/users/{uid}/study_areas/*.geojson` | 按用户隔离 |
| 项目产物 | `{WORKSPACE_ROOT}/{uid}/workspace/{项目名}/` | raw/processed/results，内含 `run_manifest.json` |

### 1.2 Agent 执行链路里"本来可以记、但没记"的东西

对照 `core/agent/geo_thermo_agent.py` 逐点核对：

| 环节 | 代码位置 | 现状 | 问题 |
|---|---|---|---|
| 领域知识 | `_build_system_prompt()` 里「## 领域知识」段 | TTRI 公式、重访周期、配对规则**硬编码在提示词** | 想改知识要改代码；无法按需补充 |
| 数据特征 | `_collect_data_features()`（data_pipeline 后调用） | 能算出 dem_std / ndvi_mean / lst_std / 样本数等 | **算完即弃**，没保存 |
| 模型结果 | `_execute_plan()` 中 rf_model 步骤 | `result.data` 里有 test_metrics / feature_importance / params | 只进了聊天气泡，**不落盘** |
| 阶段状态 | `run_manifest.json`（`core/manifest.py`） | 记录各阶段 status + 部分 stats | 机器可读，但运行即覆盖、不跨会话、无语义 |
| 用户偏好 | `settings.json` | 全局配置 | 无按项目偏好 |
| 删除 | `AppBackend.delete_project / delete_conversation` | 只删对话文件与项目列表项 | 无记忆可级联 |

### 1.3 结论

当前软件**没有任何记忆能力**：实验做完就忘、知识写死在代码里、删除不留痕。但好消息是——**核心数据（特征、指标、特征重要性）在内存里都已算好，只差"存下来 + 检索注入"这一层**。本次升级就是把这一层补上。

---

## 二、目标设计

### 2.1 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                    运行时内存（Python）                        │
│        当前 uid / 项目 id / 对话消息 / 本次实验缓冲             │
└───────────────┬───────────────────────────────┬──────────────┘
                │ 精确读写                       │ 语义读写
     ┌──────────▼──────────┐        ┌───────────▼───────────────┐
     │ JSON 结构化记忆       │        │ ChromaDB 向量记忆（RAG）   │
     │  experiments.json   │        │  global_knowledge（共享） │
     │  preferences.json   │        │  project_{id}（项目隔离） │
     └──────────┬──────────┘        └───────────┬───────────────┘
                │                              │
     ┌──────────▼──────────────────────────────▼───────────────┐
     │                    MemoryManager                       │
     │  写入：auto_save_experiment / set_preference            │
     │  读取：enrich_prompt（注入 Agent system prompt）         │
     │  删除：delete_conversation / delete_project（级联清理）  │
     └─────────────────────────────────────────────────────────┘
```

### 2.2 两种存储的分工

| 场景 | 用谁 | 原因 |
|---|---|---|
| "查武汉上次 RF 的 R²" | **JSON（精确查询）** | 精确数值，排序/过滤即可 |
| "上次武汉为什么效果不好？" | **ChromaDB（RAG 检索）** | 需按"云量高""样本不足"等语义匹配 |
| "TTRI 公式是什么？" | **ChromaDB（RAG 检索）** | 非结构化领域知识 |
| "我偏好的云量阈值" | **JSON（精确查询）** | 简单键值 |

> **核心原则：不决策，全查。** 每次构建 prompt 两条路都走，把结果一起注入，由 LLM 自己挑选；不写任何 if/else 路由。

### 2.3 数据布局与关键决策

现有 `data/users/{uid}/` 全部保留，新增一个 `memory/` 子目录：

```
data/users/{uid}/
├── conversations/               # 现有，不动
│   ├── _projects.json           # 升级点①：每项目补 id（uuid），name 可改但 id 稳定
│   └── {conv_id}.json
├── study_areas/                 # 现有，不动
├── settings.json                # 现有，不动
└── memory/                      # 新增
    ├── chromadb/                # ChromaDB 持久化根
    ├── projects/
    │   └── {project_id}/
    │       ├── experiments.json # 实验记录（精确查询）
    │       └── preferences.json # 用户偏好（键值对）
    └── knowledge_seed.json      # 全局知识种子源（首次启动播种用）
```

| 决策 | 方案（推荐） | 理由 |
|---|---|---|
| 记忆放哪 | `data/users/{uid}/memory/` 独立目录 | 不受用户清理产物目录影响；删除/备份收敛一处 |
| 项目标识 | `_projects.json` 补 `id`（uuid） | 重命名项目不失联；Collection 名用 `project_{id}` |
| 全局知识 | 按用户各播一份 `global_knowledge` | 多用户隔离清晰；种子只读、幂等播种 |

### 2.4 各文件的角色、内容与关系

| 文件 | 现状 | 角色 | 具体内容 |
|---|---|---|---|
| `settings.json` | 现有，不动 | 用户级全局配置（**非记忆**） | api 设置、模型参数、`data`(cloud_threshold/dem_source)、`processing`、`data_space` 凭据 |
| `_projects.json` | 现有，升级点① | 项目登记表 | `[{id, name, dir}]`，升级后每项目补稳定 `id`（uuid） |
| `{conv_id}.json` | 现有，不动 | 对话消息 | `{id, project, title, messages, project_dir, created_at, ...}`，经 `project` 归属项目 |
| `experiments.json` | 新增 | 项目实验记录（精确查询） | 实验级指标（字段见《数据清单》2.1/2.2），含 `conv_id` 供级联删除 |
| `preferences.json` | 新增 | 项目用户偏好（键值对） | 如 `{"cloud_threshold": 40, "preferred_model": "rf"}` |
| `knowledge_seed.json` | 新增 | 全局领域知识种子源（只读） | K01–K25 知识条目，首次启动播种进 `global_knowledge` |

**示例内容**：

```jsonc
// settings.json（现有）—— 用户全局配置
{ "api": {...}, "model": {...},
  "data": {"cloud_threshold": 30, "dem_source": "copernicus"},
  "processing": {...}, "data_space": {...} }

// _projects.json（升级后）—— 每项目补稳定 id，name 可重命名
{ "projects": [
    {"id": "9f2c...", "name": "武汉_202407", "dir": "/.../workspace/武汉_202407"}
] }

// {conv_id}.json（现有）—— 对话消息，project 字段归属项目
{ "id": "ab12cd34", "project": "武汉_202407", "title": "7月测试",
  "messages": [...], "project_dir": "/.../workspace/武汉_202407" }

// memory/projects/{project_id}/experiments.json（新增）—— 实验记录，一次实验一条
[ { "conv_id": "ab12cd34", "region": "武汉", "model": "rf",
    "params": {...}, "metrics": {...}, "feature_importance": [...],
    "status": "success", "timestamp": "2026-08-05 10:00:00" } ]

// memory/projects/{project_id}/preferences.json（新增）—— 项目偏好
{ "cloud_threshold": 40, "preferred_model": "rf" }

// memory/knowledge_seed.json（新增）—— 领域知识种子源
{ "items": [
    {"id": "K01", "topic": "TTRI 公式", "content": "TTRI = a·DEM + b·Slope + c·cos(Aspect) ..."},
    {"id": "K10", "topic": "Landsat 参数", "content": "..."}
] }
```

**关系图**：

```
settings.json（全局配置，Agent 读取）        _projects.json（项目登记表，补 id）
        │                                        │ project
        │                                ┌───────┴────────┐
        │                                │                │
        │                        {conv_id}.json    memory/projects/{project_id}/
        │                        （对话消息，不动）      ├─ experiments.json（结构化）
        │                                │ conv_id      └─ preferences.json（偏好）
        │                                │           （同一次写入 → ChromaDB project_{id}）
        │
        └────► knowledge_seed.json ──播种──► global_knowledge（领域知识，只读共享）
```

**一句话关系**：
- `settings.json` 管"这个用户怎么配置"（全局、确定性，非记忆）；
- `_projects.json` + `{conv_id}.json` 管"有哪些项目、哪些对话"（现状，仅补 id）；
- `experiments.json` / `preferences.json` 管"这个项目记住什么"（按项目隔离的记忆，双写一份进 ChromaDB）；
- `knowledge_seed.json` 管"全领域常识"，播种成 `global_knowledge` 供所有项目共享检索。

---

## 三、具体改动（对应到现有代码）

### 3.1 新增 `core/memory/` 模块

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `core/memory/rag_store.py` | ChromaDB 封装 | `project_collection()` / `save_experience()` / `search_for_agent()` / `save_knowledge()` / `delete_project_collection()` |
| `core/memory/experiment_log.py` | 实验记录 JSON | `add()` / `get_best()` / `get_recent()` / `delete_by_conv()` |
| `core/memory/preferences.py` | 偏好 JSON | `get()` / `set()` |
| `core/memory/memory_manager.py` | 聚合入口 | `auto_save_experiment()` / `enrich_prompt()` / `delete_conversation()` / `delete_project()` |

> 依赖：`chromadb>=0.5.0`（+onnxruntime，见第五节）。无新服务、无网络调用。

### 3.2 写入：实验完成后自动入库

**改动位置**：`core/agent/geo_thermo_agent.py::_execute_plan`（全流程收尾处）。

**时机**：`_execute_plan` 遍历完所有步骤后调用 `memory_manager.auto_save_experiment(...)`；中途失败也在 return 前记一条失败记录。

**数据来源（全部现成，零新计算）**：

| 字段 | 来源 |
|---|---|
| region / 日期范围 | 计划中 data_acquisition 步骤的 params |
| 数据特征 dem_std / ndvi_mean / lst_std / 样本数 | `_collect_data_features()`（已存在，改为顺带缓存） |
| model / params / test_metrics / feature_importance | rf_model 步骤的 `result.data`（已存在） |
| 闭合指标 | accuracy_eval 步骤的 `result.data` |
| conv_id / 状态 / 时间戳 | 运行时状态 |

**写入动作**（一次实验，两处落库）：
- `experiments.json` 追加一条**结构化记录**（精确查询用，字段含 conv_id）
- ChromaDB `project_{id}` 写入一条**自然语言段落**（语义检索用，metadata 带 `{conv_id, region, model, r2, date}`）

**要点**：
- 失败也留痕（供"上次为什么失败"类查询）
- 多配对时每对一条记录，pair 信息进 metadata
- 写入失败仅告警，不影响主流程

### 3.3 读取：prompt 注入 + 领域知识迁移

**改动位置**：`core/agent/geo_thermo_agent.py::process_command`。

- 在调用 `_build_system_prompt()` **之前**，调用 `memory_manager.enrich_prompt(...)`，把三段内容注入 system prompt：

```
## 当前项目历史经验（RAG 检索自 project_{id}）
- 2024-07 武汉 RF：R²=0.87，max_depth=35，特征重要性 NDVI(0.28) > DEM(0.23)
## 领域知识参考（RAG 检索自 global_knowledge）
- 影像配对要求：Landsat 与 Sentinel-2 时间差 ≤ 2 天
## 历史最佳实验（JSON 精确查询）
- 武汉 | RF | R²=0.87 | 参数 {n_estimators:300, max_depth:35}
```

- **移除** `_build_system_prompt()` 里的「## 领域知识」硬编码段，内容原样迁入 `knowledge_seed.json` 作为播种种子。
- **兜底**：检索结果为空时仍输出基础几条，保证鲁棒。
- 纯咨询路径（`_is_advisory_request` → `ask_stream`）的 context 同样带上记忆，让"上次 XX 效果如何"能答出数据。

### 3.4 删除级联

**改动位置**：`server.py::AppBackend.delete_conversation / delete_project`。

```
删除对话 → 现有逻辑（删 conv 文件）+ 新增：
    experiments.json 删除该 conv_id 记录；ChromaDB 删除 metadata.source_conv 条目
删除项目 → 现有逻辑（删全部对话 + 项目列表项）+ 新增：
    删除 memory/projects/{project_id}/ 目录；ChromaDB 删除 project_{id} Collection
```

**前端（小改）**：`Sidebar.vue` 现有删除弹窗补充文案，展示将删除的记忆影响范围（如"该对话产生的实验记录 N 条"）。

### 3.5 偏好与问答（可选增强）

- `preferences.json` 按项目记录偏好（云量阈值、常用模型、时间习惯），Agent 规划时读取参考；来源为用户显式告知或 Agent 询问确认后写入。
- 明确**不做**：把对话原文整段塞进 RAG（噪声大、成本高）。

### 3.6 改动量汇总

| 文件 | 改动 | 预估 |
|---|---|---|
| 新增 `core/memory/`（4 个文件） | 写入 / 检索 / 聚合 / 删除 | ~350 行 |
| `core/agent/geo_thermo_agent.py` | 写入钩子 + 注入 + 移除硬编码领域知识 | +80 / −10 行 |
| `server.py` | 项目 id + 按 uid 初始化 MemoryManager + 级联删除 | +50 行 |
| `frontend/src/components/Sidebar.vue` | 删除弹窗文案 | +10 行 |
| `requirements.txt` / `Dockerfile` | chromadb + 模型预下载 | 少量 |

---

## 四、建议开发流程

### 4.1 总体原则

- **分 6 步推进，每步可独立开发、独立验证、可回滚**；顺序按"依赖倒排 + 风险从低到高"。
- 第 1~3 步**只增不改**（新增文件 + 附加字段 + 一个写入钩子），现有行为零变化；第 4 步才动 Agent 行为（影响面最大，放后面重点回归）。
- 核心算法零改动，全程可随时回退到无记忆版本。

### 4.2 分步开发流程

| 步骤 | 做什么 | 怎么验证 | 回滚方式 |
|---|---|---|---|
| **① 环境与数据底座** | 装 chromadb；新建 `core/memory/` 4 个文件；把 `_build_system_prompt` 里的领域知识迁为 `knowledge_seed.json` 并播种 `global_knowledge` | 独立脚本跑通「写入→检索→删除」；重启后数据仍在；播种幂等 | 不接 Agent，删新增文件即可，主流程无感 |
| **② 项目稳定 id** | `_projects.json` 补 `id`（uuid）；加载逻辑兼容旧数据（无 id 的项目首次启动自动补齐） | 老用户数据不报错；新建/重命名/删除项目后 id 稳定 | id 是附加字段，不读不写也不影响现状 |
| **③ 记忆写入** | `_execute_plan` 收尾调 `auto_save_experiment`；数据全部复用现有 `result.data` / `_collect_data_features` | 跑一次全流程 → experiments.json + ChromaDB 有记录；失败也留痕；写入失败不影响流程 | 去掉一个钩子调用即可 |
| **④ 记忆读取（重点回归）** | `process_command` 注入 `enrich_prompt`；移除 `_build_system_prompt` 硬编码领域知识段（保留空检索兜底） | 全流程回归照常执行；"上次武汉 RF 怎么样"能引用历史；领域知识问答不退化 | 先保留兜底段，注入异常时回退；必要时整体注释注入 |
| **⑤ 删除级联 + 前端** | `AppBackend.delete_conversation / delete_project` 级联；`Sidebar.vue` 删除弹窗文案 | 删除对话/项目后 JSON 与 Collection 均无残留 | 级联调用加 try/except，失败仅告警 |
| **⑥ 偏好与问答（可选）** | `preferences.json` 读写；纯咨询路径（`ask_stream`）context 带记忆 | 设置偏好后 Agent 规划可感知；"上次 XX 效果"能答出数据 | 偏好读写独立，可单独关停 |

### 4.3 开发与测试建议

- **验证环境以 Docker 为准**：记忆「写入→检索→删除」闭环、Agent 回归等所有功能验证都在 Docker 容器内完成——先构建镜像（含 chromadb + 预下载 bge-small-zh-v1.5 模型），再在容器内跑验证脚本；
- **本地仅做轻量检查**：本地 conda 环境只用于 `py_compile` / 单元测试等快速检查，不作为功能验证依据；
- **测试分层**：`core/memory` 写纯单元测试（不依赖 FastAPI）；Agent 侧复用现有 `tests/test_skill_chain_synthetic.py` 等做回归；**当前无真实运行产物，字段映射先用合成测试验证**，后续拿到真实产物再校正；
- **先定模型再动手**：嵌入模型已定 `bge-small-zh-v1.5`（ONNX，见第五节），写入/检索接口按此实现，避免返工。

### 4.4 里程碑

| 里程碑 | 内容 | 判定 |
|---|---|---|
| M1 数据底座可用 | 记忆能写、能查、能删 | 脚本闭环通过 |
| M2 只记录不干预 | 每次实验自动入库，Agent 行为不变 | 全流程回归通过 |
| M3 记忆生效 | Agent 能引用历史经验与领域知识 | 对话问答验收通过 |
| M4 生命周期闭环 | 删除级联 + 前端影响提示 | 残留检查通过 |

---

## 五、依赖与环境改造

**嵌入模型选型**（项目数据为中文，需权衡）：

| 方案 | 依赖 | 镜像体积 | 中文效果 | 建议 |
|---|---|---|---|---|
| ChromaDB 内置 ONNX 嵌入（ONNXMiniLM_L6_V2） | `chromadb` + onnxruntime | 小（~80MB） | 一般 | **默认推荐**，免 torch |
| `sentence-transformers/all-MiniLM-L6-v2` | 需 torch | 大（数百 MB） | 一般 | v2 文档方案，体积代价高 |
| `paraphrase-multilingual-MiniLM-L12-v2` | 需 torch | 大 | 好 | 中文友好但重 |
| `BAAI/bge-small-zh-v1.5`（ONNX） | onnxruntime | 中（~100MB） | 好 | 中文最优，需自行封装 |

**选型结论**：
- **中文场景首选 `BAAI/bge-small-zh-v1.5`（ONNX）**——中文专用模型，中文检索效果最好，且免 torch（不给 OSGeo 镜像增加数百 MB PyTorch）；
- 若**不想自行封装** embedding 函数、可接受中文召回率打折，则退用 ChromaDB 内置 ONNX 嵌入（零代码默认方案）。

**bge-small-zh 落地要点**：
- 需写一个自定义 embedding 函数接入 ChromaDB（不能直接用内置 ONNX 嵌入函数）；
- 遵循 BGE 惯例：查询文本加前缀 `"为这个句子生成表示以用于检索相关文章："`，检索效果更稳定；
- 模型文件在 Dockerfile **构建期预下载**进镜像（避免首次启动联网拉取）。

**改动清单**：
- `requirements.txt` 新增 `chromadb>=0.5.0`（+`onnxruntime`）
- `Dockerfile` 构建期预下载嵌入模型到镜像（避免首次启动联网）
- 首次启动幂等播种 `global_knowledge`（Collection count>0 则跳过）

---

## 六、风险与注意事项

1. **中文语义检索**：MiniLM 英文模型对中文效果有限 → 按第五节决策点选型。
2. **镜像体积**：引入 torch 会显著增大 OSGeo 基础镜像 → 优先 ONNX 方案。
3. **存储增长**：向量随实验数线性增长 → 按项目归档或定期清理。
4. **删除一致性**：先删文件、后删 Collection，失败重试并告警，避免"记忆幽灵"。
5. **项目重命名**：Collection 名与路径用稳定 id，不得用项目名。
6. **隐私**：`preferences.json` 禁止存放 API Key（仍在 settings.json）。
7. **注入体积**：RAG top-3×2 + JSON 历史最佳，注入量小，不挤占 max_tokens。
8. **零回归**：记忆写入失败 catch 后仅告警，不影响主流程。

---

## 七、验证清单

| 阶段 | 验收点 |
|---|---|
| 数据底座 | 写入→检索→删除闭环通过；重启后数据仍在；播种幂等 |
| Agent 写入 | 一次全流程后 experiments.json 有记录且字段完整；失败也留痕 |
| Agent 读取 | 新对话"上次武汉 RF 怎么样"能引用历史结果与经验 |
| 删除级联 | 删除对话/项目后 JSON 与 Collection 均无残留；前端弹窗展示影响范围 |
| 偏好与问答 | 设置偏好后 Agent 规划行为可感知；纯咨询路径能引用历史作答 |
