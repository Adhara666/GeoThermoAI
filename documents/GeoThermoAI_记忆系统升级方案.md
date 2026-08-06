# GeoThermoAI 记忆系统升级方案

> **依据**：《GeoThermoAI_ModelScope升级规划_v2.md》第 3.3 节「记忆系统（RAG + 结构化混合存储）」。
>
> **对象**：当前 `GeoThermoAI_新数据下载_docker`（Vue3 + FastAPI、多用户隔离、ModelScope Docker 部署）。
>
> **目标**：在不改动现有对话存储、核心算法与前端框架的前提下，落地「JSON 结构化记忆 + ChromaDB 语义记忆（RAG）」的混合记忆系统，让 Agent 能够：记住历史实验、引用领域知识、感知用户偏好，并在删除时级联清理。

---

## 一、现状盘点

### 1.1 当前数据与"记忆"相关现状

| 数据 | 当前存放位置 | 说明 |
|---|---|---|
| 用户数据根 | `data/users/{uid}/`（可用 `WORKSPACE_ROOT` 指向大容量盘） | 按用户隔离 |
| 项目列表 | `data/users/{uid}/conversations/_projects.json` | `[{name, dir}]`，以**名称**标识项目 |
| 对话消息 | `data/users/{uid}/conversations/{conv_id}.json` | 平铺存储：`{id, project, title, messages, project_dir, ...}` |
| 研究区 | `data/users/{uid}/study_areas/*.geojson` | 按用户隔离 |
| 用户配置 | `data/users/{uid}/settings.json` | 全局配置（cloud_threshold、dem_source、模型参数、API 设置） |
| 项目产物 | `{WORKSPACE_ROOT}/{uid}/workspace/{项目名}/` | raw/processed/results，含 `run_manifest.json` |
| 实验结果 | **无**（仅 `run_manifest.json` 记录阶段状态与部分 stats） | 无结构化实验档案、无跨次引用能力 |
| 领域知识 | **硬编码**在 Agent 的 system prompt | TTRI/TCR 公式、重访周期、配对规则等写死在提示词里 |
| 用户偏好 | 仅全局 settings.json | 无按项目的偏好记忆 |

### 1.2 与目标记忆系统的差距

| 能力 | 现状 | 目标（升级后） |
|---|---|---|
| 精确查询历史实验（"上次武汉 RF 的 R²"） | ✗ 无 | JSON `experiments.json`，`get_best/get_recent` |
| 语义检索项目经验（"上次为什么效果不好"） | ✗ 无 | ChromaDB `project_{id}` Collection（RAG） |
| 领域知识问答（"TTRI 公式是什么"） | 部分（prompt 硬编码） | ChromaDB `global_knowledge`（RAG 检索注入） |
| 按项目用户偏好 | ✗ 无 | JSON `preferences.json` |
| 删除级联清理记忆 | ✗ 只删对话/项目文件 | 级联清理 JSON 记录 + ChromaDB Collection |
| 跨用户隔离 | 对话/配置已按用户隔离 | 记忆同样按「用户 → 项目」两级隔离 |

---

## 二、升级目标与总体设计

### 2.1 记忆系统架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    运行时内存（Python）                            │
│        当前用户 uid / 当前项目 id / 对话消息 / 实验缓冲              │
└───────────────┬───────────────────────────────┬──────────────────┘
                │ 精确读写                      │ 语义读写
     ┌──────────▼───────────┐      ┌───────────▼──────────────────┐
     │ JSON 结构化记忆        │      │ ChromaDB 向量记忆（RAG）       │
     │  experiments.json    │      │  global_knowledge（领域通识）   │
     │  preferences.json    │      │  project_{id}（项目历史经验）   │
     │  （快、稳、易调试）     │      │  （灵活、模糊匹配）             │
     └──────────┬───────────┘      └───────────┬──────────────────┘
                │                              │
     ┌──────────▼──────────────────────────────▼──────────────────┐
     │                    MemoryManager                          │
     │  ① 注入：_enrich_with_memory → Agent system prompt          │
     │  ② 写入：auto_save_experiment（实验完成自动入库）            │
     │  ③ 生命周期：delete_conversation / delete_project 级联清理   │
     │  ④ 偏好：preferences 读写                                  │
     └──────────┬─────────────────────────────────────────────────┘
                │
     ┌──────────▼─────────────────────────────────────────────────┐
     │  现有模块（不改动）：GeoAgent / Skills / AppBackend / Vue3   │
     └────────────────────────────────────────────────────────────┘
```

### 2.2 两种存储的分工（沿用 v2 设计）

| 场景 | 用谁 | 原因 |
|---|---|---|
| "查武汉上次 RF 的 R²" | **JSON** | 精确数值，排序/过滤即可 |
| "上次武汉为什么效果不好？" | **RAG** | 需搜索"云量高""样本不足"等语义描述 |
| "TTRI 公式是什么？" | **RAG** | 非结构化领域知识 |
| "我偏好的云量阈值" | **JSON** | 简单键值，读写各一次 |

**核心原则：不决策，全查。** 每次构建 prompt 时两条路都走，把结果一起注入，由 LLM 自己挑选有用信息；不写任何 if/else 路由逻辑。

### 2.3 数据布局设计（贴合当前多用户代码）

现状对话存储**完全不动**，新增独立的记忆目录。推荐方案：

```
data/users/{uid}/
├── conversations/                 # 现有，不动
│   ├── _projects.json             # 升级：每个项目补一个稳定 id（uuid）
│   └── {conv_id}.json
├── study_areas/                   # 现有，不动
├── settings.json                  # 现有，不动
└── memory/                        # 新增（记忆系统）
    ├── chromadb/                  # ChromaDB 持久化根（按用户隔离）
    ├── projects/
    │   └── {project_id}/
    │       ├── experiments.json   # 该项目实验记录（精确查询）
    │       └── preferences.json   # 该项目用户偏好（键值对）
    └── knowledge_seed.json        # 全局知识种子源（首次初始化用，可选）
```

**关键决策点：**

| 决策 | 方案 A（推荐） | 方案 B | 理由 |
|---|---|---|---|
| 记忆放哪 | `data/users/{uid}/memory/` 独立目录 | 用户项目目录内 `{project_dir}/memory/` | A 不受用户清理产物目录影响、备份/删除收敛一处；B 更直观但与结果文件耦合 |
| 项目标识 | `_projects.json` 每项目补 `id`（uuid） | 继续用项目名 | 名称可重命名；稳定 id 使 Collection 名 `project_{id}` 与路径永不失联 |
| 全局知识范围 | `global_knowledge` 按用户各一份（幂等播种） | 应用级全局一份 | 多用户下按用户播种更简单安全（领域种子本身只读、可共享，二者皆可，推荐前者） |

> 与 v2 文档的映射：v2 的 `data/projects/{项目}/memory/` 在此等价为 `data/users/{uid}/memory/projects/{project_id}/`；`global_knowledge` / `project_{id}` Collection 设计不变。

---

## 三、升级步骤（分四个阶段，每阶段可独立验收）

### 阶段 A：数据底座 —— 新增 `core/memory/` 模块

**新增文件与职责：**

| 文件 | 职责 |
|---|---|
| `core/memory/__init__.py` | 统一导出 `MemoryManager` |
| `core/memory/rag_store.py` | `ChromaRAG`：Collection 管理、`save_knowledge`、`save_experience`、`search_for_agent`、`delete_project_collection` |
| `core/memory/experiment_log.py` | `ExperimentLog`：experiments.json 增删查（`add` / `get_best` / `get_recent` / `delete_by_conv`） |
| `core/memory/preferences.py` | `Preferences`：preferences.json 键值读写 |
| `core/memory/memory_manager.py` | `MemoryManager`：聚合以上，提供 `enrich_prompt` / `auto_save_experiment` / `delete_conversation` / `delete_project` / `set_preference` 接口 |

**global_knowledge 预置知识种子**：把现在 Agent prompt 里硬编码的领域知识原样迁出，作为首次播种内容：

| 种子 | 内容要点 |
|---|---|
| TTRI 公式 | TTRI = a·DEM + b·Slope + c·cos(Aspect)，地形对地表温度的控制作用 |
| TCR 机制 | 热约束残差修正，30m 系统偏差空间化到 10m |
| 降尺度原理 | Landsat 30m LST + Sentinel 10m 多光谱 → 10m LST |
| 卫星参数 | Landsat 重访 16 天、Sentinel 重访 5 天；配对时间差 ≤ 2 天 |
| 经验条目 | 武汉夏季云量 30–50%，建议 cloud_threshold=50 等 |

**验收**：不接 Agent，直接调用 `ChromaRAG` / `ExperimentLog` 跑通「写入 → 检索 → 删除」闭环；首次启动播种幂等（count>0 跳过）。

### 阶段 B：Agent 接入 —— 记忆写入 + 读取注入

**写入时机**（全流程执行完后自动入库，成功与失败都记）：

```
用户指令 → data_acquisition → data_pipeline（收集数据特征 data_features）
        → ttri_compute → rf_model（测试指标/特征重要性/参数）→ tcr_compute
        → lst_export → accuracy_eval（粗尺度闭合指标）
        → 全流程结束：MemoryManager.auto_save_experiment()
            ① experiments.json（精确记录）
            ② ChromaDB project_{id}（语义化自然语言段落，metadata 带 conv_id）
```

**实验记录字段**：region（研究区）、日期范围、model、超参数、测试指标（R²/RMSE/MAE/MB）、独立预测指标、闭合指标、特征重要性、数据特征（dem_std / ndvi_mean / lst_std / 样本数）、配对信息、conv_id、状态（成功/失败）、时间戳。

**数据来源复用**：测试指标与特征重要性取 rf_model 结果；闭合指标取 accuracy_eval 结果；数据特征取 `_collect_data_features()` 已有输出；阶段统计可直接复用 `run_manifest.json` 的 stats，避免重复计算。

**读取注入**（`_enrich_with_memory`）：

- 调用点：Agent `_build_system_prompt` 之前、`_get_context` 之后；
- 注入内容示例（写在"当前软件状态"之前）：

```
## 当前项目历史经验（RAG 检索自 project_{id}）
- 2024-07 武汉 RF：R²=0.87，max_depth=35，特征重要性 NDVI(0.28) > DEM(0.23)
## 领域知识参考（RAG 检索自 global_knowledge）
- 武汉夏季云量高，建议 cloud_threshold=50
## 历史最佳实验（JSON 精确查询）
- 武汉 | RF | R²=0.87 | 参数 {n_estimators:300, max_depth:35}
```

- 原 prompt 中「## 领域知识」硬编码段**移除**，改由 RAG 注入；保留兜底：检索结果为空时仍回退输出基础几条，保证鲁棒。

**验收**：跑一次全流程后 `experiments.json` 有记录；新开对话询问"上次武汉 RF 怎么样"能引用到历史结果与经验。

### 阶段 C：生命周期与删除级联

删除时记忆必须一并清理，避免残留数据干扰后续检索：

```
删除对话（前端弹窗确认）
    ├─ 现有：删除 data/users/{uid}/conversations/{conv_id}.json
    └─ 新增：experiments.json 中删除该 conv_id 的记录 + RAG 删除
             metadata.source_conv == conv_id 的条目（预览时统计条数）

删除项目（前端弹窗确认）
    ├─ 现有：删除该项目全部对话文件 + _projects.json 条目
    └─ 新增：删除 data/users/{uid}/memory/projects/{project_id}/ 整个目录
             + ChromaDB 删除 project_{project_id} Collection
```

**前端改动（小）**：`Sidebar.vue` 现有删除弹窗扩展文案，展示将删除的记忆影响范围（如"该对话产生的实验记录 N 条，将从记忆库中移除"）；后端 `AppBackend.delete_conversation / delete_project` 增加级联调用。

**验收**：删除对话/项目后，JSON 与 ChromaDB 均无残留；重新检索不命中已删内容。

### 阶段 D：用户偏好与问答增强

- **偏好记录**：`preferences.json` 按项目记录用户偏好（如云量阈值、常用模型、时间习惯），Agent 规划时读取并作为参考；来源可为用户显式告知（"以后云量阈值都用 50"）或 Agent 主动询问确认后写入（低误存风险）。
- **问答增强**：现有"纯咨询"路径（`ask_stream`）的 context 中带上记忆（JSON 历史最佳 + RAG 项目经验），让"上次 XX 的效果如何"这类问题能给出带数据的回答。
- **不做**：把对话原文整段塞进 RAG（成本高、噪声大）；仅在实验完成/偏好确认时写入。

---

## 四、与现有功能的配合

| 现有机制 | 配合方式 |
|---|---|
| 多配对顺序执行 | 每组配对执行完后各写一条实验记录，pair 信息（日期/云量/覆盖率）进 metadata，支持"哪组效果最好"的对比问答 |
| `run_manifest.json` | 记忆写入复用其 stage stats 作为数据源，避免重复读取/计算 |
| 并发下载 / SSE 流 | 记忆写入为本地 IO（原子替换写 JSON、ChromaDB 单线程写），不与下载线程冲突 |
| 多用户隔离 | MemoryManager 以 uid + project_id 为键，天然隔离；全局知识按用户播种 |
| 删除/重命名项目 | 重命名仅改 `_projects.json` 的 name，id 不变，记忆不失联 |

---

## 五、依赖与环境改造

**嵌入模型选型**（关键决策，v2 文档选 `all-MiniLM-L6-v2`，但本项目数据为中文，需权衡）：

| 方案 | 依赖 | 镜像体积 | 中文检索效果 | 建议 |
|---|---|---|---|---|
| ChromaDB 内置 ONNX 嵌入（ONNXMiniLM_L6_V2） | 仅 `chromadb` + onnxruntime | 小（模型 ~80MB） | 一般（英文为主） | **默认推荐**，免 torch |
| `sentence-transformers/all-MiniLM-L6-v2` | 需 torch | 大（torch CPU 数百 MB） | 一般 | v2 文档方案，体积代价高 |
| `paraphrase-multilingual-MiniLM-L12-v2` | 需 torch | 大 | 好（多语言） | 中文友好但重 |
| `BAAI/bge-small-zh-v1.5`（ONNX） | onnxruntime | 中（~100MB） | 好（中文） | 中文场景最优，需自行封装 embedding 函数 |

**改动清单：**

- `requirements.txt` 新增：`chromadb>=0.5.0`（+`onnxruntime`，若选 ONNX 嵌入则无需 sentence-transformers/torch）；
- `Dockerfile`：嵌入模型文件在**构建期预下载**进镜像（避免首次启动联网拉取），指定国内可访问的模型源；
- 首次启动：幂等播种 `global_knowledge` 种子数据（Collection count>0 则跳过）。

---

## 六、风险与注意事项

1. **中文语义检索效果**：`all-MiniLM-L6-v2` 为英文模型，中文项目经验检索可能不准 → 按第五节决策点选择，必要时用 `bge-small-zh` 等中文模型。
2. **镜像体积**：若走 sentence-transformers 需引入 torch，OSGeo 基础镜像体积明显增大 → 优先 ONNX 方案。
3. **存储增长**：嵌入向量随实验数线性增长 → 建议按项目归档或定期清理；实验记录 JSON 定期压缩。
4. **删除一致性**：删除对话/项目与 RAG 清理需保持同语义（先删文件、后删 Collection，失败重试并告警），避免"记忆幽灵"。
5. **项目重命名**：依赖稳定 id，Collection 名与路径不得用项目名，否则改名即失联。
6. **隐私与凭据**：`preferences.json` 禁止存放 API Key 等敏感信息（仍在 settings.json）；记忆内容不包含下载凭据。
7. **注入体积**：RAG 各取 top-3（共 6 条）+ JSON 历史最佳，注入量小，不撑大 system prompt、不影响 max_tokens 预算。
8. **写入失败降级**：记忆写入失败不影响主流程（catch 后仅告警），保证既有功能零回归。

---

## 七、验证清单（按阶段验收）

| 阶段 | 验收点 |
|---|---|
| A 数据底座 | 写入→检索→删除闭环通过；重启后数据仍在；播种幂等 |
| B Agent 接入 | 一次全流程后 experiments.json 有记录且字段完整；新对话"上次武汉 RF 怎样"能引用历史；失败流程也留痕 |
| C 删除级联 | 删除对话/项目后 JSON 与 Collection 均无残留；前端弹窗展示影响范围 |
| D 偏好与问答 | 设置偏好后 Agent 规划行为可感知（如云量阈值）；纯咨询路径能引用历史数据作答 |
