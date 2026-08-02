# GeoThermoAI → ModelScope Studio 升级规划

> **目标**: 将 GeoThermoAI 从桌面应用迁移至 ModelScope Studio（Gradio + Ant Design X），同时从"LST 降尺度单任务工具"升级为"热红外遥感 GeoAI Agent 平台"。
>
> 文档基于 `d:\Files\研究和项目\10.GeoThermoAI\GeoThermoAI` 源代码深度分析编写。

---

## 一、现状总结

### 1.1 当前技术栈

| 层级 | 技术 | 代码位置 |
|---|---|---|
| **UI 容器** | PyWebView（桌面原生窗口） | [main.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/main.py) |
| **前端** | 原生 HTML + CSS + JS | [ui/](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/ui/) |
| **API 桥** | `window.pywebview.api.xxx()` | [ui/api.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/ui/api.py) |
| **AI 引擎** | OpenAI 兼容 API（DeepSeek/Kimi/OpenAI） | [core/ai_assistant.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/ai_assistant.py) |
| **Agent** | LLM → JSON 计划 → Skill 编排引擎 | [core/agent/geo_thermo_agent.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/agent/geo_thermo_agent.py) |
| **Skill 系统** | 注册表模式，内置 8 个 Skill | [core/skills/](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/skills/) |
| **核心算法** | scikit-learn RF / rasterio / GDAL | [core/rf_model.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/rf_model.py) 等 |
| **数据源** | Microsoft Planetary Computer STAC | [core/skills/builtin/data_acquisition.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/skills/builtin/data_acquisition.py) |
| **流水线** | EasyLSTPipeline 10 步联动 | [core/pipeline.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/pipeline.py) |

### 1.2 当前内置 Skill 分组

| Group | Skill 名称 | 说明 |
|---|---|---|
| `data_process` | `data_acquisition`, `data_pipeline` | 数据获取与预处理 |
| `ttri_compute` | `ttri_compute` | 地形热响应指数计算 |
| `model_train_predict` | `rf_model` | **唯一**模型训练与预测 |
| `tcr_compute` | `tcr_compute` | 热约束残差修正 |
| `lst_export` | `lst_export` | LST 最终结果导出 |
| `accuracy_eval` | `accuracy_eval` | 空间一致性评估 |
| `ai_assistant` | `ai_assistant` | 纯 LLM 对话交互 |

> **关键约束**: `model_train_predict` 组目前只有 `rf_model` 一个 Skill，Agent 同组可替换的设计优势未发挥。

---

## 二、总体架构推演

```text
               ┌──────────────────────────────────────┐
               │     ModelScope Studio (Gradio + AntDX)│
               │  ┌─────────┐ ┌────────┐ ┌──────────┐ │
               │  │ 对话面板  │ │ 工作面板 │ │ 可视化面板 │ │
               │  │(AntDX)  │ │(Gradio)│ │(folium+) │ │
               │  └────┬────┘ └───┬────┘ └─────┬────┘ │
               └───────┼──────────┼────────────┼──────┘
                       │          │            │
               ┌───────▼──────────▼────────────▼──────┐
               │           Agent 引擎（不变）            │
               │   core/agent/geo_thermo_agent.py      │
               │   core/ai_assistant.py                │
               └────────────────┬─────────────────────┘
                                │
               ┌────────────────▼─────────────────────┐
               │        Skill 注册表（扩展）             │
               │   core/skills/skill_registry.py       │
               │   + New: xgboost, lightgbm, memory... │
               └───────┬──────────────────┬───────────┘
                       │                  │
               ┌───────▼──────┐  ┌────────▼──────────┐
               │ 核心算法（不变）│  │ 新增功能模块       │
               │ rf_model.py  │  │ xgboost_model.py  │
               │ ttri.py      │  │ memory_mgmt.py    │
               │ tcr.py       │  │ benchmark.py      │
               │ ...          │  │ report_gen.py     │
               └──────────────┘  └───────────────────┘
```

---

## 三、升级维度与技术栈

---

### 3.1 [P0] BenchMark 对比系统

**优先级最高**。当前 `model_train_predict` 组只有 RF 一个模型。Skill 组可替换的设计就是为多模型准备的，增加 2~3 个模型 Skill 就能把定位从"单算法工具"拉升到"算法对比平台"。

#### 可行方案

```text
model_train_predict 组（当前: [rf_model]）
                ↓
model_train_predict 组（扩展后: [rf_model, xgboost_model, lightgbm_model, mlp_model]）
```

新增 Skill 通过已有 Skill 模板自动生成，核心实现方式：

1. **XGBoost**: 在 `core/` 下创建 `xgboost_model.py`，调用 `xgboost` 库
2. **LightGBM**: 在 `core/` 下创建 `lightgbm_model.py`，调用 `lightgbm` 库
3. **MLP**: 在 `core/` 下创建 `mlp_model.py`，调用 `sklearn.neural_network.MLPRegressor`
4. **Benchmark Skill**（`benchmark`）: 遍历 `model_train_predict` 组所有 Skill，执行后收集指标，生成对比报告

#### Scheduled Output Schema（所有模型 Skill 一致）

```json
{
  "model_path": "xxx.pkl",
  "train_metrics": {"train": {"R2": 0.95}},
  "test_metrics": {"R2": 0.87, "RMSE": 1.23, "MAE": 0.98},
  "train_time_seconds": 45.2,
  "feature_importance": [{"feature": "NDVI", "importance": 0.32}, ...]
}
```

同组 Skill 输出格式一致，Benchmark Skill 可直接汇总对比。

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| XGBoost | `xgboost` | pip install xgboost，与现有依赖无冲突 |
| LightGBM | `lightgbm` | pip install lightgbm |
| MLP | `sklearn.neural_network` | 已有依赖，零额外安装 |
| Benchmark 报告 | `matplotlib` + `pandas` | 生成柱状图对比 R²/RMSE/Time |
| BenchMark Skill | 继承 `BaseSkill`，group=`benchmark` | 自动收集同组所有输出 |

#### 差异点

- 新增 4 个 Python 文件：`core/xgboost_model.py`、`core/lightgbm_model.py`、`core/mlp_model.py`、`core/skills/builtin/benchmark.py`
- 新依赖：`xgboost`, `lightgbm`, `matplotlib`
- ModelScope 环境需确认 GDAL + rasterio 系统依赖

#### 底层核心不受影响

`core/split_dataset.py`、`core/data_preprocessing.py`、`core/ttri.py`、`core/tcr.py`、`core/lst_final.py`、`core/export_geotiff.py`、`core/evaluation.py`——这些核心算法模块**全都不需要修改**。新增模型 Skill 只是调用 `train_random_forest` 级别的接口，换个回归器而已。

---

### 3.2 [P0] 记忆系统（三层架构）

#### 3.2.1 短期记忆（当前已有）

当前 `_conversation_messages` 和 `GeoThermoAgent._get_context()` 已经实现了**会话内的短期记忆**。Agent 能知道当前对话上下文、已加载的文件路径、用户配置参数。

**不需要改动**。

#### 3.2.2 会话记忆（新增）

跨对话持久化用户偏好、成功参数组合、常用研究区。

```python
# 新增 memory/memory_manager.py
class MemoryManager:
    def __init__(self, memory_dir: str = "./data/memory"):
        # memory_dir/
        #   ├── conversations_summary.json   # 每个对话的摘要
        #   ├── user_preferences.json        # 用户偏好（区域、模型、参数）
        #   └── experiments_log.json         # 实验记录（对比）
    
    def save_experiment(self, conv_id, region, model, params, metrics):
        """保存一次实验记录"""
    
    def get_best_params(self, region: str, model: str) -> dict:
        """检索该区域该模型历史上的最佳参数"""
    
    def get_recent_experiments(self, n: int = 5) -> list:
        """获取最近 n 次实验记录"""
```

**集成到 Agent**:

- 每次 `rf_model` 执行成功，自动调用 `memory_manager.save_experiment()`
- Agent 规划时检索该区域的过往实验，在 system prompt 中注入参考信息
- 用户问"上次北京的结果怎么样"，Agent 从记忆检索回答

```markdown
# 记忆注入到 Agent System Prompt 示例

## 历史实验参考（该地区）
- 2026-07-15 | 武汉 | RF | n_estimators=300 | R²=0.87
- 2026-07-16 | 武汉 | XGBoost | n_estimators=200 | R²=0.89
- 2026-07-16 | 北京 | RF | n_estimators=200 | R²=0.82

## 建议
本次处理北京数据，历史 R² 为 0.82。建议：
- 地形复杂（DEM 标准差 ≈ 150m），推荐 max_depth=35
- 上次 RF 在北京效果不如武汉，建议尝试 XGBoost
```

#### 3.2.3 知识记忆（RAG 检索增强生成）

建一个遥感领域的向量知识库（热红外、降尺度、卫星参数等），Agent 回答问题时检索相关知识增强回答质量。

**实现方案（轻量级，不依赖外部向量数据库）**:

```python
# 新增 memory/knowledge_base.py
class KnowledgeBase:
    def __init__(self):
        self.documents = [
            {"id": "ttri_formula", "content": "TTRI = a*DEM + b*Slope + c*cos(Aspect)", 
             "tags": ["ttri", "地形", "公式"]},
            {"id": "landsat_bands", "content": "Landsat 8: Band10=LWIR(100m), Band11=LWIR2(100m)...",
             "tags": ["landsat", "波段"]},
            {"id": "s2_bands", "content": "Sentinel-2: B2=Blue(10m), B3=Green(10m), B4=Red(10m)...",
             "tags": ["sentinel2", "波段"]},
        ]
    
    def search(self, query: str, n: int = 3) -> list:
        """简单的关键词匹配检索"""
        # 或用 embedding 做语义检索（可选，加 sentence-transformers）
```

**进阶方案**：
- 用 `sentence-transformers` 生成文档/查询嵌入
- 用 FAISS（无需 GPU）做向量检索
- 支持用户自定义知识库条目

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 会话记忆 | Python 原生 JSON 文件 | 无额外依赖 |
| 知识记忆（轻量） | 关键词/`fuzzywuzzy` 匹配 | pip install fuzzywuzzy |
| 知识记忆（进阶） | `sentence-transformers` + `faiss-cpu` | 约 500MB，ModelScope 可用 |
| RAG 集成 | 在 Agent system prompt 前插入检索到的知识 | 不影响现有架构 |

#### 差异点

- 新增 2~3 个 Python 文件：`core/memory/__init__.py`、`core/memory/memory_manager.py`、`core/memory/knowledge_base.py`（可选）
- 新增目录：`data/memory/`
- 新依赖（可选）：`sentence-transformers`, `faiss-cpu`

---

### 3.3 [P1] 反思循环（自动调参 + 自动重试）

当前 `_check_exceptions()` 只做**发现问题→报告**，升级为**发现问题→自动调整→重试→报告过程**。

#### 当前状态

```python
# geo_thermo_agent.py - 当前逻辑
def _check_exceptions(self, skill_name, result, ...):
    if skill_name == "rf_model":
        r2 = result.data.get("test_metrics", {}).get("R2", 1.0)
        if r2 < 0.6:  # 只报告，不处理
            _emit(f"  ⚠️ 模型 R²={r2:.2f}，精度较低")
```

#### 升级方案

```python
# 增强后逻辑（新增 _rerun_with_adjusted_params 方法）
def _check_exceptions(self, skill_name, result, ...):
    if skill_name == "rf_model":
        r2 = result.data.get("test_metrics", {}).get("R2", 1.0)
        if r2 < 0.6:
            # 自动调参重试（最多 3 次）
            adjusted_params = self._optimize_params(result.data, r2)
            result = self._rerun_skill(skill_name, adjusted_params)
            # 如果 r2 仍有改善空间但不足，告知用户
            if result.data.get("test_metrics", {}).get("R2", 0) > r2:
                _emit(f"  🔄 自动调参重试: R²={r2:.2f} → {result.data['test_metrics']['R2']:.2f}\n")
    elif skill_name == "data_pipeline":
        valid_pixels = result.data.get("train_rows", 0)
        if valid_pixels < 5000:
            # 样本不足，自动回到 data_acquisition 扩展搜索范围
            _emit("  🔄 训练样本不足，自动扩大时间范围重新搜索...\n")
```

可加入的反射场景：

| 触发条件 | 自动响应 | 最大重试次数 |
|---|---|---|
| R² < 0.6 | 增大 n_estimators / max_depth | 3 |
| 训练样本 < 5000 | 扩大时间范围重新下载 | -（提示用户） |
| 影像配对为 0 | 放宽云量阈值（30%→50%）重新搜索 | 2 |
| DEM 范围异常 | 切换 DEM 源（Copernicus → SRTM） | 1 |

#### 技术栈

- 纯 Python 逻辑扩展
- 在 `geo_thermo_agent.py` 中新增 `_rerun_skill()` 方法
- 对现有架构零侵入

#### 差异点

- 仅修改 1 个文件：`core/agent/geo_thermo_agent.py`
- 新增方法 `_rerun_skill()`, `_optimize_params()`
- 无新增依赖

---

### 3.4 [P1] 交互式地图可视化

当前 LST 结果以静态缩略图展示（`get_lst_result()` → base64 JPEG）。升级为交互式 Leafmap/Folium 地图。

#### 当前状态

```python
# ui/api.py - 当前
def get_lst_result(self, conv_id):
    # 读取 GeoTIFF → 生成 300px 缩略图 JPEG → base64
    # 前端 <img> 显示静态图片
```

#### 升级方案

```python
# 新增: 将 GeoTIFF 输出为交互式 HTML 地图
def generate_interactive_map(self, conv_id) -> str:
    """生成交互式地图的 HTML 字符串"""
    import folium
    from folium.raster_layers import ImageOverlay
    import rasterio
    import numpy as np
    
    # 读取 GeoTIFF
    with rasterio.open(tif_path) as src:
        band = src.read(1)
        bounds = src.bounds
    
    # 创建地图
    m = folium.Map(location=[center_lat, center_lon], zoom_start=12)
    
    # 添加 LST 结果（colormap: RdYlBu_r, opacity=0.7）
    ImageOverlay(
        image=colored_band,
        bounds=[[bounds.bottom, bounds.left], [bounds.top, bounds.right]],
        colormap=cm.RdYlBu_r,
        opacity=0.7,
    ).add_to(m)
    
    # 添加底图
    folium.TileLayer('OpenStreetMap').add_to(m)
    
    return m._repr_html_()
```

#### 交互式地图 vs 静态缩略图对比

| 特性 | 当前静态缩略图 | 升级后交互式地图 |
|---|---|---|
| 缩放/平移 | 不支持 | 支持 |
| 点击取值 | 不支持 | 支持 |
| 底图叠加 | 不支持 | 多底图切换 |
| 文件大小 | ~50KB | ~500KB（可接受） |
| 加载速度 | 快 | 中等（按需加载 tile） |

#### Gradio 集成

```python
# Gradio 中显示
with gr.Blocks() as demo:
    map_html = gr.HTML()  # 显示 folium 生成的 HTML
    
    def update_map(conv_id):
        html = api.generate_interactive_map(conv_id)
        return html
    
    refresh_btn.click(update_map, inputs=[conv_id], outputs=[map_html])
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 交互式地图 | `folium` / `leafmap` | pip install folium |
| 色带渲染 | `matplotlib.cm` | 已有依赖 |
| 点取值 | `folium.ClickForMarker` + JS callback | folium 内置 |

#### 差异点

- 新增文件：`core/visualization.py`（400-600 行代码，集中可视化逻辑）
- 新依赖：`folium`
- ModelScope 中依赖网络（tile 加载），需要稳定的互联网

---

### 3.5 [P2] 拓展遥感任务 Skill

目前只做 LST 降尺度。新增后处理分析 Skill 拓展应用场景。

#### 新增内置 Skill

| Skill 名称 | Group | 输入 | 输出 | 代码量 |
|---|---|---|---|---|
| `uhi_analysis` | `post_analysis` | LST GeoTIFF + 城市边界 | 热岛强度等级图、统计报告 | ~200 行 |
| `time_series` | `post_analysis` | 多期 LST 结果 | 温度变化趋势图、各期差异 | ~300 行 |
| `eco_response` | `post_analysis` | LST + NDVI/LULC | LST-NDVI 关系散点图、生态热等级 | ~200 行 |

#### UHI 分析示例

```python
class UHIAnalysisSkill(BaseSkill):
    @property
    def name(self): return "uhi_analysis"
    @property
    def group(self): return "post_analysis"
    
    def execute(self, params, ...):
        # 1. 加载 LST GeoTIFF
        # 2. 计算热岛强度（城区均值 - 郊区均值）
        # 3. 划分热岛等级（SUHI: 0~5 级）
        # 4. 输出热岛等级 GeoTIFF + 统计报告
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 热岛分析 | numpy + rasterio | 已有依赖 |
| 时间序列 | matplotlib | 新增依赖 |
| 生态响应 | scipy.stats | 已有依赖 |

#### 差异点

- 新增 3~4 个 Python 文件：`core/skills/builtin/uhi_analysis.py` 等
- 新依赖：`matplotlib`（用于生成 XY 散点图、趋势图）
- 核心算法模块**无需修改**

---

### 3.6 [P2] 一键体验模式 + 可复现报告

#### 3.6.1 一键体验

```python
# 新增 demo_mode.py
class DemoMode:
    """预置武汉 2024年7月 demo，用户打开即用"""
    
    PREBUILT_DATA_DIR = "./demo/wuhan_202407/"
    
    def is_available(self) -> bool:
        """检查预置数据是否存在"""
        return os.path.isdir(self.PREBUILT_DATA_DIR)
    
    def launch(self):
        """自动加载预置数据，跳过下载步骤"""
        # 直接使用预置的 raw/ + processed/ + results/ 数据
```

**ModelScope Studio 中的价值**:
- 评审打开链接即可看到完整结果（不依赖 API Key / 网络下载）
- 点"一键体验"按钮，15 秒内看到从数据到产品的完整流程
- 之后再注册深层功能（上传自己的数据、修改参数）

#### 3.6.2 可复现报告

每次完整执行后自动生成一份 Markdown 报告：

```markdown
# GeoThermoAI 实验报告

## 实验信息
- 时间: 2026-07-17 14:30:22
- 区域: 武汉（bbox: 113.7,29.9,114.9,31.3）
- 日期: 2024-07-01 ~ 2024-07-31

## 数据概况
- Landsat 4 景（L8, 云量 15%）, Sentinel-2 3 景（云量 12%）
- 训练样本: 45,678 | 验证样本: 15,234 | 测试样本: 15,232

## 模型性能
| 模型 | R² (test) | RMSE (K) | MAE (K) | 训练耗时 |
|------|-----------|---------|---------|---------|
| RF   | 0.8745   | 1.2345 | 0.9876 | 45.2s  |

## 空间一致性
- MB: -0.0234, MAE: 0.9876, RMSE: 1.2345
- 值域偏差: 0.4567K (通过, 阈值<5K)

## 生成文件
- [LST 结果 GeoTIFF](./results/rf_10m_lst_final.tif)
- [模型权重](./results/train/rf_ttri_model_run001.pkl)

## 一键复现
\`\`\`bash
# 使用预置的研究区文件和设定参数
python main.py --region ./demo/wuhan_202407/study_area.geojson
\`\`\`
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 报告生成 | Jinja2 模板 + Markdown | pip install Jinja2 |
| 一键复现 | 预置数据 + Agent 快捷入口 | 数据打包约 50MB |

#### 差异点

- 新增文件：`core/report_generator.py`（~200 行）
- 新依赖：`Jinja2`（轻量模板引擎）

---

### 3.7 [P3] 第三方 Skill 开发者文档 & 模板生成

#### 当前第三方 Skill 加载机制

```python
# skill_registry.py - 已有
def load_third_party_skills(self, skills_dir: str = "skills"):
    """动态加载第三方 Skill 包"""
    # 扫描 skills/ 目录，动态导入 BaseSkill 子类
```

这个机制是**完整且可工作的**，但缺少：

1. **Skill 模板生成器**: 用户输入"我想做一个基于 UNet 的地表温度降尺度模型"，Agent 自动生成 Skill 骨架
2. **Skill 开发指南**: 当前只有 `skills/README.md`，可以补充示例和最佳实践

#### Skill 模板生成器

```python
# 新增 skill_template_generator.py
class SkillTemplateGenerator:
    def generate(self, name: str, group: str, description: str, params: list) -> dict:
        """生成 Skill 骨架代码"""
        return {
            "__init__.py": self._init_py(name, group),
            "main.py": self._main_py(name, description, params),
            "requirements.txt": self._requirements(),
        }
```

#### 技术栈

- 纯 Python 字符串模板
- 嵌入到 Agent 中（Agent 调用 Skill 生成器 Skill）

#### 差异点

- 新增文件：`core/skill_template_generator.py`（~150 行）
- 对现有架构零侵入

---

## 四、迁移到 ModelScope Studio 的 UI 层改造

### 4.1 总体 UI 架构

```text
Gradio Blocks
├── gr.Row (header + logo)
├── gr.Row (main content)
│   ├── gr.Column (sidebar - 对话列表, 30%)
│   │   ├── gr.Button("新对话")
│   │   └── gr.Dataframe (对话历史列表)
│   ├── gr.Column (对话面板, 40%)
│   │   ├── antdx.Bubble.List (对话气泡)
│   │   ├── antdx.Sender (输入框)
│   │   └── gr.Row (按钮: 上传文件, 选择文件夹)
│   └── gr.Column (可视化面板, 30%)
│       ├── gr.Tabs
│       │   ├── Tab: LST 结果 (folium 地图)
│       │   ├── Tab: 精度评估 (指标表格)
│       │   ├── Tab: 实验对比 (Benchmark)
│       │   ├── Tab: 参数设置 (Gradio 表单)
│       │   └── Tab: 工作流进度条
│       └── gr.Markdown (实验报告预览)
└── gr.Row (footer - 状态信息)
```

### 4.2 Key Gradio 组件选择

| 当前 PyWebView 实现 | Gradio 替代 | 说明 |
|---|---|---|
| 原生 HTML/CSS 对话 | `antdx.Bubble` + `antdx.Sender` | Ant Design X 对话组件 |
| 自定义 sidebar | `gr.Dataframe` + `gr.Button` | 对话列表 |
| 工作面板 tabs | `gr.Tabs` | 原生 Tab 组件 |
| GeoTIFF 静态图 | `gr.HTML` + `folium` | 交互式地图 |
| 进度条 | `gr.Progress` + `gr.JSON` | 工作流状态 |
| 参数表单 | `gr.Number` / `gr.Slider` / `gr.Dropdown` | 动态渲染 hyperparameters |
| 精度结果表格 | `gr.Dataframe` | 指标展示 |
| 文件上传 | `gr.File` | 研究区上传 |
| 文件夹选择 | `gr.Textbox` + `gr.Button("浏览")` | ModelScope 文件系统 |
| API Key 输入 | `gr.Textbox(type="password")` | 安全输入 |
| 流式输出 | generator `yield` | Gradio 原生支持 |

### 4.3 Gradio 中的流式改造

当前 PyWebView 的实现是前端轮询 `chat_stream_poll`。Gradio 原生支持 generator，可以简化：

```python
# 当前方案 (PyWebView): 前端 JS 轮询
# 升级方案 (Gradio): generator yield
def chat_fn(message, history):
    """Gradio chat function with streaming"""
    content = ""
    for token in agent.process_command_stream(message):
        content += token
        yield content
```

```python
# Gradio ChatInterface 使用
gr.ChatInterface(
    fn=chat_fn,
    type="messages",
    multimodal=True,
)
```

---

## 五、数据处理现有机制分析

### 5.1 `data_acquisition` 的数据流

```text
用户上传研究区 GeoJSON
       │
       ▼
搜索 Landsat (c2-l2) + Sentinel-2 (l2a) STAC
       │
       ▼
配对构建（Landsat 日期 / 卫星 + Sentinel 日期，时间差 ≤ 2天）
       │
       ▼
用户选择 → 下载 COG → gdal.Warp(mosaic + UTM + clip) → 5 个 GeoTIFF
       │
       ▼
raw/landsat_lst.tif, raw/sentinel2_bands.tif, raw/dem.tif, ...
```

### 5.2 `data_pipeline` 的处理流

```text
raw/xx.tif
   ↓
对齐到 10m / 30m
   ↓
QA/SCL 掩膜 + 光谱指数(NDVI, NDWI, NDBI)
   ↓
重采样到 30m + 合并 DEM 特征
   ↓
processed/30m_features_step2.csv (训练数据)
processed/10m_predict_features.csv (预测数据)
   ↓
split_dataset → train.csv / validate.csv / test.csv
```

### 5.3 `pipeline.py` (EasyLSTPipeline) 的联动

这是**不通过 Agent 的直接流水线**，10 步完全编排。

```python
steps = [
    preprocessing → split_dataset → ttri_train → train_rf → 
    predict_test → ttri_predict → tcr → lst_final → 
    export_geotiff → evaluation
]
```

Agent 的 `_build_full_workflow_plan()` 与这个对应但策略不同：
- Agent 用 SKILL_PATHS 硬编码路径（更鲁棒）
- Pipeline 用 `get_default_paths()` 动态推导路径（更灵活）

**迁移时注意**: 这两个路径机制需要统一或至少保持兼容。

### 5.4 路径硬编码机制

`geo_thermo_agent.py` 中的 `SKILL_PATHS` 和 `_normalize_plan_paths()` 是一个值得保留的优点：

```python
SKILL_PATHS = {
    "data_acquisition": {"output_dir": raw_dir},
    "data_pipeline": {"output_dir": processed_dir, "landsat_path": raw_dir + "/landsat_lst.tif", ...},
    "rf_model": {"output_dir": results_dir, ...},
    # ...
}
```

所有路径基于 `project_dir` 计算，Agent 执行时强制覆盖 LLM 生成的不一致路径。这是**防止 LLM 幻觉导致路径错误的关键保障**，迁移时需要保留。

---

## 六、升级综合对照表

| 优先级 | 升级项 | 新增文件数 | 修改文件数 | 新增依赖 | 代码量估算 | 核心算法影响 | 评审加分 |
|---|---|---|---|---|---|---|---|
| **P0** | Benchmark 对比系统 | 4 | 1 | xgboost, lightgbm, matplotlib | ~800 行 | 无 | **极高** |
| **P0** | 记忆系统（会话+知识） | 3 | 1 | optional: sentence-transformers + faiss | ~400 行 | 无 | **高** |
| **P1** | 反思循环（自动调参重试） | 0 | 1 | 无 | ~150 行 | 无 | **高** |
| **P1** | 交互式地图可视化 | 1 | 0 | folium | ~500 行 | 无 | 中 |
| **P2** | 拓展遥感任务 Skill (UHI等) | 3 | 0 | matplotlib | ~700 行 | 无 | 中 |
| **P2** | 一键体验 + 可复现报告 | 2 | 0 | Jinja2 | ~300 行 | 无 | 中 |
| **P3** | Skill 模板生成器 | 1 | 0 | 无 | ~150 行 | 无 | 低（面向社区） |

### 核心算法不动

以下文件**完全不受影响**，可以原封不动迁移到 ModelScope：

- [core/data_preprocessing.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/data_preprocessing.py)
- [core/split_dataset.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/split_dataset.py)
- [core/rf_model.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/rf_model.py)
- [core/ttri.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/ttri.py)
- [core/tcr.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/tcr.py)
- [core/lst_final.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/lst_final.py)
- [core/export_geotiff.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/export_geotiff.py)
- [core/evaluation.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/evaluation.py)

### 需要适配的模块

这些模块需要适配迁移到 Gradio：

| 文件 | 改动 | 难度 |
|---|---|---|
| [main.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/main.py) | 替换 `webview.create_window` 为 `gr.Blocks().launch()` | 高 |
| [ui/api.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/ui/api.py) | 保留核心逻辑，适配 Gradio 事件处理 | 中 |
| [ui/index.html](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/ui/index.html) | 废弃，改为 Gradio 组件 | 高（工作量） |
| [ui/scripts/](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/ui/scripts/) | 废弃，JS 逻辑改为 Python + Gradio 事件 | 中 |

---

## 七、ModelScope Studio 特有的环境适配

### 7.1 关键要求和约束

| 项目 | 要求/约束 |
|---|---|
| Gradio 版本 | `>= 6.0.0` 且 `<= 6.8.0` |
| 应用类型 | Gradio Blocks / Ant Design X |
| 运行环境 | ModelScope 容器（Ubuntu + Python 3.10+） |
| 系统依赖 | 可通过 `packages.txt` 安装 apt 包 |
| Python 依赖 | 通过 `requirements.txt` 安装 |
| 网络 | 可访问外网（Planetary Computer API 可连通） |
| 存储 | 持久化空间有限，大文件需要注意清理 |

### 7.2 需要处理的系统依赖

当前依赖中，以下库需要系统级依赖：

| Python 库 | 系统依赖 | ModelScope 兼容性 |
|---|---|---|
| `rasterio` | `libgdal` | 可通过 `packages.txt` 安装 `gdal`（已验证） |
| `geopandas` | `libgdal`, `libproj` | 同上 |
| `osgeo` | GDAL C++ 库 | 同上 |

```text
# packages.txt 示例
gdal-bin
libgdal-dev
libproj-dev
```

### 7.3 API Key 安全

当前 `settings.json` 可能硬编码 API Key。在 ModelScope 中必须移除，改为：

```python
# API Key 输入（ModelScope 安全做法）
api_key = gr.Textbox(
    label="API Key",
    type="password",
    placeholder="输入你的 API Key（不会持久化）",
)
```

---

## 八、推荐实施路线

### Phase 1: 最小可行迁移（~1-2周）

```
核心算法全复用 → Gradio UI 骨架 → 一键全流程 → 验证通过
```

最小 MVP 只输出一个能跑完整 7 步流程的 Gradio 应用，不做任何高级功能。

### Phase 2: 差异化功能（~1-2周）

```
Benchmark 对比系统 + 记忆系统 + 反思循环
```

这一步让项目脱离"另一个 demo"的层次。

### Phase 3: 拔高和打磨（~1周）

```
交互式地图 + 一键体验 + 可复现报告 + UI 打磨
```

---

## 九、风险与注意事项

1. **Planetary Computer 网络**：ModelScope 在中国大陆部署可能需要科学上网或备用数据源
2. **内存限制**：大景影像处理可能超限，需要分块处理（已有 batch_size/chunk_size 机制）
3. **无 LLM API Key**：建议预置 DeepSeek 免费模型配置，降低用户门槛
4. **路径一致性**：Agent 的 SKILL_PATHS 硬编码和 Pipeline 的动态推导需要统一
5. **Agent 的 system prompt**：需要更新，移除 PyWebView 相关描述，加入 Gradio 交互上下文
