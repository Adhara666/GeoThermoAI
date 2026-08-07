# GeoThermoAI 多角色 Agent 与记忆联动 · 技术方案 v1（v1.1 实现期修订）

> 目标读者：下一次任务里严格照此实现的开发者（人或 AI）
> 基准代码：`队友运行副本-docker版/GeoThermoAI_新数据下载_docker`（2026-08-07 逐行通读）
> 前置文档：`升级规划/GeoThermoAI_ModelScope升级规划_v2.md`（3.5.5 角色切换、3.6 七规则调优、3.11 审批模式）、`从零学会_单Agent多角色_手把手教学.md`、`记忆系统开发/实际架构/记忆系统实际架构.md`
> 硬约束：**不删除、不破坏队友现有任何功能**。所有新增能力必须以「新增文件 + 现有文件的向后兼容扩展」方式落地，未开启新特性时行为与现在完全一致。
> 原始版本备份：`GeoThermoAI_多角色Agent与记忆联动_技术方案_v1_原始备份_20260807.md`

## 修订记录

| 版本 | 日期 | 修订内容 |
|---|---|---|
| v1 | 2026-08-07 | 初稿 |
| v1.1 | 2026-08-07 | 实现期回填：① 13.1 四项待确认改为**拍板结论**（挂起等待 / 全量意图分类 / 对话式引导 / 调优默认 5 轮硬上限 8）；② 修正 1.1 中 `server.py`、`geo_thermo_agent.py` 的行数事实；③ 12 章特性开关口径改为「实现期默认关闭、全部阶段验收通过后置为开启」；④ 新增附录 C 记录魔搭（ModelScope）Docker 创空间部署硬约束与 dist 陷阱；⑤ 附录 B 补 `tuning_max_rounds` 默认值与硬上限的区分口径 |
| v1.2 | 2026-08-07 | 第三次真实全流程回填：① 4.4.2 中文数字月份与年月独立抽取（「24年七月」「去年七月」不再被反复追问月份）；② 9.4 新增第 6 条红线：`sanitize` 的路径替换要锚定词首，否则 `row/col` 被当成路径吃掉；③ 9.4 新增第 7 条红线：同一句话只由一个地方输出（失败摘要此前被执行引擎与 RoleHooks 各打一遍）；④ `export_geotiff` 在 int64 转换**之前**先检出 row/col 空值，报「上游中间 CSV 写入不完整」而不是让 NaN 变成 INT64_MIN 后报「越界」（见 10.8） |
| v1.2 | 2026-08-07 | 第二次真实全流程回填：① 模板化报告新增「降级原因」一行（区分「接口没返回」与「未通过表述检查，未过项为 XX」），并指引去日志面板；② 降级原因只用规则中文短名 `RULE_LABELS`，因为 `E-R2` 含子串 `R2` 会被 E-R1 当成决定系数关键词；③ `_last_keyword_pos` 加守卫：紧跟连字符的 `R2`（如 `E-R2`）不算指标关键词 |
| v1.2 | 2026-08-07 | 新增 **E-R7「报告必须结构完整、不得停在半句话」**：`max_tokens` 耗尽时大模型会无标记地停在任意位置，E-R1–E-R6 查不出「没写完」，之前这种半截稿会原样显示给用户。同时把「气泡文案禁止 `text[:N]` 硬切」写成 9.4 第 6 条红线 |
| v1.2 | 2026-08-07 | 首次真实全流程（鄂州市_市 2024-07）暴露的 4 个缺陷修复并回写：① 模板报告末尾用 `text[:120]` 硬切导致半截话 → 改为按句子边界截断；② `independent_prediction` 的 `metrics` 子字典未摊平 → 报告出现「样本数有值、决定系数未计算」；③ `lst_export` 的 `total_valid` 在顶层却按 `stats` 里读 → 有效像元数显示「未知」；④ `tcr_statistics` 已存入 bundle 但未进事实清单与允许值表 → 写对的残差数值会被 E-R1 误判成编造。另修掉「找到 N 组可用的影像组合」重复输出一次的问题（执行引擎与 `_ask_user_to_select_pair` 都在打印） |
| v1.1 | 2026-08-07 | 实现期补充（代码与文档已对齐）：⑥ 2.5 文件清单补 `orchestrator/agent_config.py`、`orchestrator/role_hooks.py`、`roles/slots.py`；⑦ 3.3 明确手动调参表单排除 `random_state`、恢复值在后端二次校验截断；⑧ 6.2 补 R1/R3 的兜底调优方向、R5 与 R6 的重叠记录、硬停止规则与用户「继续下一轮」的边界；⑨ 3.2 明确 `tuning_round` 不对首次训练重复弹窗；⑩ 7.3 的 E-R2/E-R6 补「显式否定豁免」与「同句内邻近判定」；⑪ 8.2 槽位来源补 `mentioned`；⑫ 新增 10.7 记录 `core/manifest.py` 的项目根推导泛化；⑬ 11.2 补 `test_roles_end_to_end_synthetic.py`；⑭ 9.4 澄清文案改写对两条路径都生效 |
| v1.2 | 2026-08-07 | **真实运行反馈缺陷修复**（P0–P6 均已落地后，实际使用中定位到的问题；本行之后的编号延续 v1.1）：⑮ 2.6（新增小节）`base_role.call_json` 系列调用的 `max_tokens` 普遍偏小（500–600），推理模型的思考 token 计入输出预算后把 JSON 截断，导致意图分类／出计划／反思频繁解析失败、掉回关键词兜底或内置默认——这正是「感觉不太像智能体」的直接原因之一；参考旧路径 `_call_api(..., max_tokens=4096)` 的经验值统一放宽，`extract_json` 增加「括号配对回扫」与「去尾随逗号」两级兜底，应对模型在 JSON 前后夹带说明文字、末尾多一个逗号等常见的非严格 JSON 输出；⑯ 4.4 时间槽位补年份合理性校验（`slots.year_plausible` / `MIN_DATA_YEAR`），修正「用户说'125年'，系统直接反问'125年确认月份'」这种不加甄别复述异常输入的问题；⑰ 5.3 的 D7 规则修正为使用**完整 30 米约束层**（`constraint_rows` / `30m_constraint_grid_meta.json` 的 `valid_ratio`）而不是 30 米训练抽样（`30m_features_step2.csv`，`step=2` 等间隔抽样，行数只有完整约束层的约四分之一），此前的实现把「分子已被抽样缩小 4 倍、分母仍是全量格网」的比值当作真实有效像元占比，系统性低估约 4 倍，导致换了时间段仍反复误报「有效像元占比偏低」；⑱ `data_quality` 审批节点新增「我接受现状，继续执行」选项（`Option.ACCEPT`）；⑲ 3.2 / 6.5「重新选择影像组合」的语义修正为**真正的阶段内回退**：总调度识别 `payload.reselect_pair` 标记后直接复用原 plan 重新执行 `_execute_plan`（自然回到 `data_acquisition` 重新搜索并弹出配对选择），不再调用规划 Agent 的 `replan()`（此前的实现虽然文字说的是「阶段内回退」，但代码路径仍会真的触发一次 LLM 重新规划，多一次不必要的调用与被规划 Agent 改写整条计划的风险）；⑳ 附录 C 补充前端底部灰边问题的已知限制与人工校准机制。 |

**文档与代码一致性约定**：本文档是实现的唯一依据。实现期若发现文档与代码矛盾，**先改文档、再改代码**，最终两者必须一致。

---

## 第 0 章 · 本文档怎么用

- 第 1 章是现状事实，来自逐行阅读，实现时不要再猜，直接以此为准。
- 第 2 章是架构决策，回答「怎么搭、谁管谁」，是防止概念套错的钉子。
- 第 3～7 章是四个角色 Agent 与总调度的详细设计，每章都给出输入、输出、反思规则、失败处理。
- 第 8 章是记忆联动，第 9 章是前端，第 10 章是对现有文件的逐处改动清单。
- 第 11～13 章是测试、实施顺序、风险。
- 实现时按第 12 章的阶段顺序推进，每阶段结束跑第 11 章对应的测试。

---

# 第 1 章 · 现状梳理

## 1.1 分层架构

```
前端 Vue3 (frontend/src)
  App.vue ── Sidebar | ChatMessages + PairSelectCard + ChatInput | Workbench(11 面板)
  stores/  chat.js(消息/SSE/暂停) project.js(项目对话) settings.js auth.js
  api/index.js  fetch + EventSource，token 走 ?token= 穿透反向代理
        │ REST + SSE
后端 FastAPI (server.py，1743 行)
  AppBackend  每用户隔离：_assistant_for / _agent_for / _memory_for
  聊天：线程 + queue.Queue → SSE 事件（token/append/pause/workflow/log/done/error）
        │
Agent (core/agent/geo_thermo_agent.py，1199 行)
  GeoThermoAgent.process_command  →  _parse_plan  →  _normalize_plan_paths  →  _execute_plan
        │
Skill 层 (core/skills/)
  BaseSkill / SkillParameter / Hyperparameter / SkillResult
  SkillRegistry（name→skill，group→names）
  8 个内置 Skill：data_acquisition / data_pipeline / ttri_compute / rf_model /
                  tcr_compute / lst_export / accuracy_eval / ai_assistant
        │
算法层 (core/)
  data_preprocessing split_dataset rf_model ttri tcr lst_final export_geotiff
  evaluation grid_mapping visualization manifest atomic_io intermediate_cleanup stage_rebuild
        │
记忆层 (core/memory/)
  MemoryManager ── RAGStore(ChromaDB) / ExperimentLog(JSON) / Preferences(JSON) / seed_data(18 条)
```

## 1.2 一次对话的完整链路（时序，务必记牢）

```
用户发消息
 └─ POST /api/chat/start  {project, conv, message}
     ├─ 校验对话存在、API 已配置
     ├─ 若 _is_workflow_command 且 _is_agent_command：强制要求已上传研究区 + 已设项目目录
     ├─ 追加占位气泡 "▍"，落盘会话 JSON
     ├─ 建 queue + threading.Event，起后台线程 _runner
     └─ 返回 messages
 └─ GET /api/chat/stream?conv=  （SSE）
     └─ _stream_events 消费队列，按 200ms 轮询，10s 发一次 keepalive

_runner 线程内：
  if _is_agent_command(user_msg):            # 关键词命中 → Agent 路径
      agent.process_command(user_msg, on_token, on_log, pause_callback,
                            workflow_callback, project_dir, settings_path,
                            study_areas_dir, conv_id, project_id, memory_manager)
  else:                                       # 未命中 → 纯聊天路径
      assistant.ask_stream(user_msg, on_token, context, prior_messages)   # ← 只有这条路带历史

process_command 内：
  ① 若存在已上传研究区 → 气泡输出 "📁 已加载研究区文件：xxx"
  ② _is_advisory_request 启发式（含"推荐+参数"/"原理"/"是什么"/含"什么"或以?结尾）
     → 命中则走 ask_stream（注入 memory），直接返回
  ③ 否则：_get_context + registry.get_tool_descriptions_for_llm + memory.enrich_prompt
     → _build_system_prompt → _call_api(temperature=0.1, max_tokens=4096)
  ④ _parse_plan（直接 json → ```json``` 块 → 首尾大括号，三级兜底）
     失败重试一次（temperature=0.0）；再失败且用户说了"全流程"→ _build_full_workflow_plan 内置计划
  ⑤ 安全网：用户要全流程但计划缺步骤/参数空/只有 ai_assistant → 强制替换为内置全流程计划
  ⑥ _normalize_plan_paths：把 LLM 生成的各种乱七八糟路径统一到 region_dir 下
  ⑦ _execute_plan
```

`_execute_plan` 内部（`core/agent/geo_thermo_agent.py` L564–L929）：

```
建 exp_state（实验记录累加器）+ 定义 _finalize_experiment / _record_step
按 project_dir 算出 raw_dir / processed_dir / results_dir
SKILL_PATHS 硬编码映射（每个 skill 的全部输入输出路径）

for i, step in enumerate(steps):
    注入 SKILL_PATHS 路径（覆盖 LLM 给的）
    data_acquisition 额外注入 settings 里的 cloud_threshold / dem_source
    强制把 region 换成最新上传的研究区 geojson 绝对路径
    _emit "**Step i/N**: skill_name — 描述"
    workflow_callback(skill, "running", i, total)

    若 skill 属于 _MODEL_TRAIN_SKILLS：
        从 settings 注入模型超参
        user_specified = bool(step["params"])      # ← 见 1.5(3)，此处恒为 True，自动调参是死代码
        if data_features and not user_specified:  → 调 LLM 推荐超参（实际不会执行）

    执行：
        data_acquisition 走"搜索 → pause_callback 让用户选配对 → 注入 selected_pair → 再执行下载"
        其余 skill 直接 skill.execute(params, progress_callback, log_callback)

    _record_step / workflow_callback(completed)
    data_pipeline 成功 → _collect_data_features（读 train.csv 统计 NDVI/DEM/LST）
    rf_model / accuracy_eval 成功 → 缓存到 exp_state
    _check_exceptions(...)                        # ← 只 _emit 警告，恒返回 True

收尾：paused → failed → success 判定 → _finalize_experiment → memory.auto_save_experiment
```

暂停协议（现状）：

```
Agent 侧 pause_callback({"type":"select_pair","pairs":[...]})
  ↓ server._runner.pause_callback：q.put(("pause", data))；pause_event.wait(timeout=300)
  ↓ _stream_events 收到 "pause"：
      有 pairs → 存 conv_state["pending_pairs"]，SSE 发 pause，return（生成器退出，不清理状态）
      无 pairs → _pause_responses[cid]=None; pause_event.set()   # ← 通用审批会在此被吞掉
  ↓ 前端 chat.js 收 pause → paused=true, pairs=..., es.close()
  ↓ PairSelectCard 让用户选 → POST /api/chat/resume {conv, pair_index}
  ↓ chat_resume：_pause_responses[cid]=pairs[idx]; pause_event.set()
  ↓ 前端重新 _listen 建立新 SSE 连接（代际号递增，旧连接自退）
超时 300s 未响应 → 静默回退 pairs[0]
```

## 1.3 GeoThermoAgent 现有能力清单

| 能力 | 位置 | 状态 |
|---|---|---|
| 咨询/执行两路分流 | `process_command` L107–L125 | 关键词 + 启发式，无语义判定 |
| LLM 生成 JSON 计划 | L146–L171 | 单次调用，三级解析兜底 |
| 全流程安全网 | L175–L201 | 关键词触发，内置 7 步计划 |
| 路径归一化 | `_normalize_plan_paths` L371–L560 | 大量 glob 搜索兜底 |
| 顺序执行 + 进度回调 | `_execute_plan` | 稳定可用 |
| 影像配对选择暂停 | `_ask_user_to_select_pair` L1042 | 只有这一个暂停点 |
| 异常检测 | `_check_exceptions` L933 | 只提示不拦截 |
| 自动调参 | L776–L801 + `_build_tuning_prompt` L1103 | **死代码，从不触发** |
| 数据特征统计 | `_collect_data_features` L1140 | 可用，是调参上下文来源 |
| 实验记录写入 | `_build_experiment_record` / `_finalize_experiment` | 可用 |
| 记忆注入 | `enrich_prompt` → system prompt | 可用 |
| 区域猜测 | `_guess_region_from_input` L209 | **硬编码武汉/北京/上海/广州，默认武汉** |

## 1.4 记忆系统现状

存储根：`data/users/{uid}/memory/`

```
knowledge_seed.json          领域知识种子落盘（审计用）
chromadb/                    PersistentClient
  ├ global_knowledge         18 条领域知识（K01–K31 子集）
  └ project_{id}             该项目历史实验的自然语言段落
projects/{project_id}/
  ├ experiments.json         结构化实验记录（数组 append，原子写）
  └ preferences.json         项目偏好键值
```

关键接口：

- `ensure_seeded()` 幂等播种；**幂等判据是 `global_knowledge.count() > 0`**，所以新增知识条目在老环境不会被灌入（见 1.5(6)）。
- `auto_save_experiment(project_id, record)` 双写 experiments.json + ChromaDB 段落，metadata 带 `source_exp/source_conv/region/model/r2/date/status`。
- `enrich_prompt(project_id, query, n=6)` 返回三段拼接文本：领域知识参考（n=8）+ 当前项目历史经验（n≤3）+ 历史最佳实验。
- `ExperimentLog.get_best(region, model)` 取测试集 R² 最高的 success 记录。
- `Preferences.get/set/all` 键值读写。
- 级联删除：`delete_conversation(project_id, conv_id)` / `delete_project(project_id)`。
- 嵌入：bge-small-zh-v1.5 ONNX（512 维，查询加 BGE 前缀），缺失时回退 ChromaDB 内置。
- `RAGStore._query_collection` **不支持 metadata `where` 过滤**（见 1.5(6)）。

## 1.5 现状与本次需求的差距（六个必须先知道的坑）

**(1) 多轮上下文在 Agent 路径上是断的。**
`server._runner` 构造了 `prior_messages`，但只传给纯聊天分支的 `ask_stream`；`process_command` 的签名里根本没有 `prior_messages`。用户描述的场景——「你认识九江镇吗？」→「生成啊」→「25 年」——第一句不含关键词走纯聊天，第二句命中关键词「生成」进入 Agent，此时 Agent 看不到任何上文，只能靠 `_guess_region_from_input` 猜，而该函数只认武汉/北京/上海/广州，猜不到就**默认返回武汉的 bbox**。这是「乱生成别的区域产品」的根因。

**(2) 研究区的选择是「取最新上传的那个文件」，与用户说的地名无关。**
`_find_study_area_file` 按 mtime 倒序取第一个 geojson，并且在 `_normalize_plan_paths` L556 和 `_execute_plan` L765 两处**强制覆盖** LLM 给的 region。上传了多个研究区时，用户说哪个都没用。

**(3) 自动调参是死代码。**
`user_specified = bool(step.get("params"))`：执行到这一行时 `params` 已经被 SKILL_PATHS 和 settings 里的模型参数填满，恒为非空，所以 `not user_specified` 恒为 False，`_build_tuning_prompt` 永远不会被调用。**七规则调优要从零实现，不是改现有循环。**

**(4) `_check_exceptions` 不拦截。**
样本不足、R²<0.6、无配对，全都只是 `_emit` 一句警告然后 `return True` 继续往下跑。用户要求的「数据没下好禁止往下跑」目前完全没有。

**(5) 暂停协议被写死在「选配对」上。**
`_stream_events` 只在 `data["pairs"]` 非空时才真正发 SSE `pause`；否则直接 `_pause_responses[cid]=None; pause_event.set()` 把暂停吞掉。`chat_resume` 也只认 `pair_index`。通用审批节点必须先把这条链路泛化。另外 `pause_event.wait(timeout=300)` 超时后**静默选 pairs[0]**，这对「由我批准」模式是不可接受的。

**(6) 记忆系统缺三样东西。**
① 播种幂等判据是 collection 非空，新增 E 系列评估知识在老用户环境不会生效；② `search_for_agent` 不支持 `where` 过滤，无法按角色/类型分流检索；③ 没有对话级的槽位状态（session state），也没有「可复用工作流」这一类记忆。

**另外两处需要在实现时留意的工程细节：**

- `RFModelSkill.execute` 结尾调用 `cleanup_stage(project_root, "rf_model")`，会删掉 train/validate/test.csv。多轮调优第 2 轮开始要靠 `ensure_stage_inputs` 重建，代价很高。
- `tcr_compute` 的 `model_path` 由 `_execute_plan` 用「`results/train` 下最新 mtime 的 `*.pkl`」推断。多轮调优若把各轮模型写在同一目录，最终被下游取走的可能不是被接受的那一轮。

---

# 第 2 章 · 目标架构（先把概念钉死）

## 2.1 一句话架构

> **GeoThermoAgent 仍然是唯一的总调度，Plan-and-Solve 嵌在它内部。Plan 交给规划 Agent，Solve 由 GeoThermoAgent 按 plan 依次调用数据 / 训练 / 评估三个执行 Agent。不新建任何与 GeoThermoAgent 同级的总 Agent。**

对照 Hello-Agents 的词汇，避免套错：

| Hello-Agents 概念 | 本项目对应物 | 不是什么 |
|---|---|---|
| Agent | `GeoThermoAgent`（总调度）+ 四个角色 Agent | 不是四个独立进程/服务 |
| Tool | 8 个 Skill | 角色不等于工具，角色不下载不训练 |
| Plan-and-Execute | 规划 Agent 出 plan，总调度 solve | 不是让规划 Agent 自己跑完 |
| Multi-Agent | 单进程内四个角色对象共用一个 LLM 客户端 | 不引入 LangGraph/AutoGen 等框架 |
| Reflection | 分层反思（见 2.3） | 不是在总调度堆一个大循环 |

## 2.2 角色划分与职责边界

```
                        ┌──────────────────────────────────────┐
用户消息 ──────────────▶│  GeoThermoAgent（总调度 / Orchestrator）│
                        │  · 执行模式（完全执行 / 由我批准）      │
                        │  · 关键节点暂停与恢复                  │
                        │  · 流程状态机 RunState                 │
                        │  · 依据子 Agent 返回决定：继续/暂停/replan │
                        └───┬───────────────┬──────────────┬────┘
                            │ Plan          │ Solve        │ Solve
                     ┌──────▼──────┐  ┌─────▼──────┐  ┌────▼─────┐
                     │ 规划 Agent   │  │ 数据 Agent  │  │ 训练 Agent │
                     │ 意图理解     │  │ 质量评估    │  │ 七规则调优 │
                     │ 多轮补全     │  │ 推荐配对    │  │ 主反思    │
                     │ 出结构化plan │  │ 轻反思      │  │          │
                     │ 轻反思       │  └─────┬──────┘  └────┬─────┘
                     └─────────────┘        │              │
                                       ┌────▼──────────────▼────┐
                                       │      评估 Agent         │
                                       │  TCR/导出/精度 + 解读    │
                                       │  轻反思：表述把关        │
                                       └────────────┬───────────┘
                                                    │
                                        ┌───────────▼──────────┐
                                        │  Skill 层（真干活）    │
                                        └──────────────────────┘
```

各角色的**能做**与**不能做**（写进各自 system prompt 的硬约束）：

| 角色 | 能做 | 不能做 |
|---|---|---|
| 总调度 GeoThermoAgent | 决定执行模式、到点调用子 Agent、依返回决定继续/暂停/replan、维护流程状态、写实验记录 | 不自己猜用户意图、不自己做完整 Reflection 循环、不自己改超参 |
| 规划 Agent | 判定意图、多轮补全槽位、反问、出可解析 plan、轻反思（信息是否齐全 / 是否误判意图） | 不执行任何 Skill、不下载不训练、信息不全禁止放行 |
| 数据 Agent | 评估候选影像质量、排序打分、给推荐标记、执行下载+预处理+TTRI、轻反思（数据是否合格） | 不自己改整条流水线、不自己换地区时间（只报原因） |
| 训练 Agent | 训练、七规则约束下的多轮调参（主反思）、阶段内回退（重选配对请求） | 不发起整单 replan（除非用户明确要换时间/地区，交总调度） |
| 评估 Agent | 生成 TCR/LST/精度、基于记忆先验解读结果、轻反思（表述把关、防幻觉） | 不重算指标、不贬低产品、不编造未计算的数字 |

## 2.3 反思分层（严格按需求）

| 角色 | 反思强度 | 反思对象 | 不通过时的动作 |
|---|---|---|---|
| 规划 Agent | 轻量 | 执行全流程的信息是否齐全；是否误判用户意图（纯聊天 vs 想生产） | 反问用户 / 改 plan / 判为纯聊天不进流程 |
| 数据 Agent | 轻量 | 数据下好没、预处理有没有问题、TTRI 算好没 | **禁止往下跑**，回报总调度 → 规划 Agent replan（换配对/换数据源/换时间/换地区） |
| 训练 Agent | **主反思** | 指标、过拟合、样本量、特征重要性 + 七条规则硬约束 | 改超参再训一轮（阶段内优化，不 replan）；默认上限 5 轮，硬上限 8 轮 |
| 评估 Agent | 轻量 | 表述把关：数字是否有出处、是否用了禁用说法、是否因端点差贬低产品 | 打回重写（≤2 次），仍不过则降级为模板化报告 |

## 2.4 replan 规则（钉死，防止子 Agent 越权）

1. **replan 只能由总调度发起，只能由规划 Agent 产出新 plan。** 子 Agent 只能在返回值里带 `replan_reason` 和 `suggestions`，不能私自改流水线。
2. **改几个训练超参再训一轮 = 阶段内优化（intra-stage），不是 replan。** 训练 Agent 自己在内部循环解决。
3. **触发 replan 的合法场景**（只有这四类）：
   - 数据 Agent 反思不通过（下载失败 / 预处理异常 / TTRI 失败 / 样本严重不足）
   - 搜不到合格影像配对
   - 用户在审批节点主动选择「换时间 / 换地区 / 换数据源」
   - 用户不接受最终精度并明确要求换时间或地区
4. **replan 有次数上限**，默认 3 次（`REPLAN_MAX = 3`）。达到上限后停止自动 replan，转为向用户说明情况并等待指令。
5. **replan 时必须把原因带给规划 Agent**，规划 Agent 在新 plan 中必须体现针对性调整（例如放宽云量阈值、扩大时间窗），不能原样重发。

## 2.5 新增目录与文件清单

全部为新增文件，遵守「多个小文件，单文件 200–400 行、上限 800 行」：

```
core/agent/
├── geo_thermo_agent.py          【改造】总调度，新增角色编排入口，保留全部现有方法
├── executor.py                  【新增】从 geo_thermo_agent 平移出的执行引擎 + 钩子机制
├── presentation.py              【新增】气泡文案渲染层（中文化、去 emoji、去变量名）
├── plan_schema.py               【新增】结构化 plan 的 schema、校验、解析、向后兼容
├── orchestrator/
│   ├── __init__.py
│   ├── exec_mode.py             【新增】ExecMode 常量与归一化（默认 approval）
│   ├── agent_config.py          【新增】settings.agent 特性开关与关键常量单一来源
│   ├── approval.py              【新增】审批节点定义、auto 模式默认策略、暂停载荷构造、恢复结果解析与校验
│   ├── run_state.py             【新增】RunState 流程状态机（阶段/replan 计数/暂停点/断点）
│   ├── hooks.py                 【新增】StepDecision + StageHooks 协议（执行引擎的扩展点，规范定义在此，executor 从这里导入）
│   ├── role_hooks.py            【新增】RoleHooks：StageHooks 的落地实现，把数据/训练/评估 Agent 接到执行引擎上，并按执行模式决定 继续/暂停/重跑/中止/replan
│   └── role_flow.py             【新增】总调度流程：run_with_roles（Plan→Solve 入口）+ solve_with_replan（replan 循环，规则 1 的落点）。`GeoThermoAgent.process_command_with_roles` 只是薄委托。
│                                  `solve_with_replan` 收到 `hooks.replan_request` 后，先检查
│                                  `payload.reselect_pair`：为真则跳过规划 Agent，`current`
│                                  原样不变直接再跑一次（见 3.2.1）；否则才走完整的
│                                  `replan()` 调用规划 Agent
├── roles/
│   ├── __init__.py
│   ├── base_role.py             【新增】RoleAgent 基类：带角色 prompt 的 LLM 调用 + JSON 三级解析（全项目单一来源，`_parse_plan` 委托到此）+ 按角色的记忆注入
│   ├── slots.py                 【新增】确定性槽位解析：研究区文件名匹配 + 中文时间表达解析
│   ├── planner_agent.py         【新增】规划 Agent
│   ├── data_agent.py            【新增】数据下载与预处理 Agent
│   ├── train_agent.py           【新增】训练与调优 Agent
│   ├── eval_agent.py            【新增】结果生成与评估 Agent
│   └── prompts/
│       ├── __init__.py
│       ├── planner.py           【新增】规划角色提示词（可配置）
│       ├── data.py
│       ├── train.py
│       └── evaluation.py
└── reflection/
    ├── __init__.py
    ├── result.py                【新增】ReflectionResult 数据类
    ├── planner_rules.py         【新增】规划轻反思规则
    ├── data_rules.py            【新增】数据轻反思规则
    ├── train_rules.py           【新增】训练主反思：七条规则兜底
    └── eval_rules.py            【新增】评估轻反思：数字核对 + 禁用表述

core/memory/
├── session_state.py             【新增】对话级槽位状态持久化
├── workflow_experience.py       【新增】可复用工作流经验读写
├── knowledge_eval.py            【新增】E 系列评估先验知识种子
├── seed_data.py                 【改造】导入并合并 E 系列
├── rag_store.py                 【改造】检索加 where 过滤；播种改为按 id 增量 upsert
└── memory_manager.py            【改造】新增 enrich_for_role / 工作流读写；enrich_prompt 保持原样

frontend/src/
├── components/
│   ├── ExecModeSelect.vue       【新增】发送键左侧的模式上拉框
│   ├── ApprovalCard.vue         【新增】通用审批选择卡片
│   └── PairSelectCard.vue       【改造】推荐标记 + 默认选中推荐项 + 去 emoji
└── stores/chat.js               【改造】execMode、approval 载荷、resumeApproval

server.py                        【改造】exec_mode 透传、通用暂停/恢复、prior_messages 透传
```

## 2.6 LLM 调用的健壮性（实现期修订 v1.2，四个角色共用）

**问题现象**：实际运行时日志频繁出现

```
[planner] 重试后仍无法解析 JSON，转确定性兜底
[planner] 意图分类退回关键词兜底：intent=task
[planner] LLM 输出无法解析为 JSON
```

即使 LLM API 调用本身成功（`is_api_failure` 判定为否），返回的文本也经常解析不出 JSON，
逼得意图分类退回 4.3 节的关键词兜底、规划退回内置全流程步骤——这正是「感觉不太像智能体」
的直接原因：不是模型不够聪明，是它的输出被系统性截断或没能被正确提取。

**根因一：`max_tokens` 普遍偏小。** 现状（`roles/planner_agent.py` / `data_agent.py` /
`train_agent.py`）：

| 调用 | 修订前 | 说明 |
|---|---|---|
| 意图分类 `classify_intent` | 600 | 4.3 节的设计初衷 |
| 出计划 `build_plan` | 1600 | — |
| 规划轻反思 `_llm_reflect` | 400，`retry_once=False`（只有一次机会） | — |
| 数据轻反思翻译 `_explain` | 400，`retry_once=False` | — |
| 调优决策 `_llm_decision` | 500，`retry_once=False` | — |

而 `core/agent/geo_thermo_agent.py`（改造前的旧路径）生成 JSON 计划时用的是
`max_tokens=4096`，并在代码注释里明确写了原因：「推理 token 也计入输出预算，太小的值会
把 JSON 截断」。角色化的四个新 Agent 没有沿用这条已经验证过的经验值，对于带隐藏推理链的
模型（如截图中的 DeepSeek-V4-Flash），几百 token 的预算大概率不够思考过程用，真正的 JSON
答案还没写完就被截断，`extract_json` 自然解析不出来。

**修订**：统一放宽为参考旧路径量级的预算——意图分类、出计划一类的主调用给到
2048–4096；反思、翻译、决策一类结构更小的调用给到 1024 起；`base_role.call_json` 的
兜底默认值从 800 提到 1200。具体数值见 `core/agent/roles/*.py` 各调用点（技术方案不逐一
列出以免与代码漂移，以代码为准）。

**根因二：`extract_json` 只覆盖了三种最理想的情况。** 直接 `json.loads` → 代码块提取 →
首尾大括号截取，三级都假设模型输出「干净」的 JSON。两类常见的不干净输出没有兜底：

1. 模型在真正的 JSON 前面加了一段说明文字，说明文字里恰好也有花括号（例如举例子、贴一段
   历史 JSON 做参考），导致「首尾大括号」截到了一大段跨越说明文字与真实 JSON 的非法拼接；
2. 模型输出的 JSON 带尾随逗号（`{"a": 1,}`）——不是合法 JSON，但很多模型会这么写。

**修订**：`extract_json` 增加两级兜底：① 从文本**末尾向前**逐个尝试「配对花括号」（正确跳过
字符串内部的花括号，不会被 `"reason": "示例：{a:1}"` 这类字符串值误导），优先命中离结尾最近
的合法对象，模拟「模型通常把最终答案放在推理之后」的常见习惯；② 对每个候选片段都额外尝试
「去掉尾随逗号后再解析」一次。四级兜底顺序：直接解析 → 代码块（遍历全部匹配，不只取第一个）
→ 花括号配对回扫（从后往前） → 首尾大括号截取；每一级都追加一次去尾逗号的重试。

这两处修订只影响 `core/agent/roles/base_role.py`，不改变任何角色的 prompt 内容与业务逻辑，
纯粹是「让模型说的话更容易被程序听懂」。

---

# 第 3 章 · 执行模式与审批协议

## 3.1 两种模式

```python
# core/agent/orchestrator/exec_mode.py
class ExecMode:
    APPROVAL = "approval"   # 由我批准：关键节点弹窗，由用户选择下一步
    AUTO     = "auto"       # 完全执行：按默认策略走完全流程并输出结果

DEFAULT_EXEC_MODE = ExecMode.APPROVAL
```

**默认值必须是 `APPROVAL`。** 理由：现状代码在配对选择处一定会暂停问用户，默认 APPROVAL 才能保证「不改变现有功能」。

模式由总调度统一掌握，子 Agent 只读不写：子 Agent 通过 `ctx.exec_mode` 知道当前模式，但是否真的暂停由总调度的 `_pause_for_approval` 决定。

## 3.2 审批节点清单

| 节点 id | 触发时机 | 由谁提出 | 用户可选项 | AUTO 模式默认策略 |
|---|---|---|---|---|
| `plan_confirm` | 规划完成、信息齐全、即将开跑 | 规划 Agent | 开始执行 / 我要改需求 | 直接开始执行 |
| `pair_selection` | 搜索到 ≥1 组合格配对 | 数据 Agent | 逐组列出，最优组带「（推荐）」 | 自动选质量得分最高的一组 |
| `no_pair` | 搜索不到合格配对 | 数据 Agent | 放宽云量 / 扩大时间 / 换地区 / 换数据源 / 停止 | 带原因交规划 Agent replan（≤3 次），仍不行则停下问用户 |
| `data_quality` | 数据 Agent 反思不通过 | 数据 Agent | 重新选择影像组合（阶段内回退，不 replan） / 更换数据源 / 换时间地区（replan） / **我接受现状，继续执行**（v1.2 新增） / 停止 | 同上：replan（≤3 次）→ 停下问用户 |
| `tuning_decision` | 首次训练完成 | 训练 Agent | AI 调优（推荐）/ 我自己调 / 不调优 / 换时间地点 / 重选影像对 | AI 调优（七规则约束，默认 ≤5 轮，硬上限 8 轮） |
| `tuning_round` | 每一轮**调优**完成（首次训练由 `tuning_decision` 负责，不重复弹） | 训练 Agent | 接受本轮 / 继续下一轮 / 停止调优 | 按七规则自动决定，不暂停 |
| `final_report` | 全流程完成、评估报告生成 | 评估 Agent | 结束 / 做其他分析 | 直接结束并输出报告 |

**用户明确要求：由我批准模式下，无论精度好坏都要在 `tuning_decision` 与 `tuning_round` 询问用户并报告结果。** 不允许因为 R² 已经很高就跳过询问。

### 3.2.1「重新选择影像组合」的语义（实现期修订 v1.2，钉死，避免再被误实现为整单 replan）

`no_pair` / `data_quality` / `tuning_decision` 三个节点都可能选到「重新选择影像组合」。
它是**阶段内回退**，不是重新规划，行为必须是：

```
用户选「重新选择影像组合」
  ↓
数据 Agent / 训练 Agent 只标记 payload = {"reselect_pair": True}，
不改任何 constraints/time_range，把 StepDecision.REPLAN 返回给执行引擎
  ↓
总调度（role_flow.solve_with_replan）识别到 payload.reselect_pair 为真：
  · 不调用规划 Agent 的 replan()（不产生额外 LLM 调用，不会被规划 Agent 改写整条计划）
  · 不计入 replan 次数
  · current（原 plan，region/time_range/constraints 原样不变）直接再跑一次 _execute_plan
  ↓
_execute_plan 天然从第 1 步 data_acquisition 开始：重新搜索影像
（搜索参数没变，通常拿到同一批候选）并再次弹出配对选择卡片
```

**为什么允许「整单重跑 `_execute_plan`」等价于「回到配对选择」**：`data_acquisition` 固定是
`WORKFLOW_STEPS` 的第一步，重新执行整个 plan 时第一件事就是重新搜索并等待用户选配对，效果与
「跳转到 pair_selection 断点」完全一致，不需要额外实现"从计划中间某一步开始跑"的断点续跑机制。
若触发点是 `tuning_decision`（已经训练过一次），换一组影像意味着此前基于旧影像的下载/预处理/
训练产物全部作废，本就应该从头来一遍——这是正确的完整回滚语义，不是偷懒。

`RunState.resume_point` 仍会被设为 `Node.PAIR_SELECTION`（供前端/日志展示「回到了哪一步」），
但**不再被执行引擎用来决定要不要调用规划 Agent**——是否调用规划 Agent 只看
`payload.reselect_pair` 这一个标记。

**实现陷阱（务必保留，不要当作多余代码删掉）**：`current` 是同一个 plan 对象，
`data_acquisition` 步骤的 `params["selected_pair"]` 是执行引擎**就地修改**写入的
（不是不可变拷贝）。原样复用 `current` 重新执行前，`role_flow.clear_selected_pair(current)`
必须先把这个残留字段清掉，否则执行引擎会看到「`selected_pair` 已经有值」而认定「已经选好了」，
直接跳过搜索分支——而 `executor.py` 里这个分支唯一的「else」出口其实并不存在（只有「未选择→
搜索+下载」这一条路径），会在拼装结果消息时抛出 `result` 变量未定义的异常，被外层 `try/except`
吞掉后表现为「data_acquisition 失败」。`clear_selected_pair` 返回新的 plan 对象、新的
`data_acquisition` 步骤与新的 `params` 字典，不修改传入的 `current`（保持项目「不可变」编码
约定），其它步骤原样引用不变。

### 3.2.2「我接受现状，继续执行」（`data_quality` 新增，实现期修订 v1.2）

数据轻反思的 D1–D7 是确定性规则，但规则本身也可能有考虑不周的地方（例如 5.3 节 D7 曾经把
分母/分子的抽样步长搞错，见修订记录 v1.2 第 ⑰ 条）。用户比系统更清楚「这批数据能不能用」，
所以在 `data_quality` 节点必须给一个「我知道有告警，但我要继续」的出口：选择该项后总调度
直接放行进入训练阶段（`StepDecision.CONTINUE`），不触发 replan、不暂停、不询问第二遍。

## 3.3 暂停载荷协议（泛化，向后兼容）

现有的 `select_pair` 载荷保持不变（前端 `PairSelectCard` 继续可用），只在每个 pair 上**增加两个字段**：

```json
{
  "type": "select_pair",
  "pairs": [
    {
      "index": 0,
      "landsat_date": "2025-07-17", "landsat_satellite": "L9",
      "landsat_count": 1, "landsat_scenes": [...],
      "landsat_cloud": 8.3, "landsat_coverage": 96.1,
      "sentinel_date": "2025-07-18", "sentinel_count": 2,
      "sentinel_scenes": [...], "sentinel_cloud": 5.1, "sentinel_coverage": 98.4,

      "recommended": true,
      "recommend_reason": "云量最低、覆盖最完整、成像时间只差 1 天"
    }
  ]
}
```

新增通用审批载荷：

```json
{
  "type": "approval",
  "node": "tuning_decision",
  "title": "模型训练完成，请选择下一步",
  "summary": "测试集决定系数 0.72，均方根误差 1.45 开尔文。植被指数与高程对温度的贡献最大。该区域上一次的结果是 0.87。",
  "options": [
    {"id": "ai_tune",      "label": "让系统继续自动调优", "recommended": true,
     "hint": "在硬性规则约束下最多再训练 8 轮，自动选取误差最小的一轮"},
    {"id": "manual_tune",  "label": "我自己设置参数",
     "fields": [
       {"name": "n_estimators", "label": "决策树数量", "type": "number",
        "default": 200, "min": 50, "max": 1000, "step": 50},
       {"name": "max_depth", "label": "最大深度", "type": "number",
        "default": 25, "min": 5, "max": 50, "step": 1}
     ]},
    {"id": "accept",       "label": "接受当前结果，继续下一步"},
    {"id": "reselect_pair","label": "重新选择影像组合"},
    {"id": "replan",       "label": "换时间或地区，重新规划"}
  ],
  "default_option": "ai_tune"
}
```

`manual_tune` 的 `fields` **必须从 `RFModelSkill.hyperparameters` 动态生成**（`Hyperparameter` 已经带 label/type/default/min/max/step/description），不要在前端硬编码。生成时**排除 `random_state`**——它是可复现性种子，不是调优旋钮，放进「我自己设置参数」表单只会干扰用户。

恢复值必须在**后端**按字段声明做二次校验与范围截断（`approval.sanitize_values`）：只保留声明过的字段名，数值超出 `min/max` 时截断，不可解析时回落 `default`。前端的范围校验只是体验优化，不作为安全边界。

恢复载荷（前端 → 后端）：

```json
POST /api/chat/resume
{"conv": "xxx", "pair_index": 0}                                   // 旧协议，保留
{"conv": "xxx", "option_id": "manual_tune",
 "values": {"n_estimators": 400, "max_depth": 30}}                 // 新协议
```

Agent 侧 `pause_callback` 的返回值统一为：

```python
{"paused": False, "data": {"option_id": "ai_tune", "values": {...}}}   # 用户已选
{"paused": True}                                                       # 超时或对话被删，流程挂起
```

## 3.4 server.py 的三处泛化改动

**(a) `_stream_events` 的 pause 分支**（当前 L1334–L1343）改为：

```python
elif event_type == "pause":
    payload = data if isinstance(data, dict) else {}
    pairs = payload.get("pairs", [])
    if pairs:
        self._get_conv_state(cid)["pending_pairs"] = pairs
        yield from _emit("pause", {"pairs": pairs})
        paused = True
        return
    if payload.get("type") == "approval":
        self._get_conv_state(cid)["pending_approval"] = payload
        yield from _emit("pause", {"approval": payload})
        paused = True
        return
    # 既无 pairs 也非 approval：维持现有行为，直接放行
    self._pause_responses[cid] = None
    pause_event.set()
```

**(b) `chat_resume` 支持两种协议**：

```python
def chat_resume(self, cid: str, payload: dict) -> dict:
    if cid not in self._stream_queues or cid not in self._pause_events:
        return {"ok": False, "message": "没有待恢复的流"}
    conv_state = self._get_conv_state(cid)

    if "option_id" in payload:                       # 新：通用审批
        pending = conv_state.get("pending_approval")
        if not pending:
            return {"ok": False, "message": "没有待处理的选择，请重新发送指令"}
        valid = {o["id"] for o in pending.get("options", [])}
        if payload["option_id"] not in valid:
            return {"ok": False, "message": "无效的选项"}
        self._pause_responses[cid] = {
            "option_id": payload["option_id"],
            "values": payload.get("values") or {},
        }
        conv_state.pop("pending_approval", None)
    else:                                            # 旧：配对选择，逻辑保持不变
        ...原有 pair_index 分支原样保留...

    self._pause_events[cid].set()
    return {"ok": True}
```

**(c) `pause_callback` 的等待策略**（当前 L1188–L1199）：把固定 `wait(timeout=300)` 改为分片轮询，并**移除静默选 pairs[0] 的兜底**。

```python
APPROVAL_WAIT_TIMEOUT = 1800   # 秒，模块级常量，可由环境变量覆盖

def pause_callback(pause_data):
    q.put(("pause", pause_data))
    deadline = time.time() + APPROVAL_WAIT_TIMEOUT
    while time.time() < deadline:
        if pause_event.wait(timeout=5):
            pause_event.clear()
            break
        if cid in self._deleted_convs:
            return {"paused": True}
    selected = self._pause_responses.pop(cid, None)
    if selected is not None:
        return {"paused": False, "data": selected}
    return {"paused": True}          # 超时 → 挂起，不替用户做决定
```

> **行为变更说明（已拍板，见 13.1 结论 1）**：原代码超时后会静默选择第一组配对继续跑。改为挂起后，超时会让流程停在暂停点，用户重新发送指令即可重跑。这是「由我批准」模式的正确语义，实现时直接按此落地，不再保留静默兜底。
>
> 兼容性保护：该分片轮询实现仅在 `roles_enabled=True`（角色路径）时生效；`roles_enabled=False` 的旧路径保留原 `wait(timeout=300)` + 静默选 pairs[0] 行为，保证特性开关关闭时行为逐字节一致。

## 3.5 exec_mode 的传递路径

```
前端 ExecModeSelect（localStorage 记忆 + 每对话覆盖）
  → POST /api/chat/start  {project, conv, message, exec_mode}
  → AppBackend.chat_start：写 _get_conv_state(cid)["exec_mode"]，
                           并 _update_conversation_file(cid, pid, exec_mode=...) 落盘
  → _runner：agent.process_command(..., exec_mode=conv_state["exec_mode"])
  → RunState.exec_mode
```

`chat_start` 未传 `exec_mode` 时取会话文件里的值，再没有则用 `DEFAULT_EXEC_MODE`。

---

# 第 4 章 · 规划 Agent（planner）

## 4.1 职责

1. **判定意图**：纯聊天 / 领域问答 / 要跑流程 / 修改已有需求 / 说不清。
2. **多轮补全**：结合完整对话历史与本对话已确认的槽位，把模糊指令补全到可执行。
3. **反问**：信息不全时只反问，绝不放行执行。
4. **出结构化 plan**：可解析的 JSON，供总调度直接 solve。
5. **轻反思**：信息是否齐全、意图是否误判。
6. **联动记忆**：读领域知识、项目历史、偏好、可复用工作流；写会话槽位与用户偏好。

## 4.2 输入上下文（必须全给）

```python
PlannerContext(
    user_input: str,                  # 本轮消息
    prior_messages: list[dict],       # 完整对话历史（server 已有，需透传）
    session_state: dict,              # 本对话已确认槽位 + 上一次待补问题
    study_areas: list[str],           # 当前用户已上传的研究区文件名
    project_dir: str,
    settings: dict,                   # 云量阈值、DEM 源、模型参数默认值
    memory_block: str,                # memory.enrich_for_role(project_id, "planner", query)
    skill_catalog: str,               # registry.get_tool_descriptions_for_llm()
    replan_reason: str = "",          # 非空表示这是一次 replan
    replan_count: int = 0,
)
```

**这是修复 1.5(1) 的关键：`prior_messages` 必须从 `server._runner` 一路透传到规划 Agent。**

## 4.3 意图分类与路由

规划 Agent 的第一次 LLM 调用是**低成本意图分类 + 槽位抽取**（`temperature=0`，`max_tokens≈600`），返回：

```json
{
  "intent": "task",
  "intent_confidence": 0.92,
  "reason": "用户先问九江镇，随后说'生成啊'并补充'25年'，是要生成该镇 2025 年的地表温度产品",
  "slots": {
    "region_name": "九江镇",
    "time_expression": "25年",
    "product": "lst_10m",
    "model": null
  },
  "missing": [],
  "question": null
}
```

`intent` 取值与总调度的路由：

| intent | 含义 | 总调度动作 |
|---|---|---|
| `chat` | 纯闲聊 | 走现有 `assistant.ask_stream`（带 prior_messages），**不进下载训练** |
| `qa` | 领域问答（原理、参数、数据源） | 走现有 `ask_stream` + `enrich_prompt` 注入，**不进下载训练** |
| `task` | 要跑流程 | 进入槽位补全 → 出 plan |
| `modify` | 修改上一个 plan / 换参数 | 基于 session_state 增量修改 plan |
| `unclear` | 说不清 | 反问，不跑 |

> **关键设计决策：路由不再只靠关键词。**
> `server._is_agent_command` 的关键词表保留，但降级为「LLM 不可用时的兜底」。正常情况下所有消息都先过一次规划 Agent 的意图分类。这是唯一能同时解决「用户只是聊天却被拉去跑流程」和「用户说'生成啊'但上文才有地名」两个问题的做法。
>
> 成本控制：意图分类是一次约 600 token 输出的调用，`intent ∈ {chat, qa}` 时立刻转交流式聊天，不再有额外调用。

## 4.4 槽位定义与补全规则

| 槽位 | 必填 | 解析规则 | 缺失时的反问示例 |
|---|---|---|---|
| `region` | 是 | **必须解析到当前用户已上传研究区文件的绝对路径**。按用户说的地名与文件名做匹配（先精确、再包含、再交给 LLM 做中文近似匹配）。无匹配 → 反问并列出已上传文件；多个候选且无法区分 → 反问让用户确认 | 「你已上传的研究区有：九江镇.geojson、南海区.geojson。这次要处理哪一个？」 |
| `time_range` | 是 | 支持「2025 年 7 月」「25 年」「去年夏天」等表达。**只要落到「年」这一级而没到月，必须反问确认月份**（现有 system prompt 已有这条规则，继承）。**年份还要做合理性校验**（见下方说明），不合理的年份不能直接当正常年份反问月份 | 「2025 年范围比较大，请确认具体月份，例如 2025 年 7 月。」 |
| `product` | 是 | 默认 `lst_10m`（10 米地表温度）。将来扩展城市热岛分析时新增枚举 | — |
| `model` | 否 | 默认读 `preferences.preferred_model`，再默认 `rf` | — |
| `cloud_threshold` | 否 | 默认读 `preferences.cloud_threshold`，再读 settings，再默认 30 | — |
| `dem_source` | 否 | 默认 `copernicus` | — |

**硬规则：`region` 未解析到实际文件路径时，一律不放行。** 这直接根除 1.5(1)(2) 两个坑——不再有「默认武汉」，也不再有「取最新上传文件」。

**兼容性保护**：当用户目录下只有一个研究区文件且用户没有说任何地名时，沿用现状取该文件（保持现有单研究区用户的体验不变）。

### 4.4.1 时间年份的合理性校验（实现期修订 v1.2）

**问题现象**：用户说「要125年的影像」，系统直接反问「125年范围比较大，请确认具体月份」——
原样复述了一个明显不合理的年份，一点都不像在「理解」用户，只是在做字符串填空。

根因在 `roles/slots.py` 的 `_expand_year`：两位数补全为 `20xx`（「25年」→2025），但三位数
及以上（「125」）没有任何合理性判断，直接当作字面年份 125 使用；随后 `_ask_time` 只看
「有没有年、有没有月」就机械拼句子，完全不检查年份本身是否落在系统可能有数据的范围内。

**修订**：`slots.py` 新增

```python
MIN_DATA_YEAR = 2015  # Sentinel-2A 于 2015 年发射，早于该年份系统没有可用影像（K11）

def year_plausible(year, today=None) -> bool:
    """年份是否落在系统可能有数据的合理范围内；空值/超出范围都判为不合理。"""
    if not year:
        return False
    today = today or datetime.date.today()
    return MIN_DATA_YEAR <= int(year) <= today.year
```

`planner_agent._ask_time` 在「只有年没有月」的分支里先判断 `year_plausible`：不合理时**不再
盲目反问月份**，而是引用用户的原话指出年份本身有问题，并给出示例：

> 「125年」看起来不像一个可以下载到影像的年份（系统数据从 2015 年前后才开始覆盖），
> 你是想说哪一年？请确认具体年份和月份，例如 2025 年 7 月。

**同一个漏洞还有一个更隐蔽、危害更大的分支**：如果用户一次性把年和月都说全了（例如手误多打
一位，「125年7月」），`merge_time_parts` 只检查「有年有月」就判定为 `PRECISION_MONTH`
（可执行！），完全不检查数值是否合理，规划 Agent 会直接生成 `start_date="0125-07-01"` 并真的
交给 `data_acquisition` 去下载——这在旧的 P2 规则（`time_range_valid`）里也没被拦住，因为
P2 只检查「起始 ≤ 结束」「不晚于今天」，没有下界检查。修订同时补上这一处：

```python
def time_range_valid(start, end, today=None) -> str:
    ...
    if s.year < MIN_DATA_YEAR:
        return f"起始时间早于 {MIN_DATA_YEAR} 年，系统没有这么早的可用影像"
    if s > e: ...
    if s > today: ...
```

两处修订都只在 `roles/slots.py`，`planner_rules.py` 的 P2 规则直接复用
`time_range_valid`，无需改动即可获得下界保护。

### 4.4.2 中文数字月份与年月独立抽取（实现期修订 v1.2）

**问题现象**：用户输入「我要这区域24年七月」，系统回「2024 年范围比较大，请确认具体月份，
例如 2024 年 7 月」——用户明明已经说了七月，却被当成「只说了年」反复追问。

**两个根因**（都在 `roles/slots.py` 的 `parse_time_expression`）：

1. **正则只认阿拉伯数字**。`(\d{2,4})\s*年\s*(\d{1,2})\s*月` 对「24年七月」中的「七」不匹配，
   于是落到「只有年份」的分支。中文数字月份（七月、十月、十一月、十二月）与中文数字年份
   （二〇二四年、二五年）都识别不到。
2. **年月采用级联匹配**：先试「年+月」，匹配不上就只当年份，并**立即 return**。这导致
   「去年七月」这类「相对年 + 月份」的表达在相对年分支就返回了，月份被整个丢掉——
   即使把中文数字问题修好，这一类仍然会被反复追问。

**修订**：

- 新增 `normalize_cn_numerals(text)`：把**紧跟「年」「月」的中文数字**换成阿拉伯数字，
  其它位置一律不动（「第二年的一些数据」不会被改写）。月份的匹配顺序必须是
  `十一|十二 → 十 → 个位数`，否则「十二月」会被切成「十」。
- `parse_time_expression` 改为**年、月各自独立抽取再组合**（`_extract_year` / `_extract_month`），
  取消级联与提前 return。年份来源包括绝对年（2025年 / 25年 / 2025）与相对年（今年/去年/前年）；
  月份来源为归一化后的 `N月`；`2025-07` 这种既无「年」也无「月」字的写法单独识别。
- 合理性判断的分层不变：解析层只做机械抽取，`year_plausible` 与 `time_range_valid`（P2）
  继续负责拦截不合理年份（见 4.4.1）。

覆盖用例见 `tests/test_planner_reflection.py` 的第 8 组：「我要这区域24年七月」「二〇二四年七月」
「去年七月」「2024年十一月」等均须解析到月、可直接放行；「十月」（缺年份）仍需反问。

## 4.5 结构化 plan（`plan_schema.py`）

```json
{
  "plan_version": 1,
  "plan_id": "plan_7f3a2b",
  "intent": "task",
  "goal": "生成九江镇 2025 年 7 月的 10 米地表温度产品",
  "region": {
    "name": "九江镇",
    "study_area_file": "/app/data/users/u1/study_areas/九江镇.geojson"
  },
  "time_range": {"start": "2025-07-01", "end": "2025-07-31"},
  "constraints": {"cloud_threshold": 30, "dem_source": "copernicus", "model": "rf"},
  "steps": [
    {"id": "s1", "stage": "data",  "skill": "data_acquisition", "params": {...}, "reason": "下载遥感数据"},
    {"id": "s2", "stage": "data",  "skill": "data_pipeline",    "params": {}, "reason": "数据预处理与划分"},
    {"id": "s3", "stage": "data",  "skill": "ttri_compute",     "params": {}, "reason": "拟合地形热响应指数"},
    {"id": "s4", "stage": "train", "skill": "rf_model",         "params": {}, "reason": "训练降尺度模型"},
    {"id": "s5", "stage": "eval",  "skill": "tcr_compute",      "params": {}, "reason": "计算热约束残差"},
    {"id": "s6", "stage": "eval",  "skill": "lst_export",       "params": {}, "reason": "导出最终产品"},
    {"id": "s7", "stage": "eval",  "skill": "accuracy_eval",    "params": {}, "reason": "精度评估"}
  ],
  "approval_nodes": ["pair_selection", "tuning_decision", "final_report"],
  "memory_refs": ["exp_9f2c3a4b_1754...", "K13", "K24"],
  "reflection": {"info_complete": true, "risks": [], "note": ""}
}
```

**向后兼容硬性要求**：`steps[].skill / params / reason` 三个字段的形状与现在完全一致，因此：

- `_normalize_plan_paths(plan)` 不需要改动即可工作；
- `_execute_plan(plan)` 不需要改动即可工作；
- `plan_schema.parse(obj)` 遇到只有 `{"steps": [...]}` 的旧格式时，自动补齐默认值（`stage` 按 `STAGE_OF_SKILL` 映射表推断），保证老路径与测试不受影响。

`STAGE_OF_SKILL` 映射（写在 `plan_schema.py`）：

```python
STAGE_OF_SKILL = {
    "data_acquisition": "data",
    "data_pipeline":    "data",
    "ttri_compute":     "data",
    "rf_model":         "train",
    "tcr_compute":      "eval",
    "lst_export":       "eval",
    "accuracy_eval":    "eval",
    "ai_assistant":     "chat",
}
```

## 4.6 规划轻反思（`reflection/planner_rules.py`）

先跑确定性规则，再跑 LLM 反思，**规则结论覆盖 LLM 结论**。

确定性规则：

| 编号 | 检查 | 不通过动作 |
|---|---|---|
| P1 | `region.study_area_file` 存在且是当前用户 study_areas 目录下的文件 | `need_more_info`，反问选研究区 |
| P2 | `time_range.start <= end`，且已精确到月，且不晚于今天 | `need_more_info`，反问时间 |
| P3 | `intent ∈ {chat, qa}` 时 `steps` 必须为空 | 丢弃 steps，改走聊天路径 |
| P4 | `steps[].skill` 全部在 `SkillRegistry` 中存在 | 剔除非法步骤；若剔完为空则 `need_more_info` |
| P5 | 全流程任务的 steps 必须是 7 步且顺序与 `WORKFLOW_STEPS` 一致 | 用 `_build_full_workflow_plan` 的顺序修正（复用现有安全网思路） |
| P6 | `replan_count <= REPLAN_MAX` | 停止自动 replan，转人工询问 |
| P7 | replan 时新 plan 必须与上一版有实质差异（时间窗/云量/地区/数据源至少一项变化） | 判为无效 replan，转人工询问 |

LLM 轻反思提示词要点（`prompts/planner.py` 内 `PLANNER_REFLECT_PROMPT`）：

```
你在复查自己刚才对用户意图的判断和生成的计划。只回答三件事：
1. 用户这次到底是想聊天、想问原理，还是真的想让系统跑一次生产流程？
2. 如果是生产流程，执行所需的信息是否已经齐全（研究区、时间范围、产品类型）？
3. 有没有把用户的话理解偏（例如用户只是在确认某个地名，被误当成下达任务）？

严格返回 JSON：
{"ok": true|false,
 "action": "proceed" | "ask" | "chat_only",
 "question": "若 action=ask，写一句要问用户的话，中文，不超过40字",
 "note": "一句话说明理由"}
```

反思结果 `action` 的处理：

- `proceed` → 交总调度进入 solve
- `ask` → 输出 `question`，写入 `session_state.pending_question`，本轮结束，不跑任何 Skill
- `chat_only` → 转交 `assistant.ask_stream`，本轮结束

## 4.7 「九江镇」场景的完整推演（验收用例）

| 轮次 | 用户说 | 意图分类 | 槽位状态 | 系统回应 |
|---|---|---|---|---|
| 1 | 你认识九江镇吗？ | `qa`，置信 0.9 | `region_name=九江镇`（暂存，未确认为任务） | 走聊天回答，同时把 `九江镇` 记入 `session_state.slots.region_name`（source=`mentioned`） |
| 2 | 生成啊 | `task`，置信 0.8 | region 命中上文「九江镇」→ 匹配到 `九江镇.geojson`；`time_range` 缺失 | 反问「要生成哪个时间段的产品？例如 2025 年 7 月」，写 `pending_question` |
| 3 | 25 年 | `task`（延续 pending_question） | `time_range` 解析为 2025 年但缺月份 | 反问「2025 年范围较大，请确认具体月份」 |
| 4 | 7 月 | `task` | 槽位齐全 | P1–P7 全过 → 出 plan → `plan_confirm`（APPROVAL 模式）或直接开跑（AUTO 模式） |

**任何一轮都不允许出现「默认武汉」或「取最新上传文件」。**

---

# 第 5 章 · 数据下载与预处理 Agent（data）

## 5.1 覆盖范围

`data_acquisition` + `data_pipeline` + `ttri_compute` 三个 Skill。TTRI 归在这里，是因为用户明确要求本 Agent 的反思要检查「TTRI 算好没」。

## 5.2 影像质量评估与推荐

数据源已有的可用字段（来自 `DataAcquisitionSkill._build_pairs`，`data_acquisition.py` L946–L959）：

```
landsat_date / landsat_satellite / landsat_count / landsat_scenes[{id,date,satellite,cloud_cover}]
landsat_cloud_cover / landsat_coverage
sentinel2_date / sentinel2_count / sentinel2_scenes / sentinel2_cloud_cover / sentinel2_coverage
time_diff_days
```

**评分函数必须是确定性的**（可复现、可解释、可测试），不用 LLM 打分：

```python
# core/agent/roles/data_agent.py
PAIR_WEIGHTS = {"cloud": 0.45, "coverage": 0.30, "time_diff": 0.15, "scene_count": 0.10}

def score_pair(pair: dict) -> tuple[float, list[str]]:
    """返回 (0~1 的质量得分, 人话理由列表)。"""
    cloud = max(_num(pair.get("landsat_cloud_cover"), 100),
                _num(pair.get("sentinel2_cloud_cover"), 100))
    cloud_score = max(0.0, 1.0 - cloud / 100.0)

    coverage = min(_num(pair.get("landsat_coverage"), 0),
                   _num(pair.get("sentinel2_coverage"), 0))
    coverage_score = max(0.0, min(1.0, coverage / 100.0))

    dt = _num(pair.get("time_diff_days"), 2)
    dt_score = max(0.0, 1.0 - dt / 2.0)          # 配对规则上限就是 2 天（知识条目 K13）

    scenes = _num(pair.get("landsat_count"), 1) + _num(pair.get("sentinel2_count"), 1)
    scene_score = 1.0 if scenes <= 2 else max(0.0, 1.0 - (scenes - 2) * 0.15)

    score = (PAIR_WEIGHTS["cloud"] * cloud_score
             + PAIR_WEIGHTS["coverage"] * coverage_score
             + PAIR_WEIGHTS["time_diff"] * dt_score
             + PAIR_WEIGHTS["scene_count"] * scene_score)
    return round(score, 4), _reasons(cloud, coverage, dt, scenes)
```

`_reasons` 生成中文短语，例如 `["云量最低（最高 5%）", "研究区覆盖完整（98%）", "两星成像只差 1 天"]`，最终拼成 `recommend_reason`。

**两种模式的行为差异（严格按需求）：**

| 模式 | 行为 |
|---|---|
| 由我批准 | 按得分降序排列，**只在最优的一组后面加「（推荐）」并附一句理由**，前端默认选中它，但**决定权完全在用户**。系统不代选。 |
| 完全执行 | 直接选得分最高的一组，并在气泡里说明「已自动选择第 N 组：云量最低、覆盖最完整」。 |

**无合格配对时（两种模式都不许硬跑）：**

```
数据 Agent 返回 {ok: False, node: "no_pair", detail: {...}}
  detail 包含：搜索到的 Landsat 景数、Sentinel 景数、被覆盖度<70% 淘汰的组数、
              被时间差>2天淘汰的组数、当前云量阈值
总调度处理：
  APPROVAL → 弹 no_pair 审批节点，说明情况 + 给出可选项
  AUTO     → 带 replan_reason 交规划 Agent，规划 Agent 必须做实质调整
             （放宽云量 / 扩大时间窗），replan 次数达上限后弹窗问用户
```

气泡里必须说清「搜到了什么、为什么都不合格」，不能只说「未找到影像配对」。

## 5.3 数据轻反思（`reflection/data_rules.py`）

在 `data_acquisition` → `data_pipeline` → `ttri_compute` 三步全部执行完之后统一做一次，规则优先：

| 编号 | 检查项 | 数据来源 | 不通过时的建议动作 |
|---|---|---|---|
| D1 | 五个栅格文件均存在、非零字节、可被 GDAL 打开 | `raw/landsat_lst.tif`、`landsat_qa_pixel.tif`、`sentinel2_bands.tif`、`sentinel2_scl.tif`、`dem.tif` | 换配对 / 换数据源 |
| D2 | `run_manifest.json` 中 `data_acquisition`/`data_pipeline`/`ttri_compute` 均为 `completed` | `core.manifest.load_manifest(project_dir)` | 按失败阶段定位 |
| D3 | `train_rows >= MIN_TRAIN_ROWS`（默认 10000，与现有 `_check_exceptions` 阈值一致） | `data_pipeline` 结果 `train_rows` | 换配对（云量更低的）/ 换时间 |
| D4 | `constraint_rows > 0` 且 `predict_valid_pixels > 0` | `data_pipeline` 结果 | 换配对 / 检查研究区范围 |
| D5 | 三个划分集合行数均 > 0，且 train/val/test 比例未严重失衡（任一集合占比 < 5% 判失衡） | `split_stats` | 换配对 / 调整划分比例 |
| D6 | train/validate/test CSV 中存在 `TTRI` 列，且 TTRI 非全 NaN、标准差 > 0 | 读 CSV 表头 + 抽样统计 | TTRI 拟合失败，换配对或检查 DEM |
| D7 | 有效像元占比 ≥ `MIN_VALID_RATIO`（默认 0.15，即云掩膜后仍有 15% 以上有效） | **完整 30 米约束层**的 `valid_ratio`（`30m_constraint_grid_meta.json` 直接给出的字段），拿不到该字段时退回 `constraint_rows / (height×width)` 计算 | 云量太重，换配对 |

任一规则不通过 → **禁止往下跑**。LLM 轻反思只负责把失败原因翻译成人话并给出建议排序，**不参与放行决策**。

### 5.3.1 D7 计算口径修正（实现期修订 v1.2，重要）

**问题现象**：用户反馈换了时间段后仍反复报「云掩膜后有效像元只占 4.4%～6.9%，低于 15%」，
但同一区域早前（24 年 7 月）跑同类数据完全没问题，怀疑云量没那么夸张。

**根因**：初版实现把分子和分母的抽样粒度搞混了。`core/data_preprocessing.py` 的
`process_preprocessing` 会同时产出两条独立的数据流（见 K01 知识条目「A-05 双流设计」）：

- `30m_features_step2.csv`（训练抽样）：`step=2` 等间隔抽样后再套云掩膜，行数
  `train_rows` 大约只有「完整有效像元数」的四分之一（`joint_mask[::2, ::2]` 在均匀分布
  假设下面积缩小到 1/4）；
- `30m_constraint_grid.csv`（完整 30 米约束层）：`step=1`，覆盖 `joint_mask` 的**全部**
  有效像元，行数记为 `constraint_rows`，且其元数据 `30m_constraint_grid_meta.json`
  本身就直接写出了 `valid_ratio = constraint_rows / (height×width)`。

初版 `_check_d7` 却用 `train_rows`（分子已经被抽样缩小到约 1/4）去除以
`30m_features_step2_meta.json` 里的 `height×width`（**未抽样**的全量格网），把真实有效
像元占比系统性低估了约 4 倍——例如真实占比 27.6% 会被算成约 6.9%，真实占比 17.6% 会被算成
约 4.4%（与截图里的两次报错数字吻合），只要真实占比落在 15%～60% 之间，误算后几乎必然跌破
15% 的阈值，与云量高低无关，纯粹是分子分母抽样步长不一致导致的系统性 bug。24 年 7 月鄂州能
跑通，只是因为那次真实占比足够高（很可能 >60%），即使被除以 4 依然侥幸留在阈值之上。

**修订**：D7 改读 `30m_constraint_grid_meta.json`，优先直接使用其中的 `valid_ratio` 字段；
若该字段缺失（旧项目目录 / 合成测试未提供），退回 `constraint_rows / (height×width)` 计算，
两者分子分母的抽样粒度（都是 `step=1`）保持一致，不再有系统性偏差。`meta_probe` 的探测目标
文件从 `30m_features_step2_meta.json` 换成 `30m_constraint_grid_meta.json`；`pipeline_data`
里改读 `constraint_rows` 而不是 `train_rows`（两者都已是 `data_pipeline` 结果里现成的字段，
不需要新增任何上游产出）。

LLM 轻反思提示词要点：

```
你是遥感数据质量检查员。下面是一次数据准备的检查结果，其中已经标明哪几项不合格。
请用中文写：
1. 一句话说明最可能的原因（不要罗列所有可能）
2. 按可行性排序给出 2-3 条建议（换影像组合 / 换数据源 / 换时间 / 换地区）
禁止：不要编造检查结果里没有的数字；不要说"可能是网络问题"这类无法验证的猜测。
严格返回 JSON：{"cause": "...", "suggestions": ["...", "..."]}
```

## 5.4 数据 Agent 的记忆联动

- **读**：领域知识 K05（数据源）/K10–K13（Landsat、Sentinel、DEM 参数、配对规则）；项目历史中同区域用过哪组影像、失败过什么。
- **写**：候选配对及其得分、最终选中的配对与选择依据（用户选的还是自动选的）、失败原因，全部并入本次实验记录草稿（最终由总调度在收尾时通过 `auto_save_experiment` 一次性落库，**不新增单独的写入时机**，避免与现有实验记录逻辑冲突）。

---

# 第 6 章 · 训练与调优 Agent（train）· 主反思

## 6.1 两种模式的行为

**由我批准模式**：首次训练完成后，**无论精度如何**都弹 `tuning_decision`：

```
选项：
  ① 让系统自动调优（推荐）    → 进入七规则调优循环
  ② 我自己设置参数            → 弹参数表单（字段来自 RFModelSkill.hyperparameters），用用户值再训一轮
  ③ 不调优，接受当前结果       → 直接进入评估阶段
  ④ 重新选择影像组合           → 阶段内回退到 pair_selection（不整单 replan）
  ⑤ 换时间或地区，重新规划     → 报告总调度 → 规划 Agent replan
```

每轮调优结束后弹 `tuning_round`，报告本轮指标与七规则的判定结果，让用户选「接受本轮 / 继续下一轮 / 停止调优」。

**完全执行模式**：直接进入七规则调优循环，最多 `settings.agent.tuning_max_rounds` 轮（默认 5，硬上限 8），自动选取均方根误差最低的一轮，最后在报告里说明调优轨迹。

## 6.2 七条规则（与 v2 升级规划 3.6 完全一致，逐条落到 `reflection/train_rules.py`）

```python
TUNING_RULES = {
    "R1": "测试集 R² < 0.60 → 强制调优（覆盖 LLM 给出的 accept）",
    "R2": "测试集 R² ≥ 0.88 → 禁止继续调优（覆盖 LLM 给出的 adjust）",
    "R3": "训练 R² − 测试 R² > 0.20 → 判定过拟合，强制干预调优方向："
          "max_depth 下调 10（下限 5），min_samples_leaf 上调至少 2",
    "R4": "参数越界 → 截断到安全区间："
          "n_estimators∈[10,2000]，max_depth∈[1,100]，"
          "min_samples_split∈[2,100]，min_samples_leaf∈[1,50]，max_features∈[0.1,1.0]",
    "R5": "最近两轮 R² 提升均 < 0.01 → 强制停止（已收敛）",
    "R6": "最近两轮 R² 连续下降 → 强制停止（走势恶化）",
    "R7": "调优轮数达到生效上限 → 强制停止，取均方根误差最低的一轮作为最终结果",
}
MAX_TUNING_ROUNDS = 8        # 硬上限，任何配置都不得突破（拍板结论 4）
DEFAULT_TUNING_ROUNDS = 5    # 默认值，settings.agent.tuning_max_rounds 未配置时生效

def resolve_max_rounds(configured=None) -> int:
    """生效上限 = clamp(配置值 或 默认 5, 1, MAX_TUNING_ROUNDS)。"""
```

**R1 与 R3 的兜底调优方向（实现期补充，规则层保证「强制调优」不空转）**：

| 规则 | LLM 给了 new_params | LLM 没给 new_params |
|---|---|---|
| R1（精度过低，通常是欠拟合） | 用 LLM 的参数，经 R4 截断 | 用确定性方向：`n_estimators × 1.5`、`max_depth + 5`（再经 R4 截断）。**无论 LLM 原本给的是 accept 还是 adjust 都补这个方向**，否则「强制调优」会因为没有参数而退化成 accept |
| R3（过拟合） | 在 LLM 参数上**叠加**强制方向 | 直接用强制方向：`max_depth − 10`（下限 5）、`min_samples_leaf + 2` |

若最终 `action=adjust` 但 `new_params` 仍为空（例如 LLM 不可用且未命中 R1/R3），
判为「无从调整」，按 `accept` 处理并在报告里说明，避免用同一组参数无限空转。

**R5 与 R6 的重叠说明**：R² 连续下降同时满足「提升 < 0.01」，因此两条规则可能同时命中。
实现上两者一起判定、一起记录（`rule_hits` 里同时出现 R5 与 R6），动作都是停止，
这样报告里能说清「既已收敛又在恶化」，不会因为先命中 R5 就漏掉 R6 的证据。

**硬停止规则与用户意愿的边界**：`tuning_round` 节点上用户明确选「继续下一轮」时，
只有 **R2 / R5 / R6 / R7** 这四条**硬停止规则**能覆盖用户（此时气泡说明「按硬性规则已不宜
继续调优」）。R1 / R3 是「强制继续调优」，R4 只截断参数，都不构成停止理由。
若此时没有硬停止规则命中、但大模型没给出调优方向（例如 LLM 不可用），
用与 R1 相同的确定性兜底方向真的再训一轮——**不允许把用户的「继续」悄悄变成「停止」**。

决策顺序（**规则永远覆盖 LLM**）：

```
本轮训练结果
   ↓
LLM 决策：{"action": "accept|adjust|stop", "reason": "...", "new_params": {...}}
   ↓
_rule_safeguard(llm_decision, context)   # 按 R1→R7 顺序逐条修正
   ↓
最终 action
   accept / stop → 结束循环，取 RMSE 最低轮次
   adjust        → 应用修正后的 new_params，进入下一轮
```

LLM 决策上下文（`_build_tuning_context`）应包含：当前模型名、地形复杂度（DEM 标准差分档）、植被覆盖（NDVI 均值分档）、温度变异（LST 标准差分档）、当前参数、训练/测试 R²、RMSE、特征重要性前 5、历史调优轨迹。这些数据现有代码已能拿到：`_collect_data_features`（`geo_thermo_agent.py` L1140）和 `RFModelSkill` 返回的 `train_metrics/test_metrics/feature_importance/params`。

## 6.3 主反思的额外检查项（用户要求的「指标、过拟合、样本」）

在七规则之外，主反思还要产出这些结论并写入调优轨迹（**只影响给 LLM 的上下文与最终报告，不改变七规则的硬判定**）：

| 检查 | 判据 | 用途 |
|---|---|---|
| 样本量与模型容量匹配 | 参照记忆条目 K20（样本 >5 万可增大 n_estimators；<1 万应减小到 100–150） | 给 LLM 的调优方向提示 |
| 地形复杂度与深度匹配 | K21（DEM 标准差 >100m → max_depth 30–40；<30m → 15–20） | 同上 |
| 温度变异与叶节点匹配 | K22（LST 标准差 >5K → min_samples_leaf 减到 5） | 同上 |
| 植被覆盖与特征比例匹配 | K23（NDVI 均值 >0.5 → max_features 可增到 0.7） | 同上 |
| 特征重要性异常 | TTRI 贡献度 < 0.01 → 提示地形信号极弱，可能 DEM 有问题 | 报告与建议 |
| 独立预测一致性 | `independent_prediction.json` 的 R² 与测试集 R² 差 > 0.10 → 提示空间泛化不稳 | 报告与建议 |
| 指标解读分档 | K24（≥0.85 优秀 / 0.80–0.85 良好 / 0.75–0.80 合格 / <0.75 偏低） | 报告用语 |

## 6.4 多轮训练的两个工程问题（必须解决，否则调优跑不动）

**问题 A：中间产物被提前清理。**
`RFModelSkill.execute` 结尾调用 `cleanup_stage(project_root, "rf_model")`，删掉 train/validate/test.csv。第 2 轮开始会触发 `ensure_stage_inputs` 重建，每轮都重建代价极高。

**解决方案（对现有 Skill 的最小改动）**：给 `RFModelSkill` 增加一个可选参数 `defer_cleanup`：

```python
# core/skills/builtin/rf_model.py，现有 L229–L230 附近
if project_root:
    run_manifest.record_stage(...)
    if not params.get("defer_cleanup"):          # ← 新增这一行判断
        cleanup_stage(project_root, "rf_model")
```

训练 Agent 在第 0..n-1 轮传 `defer_cleanup=True`，接受最终结果后再显式调用一次 `cleanup_stage(project_root, "rf_model")`。
未传该参数时行为与现在**完全一致**。

**问题 B：多轮模型文件混淆，下游可能取错。**
`_execute_plan` 的 SKILL_PATHS 用「`results/train` 下最新 mtime 的 `*.pkl`」推断 `tcr_compute` 的 `model_path`。

**解决方案**：每轮训练输出到 `results/tuning/round_{i}/`，选定最佳轮次后，把该轮的模型与指标**复制**到规范位置 `results/train/`（复制而非移动，保留调优轨迹供审计），并确保复制后的文件是该目录下 mtime 最新的。这样 `tcr_compute`/`accuracy_eval` 的现有推断逻辑无需改动即可取到正确模型。

## 6.5 训练 Agent 的边界

- **只做阶段内优化**。改超参再训一轮属于本 Agent 内部循环，不发起 replan、不通知规划 Agent。
- **只有一种情况上报总调度请求 replan**：用户在审批节点明确选择「换时间或地区」。此时返回 `{ok: False, replan_reason: "用户不接受当前精度，要求更换时间/地区", user_hint: {...}}`。
- 「重新选择影像组合」属于**阶段内回退**，具体机制见 3.2.1：训练 Agent 只把
  `payload={"reselect_pair": True}` 交给总调度，**不亲自判断要不要走规划**——是否调用规划
  Agent 完全由总调度（`role_flow.solve_with_replan`）按这个标记决定，训练 Agent 自己永远
  走 `StepDecision.REPLAN` 这一条统一出口，不需要区分「阶段内回退」和「真正 replan」两种
  返回形状（这样 `role_hooks.py` 与 `train_agent.py` 两处触发点可以共用同一套判定逻辑，
  不用各自维护一份）。

---

# 第 7 章 · 结果生成与评估 Agent（eval）

## 7.1 覆盖范围

`tcr_compute` + `lst_export` + `accuracy_eval` 三个 Skill，加上**基于记忆先验的结果解读**。

**评估方法完全复用现有实现**（`core/evaluation.py` 的两个协议函数），本 Agent 不重算任何指标，只读取：

- `results/independent_prediction.json`（独立预测协议：R²/RMSE_K/MAE_K/MB_K/n_samples/split_method/guard_buffer_m）
  > **实际形状注意**：`core/evaluation.evaluate_independent_prediction` 把 R²/RMSE_K/MAE_K/MB_K/r2_null_reason 放在 **`metrics` 子字典**里，只有 `n_samples`、`protocol` 等在顶层。读取时必须先摊平成一层（`eval_agent._flatten_metrics`），否则报告会出现「样本数有值、决定系数却显示未计算」的自相矛盾输出，且 E-R1 的允许值表会漏掉这些指标、把写对的数值误判成编造。
- `lst_export` 结果的 **`total_valid` 在顶层**，不在 `stats` 里（`stats` 只有 min/max/valid_percent 等）；漏读会让报告里的「有效像元数」显示为「未知」。
- `results/coarse_constraint_closure.json`（粗尺度闭合协议：closure.metrics 的 MB_K/MAE_K/RMSE_K/R²、n_matched_cells、coverage_ratio、value_range 的低/高端有符号温差）
- `rf_model` 的 train_metrics / test_metrics / feature_importance / params
- `tcr_compute` 的 tcr_statistics / mode / validity
- `lst_export` 的 stats / image_size / total_valid

## 7.2 评估先验知识（新增 E 系列，写入记忆系统）

新建 `core/memory/knowledge_eval.py`，条目结构与 `seed_data.SEED_ITEMS` 完全一致（`id / topic / tags / content`），由 `seed_data.SEED_ITEMS` 合并导出，随播种一起灌入 `global_knowledge`。

| ID | 主题 | 内容要点 |
|---|---|---|
| E01 | 两套评估协议的口径 | independent_prediction 是「未参与训练/调参/TCR 的 30 米测试集上的泛化精度」；coarse_constraint_closure 是「10 米结果回聚合到 30 米产品格网的算术均值闭合度」。**闭合不是精度，不是独立 10 米精度，不代表能量或辐射守恒。**两者不得混为一谈，不得互相替代。 |
| E02 | 精度分档基准 | 地表温度降尺度测试集 R² 常见区间 0.75–0.85：≥0.85 优秀，0.80–0.85 良好，0.75–0.80 合格，<0.75 偏低需检查数据质量或调参。（与 K24 一致） |
| **E03** | **10 米与 30 米极值差异属正常现象** | 降尺度的目的就是恢复亚像元尺度的空间细节。10 米像元能分辨出更热的裸露屋顶、沥青路面和更冷的水体、树荫，而 30 米像元把它们混合平均了。因此 **10 米产品的最小值更低、最大值更高是分辨率提升的预期表现（混合像元分解与尺度效应），不是产品质量问题**。判断产品好坏必须看闭合指标（MB/MAE/RMSE）、匹配格数与覆盖率，以及独立预测精度，**不得仅凭 10 米与 30 米的极值差就下负面结论**。 |
| E04 | 闭合指标的合理判读 | 默认 block_constant 模式下，每个 30 米格内所有有效 10 米像元加同一残差常数，理论上应精确满足算术均值闭合，因此 MB_K 应接近 0、MAE_K 很小。若 MB_K 明显偏离 0，指向格网映射或聚合环节的问题，而不是模型精度问题。 |
| E05 | 禁用表述清单 | 禁止把闭合称为「10 米精度」；禁止使用「能量守恒」「辐射守恒」；禁止因 10 米与 30 米端点差而断言产品不可用；禁止输出未在结果文件中出现的指标数值；R² 为 null（存在 r2_null_reason）时禁止编造 R²。 |
| E06 | MB 符号约定 | MB = 预测 − 参考，单位开尔文，正值表示整体偏暖。闭合协议中 MB = 10 米回聚合值 − 30 米参考值。（与 K04 一致） |
| E07 | 覆盖率与子像元计数的解读 | coverage_ratio 是有闭合结果的 30 米格占参考有效格的比例；subpixel_count_per_matched_cell 的中位数应接近 9（30/10 的平方），显著偏离说明格网对齐或有效掩膜存在问题。 |
| E08 | 特征重要性的物理解读框架 | NDVI 高贡献 → 植被蒸腾降温对温度分布控制强；NDBI 高贡献 → 不透水面主导城市增温；NDWI 高贡献 → 水体热惯量效应；TTRI 高贡献 → 地形（高程/坡度/坡向）控制强，多见于山地丘陵；短波红外与近红外高贡献 → 地表干湿状况主导。解读时必须结合研究区实际下垫面，不得套模板。 |
| E09 | 结果报告的必备结构 | 一份合格的结果说明必须包含：产品概况（区域、时间、分辨率、有效像元数）、模型精度（独立预测协议）、闭合情况（粗尺度闭合协议，并注明其口径）、关键特征解读、明确的局限性说明。 |

播种问题的修复见 8.4。

## 7.3 评估轻反思（`reflection/eval_rules.py`）· 表述把关

**这是本 Agent 的核心价值：防止 AI 乱说。** 确定性规则优先，命中即打回重写。

| 编号 | 检查 | 实现方式 |
|---|---|---|
| E-R1 | **数字有出处**：解读文本中出现的每一个指标数值，必须能在 `independent_prediction.json` / `coarse_constraint_closure.json` / rf_model 结果中找到对应值（允许四舍五入到文本给出的位数） | 用正则抽出文本中所有数值 + 其上下文关键词（R²/RMSE/MAE/MB/覆盖率/像元数），逐个到一张「允许值表」里比对 |
| E-R2 | **禁用表述**：命中禁用词表即打回，**显式否定语境除外** | 词表：`能量守恒`、`辐射守恒`、`10m精度`、`10 米精度`、`十米精度`、`独立10m精度`、`独立十米精度`、`完全准确`、`零误差`、`产品不可用`、`产品质量差`（后两者在闭合指标正常时禁止出现）。<br>**否定豁免**：7.4 要求正文必须写「不是 10 米精度，也不代表能量守恒」，因此禁用词前 12 字内出现 `不是/不代表/并非/而非/不等于/不能说/无法保证/不宣称/不意味` 时视为合法口径说明；只有「非否定语境」的出现才判违规（例如「闭合精度达到能量守恒」）。E-R6 用同一套否定豁免规则 |
| E-R3 | **null 指标不得编造** | 若 `metrics.R2 is None` 且存在 `r2_null_reason`，文本中不得出现 R² 的具体数值，必须复述 `r2_null_reason` |
| E-R4 | **极值差不得作为负面结论依据** | 若文本中同时出现「最大值/最小值/值域/极值」与负面判断词（「差」「不好」「不理想」「有问题」「不可靠」），且闭合 MB/MAE 在正常区间内，则打回，并在重写提示中注入知识条目 E03 |
| E-R5 | **结论极性一致** | 依据 E02 分档由代码先算出「应有的评级」（优秀/良好/合格/偏低），文本中的评级词必须与之一致 |
| E-R6 | **口径不得混用** | 文本中把闭合指标描述为「精度」的，打回（正则匹配「闭合」附近 20 字内出现「精度」）。<br>两条豁免：① 与 E-R2 相同的否定豁免（「不是十米独立精度」合法）；② 邻近判定**只在同一句内**进行，不跨句号、分号、换行——否则「闭合平均偏差 0.05 开尔文；模型精度见独立预测协议」这种正常的分句陈述会被误杀 |
| **E-R7**（实现期新增） | **报告必须结构完整、不得停在半句话** | 大模型的生成预算（`max_tokens`）耗尽时会在任意位置停下且**不带任何标记**，E-R1–E-R6 只查数字与用词、查不出「没写完」，因此单列这条：① 正文长度不低于 `MIN_REPORT_CHARS`（80 字）；② 必须含 E09 要求的四个小节关键词（`产品概况` / `模型精度` / `闭合` / `局限`）；③ 末尾必须是句子收尾符号（`。！？…」）`）。任一不满足即判为未完成，走与其它规则相同的重写→降级流程。<br>**开关**：`eval_rules.check(..., require_structure=True)`。正式出报告的路径必须传 `True`；针对单条规则的单元测试传默认 `False`，避免短句片段被结构检查干扰 |

流程：

```
生成解读文本（LLM，注入 E 系列 + 实际指标）
   ↓
跑 E-R1 ~ E-R7
   ├─ 全过 → 输出，并把这次工作流写入记忆
   └─ 不过 → 把具体违规项作为「修改要求」回灌，重写（最多 2 次）
            两次仍不过 → 降级为模板化报告（纯指标表 + 固定口径说明），
                        绝不输出未通过检查的文案
```

模板化报告（降级兜底）的固定结构由 `eval_agent.template_report` 生成，只填数字，不含任何 LLM 生成的评价性语句。

**降级必须自解释**：报告的「局限性」小节要写一行「降级原因」，说明是「大模型没有返回解读内容」还是「连续两次未通过表述检查，未过项为 XX」，并指引用户去日志面板看逐条违规项。否则用户只看到「未包含自动生成的解读文字」，无法判断是接口问题、模型问题还是规则误杀。
降级原因里**只能放规则的中文短名**（`eval_rules.RULE_LABELS`），不能放 `E-Rx` 编号，也不能引用违规项原文：① 原文可能含被编造的数字，写进报告等于把违规内容又输出一次；② `E-R2` 含子串 `R2`，会被 E-R1 当成「决定系数」关键词，把编号里的数字判成编造的指标（真实踩过的坑，`_last_keyword_pos` 已对此加了守卫）。

**两条实现约束**：

1. **允许值表必须覆盖事实清单里给出的每一个数**。事实清单给了什么，E-R1 的允许值表就要收录什么——包括独立预测指标（摊平后）与热约束残差统计（`tcr_statistics` 的 mean/std/n_valid_blocks）。漏收会导致 LLM 照着事实清单写对了数，反而被判成「编造」，进而无谓地触发两次重写并降级为模板报告。
2. **气泡里任何截断都必须落在句子边界上**。模板报告末尾引用 E01 口径原文时用 `_cut_at_sentence(text, limit)` 按句号/分号切，超长且找不到边界时以省略号收尾；**禁止**用 `text[:N]` 这种硬切，否则用户看到的是半截话（会被误认为「回复被 token 限制截断」）。

## 7.4 结果解读提示词要点（`prompts/evaluation.py`）

```
你是地表温度降尺度领域的研究人员，正在为一次已完成的生产任务撰写结果说明。

【必须遵守的领域约定】
（此处注入 E01–E09 全文，由 memory.enrich_for_role(project_id, "eval", query) 提供）

【本次实际结果】
（此处注入从两个 JSON 与 rf_model 结果中读出的真实数值，逐项列出）

【写作要求】
- 用中文，面向懂遥感但不看代码的读者
- 只能使用上面给出的数值，一个字都不许编
- 分四段：产品概况 / 模型精度 / 闭合情况 / 关键特征与局限
- 讲闭合时必须注明它是算术均值闭合，不是 10 米精度，也不代表能量守恒
- 若 10 米产品的极值范围比 30 米宽，按领域约定 E03 说明这是分辨率提升的正常表现，
  不得据此贬低产品
- 不使用任何表情符号，不出现文件路径、变量名、英文技能名
```

## 7.5 工作流写回记忆

评估反思全部通过、且本次运行状态为 success 时，写入一条**可复用工作流经验**（详见 8.3）。这是用户要求的「把靠谱工作流写进记忆」。

---

# 第 8 章 · 记忆系统联动

## 8.1 各角色的读写矩阵

| 角色 | 读 | 写 |
|---|---|---|
| 规划 Agent | 领域知识（K 系列全部）；项目历史经验（同区域、同时间段）；`ExperimentLog.get_best(region)`；`Preferences`（云量阈值、偏好模型、常用研究区）；`SessionState`（本对话已确认槽位）；`WorkflowExperience`（可复用工作流） | `SessionState`（槽位、待补问题、plan_id、replan 计数）；`Preferences`（用户明确表达的偏好，如「以后云量都按 40 算」） |
| 数据 Agent | K05、K10–K13；同区域历史用过的配对与失败原因 | 候选配对得分与选中结果 → 并入实验记录草稿（不单独落库） |
| 训练 Agent | K20–K24；`get_best(region, model)` 的历史最佳参数；同区域历史调优轨迹 | 调优轨迹与最终生效参数 → 并入实验记录草稿 |
| 评估 Agent | E01–E09；K02、K04、K07、K24 | 完整实验记录（复用现有 `auto_save_experiment`）；**可复用工作流经验（新增）**；评估结论段落 |

## 8.2 新增：对话级会话状态 `SessionState`

```
data/users/{uid}/memory/sessions/{conv_id}.json
```

```json
{
  "schema_version": 1,
  "conv_id": "ab12cd34",
  "project_id": "9f2c3a4b0001",
  "updated_at": "2026-08-07 14:20:11",
  "intent": "task",
  "slots": {
    "region_name":  {"value": "九江镇", "source": "user", "turn": 1,
                     "resolved_file": "/app/data/users/u1/study_areas/九江镇.geojson"},
    "time_range":   {"value": ["2025-07-01", "2025-07-31"], "raw": "25年7月", "turn": 4},
    "product":      {"value": "lst_10m", "source": "default"},
    "model":        {"value": "rf", "source": "preference"}
  },
  "missing": [],
  "pending_question": "",
  "plan_id": "plan_7f3a2b",
  "replan_count": 0,
  "last_approval_node": "tuning_decision"
}
```

设计约定：

- 每轮对话结束后写一次，写失败仅告警（与记忆系统现有约定一致）。
- 删除对话时级联删除该文件（挂到现有 `MemoryManager.delete_conversation` 里）。
- 删除项目时级联删除该项目下所有对话的 session 文件（挂到 `delete_project`）。
- `slots[*].source ∈ {user, mentioned, default, preference, memory, inferred}`，规划 Agent 反问时要区分「用户明确说的」和「系统推断的」，只有 `user` 来源的槽位在多轮中优先级最高。
  其中 `mentioned` 表示「用户在非任务轮里提到过」（4.7 第 1 轮的「九江镇」就是这种），后续轮次可据此延续上下文，但不视为已确认的任务参数；`user` 来源的槽位不会被非 `user` 来源覆盖。

## 8.3 新增：可复用工作流经验 `WorkflowExperience`

```
data/users/{uid}/memory/projects/{project_id}/workflows.json
```

```json
[{
  "schema_version": 1,
  "workflow_id": "wf_9f2c3a4b_1754...",
  "experiment_id": "exp_9f2c3a4b_1754...",
  "conv_id": "ab12cd34",
  "region": "九江镇.geojson",
  "date_range": ["2025-07-01", "2025-07-31"],
  "exec_mode": "approval",
  "pair": {"landsat_date": "2025-07-17", "sentinel2_date": "2025-07-18",
           "time_diff_days": 1, "score": 0.91, "selected_by": "user"},
  "final_params": {"n_estimators": 400, "max_depth": 30, "min_samples_leaf": 6},
  "tuning_rounds": 3,
  "tuning_trace": [{"round": 0, "test_r2": 0.79, "rmse": 1.51},
                   {"round": 1, "test_r2": 0.84, "rmse": 1.33},
                   {"round": 2, "test_r2": 0.87, "rmse": 1.23}],
  "metrics": {"test_r2": 0.87, "rmse": 1.23,
              "closure_mb": 0.05, "closure_mae": 0.40},
  "approval_choices": {"pair_selection": "0", "tuning_decision": "ai_tune",
                       "final_report": "done"},
  "verdict": "good",
  "timestamp": "2026-08-07 15:02:33"
}]
```

同时写一条 ChromaDB 段落到 `project_{id}`，metadata 增加 `kind: "workflow"`（现有实验段落补写 `kind: "experiment"`），供规划 Agent 按 `kind` 过滤检索。

写入条件（三个都满足才写，保证「靠谱」）：

1. 整体状态为 `success`；
2. 评估轻反思全部通过（无降级）；
3. 测试集 R² ≥ 0.75（K24 的「合格」下限）。

规划 Agent 在处理新任务时，若检索到同区域的可复用工作流，应在 plan 的 `constraints` 中沿用其 `final_params` 和云量阈值，并在气泡中说明「参考了上一次在该区域的成功流程」。

## 8.4 对现有记忆模块的三处扩展（全部向后兼容）

**(a) 播种改为按 id 增量 upsert**（修复 1.5(6)①）

`rag_store.RAGStore.save_knowledge` 当前逻辑是 `if col.count() > 0: return`。改为：

```python
def save_knowledge(self, items):
    col = self.global_collection()
    try:
        existing = set(col.get(include=[]).get("ids", []))
    except Exception:
        existing = set()
    ids, docs, metas = [], [], []
    for item in items:
        if item["id"] in existing:
            continue                      # 已有的不动，避免重复写与内容漂移
        ids.append(item["id"]); docs.append(item.get("content", ""))
        metas.append(_clean_metadata({...}))
    if ids:
        col.add(ids=ids, documents=docs, metadatas=metas)
```

同时 `MemoryManager.ensure_seeded` 中 `knowledge_seed.json` 的落盘条件从「文件不存在」改为「文件不存在 或 文件内 schema_version 与当前不一致」，并把 `SEED_SCHEMA_VERSION` 从 1 提升到 2。

**(b) 检索支持 metadata 过滤**（修复 1.5(6)②）

```python
def _query_collection(self, col, query, n, where=None):
    ...
    kwargs = {"n_results": min(n, col.count())}
    if where:
        kwargs["where"] = where
    res = col.query(query_embeddings=emb, **kwargs)   # 或 query_texts=...
```

`search_for_agent(project_id, query, n=3, where=None)`、`search_knowledge(query, n=3, where=None)` 均增加可选 `where` 参数，**默认 None 时行为与现在完全一致**。

**(c) 新增按角色定制的注入接口**（`enrich_prompt` 原样保留，不动）

```python
ROLE_RETRIEVAL = {
    "planner": {"knowledge_n": 6, "experience_n": 3, "include_best": True,
                "experience_where": None,           # 实验与工作流都要
                "include_workflows": True, "include_preferences": True},
    "data":    {"knowledge_n": 5, "experience_n": 2, "include_best": False,
                "knowledge_where": {"tags": {"$in": ["数据源","配对","云量","landsat","sentinel2","dem"]}}},
    "train":   {"knowledge_n": 5, "experience_n": 3, "include_best": True,
                "knowledge_where": {"tags": {"$in": ["调参","指标","解读"]}}},
    "eval":    {"knowledge_n": 9, "experience_n": 2, "include_best": False,
                "knowledge_where": {"kid": {"$in": ["E01","E02","E03","E04","E05",
                                                    "E06","E07","E08","E09"]}}},
}

def enrich_for_role(self, project_id: str, role: str, query: str) -> str:
    """按角色定制的记忆注入；role 未知时退化为 enrich_prompt。"""
```

> 注意：`_clean_metadata` 当前把 tags 存成逗号拼接的字符串，`$in` 对字符串字段做不了包含匹配。实现时二选一：① 把 `kid` 作为主过滤键（E 系列用 `kid $in` 完全可行）；② 给每条知识增加一个 `domain` 标量字段（取值 `data/model/eval/theory`）用于过滤。**推荐方案 ②**，改动小且语义清晰，`seed_data` 各条目补一个 `domain` 字段即可。

---

# 第 9 章 · 前端改造

## 9.1 模式上拉框（发送键左边）

新建 `frontend/src/components/ExecModeSelect.vue`：

- 外观：一个紧凑的按钮，显示当前模式的短标签（`完全执行` / `由我批准`）+ 向上的箭头图标；点击向上弹出选项面板（`position: absolute; bottom: 100%;`）。
- 选项：两项，每项一行标题 + 一行小字说明。
  - `由我批准` —— 关键节点会停下来问你
  - `完全执行` —— 一次跑完，不打断
- 位置：插入 `ChatInput.vue` 的 `.chat-input-box` 内，**在 `<textarea>` 之后、`<button class="chat-send">` 之前**，与发送键同在一行右侧。
- 状态：写入 `chat` store 的 `execMode`，同时 `localStorage.setItem('gtai_exec_mode', ...)` 持久化；每次 `chat.send()` 带上 `exec_mode`。
- 流式进行中禁用切换（`:disabled="chat.streaming"`），避免中途换模式导致状态不一致。

`ChatInput.vue` 的改动示意：

```vue
<div class="chat-input-box">
  <textarea ... />
  <ExecModeSelect />                                <!-- 新增：在发送键左边 -->
  <button class="chat-send" ...>...</button>
</div>
```

## 9.2 通用审批卡片

新建 `frontend/src/components/ApprovalCard.vue`，在 `App.vue` 中与 `PairSelectCard` 并列：

```vue
<PairSelectCard v-if="chat.paused && chat.pairs.length" />
<ApprovalCard   v-else-if="chat.paused && chat.approval" />
```

组件职责：

- 渲染 `title` + `summary`（纯中文段落）
- 渲染 `options` 单选列表；`recommended: true` 的项在标签后追加「（推荐）」，并默认选中；`hint` 作为该项下方的小字说明
- 选中的项若带 `fields`，展开一组数字输入框（`label` / `min` / `max` / `step` / `default`），前端做范围校验
- 「确认」按钮 → `chat.resumeApproval(optionId, values)`

`stores/chat.js` 新增：

```js
state: { ..., execMode: localStorage.getItem('gtai_exec_mode') || 'approval', approval: null }

// _listen 的 pause 分支
} else if (type === 'pause') {
  this.paused = true
  this.pairs = data.pairs || []
  this.approval = data.approval || null
  this.streaming = false
}

// send 时带模式
await api.post('/api/chat/start', { project, conv, message: msg, exec_mode: this.execMode })

// 新增恢复方法
async resumeApproval(optionId, values) {
  const r = await api.post('/api/chat/resume', {
    conv: useProjectStore().currentConv, option_id: optionId, values: values || {},
  })
  if (!r.ok) { useToast().error(r.message); return }
  this.paused = false; this.approval = null; this.streaming = true
  await this._listen(useProjectStore().currentConv)
}

// clear() 里补 this.approval = null
```

## 9.3 配对卡片改造

`PairSelectCard.vue`：

- 标题 `📋 找到 N 组影像配对，请选择一组` → `找到 N 组影像配对，请选择一组`
- 确认按钮 `✅ 确认选择` → `确认选择`
- 推荐项标签后追加「（推荐）」，并在其下方显示 `recommend_reason`
- `selected` 初值改为 `chat.pairs.findIndex(p => p.recommended)`，找不到则 0
- 字段名中文化：`Landsat L9 2025-07-17（1 景, 覆盖 96%）＋ Sentinel 2025-07-18（2 景, 覆盖 98%）` → 保持这个可读格式即可，但把 `云量` 未知时的显示从 `未知` 保留（已经是中文）

## 9.4 气泡文案规范（后端主导，前端只负责展示）

**规范条款（写进 `core/agent/presentation.py` 的模块 docstring，作为实现红线）：**

1. 气泡文案一律中文，不出现英文技能名（`data_acquisition`）、变量名（`train_csv`）、JSON、文件路径、堆栈。
2. 气泡文案不使用表情符号。状态用中文词表达：`开始` / `完成` / `未通过` / `已暂停` / `已停止`。
3. 数字保留，但要带中文单位与含义：`测试集决定系数 0.87`、`均方根误差 1.23 开尔文`、`有效像元 4,231,905 个`。
4. 一切技术细节（路径、参数字典、进度百分比、原始报错）只走 `on_log` 进日志面板，**不进气泡**。现有 `_emit(text, to_log=True)` 机制已经支持，只需把违规调用改掉。
5. 每个阶段的气泡最多三行：一行「第 N 步／共 M 步：中文阶段名」、一行阶段说明、一行结果摘要。
6. **`sanitize` 的路径替换必须锚定在词首**。路径正则前要加 `(?<![A-Za-z0-9\u4e00-\u9fff])`
   负向回顾，否则 `row/col`、`MB/MAE`、`训练/验证` 这类正常写法会被当成路径吃掉
   （真实踩过的坑：诊断信息「有 1 行 row/col 越界」被替换成「有 1 行 row（详见日志） 越界」，
   读者完全看不出问题在哪）。
7. **同一句话只由一个地方输出**。执行引擎在调用 `after_step` 之前已经把结果摘要打进气泡，
   因此 `RoleHooks` 的失败分支只把理由放进 `StepDecision.reason`，**不再 `ctx.emit` 同一句**；
   ABORT / REPLAN 的用户可见文案统一由 `_handle_control_decision` 输出。
   同理，「找到 N 组可用的影像组合」只由执行引擎输出，`_ask_user_to_select_pair` 不重复打印。
8. **任何长文本截断都必须落在句子边界上**，禁止 `text[:N]` 硬切。用户看到半截话会误以为「回复被 token 限制截断」，而实际上气泡是后端拼好的完整字符串、根本不受模型输出预算影响。需要限长时用按句号/分号切、超长以省略号收尾的方式。

**文案改写的生效范围（实现期澄清）**：P6 的文案改写对**两条路径都生效**（角色路径与
`roles_enabled=False` 的旧路径）。开头「硬约束」里的「未开启新特性时行为与现在完全一致」
指的是**执行路径、产物与接口契约**一致；气泡措辞属于展示层，统一中文化不改变任何行为，
也不影响 `results` 返回值（`results` 里仍是各 Skill 的原始 `message`，日志面板同样保留
原始技术细节，只有气泡走 `presentation` 渲染）。

**需要逐条改写的现有 `_emit` 调用**（`core/agent/geo_thermo_agent.py`）：

| 行号 | 现文案 | 改为 |
|---|---|---|
| L101 | `📁 已加载研究区文件：{name}` | `已载入研究区：{中文名}` |
| L142 | `正在调用 LLM 生成执行计划...` | `正在理解你的需求并规划执行步骤` |
| L155 | `⚠️ 执行计划解析失败，重试一次...` | `规划结果需要修正，正在重新整理` |
| L164 | `⚠️ LLM 计划解析失败，改用内置完整工作流计划继续执行...` | `改用标准全流程继续执行` |
| L172 | `执行计划已生成` | `执行方案已确定，共 {N} 步` |
| L199 | `⚠️ LLM 返回的执行计划不完整，自动修正为完整工作流...` | `已补全为完整流程` |
| L770 | `**Step {i}/{N}**: {skill_name} — {desc}` | `**第 {i} 步／共 {N} 步：{中文阶段名}**\n{中文说明}` |
| L785 | `正在调用 LLM 推荐模型参数...` | `正在根据数据特征推荐训练参数` |
| L823 | `📋 找到 {n} 组影像配对` | `找到 {n} 组可用的影像组合` |
| L836 | `✅ 自动选择第 1 对` | `已自动选择第 1 组：{推荐理由}` |
| L840 | `**开始下载所选配对数据**` | `**开始下载所选影像**` |
| L850/L859 | `❌ {msg}` / `⚠️ 未找到影像配对（Landsat N 景 / Sentinel M 景）...` | 去掉符号，改为中文说明 + 具体原因 |
| L876 | `{skill_name}: {result.message}` | `{中文阶段名}：{中文结果摘要}`（`result.message` 需经 `presentation.summarize(skill, result)` 转写） |
| L910/L831 | `⏸️ 等待用户选择...` | `已暂停，等待你的选择` |
| L947–L961 | `_check_exceptions` 内各条 `⚠️`/`✅` | 去符号，中文化 |

`server.py` 侧：

- `format_bubble` 的 `💭 {label}` → `思考过程`（该分支目前实际不触发，但一并规范）
- `_WORKFLOW_LABELS` 与 agent 内的 `_STEP_DESCRIPTIONS` 合并到 `presentation.py`，两边共用一份，避免同一阶段两处中文名不一致

`presentation.summarize(skill_name, result)` 的转写表（示例）：

| Skill | 原 message | 转写后 |
|---|---|---|
| `data_pipeline` | `预处理完成: 训练(step2) 45,678 行, 完整约束层 ... 空间块划分: 训练 27,406, 验证 9,136, 测试 9,136` | `数据准备完成：训练样本 27,406 个，验证 9,136 个，测试 9,136 个` |
| `rf_model` | `模型训练完成: 训练R²=0.90, 测试R²=0.87, RMSE=1.23, MB=0.12` | `模型训练完成：测试集决定系数 0.87，均方根误差 1.23 开尔文` |
| `accuracy_eval` | `粗尺度闭合评估完成: MB=0.05K, MAE=0.40K, ... 匹配格数=373,240（不代表能量/辐射守恒，不是独立10m精度）` | `闭合校核完成：平均偏差 0.05 开尔文，平均绝对误差 0.40 开尔文，共比对 373,240 个格网（这是均值闭合校核，不是 10 米独立精度）` |

---

# 第 10 章 · 对现有文件的改动清单

> 原则：能新增文件解决的，绝不改现有文件；必须改的，只做加法和默认值兼容。

## 10.1 `core/agent/geo_thermo_agent.py`

| 改动 | 类型 | 说明 |
|---|---|---|
| `_execute_plan` 整体平移到 `core/agent/executor.py` | 纯重构 | `GeoThermoAgent._execute_plan(*args, **kwargs)` 保留为薄委托，签名一字不改（`tests/test_memory_synthetic.py` 直接调用它，是回归护栏） |
| `_execute_plan` 新增可选参数 `hooks=None` | 加法 | `hooks is None` 时行为与现在**逐字节一致** |
| `process_command` 新增可选参数 `exec_mode`、`prior_messages`、`session_state` | 加法 | 均有默认值；不传时走原有逻辑 |
| 新增 `process_command_with_roles(...)` | 加法 | 角色编排入口的**薄委托**，实现在 `orchestrator/role_flow.run_with_roles`。`process_command` 第一行按开关分流：`settings.agent.roles_enabled` 为真时走新入口，否则完全走原逻辑 |
| 新增 `_agent_settings(settings_path)` | 加法 | 解析 `settings.agent`：每用户设置的 agent 段 > 全局 `config/settings.json` > 代码默认。与 `server._agent_settings()` 同一口径 |
| 新增 `_list_study_areas` / `_resolved_study_areas_dir` | 加法 | 规划 Agent 需要「当前用户已上传研究区清单」与目录绝对路径 |
| `_ask_user_to_select_pair` 的 `pairs_info` | 加法 | 数据 Agent 打过分时透传 `recommended` / `recommend_reason` / `quality_score`；未打分时这三个字段不出现，前端旧逻辑不受影响 |
| `_find_study_area_file` 新增可选参数 `preferred_name` | 加法 | 传入时按名匹配，匹配不到再退回「取最新」；不传时行为不变 |
| `_normalize_plan_paths` L556 与 `_execute_plan` L765 的 region 强制覆盖 | 条件化 | 若 plan 带 `region.study_area_file` 则用它，否则维持现状 |
| `_emit` 文案 | 改写 | 见 9.4 表格，全部改为调用 `presentation` 的渲染函数 |
| `_guess_region_from_input` | **保留不动** | 只在没有规划 Agent 的兜底路径上使用；角色路径不再调用它 |
| `_build_tuning_prompt` / 自动调参死代码 | **保留不动** | 训练 Agent 用全新的七规则实现，不复用这段死代码，避免动到现有分支 |

## 10.2 `core/agent/executor.py`（新增，承接平移过来的执行引擎）

在原有循环基础上插入三个钩子调用点：

```python
class StepDecision:
    CONTINUE  = "continue"      # 继续下一步
    RETRY     = "retry"         # 用 new_params 重跑当前步（训练调优用）
    PAUSE     = "pause"         # 弹审批节点
    ABORT     = "abort"         # 停止，向用户说明
    REPLAN    = "replan"        # 交回总调度发起 replan
```

```
for i, step in enumerate(steps):
    ...注入路径与配置（原逻辑不动）...

    ① hooks.before_step(skill_name, step, ctx)      → 可改 params / 直接 PAUSE
    ② 执行 skill（data_acquisition 的搜索-选择-下载分支原逻辑不动，
       只把「选哪一组」的排序与推荐委托给 hooks.rank_pairs 时才生效）
    ③ hooks.after_step(skill_name, result, ctx)     → 返回 StepDecision
       CONTINUE → 进入下一步
       RETRY    → 用 decision.new_params 更新 step["params"]，重跑当前 i（有次数上限）
       PAUSE    → 走 pause_callback，按用户选择映射为 CONTINUE / RETRY / ABORT / REPLAN
       ABORT    → 收尾写记忆并返回
       REPLAN   → 收尾写记忆并把 replan_reason 返回给总调度
    ④ _check_exceptions（原逻辑保留，作为无 hooks 时的兜底）
```

`hooks is None` 时三个钩子全部短路，等价于现有代码路径。

## 10.3 `server.py`

| 位置 | 改动 |
|---|---|
| `chat_start` 签名与路由 | 接收并持久化 `exec_mode` |
| `_runner` | `prior_messages` 一并传给 `process_command`；传 `exec_mode` |
| `_runner.pause_callback` | 改为分片轮询 + 移除静默选 pairs[0]（见 3.4c） |
| `_stream_events` 的 `pause` 分支 | 支持 `approval` 载荷（见 3.4a） |
| `chat_resume` | 支持 `option_id` + `values`（见 3.4b） |
| `_is_agent_command` / `_is_workflow_command` | **保留不删**，降级为 LLM 不可用时的兜底路由 |
| `chat_start` 的工作流前置检查（L1147–L1158） | **保留**。但当规划 Agent 可用时，把「未上传研究区」「未设项目目录」交给规划 Agent 以对话方式提示，避免生硬拦截。实现方式：前置检查只在 `roles_enabled=False` 时执行 |
| `format_bubble` | `💭` → 中文标签 |
| 新增 `GET /api/exec-mode` / `POST /api/exec-mode`（可选） | 若前端只用 localStorage + chat/start 透传，可不加此接口 |

## 10.4 `core/skills/builtin/rf_model.py`

唯一改动：`cleanup_stage` 调用加 `defer_cleanup` 条件（见 6.4 问题 A）。三行以内。

## 10.5 `core/memory/`

| 文件 | 改动 |
|---|---|
| `rag_store.py` | `save_knowledge` 改增量 upsert；`_query_collection` / `search_*` 增加可选 `where` |
| `seed_data.py` | 各条目补 `domain` 字段；合并导入 `knowledge_eval.EVAL_SEED_ITEMS`；`SEED_SCHEMA_VERSION` → 2 |
| `memory_manager.py` | `ensure_seeded` 按 schema_version 重写种子文件；新增 `enrich_for_role`、`save_workflow`、`search_workflows`；`delete_conversation` / `delete_project` 级联删除 session 与 workflows |
| `__init__.py` | 导出 `SessionState`、`WorkflowExperience`、`WORKFLOW_MIN_R2`、`ROLE_RETRIEVAL`、`EVAL_SEED_ITEMS` |

`enrich_prompt` 保持原样不动（现有测试 `test_memory_synthetic.py` 断言其输出中含「领域知识参考」「历史最佳实验」）。

## 10.8 `core/export_geotiff.py`（实现期新增的一处改动，仅报错路径）

| 改动 | 类型 | 说明 |
|---|---|---|
| 在 `astype(np.int64)` **之前**先检出 `row`/`col` 空值 | 只加报错分支 | `float NaN → int64` 会变成 `INT64_MIN`（-9223372036854775808），于是空值最终被报成「row/col 越界，例如 [(3704, -9223372036854775808)]」，读者完全看不出真实原因。新增的空值分支直接说明「row 或 col 是空值，通常是上游中间 CSV 写入不完整（磁盘空间不足或写入被中断）」，并给出首个出现的大致行号。**正常数据的导出行为完全不变**，只是把一类原本难以定性的失败变成可定位的失败 |

> 这条属于「拒绝导出以避免静默错位」这一既有守卫的**诊断质量**改进，不改变任何判定口径：
> 空值本来就会被判为越界而拒绝导出，改动只让错误信息说得清。

## 10.7 `core/manifest.py`（实现期新增的一处改动）

| 改动 | 类型 | 说明 |
|---|---|---|
| `project_root_from_stage_output_dir` 由「只看最后一段」改为「自末尾向上找第一个固定子目录名」 | 加法式泛化 | 多轮调优的输出目录是 `project_dir/results/tuning/round_N`，原实现的 `base in ("raw","processed","results")` 判定不成立，会把 round 目录本身当成项目根，导致 `run_manifest.json` 写进 round 目录、阶段重建与清理找错目录。泛化后 `project_dir/results` 与 `project_dir/results/tuning/round_0` 都返回 `project_dir`；不含固定子目录名的自定义路径仍原样返回，旧行为不变。断言见 `test_train_agent_modes.py` 的第 7 组 |

## 10.6 前端

| 文件 | 改动 |
|---|---|
| `components/ExecModeSelect.vue` | 新增 |
| `components/ApprovalCard.vue` | 新增 |
| `components/ChatInput.vue` | 插入 ExecModeSelect |
| `components/PairSelectCard.vue` | 推荐标记、默认选中、去 emoji |
| `App.vue` | 渲染 ApprovalCard |
| `stores/chat.js` | execMode、approval、resumeApproval |
| `styles/main.css` | 新增 `.exec-mode-select`、`.approval-card` 相关样式（沿用现有 CSS 变量与 `.pair-card` 的视觉语言） |

---

# 第 11 章 · 测试方案

沿用仓库现有的「合成测试、零网络依赖、可在容器内单文件运行」的风格（参考 `tests/test_memory_synthetic.py`）。**先写测试再写实现。**

## 11.1 回归护栏（必须先跑通，且实现全程保持绿）

| 测试 | 保证什么 |
|---|---|
| `tests/test_memory_synthetic.py`（现有，不改） | `_execute_plan` 签名与记忆写入行为不变 |
| `tests/test_skill_chain_synthetic.py`（现有，不改） | Skill 链路不变 |
| `tests/test_ttri.py` / `test_tcr.py` / `test_geo_transform.py` / `test_sentinel2_calibration.py` / `test_easylst_pipeline_synthetic.py`（现有，不改） | 算法层不受影响 |

## 11.2 新增测试

| 文件 | 覆盖点 |
|---|---|
| `tests/test_plan_schema.py` | 新格式解析；旧格式 `{"steps":[...]}` 自动补全 `stage`；非法 skill 剔除；7 步顺序校验 |
| `tests/test_planner_agent_synthetic.py` | 用 FakeAssistant 返回预设 JSON：① 纯聊天不产生 steps；② 领域问答不产生 steps；③ 缺时间 → 反问；④ 地名匹配不到研究区 → 反问并列出候选；⑤ 多轮场景（九江镇→生成啊→25年→7月）四轮后槽位齐全并出合法 plan；⑥ replan 次数达上限后不再自动 replan |
| `tests/test_planner_reflection.py` | P1–P7 七条规则逐条；规则结论覆盖 LLM 结论 |
| `tests/test_exec_mode_approval.py` | AUTO 模式各节点默认策略；APPROVAL 模式在每个节点都触发 pause；pause 载荷 schema 合法；resume 的两种协议；超时挂起而非静默选择 |
| `tests/test_data_agent_pairs.py` | `score_pair` 排序正确性（构造 5 组配对，人工核对期望顺序）；只标记推荐不代选（APPROVAL）；AUTO 自动选最高分；无合格配对时返回 `no_pair` 而非继续 |
| `tests/test_data_agent_reflection.py` | D1–D7 逐条；任一不通过时执行引擎收到 ABORT/REPLAN 而不是 CONTINUE |
| `tests/test_train_tuning_rules.py` | R1–R7 逐条单测；用脚本化指标序列驱动完整循环，验证：收敛提前停、恶化提前停、8 轮上限、最终取 RMSE 最低轮；参数越界被截断；过拟合触发 max_depth 下调 |
| `tests/test_train_agent_modes.py` | APPROVAL 模式下高精度也弹窗；五个选项各自映射到正确的 StepDecision；`defer_cleanup` 在非最终轮为 True |
| `tests/test_eval_reflection.py` | E-R1 数字无出处被打回；E-R2 禁用词被打回；E-R3 null R² 编造被打回；E-R4「因极值差贬低产品」被打回；E-R5 评级不一致被打回；E-R6 口径混用被打回；两次重写仍不过 → 降级模板报告 |
| `tests/test_memory_role_linkage.py` | `enrich_for_role` 四个角色各自的检索范围；E 系列在已有 collection 上能被增量播种；`where` 过滤生效；SessionState 读写与级联删除；WorkflowExperience 三个写入条件 |
| `tests/test_presentation.py` | 对全部 8 个 Skill 的 message 转写结果做断言：不含表情符号（Unicode 范围检查）、不含 ASCII 技能名、不含路径分隔符、不含 `_` 开头的变量名；阶段中文名单一来源；后端源码里不再有带表情符号的气泡调用 |
| `tests/test_roles_end_to_end_synthetic.py`（实现期新增） | **11.3 全部场景的自动化版本**：用合成 Skill + FakeAssistant 走完整 `process_command_with_roles`。合成 Skill 会真实写出 5 个小栅格与含地形指数列的 CSV，让 D1–D7 走默认探针而不是注入替身。覆盖：纯聊天/问答不触发 Skill、九江镇四轮、完全执行一次跑完、由我批准四个节点都停、高精度也弹窗、预处理失败拦在训练前、未设项目目录与未上传研究区的对话式引导、特性开关关闭走旧路径、自动调优多轮取最佳轮 |

## 11.3 端到端（人工验收，非自动化）

> 下表的场景已在 `tests/test_roles_end_to_end_synthetic.py` 里自动化（零网络依赖）。
> 人工验收仍需在真实数据上过一遍，重点看真实下载耗时、真实精度与气泡观感。

| 场景 | 期望 |
|---|---|
| 纯聊天：「你好」「你是谁」 | 只聊天，不触发任何 Skill，不写实验记录 |
| 领域问答：「TTRI 是解决什么问题的」 | 注入 K06 回答，不跑流程 |
| 多轮模糊：九江镇 → 生成啊 → 25 年 → 7 月 | 四轮后正确出 plan，区域是九江镇，绝不出现武汉 |
| 由我批准 · 全流程 | 在 pair_selection、tuning_decision、每轮 tuning_round、final_report 都停下问 |
| 完全执行 · 全流程 | 一次跑完，自动选最优配对、自动调优、直接出报告 |
| 无合格影像 | 说明搜到多少景、为什么不合格、给出可选项，不硬跑 |
| 数据预处理失败 | 停下，说明原因，走 replan 或问用户，不进训练 |
| 结果解读 | 数字全部对得上 JSON；不出现「能量守恒」「10 米精度」；10 米极值比 30 米宽时按 E03 正确解释 |

---

# 第 12 章 · 实施阶段与验收标准

严格按此顺序推进，每阶段结束必须跑通对应测试且回归测试全绿才进入下一阶段。

| 阶段 | 内容 | 验收标准 | 预估改动量 |
|---|---|---|---|
| **P0 · 重构打底** | `_execute_plan` 平移到 `executor.py`（薄委托保留）；抽 `presentation.py`；抽 `plan_schema.py`（先只做旧格式兼容） | 全部现有测试零修改通过；手动跑一次全流程行为与改前一致 | ~400 行（其中 360 行是平移） |
| **P1 · 执行模式与审批协议** | `exec_mode.py`、`approval.py`、`run_state.py`、`hooks.py`；server 三处泛化；前端 ExecModeSelect + ApprovalCard | `test_exec_mode_approval.py` 通过；前端能切模式、能弹通用审批卡并恢复 | ~450 行（后端 250 / 前端 200） |
| **P2 · 规划 Agent** | `base_role.py`、`planner_agent.py`、`prompts/planner.py`、`planner_rules.py`、`session_state.py`；server 透传 prior_messages；路由改造 | `test_planner_*.py` 通过；九江镇四轮场景人工验收通过 | ~600 行 |
| **P3 · 数据 Agent** | `data_agent.py`、`prompts/data.py`、`data_rules.py`；PairSelectCard 推荐标记 | `test_data_agent_*.py` 通过；无合格配对场景人工验收通过 | ~450 行 |
| **P4 · 训练 Agent** | `train_agent.py`、`prompts/train.py`、`train_rules.py`；rf_model 的 defer_cleanup | `test_train_*.py` 通过；两种模式下调优人工验收通过 | ~500 行 |
| **P5 · 评估 Agent 与记忆扩展** | `eval_agent.py`、`prompts/evaluation.py`、`eval_rules.py`、`knowledge_eval.py`、`workflow_experience.py`；memory 三处扩展 | `test_eval_reflection.py`、`test_memory_role_linkage.py` 通过；结果解读人工验收通过 | ~550 行 |
| **P6 · 气泡文案与前端打磨** | 全量文案改写；样式统一 | `test_presentation.py` 通过；人工过一遍全流程气泡，无英文名、无路径、无表情符号 | ~250 行 |

**总量约 3200 行新增 + 约 250 行现有文件的加法式改动。**

**特性开关**：新增 `settings.agent` 配置段（写入 `config/settings.json` 与每用户 `settings.json` 的默认值）：

```json
{"agent": {"roles_enabled": true, "replan_max": 3, "tuning_max_rounds": 5,
           "approval_wait_seconds": 1800, "default_exec_mode": "approval"}}
```

`roles_enabled=false` 时，`process_command` 完全走现有旧路径。这是随时可以回退的安全阀：**实现期（P0–P6 进行中）默认 `false`，P6 全部阶段测试与回归验收通过后置为 `true`**，作为最终交付状态。`tuning_max_rounds` 默认 5、硬上限 8（拍板结论 4）。

---

# 第 13 章 · 风险与待确认项

## 13.1 拍板结论（已确认，实现时不得再改）

| # | 议题 | 拍板结论 | 落地位置 |
|---|---|---|---|
| 1 | 暂停超时行为 | **挂起等待**。超时不替用户做决定，流程停在暂停点，用户重新发送指令即可重跑。移除「静默选 pairs[0]」兜底 | `server.py` `pause_callback`（3.4c） |
| 2 | 意图分类范围 | **每条消息都过意图分类**（接受每条消息多一次 LLM 调用）。`_is_agent_command` 关键词表保留，降级为 LLM 不可用时的兜底 | `roles/planner_agent.classify_intent`（4.3） |
| 3 | 无研究区 / 未设项目目录 | **角色化后由规划 Agent 以对话方式引导**，不再生硬拦截。`chat_start` 的前置硬拦截仅在 `roles_enabled=False` 时执行 | `server.chat_start`（10.3） |
| 4 | 调优轮数 | **默认 5 轮，可配置，硬上限 8**。`settings.agent.tuning_max_rounds` 默认 5；配置值超过 8 时截断为 8（规则 R7 的硬上限不变） | `reflection/train_rules.py`（6.2） |

> 结论 4 的口径说明：`MAX_TUNING_ROUNDS = 8` 是**硬上限**（规则 R7，任何配置都不得突破）；`DEFAULT_TUNING_ROUNDS = 5` 是**默认值**（`settings.agent.tuning_max_rounds` 未配置时生效）。第 6.2 节 R7 的表述「达到 8 轮强制停止」指硬上限；实际生效轮数 = `min(配置值, 8)`。

## 13.2 技术风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 规划 Agent 输出的 JSON 不稳定 | 计划解析失败 | 复用现有三级解析兜底 + `plan_schema` 校验 + 失败重试一次 + 最终退回现有 `_build_full_workflow_plan` |
| 七规则与 LLM 决策冲突 | 调优行为不可预期 | 规则**永远**覆盖 LLM；每次覆盖都在日志与报告中标注「[规则] R{n}」，可追溯 |
| 评估反思误杀正常表述 | 报告频繁降级为模板 | 禁用词表与数值核对都要写在独立文件里，配有单测；上线初期把降级次数打点到日志，据实调整 |
| ChromaDB `where` 过滤语法差异 | 角色化检索失效 | `enrich_for_role` 在 where 查询异常时自动退回无过滤查询（与现有「检索失败返回空列表」的容错风格一致） |
| 多轮训练模型文件混淆 | 下游取到错误模型 | 见 6.4 问题 B 的复制方案；`test_train_agent_modes.py` 增加断言：最终 `results/train` 下最新 pkl 就是被接受的那一轮 |
| 平移 `_execute_plan` 引入回归 | 全流程挂掉 | P0 阶段只做平移不改逻辑，用现有测试当护栏；`git diff` 应显示为纯移动 |
| 单文件超长 | 违反项目编码规范 | 角色文件目标 250–350 行，超过 400 行就把提示词与规则拆到独立文件；`executor.py` 控制在 600 行内（实际 565 行：执行循环 + 路径映射 + 实验记录组装，进一步拆分会把强耦合的执行状态散到多处，收益为负）。<br>**实现期结果**：新增文件最长 565 行（`executor.py`），其余均在 350 行内；`geo_thermo_agent.py` 由改造前的 1199 行降到 914 行（`_execute_plan` 移出、角色编排移入 `orchestrator/role_flow.py`）。它仍高于 800 行门槛，剩余体量全部来自**改造前就存在**的旧路径代码（`_normalize_plan_paths` 约 195 行、旧 system prompt 与内置全流程计划约 120 行），继续拆分属于队友旧路径的重构，超出本次范围。仓库里另有两个改造前就超限的文件（`core/data_preprocessing.py` 875 行、`core/skills/builtin/data_acquisition.py` 1521 行），本次未触碰 |

## 13.3 明确不在本次范围内的事

- 不引入 LangChain / LangGraph / AutoGen 等多智能体框架
- 不拆分成多进程 / 多服务
- 不新增第二个 LLM 供应商或第二套模型调用封装
- 不改动任何算法层文件（`ttri.py` / `tcr.py` / `rf_model.py` / `evaluation.py` / `data_preprocessing.py` 等）
- 不改动多用户隔离、鉴权、下载、地图渲染相关逻辑
- 城市热岛分析等新功能只在 `plan_schema` 的 `product` 枚举上预留位置，本次不实现

---

## 附录 A · 角色提示词骨架（终稿需与队友对齐后固化）

每个角色的提示词必须包含四段，缺一不可（这是「从零学会单 Agent 多角色」第 9 课检查清单的要求）：

```
① 你是谁          —— 一句话身份
② 你只负责什么     —— 职责边界，正面清单
③ 你禁止做什么     —— 硬约束，负面清单
④ 你的输出格式     —— 严格 JSON schema 或固定小节，必须可被程序解析
```

示例（规划 Agent）：

```
① 你是 GeoThermoAI 的规划专家。
② 你只负责：判断用户意图、把模糊需求补全到可执行、生成结构化执行计划。
③ 你禁止：执行任何下载或训练操作；在信息不全时生成计划；
          编造用户没有上传的研究区；把用户的闲聊当成任务指令；
          在用户只说了年份没说月份时自行假设月份。
④ 你只输出一个 JSON 对象，格式见下，不输出任何解释文字或代码块标记。
```

四份终稿放在 `core/agent/roles/prompts/` 下，每份一个 Python 模块常量，便于版本管理与 A/B 对比。

## 附录 B · 关键常量集中表

| 常量 | 默认值 | 位置 | 说明 |
|---|---|---|---|
| `DEFAULT_EXEC_MODE` | `"approval"` | `orchestrator/exec_mode.py` | 保持与现状一致 |
| `REPLAN_MAX` | `3` | `orchestrator/agent_config.py` | 自动 replan 上限（`run_state.py` 从这里导入） |
| `MAX_TUNING_ROUNDS` | `8` | `orchestrator/agent_config.py` | 规则 R7 的**硬上限**，配置不得突破；`reflection/train_rules.py` 从这里导入 |
| `DEFAULT_TUNING_ROUNDS` | `5` | `orchestrator/agent_config.py` | 调优轮数**默认值**（拍板结论 4） |
| `MAX_RETRY_PER_STEP` | `10` | `executor.py` | 单步 RETRY 硬上限（防止死循环，应大于调优轮数） |
| `APPROVAL_WAIT_TIMEOUT` | `1800` 秒 | `orchestrator/agent_config.py` | 审批等待超时（`server.py` 从这里读） |
| `MIN_TRAIN_ROWS` | `10000` | `reflection/data_rules.py` | 与现有 `_check_exceptions` 阈值一致 |
| `MIN_VALID_RATIO` | `0.15` | `reflection/data_rules.py` | 云掩膜后有效像元占比下限 |
| `PAIR_WEIGHTS` | 云 0.45 / 覆盖 0.30 / 时间差 0.15 / 景数 0.10 | `roles/data_agent.py` | 配对质量评分权重 |
| `EVAL_REWRITE_MAX` | `2` | `reflection/eval_rules.py` | 解读文本重写上限 |
| `WORKFLOW_MIN_R2` | `0.75` | `memory/workflow_experience.py` | 工作流写回的精度门槛（K24 合格线） |

---

## 附录 C · 魔搭（ModelScope）Docker 创空间部署硬约束

> 依据：`docs/modelscope/魔搭操作文档/创空间/Docker创空间介绍.pdf`、`docs/modelscope/01-速查/创空间-环境与持久化.md`、`docs/modelscope/01-速查/创空间-部署OpenAPI与CLI.md`。
> **本附录只约束部署与工程结构，不覆盖第 2–10 章的任何架构决策。**

| 约束 | 要求 | 本项目现状 / 本次改造的合规性 |
|---|---|---|
| SDK 类型 | 本项目属于 FastAPI + Vue 构建产物，超出 Gradio/Streamlit/Static 范畴，必须用 `sdk_type: docker`（需实名认证） | `ms_deploy.json` 已声明 `"sdk_type": "docker"`，本次不改 |
| 监听端口 | 必须 `0.0.0.0:7860`；**禁止 8080**（平台自带进程占用） | `server.py main()` 为 `uvicorn.run(app, host="0.0.0.0", port=7860)`，`Dockerfile` `EXPOSE 7860`，本次不改 |
| 资源规格 | 免费额度为 `platform/2v-cpu-16g-mem` | `ms_deploy.json` 已声明该规格。**约束推论**：多轮调优是 CPU 密集型，2 vCPU 下单轮随机森林训练耗时可观，这是拍板结论 4 把默认轮数定为 5 的工程理由之一 |
| HTTP Header | `Authorization`、`X-modelscope-*`、`X-studio-*` 被平台占用，后端接口不得依赖 | 前端 `api/index.js` 已对所有请求追加 `?token=` 穿透（`withTokenQuery`），`Authorization` 仅作本地直连双保险；本次新增的 `POST /api/chat/start`（带 `exec_mode`）与 `POST /api/chat/resume`（带 `option_id`）沿用同一封装，**不新增任何自定义请求头** |
| 数据持久化 | 容器重启后磁盘数据丢失；持久化目录 `/mnt/workspace`（仅运行期可用，构建期不可用） | 现状用 `WORKSPACE_ROOT` 环境变量指定项目数据根，未设置时落 `data/users`。本次新增的 `memory/sessions/{conv_id}.json` 与 `memory/projects/{pid}/workflows.json` 均位于既有 `data/users/{uid}/memory/` 下，**不新增持久化根路径**，与现有对话/实验记录同生共死 |
| 依赖 | 依赖写 `requirements.txt`，构建期按 `Dockerfile` 安装 | 本次改造**不新增任何 pip 依赖**：角色 Agent 复用现有 `GeoThermoAI_Assistant`（requests），plan 校验用标准库 `json/re/dataclasses`，前端组件为纯 Vue 3 SFC（无新 npm 包） |
| 不引入的东西 | 不引入 `modelscope_studio` 组件库（那是 Gradio 创空间用的，本项目是 Docker + 自建前端）；不引入 LangChain/LangGraph/AutoGen | 与 13.3「明确不在本次范围内的事」一致 |
| 构建期自检 | `Dockerfile` 已有 bge 模型 fail-fast 自检 | 本次不改 `Dockerfile`。新增 `core/memory/knowledge_eval.py` 的 E 系列种子随 `COPY . .` 带入镜像，无需额外下载 |

**部署自检清单（每次改完代码后跑一遍）**：

1. `python3 -c "import server"` 能在容器内成功导入（无语法错误、无缺失依赖）；
2. `requirements.txt` 无新增条目（`git diff requirements.txt` 为空）；
3. `frontend/package.json` 无新增依赖（`git diff frontend/package.json` 为空）；
4. **`dist/` 前端产物必须重新构建并真正进入创空间仓库**（详见下面的「dist 陷阱」）；
5. `ms_deploy.json` 的 `port` 仍为 `7860`；
6. `python3 tests/test_roles_end_to_end_synthetic.py` 通过（多角色路径端到端可跑）。

### dist 陷阱（改前端后必看）

`队友运行副本-docker版/.gitignore` 第 2–4 行忽略了 `node_modules/`、`frontend/dist/`、`dist/`。
`server.py` 只有在 `dist/` 存在时才挂载静态资源（`if _DIST.exists()`），因此**如果同步到创空间时
`dist/` 被 gitignore 拦下，线上就只有 `/api/*` 能用，页面打不开**（`.dockerignore` 没有排除
`dist/`，所以本地 `docker build` 反而正常，问题只在 Git 同步这一步暴露）。

改过 `frontend/src/**` 之后的正确做法：

```bash
cd frontend && npm install && npm run build && cd ..
rm -rf dist && cp -r frontend/dist dist
git add -f dist                 # -f 绕过 .gitignore，把构建产物带进创空间仓库
git commit -m "chore: rebuild frontend dist"
git push modelscope master
```

本次改造涉及 `ExecModeSelect.vue`、`ApprovalCard.vue`、`ChatInput.vue`、`PairSelectCard.vue`、
`App.vue`、`stores/chat.js`、`styles/main.css`，**必须重新构建 dist**，否则线上前端仍是旧版本：
没有模式上拉框、收不到通用审批卡片、`chat/start` 不带 `exec_mode`（后端会回落到 `approval`）。

### 底部灰边的已知限制与人工校准机制（实现期修订 v1.2）

**现象**：部署到魔搭创空间后，聊天区底部会露出一小条没被填满的灰色空白（`--bg` 背景色），
输入框看起来没有贴到可视区域的最底部。

**根因（已知限制，无法在不接触真实线上页面的情况下精确修复）**：`frontend/src/embedFit.js`
用固定常量 `DEFAULT_MODELSCOPE_CHROME_PX = 64` 估计魔搭 Studio 顶栏遮住 iframe 的高度，
再从 `--app-height` 里扣掉。这个 64 是经验猜测值，不是从真实页面量出来的精确值——如果魔搭
实际的顶栏高度小于 64px，应用外壳就会比真正可视区域更矮，多扣的那部分露出灰色背景。

这个数值**不能靠 iframe 内部的 JS 自动纠正**：iframe 内部唯一能测到的「外壳底部到
`window.innerHeight` 之间还有多少空隙」这件事，测出来的结果对「顶栏确实存在、只是猜大了」
和「顶栏本来就不需要扣」两种情况是**完全无法区分的**（两种情况在 iframe 内部量出来的空隙
数值一样大），盲目地把外壳撑满填掉这条空隙，反而可能重新引入这个机制本来要防的「聊天输入框
被顶栏遮住」的问题。这是跨域 iframe 天然的信息盲区，只有真的打开线上页面用肉眼确认才能定下
精确数值。

**本次修订（在无法现场量测的前提下能负责任地做的事）**：

1. `readChromeOverride` 除了原有的 `?embedChrome=N` URL 参数，新增读取
   `localStorage['gtai_embed_chrome']`，优先级为 `URL 参数 > 本地存储 > 默认值`；
2. 新增一个只在检测到嵌入环境（`document.documentElement.dataset.embedFit==='modelscope'`）
   时才出现的极简校准控件（右下角一个不起眼的小按钮，默认收起），可以现场用 +/− 按钮微调
   顶栏偏移量并立即看到灰边变化，确认无误后点击保存即写入 `localStorage`，之后同一浏览器
   再打开都会用校准后的值，不需要每次手工拼 `?embedChrome=` 参数；
3. **后续动作（需要人工配合）**：请在真实的魔搭创空间页面里打开这个校准控件，调到灰边刚好
   消失、输入框也没有被顶栏裁切为止，把当时看到的偏移量数值告诉我，我再把
   `DEFAULT_MODELSCOPE_CHROME_PX` 改成这个真实值，让新用户不需要再自己校准一次。
