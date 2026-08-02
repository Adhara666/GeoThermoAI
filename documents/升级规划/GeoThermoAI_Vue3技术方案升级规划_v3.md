# GeoThermoAI → Vue 3 技术方案升级规划（V3 版本）

> **目标**：将 GeoThermoAI 从 Gradio（Docker 版）前端重构为 **Vue 3 + FastAPI** 前后端分离架构，核心算法 `core/` 零改动迁移，实现可上传 ModelScope Studio 的生产级 Docker 应用。
>
> **架构定位**：LLM-Orchestrated Workflow — LLM 负责理解意图/推荐参数/分析结果，核心科学计算保持确定性工作流。本版本聚焦 UI 层重构，为后续记忆模块（ChromaDB）、断点执行、多模型 Benchmark 等高级功能预留扩展点。

---

## 一、为什么弃用 Gradio 改用 Vue 3

| 维度 | Gradio（现状） | Vue 3 + FastAPI（目标） |
|---|---|---|
| 前端自由度 | 组件受限，样式难定制 | 完全自由，像素级控制 |
| 文件下载 | `allowed_paths` 限制 + iframe 沙箱拦截（曾出现"无权限"） | 自建 `/api/download` 路由 + `FileResponse`，新标签页下载，无权限问题 |
| 流式对话 | Gradio queue + yield 元组 | SSE（Server-Sent Events），标准、稳定 |
| 项目/对话管理 | 组件分离（项目下拉 + 对话单选） | 一体化的分组下拉菜单（按项目分组） |
| 版本依赖 | Gradio 版本 API 差异导致兼容坑（placeholder 报错等） | 无框架版本坑，Vue 3 稳定 |
| 界面设计 | 受限 | 参照 DeepSeek / Codex / 旧版 UI，流畅直观 |

---

## 二、技术选型

| 层 | 选型 | 版本 | 说明 |
|---|---|---|---|
| 后端框架 | **FastAPI** | ≥0.110 | 异步 + 自动文档，直接复用 `core/` |
| ASGI 服务器 | **uvicorn** | ≥0.29 | 生产级 ASGI |
| SSE 流式 | FastAPI `StreamingResponse` | 内置 | `text/event-stream` |
| 前端框架 | **Vue 3** | ^3.4 | Composition API |
| 构建工具 | **Vite** | ^5 | 本地 `npm run build` 产出静态文件，Docker 无需 Node |
| 状态管理 | **Pinia** | ^2 | 会话/设置/流式状态 |
| 路由 | **Vue Router** | ^4 | 单页应用路由（预留多视图扩展） |
| HTTP 客户端 | **原生 fetch + EventSource** | 内置 | 不引入 axios，减少依赖 |
| 交互地图 | **Folium**（后端生成）+ iframe 嵌入 | ≥0.16 | 复用 `core/visualization.py` 现有能力 |
| 图标 | 内联 SVG | — | 无图标库依赖 |
| 遥感/算法 | GDAL / rasterio / numpy / scikit-learn | 现有 | `core/` 零改动 |

---

## 三、架构总览

```text
┌─────────────────────────────────────────────────────────────┐
│                  ModelScope Studio 容器（Docker）             │
│                                                             │
│  浏览器                                                        │
│  └─ Vue 3 SPA（dist/ 静态资源）                                │
│      ├─ 侧边栏：项目/对话一体化下拉 + 路径 + 研究区              │
│      ├─ 对话区：气泡 + SSE 流式 + 影像配对选择                  │
│      └─ 工作面板：API设置/参数/数据源/下载/地图/精度/进度         │
│              │  fetch / EventSource / FileResponse            │
│  ┌───────────▼───────────────────────────────────────────┐  │
│  │              FastAPI 后端（server.py）                  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  AppBackend（对话持久化 + 流式队列 + 暂停/恢复）   │  │  │
│  │  │  · data/conversations/*.json  对话持久化          │  │  │
│  │  │  · queue.Queue + threading    流式管道            │  │  │
│  │  │  · SSE 事件：token/append/pause/workflow/done/error│  │  │
│  │  └────────────────┬────────────────────────────────┘  │  │
│  │                   ▼                                   │  │
│  │  LLM Agent 引擎（core/agent/geo_thermo_agent.py）       │  │
│  │  Skill 注册表 + 执行引擎（core/skills/）                 │  │
│  │  核心算法：rf_model / ttri / tcr / lst_final / ...      │  │
│  │  可视化：LayerVisualizer → folium HTML                  │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、后端 API 设计（FastAPI）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 返回 Vue SPA（`dist/index.html`） |
| GET | `/api/bootstrap` | 初始化数据：项目树 + 设置 + 研究区列表 |
| POST | `/api/projects` | 创建项目 `{name}` |
| DELETE | `/api/projects/{pid}` | 删除项目（含对话文件） |
| POST | `/api/conversations` | 创建对话 `{project, title}` |
| DELETE | `/api/conversations/{cid}` | 删除对话（含运行中线程清理） |
| POST | `/api/project/{pid}/dir` | 保存项目目录 `{path}`（自动创建目录） |
| GET | `/api/messages` | 获取对话消息 `?project=&conv=` |
| GET | `/api/settings` / POST `/api/settings` | 读写 LLM API 配置（保存时热更新 assistant） |
| POST | `/api/study-area` | 上传研究区文件（multipart，shp→geojson） |
| GET | `/api/study-areas` | 研究区列表 |
| POST | `/api/chat/start` | 启动对话 `{project, conv, message}` → 返回任务 id |
| GET | `/api/chat/stream` | SSE 流式（token/append/pause/workflow/done/error） |
| POST | `/api/chat/resume` | 配对选择后恢复 `{conv, pair_index}` |
| GET | `/api/workflow` | 工作流 7 步状态 |
| GET | `/api/accuracy` | 精度评估摘要 |
| GET | `/api/map/html` | 生成 folium 地图完整 HTML（iframe src） |
| GET | `/api/layers` | 可用图层列表 |
| GET | `/api/files` | 列出项目目录文件 `?project_dir=` |
| GET | `/api/download` | **下载文件**（FileResponse + Content-Disposition） |
| POST | `/api/test/planetary` | Planetary Computer 连通性测试 |
| POST | `/api/test/gdal` | GDAL 环境测试 |

### 下载方案（根治"无权限"）

```python
@app.get("/api/download")
def download(path: str):
    # 仅允许访问下载暂存目录（/tmp/geothermoai_downloads 或项目目录内）
    # 校验 realpath 防目录穿越
    return FileResponse(
        target, filename=os.path.basename(target),
        media_type="application/octet-stream",
        content_disposition_type="attachment",
    )
```

前端用 `<a href="/api/download?path=..." target="_blank">` 新标签页打开下载：
- 不经过 Gradio `/gradio_api/file=`（无 `allowed_paths` 限制）；
- 新标签页不受创空间 iframe 沙箱限制；
- 同时展示原始 URL 文本，可复制手动打开（双保险）。

### 流式对话协议（SSE）

```
POST /api/chat/start {project, conv, message}
  → 后台线程：agent.process_command(on_token=写入队列, pause_callback=暂停等待)
  → 返回 {ok: true}

GET /api/chat/stream?conv=xxx
  → text/event-stream，事件格式：
  event: token    data: {"content": "累积文本"}
  event: append   data: {"content": "追加文本"}
  event: pause    data: {"pairs": [...]}          # 前端展示配对选择卡
  event: workflow data: {"steps": [...]}
  event: done     data: {}
  event: error    data: {"message": "..."}

POST /api/chat/resume {conv, pair_index}
  → 写入 pause_response + 唤醒事件 → 前端重新连接 /api/chat/stream 继续消费
```

线程/队列模型完全复刻 Gradio 版 `_consume_stream` 的暂停/恢复语义：
- pause 时不清理队列/线程/事件（保留供 resume 使用）；
- 超时 5 分钟自动选择第一对；
- 删除对话时唤醒阻塞线程并打删除标记，防止重建磁盘文件。

---

## 五、前端设计（Vue 3 SPA）

### 5.1 布局（参照 DeepSeek / Codex + 旧版 UI）

```
┌─────────┬──────────────────────────────┬───────────────────┐
│ 侧边栏    │         对话区                 │   工作面板（可折叠）   │
│ 250px    │         自适应                 │   320px           │
│          │                              │                   │
│ [Logo]   │  当前模型标签                   │  Tab 切换：         │
│ GeoThermoAI                            │  🔑 API设置         │
│ [＋新对话] │  ▸ 用户气泡（右）              │  ⚙️ 参数            │
│          │  ▸ AI 气泡（左）含思考折叠      │  🔗 数据源(测试)     │
│ [项目▾]   │  ▸ 配对选择卡片（暂停时出现）   │  ⬇️ 下载            │
│ [对话▾]   │                              │  🌍 地图            │
│          │                              │  📊 精度            │
│ 项目保存路径 │  ┌──────────────────────────┐│  📋 进度            │
│ 研究区上传  │  │ 输入框（Enter发送）        ││                   │
│          │  └──────────────────────────┘│                   │
└─────────┴──────────────────────────────┴───────────────────┘
```

- **项目/对话一体化**：侧边栏顶部一个「对话下拉框」，按项目分组（`<optgroup>`），选中即同时激活项目与对话；项目级操作（新建/删除项目、项目目录）收拢在折叠区，避免"两个分离组件"。
- **响应式**：`< 1200px` 右侧工作面板折叠为抽屉；`< 768px` 侧边栏隐藏为汉堡菜单。

### 5.2 前端目录结构

```text
frontend/
├── index.html
├── vite.config.js          # dev 代理 /api → localhost:8000
├── package.json
└── src/
    ├── main.js             # createApp + Pinia + Router
    ├── App.vue             # 三栏布局骨架
    ├── router/index.js
    ├── stores/
    │   ├── chat.js         # 会话、消息、流式状态
    │   ├── project.js      # 项目/对话树、项目目录
    │   └── settings.js     # API/参数配置
    ├── api/index.js        # fetch 封装 + SSE 客户端
    ├── components/
    │   ├── Sidebar.vue         # 侧边栏
    │   ├── ProjectDropdown.vue # 项目/对话一体化下拉
    │   ├── ChatMessages.vue    # 消息气泡列表
    │   ├── ChatInput.vue       # 输入区
    │   ├── PairSelectCard.vue  # 影像配对选择
    │   ├── Workbench.vue       # 右侧工作面板（Tabs）
    │   ├── panels/
    │   │   ├── ApiSettings.vue
    │   │   ├── ModelParams.vue
    │   │   ├── DataSource.vue  # 测试连接/GDAL
    │   │   ├── FileDownload.vue# 文件列表+新标签页下载
    │   │   ├── MapView.vue     # folium iframe
    │   │   ├── Accuracy.vue
    │   │   └── Workflow.vue
    │   └── MarkdownRender.vue  # 消息 Markdown/数学公式渲染
    └── styles/main.css     # 设计变量 + 响应式
```

### 5.3 关键交互实现

| 功能 | 实现 |
|---|---|
| 流式气泡 | SSE `token` 事件累积文本 → `reactive` 消息更新 → 自动滚动到底部；支持 `$`/`$$` 数学公式（MathJax，CDN） |
| 思考链折叠 | 消息内 `<details>` 渲染（DeepSeek 风格），历史回传时后端 `strip_thinking` 剥离 |
| 配对选择 | SSE `pause` → 显示选择卡（radio 列表 + 确认按钮）→ `POST /resume` |
| 地图 | `/api/map/html?conv=` 生成 folium HTML → `<iframe>` 嵌入；刷新按钮 + 图层可用性列表 |
| 下载 | `/api/files` 列出项目目录文件 → 选择 → `/api/download?path=` 新标签页下载 |
| 工作流/精度 | `/api/workflow`、`/api/accuracy` 轮询刷新 |
| 测试 | `/api/test/planetary`、`/api/test/gdal` 一键执行显示结果 |

---

## 六、数据模型（对话持久化，与旧版兼容）

```json
{
  "id": "a0217bf6b3ef",
  "project": "武汉_2024",
  "title": "武汉7月LST",
  "messages": [{"role": "user|assistant", "content": "..."}],
  "project_dir": "/home/studio_service/PROJECT/output/wuhan_202407",
  "created_at": "2026-08-01 12:00:00",
  "updated_at": "2026-08-01 12:30:00",
  "starred": false
}
```

- 目录：`data/conversations/{id}.json`
- 项目目录保存在每段对话 JSON 的 `project_dir` 字段 + 项目首条对话的 `__dir__` 约定（兼容旧版读取）

---

## 七、生产级质量保障

| 维度 | 措施 |
|---|---|
| 代码规范 | ESLint（vue 3 + prettier），组件单一职责，Composition API 组织 |
| 性能 | 消息列表虚拟滚动（>200 条）；folium 图 `max_size=1024` 限制；SSE 合并高频 token |
| 响应式 | CSS Grid/Flex + 断点（1200/768px）；工作面板折叠抽屉 |
| 兼容性 | 目标 Chrome/Edge/Firefox/Safari 最新两版；CSS 前缀自动（autoprefixer）；`<dialog>` 降级 |
| 安全 | 下载路径 realpath 校验防目录穿越；API Key 前端不明文展示；上传文件类型白名单 |
| 可观测 | 后端结构化日志；前端错误提示 toast；SSE 断线自动重连 |

---

## 八、与升级规划 v2 的衔接（高级功能扩展点）

| v2 功能 | 本版预留方式 |
|---|---|
| 记忆模块（ChromaDB + RAG） | 后端新增 `/api/memory` 接口族；`AppBackend` 预留 `memory_manager` 属性；前端侧边栏预留"记忆"入口 |
| 断点执行 | `/api/chat/stream` 已支持 `workflow` 事件，可扩展 `ready` 事件携带缺失依赖；前端预留"从步骤继续"按钮 |
| 多模型 Benchmark | 参数面板已是独立组件，可扩展模型选择下拉 + 对比图表 |
| 执行模式（自动/批准） | 输入区上方预留模式切换；SSE `pause` 事件类型可扩展 `approval` |
| 一键体验/报告 | `/api/map`、`/api/accuracy` 可直接复用；新增报告生成端点即可 |

---

## 九、部署与构建

```text
本地构建（需要 Node 20+）：
  cd frontend && npm install && npm run build
  → 产出 frontend/dist/，由 server.py 静态托管

Docker（ModelScope Studio，Docker 类型）：
  FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.3
  pip install fastapi uvicorn python-multipart + core 依赖
  COPY core/ config/ dist/ server.py
  CMD ["python3", "server.py"]        # 监听 0.0.0.0:7860
```

- Studio 约束：端口 7860、浏览器访问 → 完全满足；
- Docker 镜像内无需 Node（dist 已构建好），构建快、体积小。

---

## 十、里程碑

1. **M1 骨架**：FastAPI 后端 + Vue 布局 + 项目/对话一体化 + 纯 LLM 对话 SSE
2. **M2 核心**：Agent 全流程（配对选择/工作流/流式气泡）+ 下载 + 地图
3. **M3 完整**：工作面板全模块 + 响应式 + 打磨
4. **M4 上线**：本地端到端验证 → Docker 构建 → Studio 部署
