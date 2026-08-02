# GeoThermoAI 产品级 AI 遥感软件升级路线图

> **目标**: 将 GeoThermoAI 从"一个算法的桌面工具"升级为**面向科研与生产的专业级遥感 AI 软件平台**。
>
> 本文档不局限于比赛，而是从工程化、数据能力、AI 能力、产品体验、生态建设五个维度全面规划。
>
> 基于 `d:\Files\研究和项目\10.GeoThermoAI\GeoThermoAI` 源代码深度分析编写。

---

## 一、现状能力与短板

### 1.1 当前做得好的（保持）

| 维度 | 现状 | 评价 |
|---|---|---|
| **AI Agent 编排** | LLM → JSON 计划 → Skill 自动执行 | 核心架构优秀，产品级设计 |
| **Skill 插件系统** | 注册表 + 同组可替换 + 自动加载 | 开源生态基础已具备 |
| **自动调参** | LLM 根据数据特征推荐参数 | 有雏形，可强化 |
| **路径硬编码防御** | SKILL_PATHS 覆盖 LLM 幻觉路径 | 值得保留的工程智慧 |
| **分块处理** | batch_size/chunk_size 机制 | 大影像处理基础 |
| **多模型 API 支持** | DeepSeek / Kimi / OpenAI / Anthropic | 灵活 |

### 1.2 当前短板（需升级）

| 维度 | 短板 | 严重程度 |
|---|---|---|
| **工程化** | 无测试、无日志系统、无错误追踪、无配置校验 | 严重 |
| **数据能力** | 仅支持 Planetary Computer，不支持本地数据/其他云 | 严重 |
| **AI 能力** | 只有 RF、无 Deep Learning、无 GPU 支持 | 严重 |
| **产品 UX** | PyWebView 桌面、无批量处理、无任务队列 | 中等 |
| **科学严谨** | 单一验证策略、无模型对比工具、无不确定性量化 | 中等 |
| **可扩展性** | 技能模板有但无在线市场、无 API 接口 | 中等 |
| **部署交付** | 仅桌面、无 Web/云部署方案 | 高 |

---

## 二、总体架构演进路线

```text
                  ┌─────────────────────────────────────────────┐
                  │         GeoThermoAI Platform v2              │
                  │                                              │
Layer 5: 交付层   │  Desktop CLI  Web App  API Server  Docker   │
                  ├─────────────────────────────────────────────┤
Layer 4: 应用层   │  Agent Engine  +  Web UI  +  REST API       │
                  ├─────────────────────────────────────────────┤
Layer 3: 服务层   │  Task Queue  Cache  Logging  Auth  Monitor  │
                  ├─────────────────────────────────────────────┤
Layer 2: 核心层   │  Skill Registry  +  Core Algorithms         │
                  ├─────────────────────────────────────────────┤
Layer 1: 数据层   │  Raster Store  Vector Store  Model Store    │
                  └─────────────────────────────────────────────┘
```

---

## 三、升级维度详情

---

### 3.1 [CRITICAL] 工程化基础

#### 3.1.1 单元测试体系

当前**零测试**。这是走向产品化的第一道坎。

```text
tests/
├── unit/
│   ├── test_rf_model.py        # RF 训练/预测逻辑
│   ├── test_ttri.py            # TTRI 计算逻辑
│   ├── test_tcr.py             # TCR 计算逻辑
│   ├── test_evaluation.py      # 评估逻辑
│   ├── test_skill_registry.py  # Skill 注册/查找
│   └── test_agent_plan.py      # Agent 解析/规划
├── integration/
│   ├── test_full_pipeline.py   # 端到端流水线（用小数据）
│   └── test_agent_workflow.py  # Agent 全流程编排
└── fixtures/
    ├── small_dem.tif           # 测试用小影像
    ├── sample_30m.csv          # 30m 样本数据
    └── sample_10m.csv          # 10m 样本数据
```

**关键**：小数据集成测试（10x10 像素的 DEM + LST），确保 CI 中几秒跑完。

#### 3.1.2 结构化日志系统

当前用 `log_callback` + `print`，无法生产使用。

```python
# 新增 core/logging_config.py
import logging
import structlog  # 结构化的 Python 日志

def setup_logging(log_dir: str = "./logs", level: str = "INFO"):
    """
    日志架构:
    - 控制台: 彩色、人类可读
    - 文件: JSON 格式、机器可解析  
    - 每个工作流 session 独立日志文件
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()  # 开发时
            # structlog.processors.JSONRenderer()  # 生产时
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
```

```text
logs/
├── workflow_20260717_143022.jsonl   # JSON Lines: 每步执行的完整记录
├── errors.log                        # ERROR 级别汇总
└── access.log                        # API 调用记录
```

#### 3.1.3 配置校验与热加载

当前 `settings.json` 无校验，字段不匹配只有在运行时才能发现。

```python
# 新增 core/config.py
from pydantic import BaseModel, Field, validator

class DataSourceConfig(BaseModel):
    source: str = Field("planetary_computer", pattern="^(planetary_computer|local|aws|azure)$")
    cloud_threshold: int = Field(30, ge=0, le=100)
    dem_source: str = Field("copernicus", pattern="^(copernicus|srtm)$")

class ModelConfig(BaseModel):
    n_estimators: int = Field(200, ge=50, le=1000)
    max_depth: int = Field(25, ge=5, le=50)
    # ...

class APIConfig(BaseModel):
    api_key: str = Field("", min_length=0)
    api_base_url: str = Field("")
    
class AppConfig(BaseModel):
    data: DataSourceConfig
    model: ModelConfig
    api: APIConfig
    processing: ProcessingConfig
    
    @classmethod
    def from_file(cls, path: str) -> "AppConfig":
        """从 JSON 加载并校验"""
        with open(path) as f:
            data = json.load(f)
        return cls(**data)
```

#### 3.1.4 错误处理与异常追踪

当前 `try/except` 散落各处，错误信息不统一。

```python
# 新增 core/exceptions.py
class GeoThermoError(Exception):
    """基础异常"""
    code: str = "UNKNOWN_ERROR"
    
class DataSourceError(GeoThermoError):
    code = "DATA_SOURCE_ERROR"
    
class ModelTrainError(GeoThermoError):
    code = "MODEL_TRAIN_ERROR"
    
class SkillExecutionError(GeoThermoError):
    code = "SKILL_EXECUTION_ERROR"
```

```python
# 统一使用 sentry 或类似服务追踪错误
# core/monitoring.py
import sentry_sdk
sentry_sdk.init(
    dsn=SENTRY_DSN,  # 可选，开源项目可配置
    traces_sample_rate=0.1,
)
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 测试框架 | `pytest` + `pytest-cov` | 零额外心智负担 |
| 日志 | `structlog` + `logging` | JSON 格式，可搜索 |
| 配置校验 | `pydantic` v2 | 类型安全、自动校验 |
| 错误追踪 | `sentry-sdk` (可选) | 开源版免费 |

**估计代码量**: ~600 行（4 个新文件）

---

### 3.2 [CRITICAL] 多数据源支持

当前只支持 Microsoft Planetary Computer 一个数据源，限制了用户覆盖面和可用性。

#### 3.2.1 支持本地数据导入

```python
# 新增 core/data_source/local_source.py
class LocalDataSource:
    """本地数据源: 用户上传自己的 GeoTIFF"""
    
    SUPPORTED_FORMATS = [".tif", ".tiff", ".img", ".hdf", ".nc"]
    
    def validate(self, files: List[str]) -> Dict:
        """验证文件完整性: 空间参考、范围一致性、波段匹配"""
        
    def ingest(self, files: List[str], output_dir: str) -> Dict:
        """导入并标准化为 raw/ 目录结构"""
```

#### 3.2.2 支持主流云数据源

```python
# 新增 core/data_source/
#   ├── base.py          # 数据源基类
#   ├── planetary_computer.py  # 已有
#   ├── earth_search.py        # Element 84 Earth Search
#   ├── stac_index.py          # 通用 STAC API
#   └── local_source.py        # 本地文件
```

| 数据源 | 地址/说明 | 优势 |
|---|---|---|
| Planetary Computer | `planetarycomputer.microsoft.com` | 已有，免费 |
| Earth Search | `earth-search.aws.element84.com` | 更大数据量 |
| Copernicus Data Space | `dataspace.copernicus.eu` | 直接访问 Sentinel |
| USGS Earth Explorer | `earthexplorer.usgs.gov` | Landsat 原生 |
| AWS Open Data | `registry.opendata.aws` | STAC 格式 |

#### 3.2.3 栅格存储引擎

当前用 GDAL GeoTIFF 直接读写。大数据量时效率不高。

```python
# 新增 core/storage/engine.py
class RasterStore:
    """抽象栅格存储后端"""
    
    def read(self, bounds, resolution) -> np.ndarray:
        ...
        
    def write(self, data, bounds, crs):
        ...

class GeoTIFFStore(RasterStore):
    """本地 GeoTIFF 后端（当前方案）"""
    
class COGStore(RasterStore):
    """Cloud Optimized GeoTIFF 后端（推荐升级）"""
    
class ZarrStore(RasterStore):
    """Zarr 后端（超大规模数据）"""
```

COG 对比传统 GeoTIFF:
- 支持 HTTP Range Requests（不下载整个文件）
- 支持多分辨率预览（overviews 内嵌）
- 云原生，适合 Web 部署

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| STAC 客户端 | `pystac-client` | 已有 |
| 栅格存储 | `rasterio` + `rioxarray` | 已有 |
| COG | GDAL COG 驱动 | 已有（只需改创建选项） |
| 云存储 | `boto3` (AWS) / `adlfs` (Azure) | 按需 |
| 并行下载 | `concurrent.futures` + `aiohttp` | 已有 ThreadPool |

**估计代码量**: ~1500 行（5~8 个新文件）

---

### 3.3 [CRITICAL] AI/ML 能力深度升级

#### 3.3.1 深度学习模型支持

当前只有 RF，无法利用高分辨率遥感的空间特征。

```text
新增 core/deep_learning/
├── models/
│   ├── unet_lst.py        # UNet 降尺度
│   ├── resunet_lst.py     # ResUNet 降尺度
│   └── gan_lst.py         # 对抗生成降尺度
├── trainer.py             # 统一训练器（支持 GPU）
├── dataset.py             # 遥感数据集加载器
└── augmentations.py       # 数据增强
```

```python
# core/deep_learning/models/unet_lst.py
import torch
import torch.nn as nn

class UNetLST(nn.Module):
    """将 10m 多光谱 + DEM 特征 → 10m LST
    输入: [B, C=9, H, W] (R,G,B,NIR,SWIR1,NDVI,NDWI,NDBI,DEM)
    输出: [B, 1, H, W]
    """
    def __init__(self, in_channels=9):
        super().__init__()
        # 标准 UNet 结构
        
    def forward(self, x):
        return self.model(x)
```

#### 3.3.2 Run Configuration 管理系统

```python
# 新增 core/model_registry.py
class ModelRegistry:
    """模型版本管理"""
    
    def __init__(self, models_dir: str = "./models"):
        # models/
        #   ├── rf/
        #   │   ├── v1_20260715.pkl
        #   │   ├── v2_20260716.pkl
        #   │   └── metadata.json
        #   ├── xgboost/
        #   └── unet/
    
    def save(self, model, name: str, version: str, metrics: dict):
        """保存模型, 记录版本、精度、训练参数"""
    
    def load(self, name: str, version: str = "latest"):
        """加载指定版本"""
    
    def compare(self, runs: List[str]) -> DataFrame:
        """对比多个训练的指标"""
```

#### 3.3.3 GPU 支持策略

```python
# core/utils/device.py
import torch

def get_device() -> str:
    """自动检测最优计算设备"""
    if torch.cuda.is_available():
        return f"cuda:{torch.cuda.current_device()}"
    try:
        import torch_directml
        return "dml"  # DirectML (Windows AMD/Intel)
    except ImportError:
        pass
    return "cpu"
```

#### 3.3.4 模型精度智能诊断

当前 Agent 只能报告 R² 高低，升级为可解释性诊断：

```python
class ModelDiagnostics:
    """模型精度诊断"""
    
    def analyze_errors(self, predictions, ground_truth, features) -> Dict:
        """
        返回:
        - 哪些特征导致最大误差？
        - 误差的空间分布（边缘 vs 中心）？
        - 误差与地形/植被的关系？
        - 建议: 增加什么特征/调整什么参数？
        """
```

```text
诊断报告示例:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 模型精度诊断报告
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
整体: R²=0.78 → 良好但没有完美

📌 主要误差来源（按特征）
1. DEM > 200m 区域: MAE=2.3K（整体 1.2K）
   → 建议: 复杂地形加大 max_depth

2. NDVI < 0.1（裸土/城市）: 预测偏低 1.5K
   → 建议: 加入地表覆盖类型特征

📌 空间分布
- 误差集中在研究区东南角（水体边缘）
- 可能原因: Sentinel-2 SCL 对水陆边界分类不准
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 深度学习框架 | PyTorch (首选) / TensorFlow | CUDA 12+ |
| 遥感 DL 工具 | `torchgeo` | 遥感专用的 torch 扩展 |
| 模型对比 | MLflow / 自建 Registry | 实验追踪 |
| GPU 加速 | CUDA + `torch.compile` + TensorRT | 推理加速 2-5x |
| 可解释性 | SHAP / LIME | 基于梯度的特征归因 |

**估计代码量**: ~3000 行（5~10 个新文件）

---

### 3.4 [HIGH] 任务管理与并行计算

当前所有工作流在主线程中同步执行，用户不能：
- 同时跑多个工作流
- 查看历史任务状态
- 在多台机器上分布式执行

#### 3.4.1 异步任务队列

```python
# 新增 core/workflow/task_queue.py
class TaskQueue:
    """异步任务队列"""
    
    DASK_SCHEDULER = "distributed"  # 分布式
    LOCAL_THREAD = "thread"         # 本机多线程
    
    def submit(self, workflow: str, params: dict) -> str:
        """提交一个工作流，返回 task_id"""
        task_id = uuid.uuid4().hex
        
        # 持久化任务状态
        self._save_task({
            "id": task_id,
            "status": "pending",
            "workflow": workflow,
            "params": params,
            "created_at": now(),
        })
        
        # 提交到执行线程
        self._executor.submit(self._run_task, task_id)
        return task_id
    
    def get_status(self, task_id: str) -> dict:
        """获取任务状态"""
        
    def cancel(self, task_id: str) -> bool:
        """取消任务"""
        
    def list_tasks(self, status: str = None) -> list:
        """列出所有任务"""
```

#### 3.4.2 分布式计算支持（面向大区域）

```python
# core/workflow/dask_executor.py
import dask.dataframe as dd
import dask.array as da

class DaskExecutor:
    """用 Dask 对超大区域做分块并行处理"""
    
    def process_large_area(self, geojson_path: str, tile_size: tuple = (1000, 1000)):
        """
        1. 将研究区划分为 1000x1000 像素的块
        2. 每块提交为独立 task
        3. 所有块结果合并
        """
```

对于遥感大面积处理，Dask 是业界标准方案，与 rasterio/xarray 原生集成。

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 任务队列 | Redis + Celery / RQ | 传统方案（需 Redis） |
| 轻量任务队列 | `arq` / `dramatiq` | 无需 Redis，纯 Python |
| 分布式计算 | `dask` + `dask-image` | 遥感标准方案 |
| 任务状态 DB | SQLite / PostgreSQL | 按并发规模选择 |

**估计代码量**: ~2000 行（3~5 个新文件）

---

### 3.5 [HIGH] 数据管理与版本控制

#### 3.5.1 结果管理

当前每次跑完结果直接覆盖 `results/`。升级为：

```text
project_dir/
├── experiments/
│   ├── exp_001_20260715/
│   │   ├── config.json              # 本次运行的所有参数
│   │   ├── results/
│   │   │   ├── rf_10m_lst_final.tif
│   │   │   └── spatial_consistency.json
│   │   ├── artifacts/
│   │   │   ├── rf_model.pkl
│   │   │   └── feature_importance.csv
│   │   └── report.md
│   ├── exp_002_20260716/  (不同参数/模型)
│   └── experiments_index.json       # 所有实验索引
├── raw/
├── processed/
└── results/  (链接到最新实验)
```

```python
# 新增 core/experiment.py
class ExperimentManager:
    """实验管理"""
    
    def create_experiment(self, config: dict) -> str:
        """创建新实验，生成 exp_id"""
        
    def get_experiment(self, exp_id: str) -> dict:
        """加载实验信息"""
        
    def compare_experiments(self, exp_ids: List[str]) -> DataFrame:
        """对比多个实验"""
        
    def export_report(self, exp_id: str, format: str = "markdown") -> str:
        """生成实验报告"""
```

#### 3.5.2 DVC（Data Version Control）

对于科研可复现性，Git 管理代码但无法管理数据。DVC 解决了这个问题。

```bash
# 用 DVC 管理遥感数据
dvc init
dvc add data/raw/landsat_lst.tif
dvc run -n preprocessing \
    -d data/raw \
    -o data/processed/30m_features_step2.csv \
    -o data/processed/10m_predict_features.csv \
    python -m core.data_preprocessing
```

每个数据版本对应一个 Git commit，全文可复现。

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 实验管理 | 自建（JSON + 文件系统） | 轻量，无依赖 |
| 数据版本控制 | DVC（Data Version Control） | 业界标准 |
| 实验对比 | MLflow Tracking / Weights & Biases | 更专业但需服务 |

**估计代码量**: ~1200 行（3 个新文件）

---

### 3.6 [MEDIUM] 产品级 Web 前端

当前 PyWebView 桌面端有以下几个无法回避的问题：

1. **跨平台维护成本高**——每个平台都可能遇到不同的 PyWebView 兼容性问题
2. **无法远程使用**——必须是带显示器的桌面环境
3. **分发困难**——用户需要配置 Python 环境、安装 GDAL 等

#### 3.6.1 前后端分离架构

```text
┌──────────────┐     HTTP/REST     ┌─────────────────┐
│   Web 前端    │ ◄──────────────► │   Python 后端    │
│  React/Vue   │    WebSocket      │  FastAPI/Django  │
│              │     SSE           │                  │
│  - 对话界面   │                  │  - Agent 引擎    │
│  - 地图展示   │                  │  - Skill 执行    │
│  - 仪表盘    │                  │  - 任务队列      │
│  - 数据管理   │                  │  - 文件服务      │
└──────────────┘                   └─────────────────┘
```

#### 3.6.2 后端框架选择

```python
# 新增 server/app.py
from fastapi import FastAPI, UploadFile, BackgroundTasks
from fastapi.responses import StreamingResponse
import asyncio

app = FastAPI(title="GeoThermoAI API")

@app.post("/api/chat/stream")
async def chat_stream(message: str, conv_id: str):
    """SSE 流式对话"""
    async def event_generator():
        async for token in agent.process_command_stream_async(message):
            yield f"data: {json.dumps({'content': token})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/workflow/run")
async def run_workflow(params: dict, background_tasks: BackgroundTasks):
    """异步启动工作流"""
    task_id = task_queue.submit(params)
    background_tasks.add_task(task_queue.run, task_id)
    return {"task_id": task_id}

@app.get("/api/workflow/status/{task_id}")
async def get_workflow_status(task_id: str):
    return task_queue.get_status(task_id)

@app.post("/api/study-area/upload")
async def upload_study_area(file: UploadFile):
    """上传研究区文件"""
    # ...

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

#### 3.6.3 前端框架选择

| 框架 | 适用场景 | 说明 |
|---|---|---|
| React + Leaflet/MapLibre | 最专业、最灵活 | 前端生态丰富 |
| Vue + OpenLayers | 入门友好 | 中文文档完善 |
| Gradio | 快速原型 | 适合小规模、AI 展示场景 |
| Streamlit | 极快原型 | 数据应用场景 |

**推荐**: 核心功能用 FastAPI + React（生产级），快速原型用 Gradio（比赛/演示）。

#### 3.6.4 Docker 化部署

```dockerfile
# Dockerfile
FROM continuumio/miniconda3:latest

RUN apt-get update && apt-get install -y \
    gdal-bin libgdal-dev libproj-dev

COPY environment.yml .
RUN conda env create -f environment.yml

COPY . /app
WORKDIR /app

EXPOSE 8000

# 生产启动
CMD ["conda", "run", "-n", "geothermo", \
     "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml
version: '3.8'
services:
  api:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - LOG_LEVEL=INFO
      - SENTRY_DSN=${SENTRY_DSN}
  redis:
    image: redis:7
    ports:
      - "6379:6379"
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | 异步 + 高性能 |
| API 文档 | OpenAPI (自动生成) | FastAPI 内置 |
| 前端 | React + TypeScript | 生产级 |
| 地图前端 | MapLibre GL JS / Leaflet | 开源地图库 |
| 部署 | Docker + Docker Compose | 容器化 |
| 反向代理 | Nginx + Let's Encrypt | HTTPS + 域名 |
| CI/CD | GitHub Actions | 自动构建、测试、部署 |

**估计代码量**: ~5000 行（10~20 个新文件，含前端）

---

### 3.7 [MEDIUM] 专业遥感算法增强

#### 3.7.1 更多降尺度算法

当前只有 TTRI + TCR 这一种方法。加入对照方法：

| 算法 | 原理 | 代码位置 | 代码量 |
|---|---|---|---|
| MTVDI (已有 TTRI 类似) | 温度-植被干燥度指数 | 新 Skill | ~200 行 |
| TsHARP | 基于 NDVI 的尺度转换 | 新 Skill | ~200 行 |
| DisTrad | 热红外锐化（基于影像统计） | 新 Skill | ~300 行 |
| SFS (Spatial Frequency-based) | 空间频率信息融合 | 新 Skill | ~300 行 |
| Deep Downscaling | CNN/LSTM 超分辨率 | 新 DL 模块 | ~500 行 |

用户可以选择不同降尺度方法做结果对比。

#### 3.7.2 不确定性量化

```python
# core/uncertainty.py
class UncertaintyEstimator:
    """不确定性评估"""
    
    def quantile_regression(self, X, y, lower=0.05, upper=0.95):
        """分位数回归: 给出预测区间的上下界"""
        
    def dropout_mc(self, model, X, n_iterations=100):
        """Monte Carlo Dropout: 深度学习不确定度"""
        
    def ensemble_uncertainty(self, models: list, X):
        """集成模型不确定度: 多模型预测的方差"""
```

```text
结果输出示例:
像素 (100, 200):
  预测温度: 305.2K (32.1°C)
  不确定度: ±1.8K (95%置信区间)
  主要不确定源: 云影边缘、NDVI 低区域
```

#### 3.7.3 时间序列分析

```python
# core/skills/builtin/time_series.py
class TimeSeriesAnalysisSkill:
    """多时相 LST 时间序列分析"""
    
    def compute_trend(self, lst_files: list, dates: list) -> Dict:
        """计算每个像元温度变化趋势 (K/年)"""
        
    def detect_anomaly(self, lst_current, lst_historical) -> Dict:
        """检测温度异常"""
        
    def seasonal_decompose(self, lst_series, period=12) -> Dict:
        """季节分解: 趋势 + 季节 + 残差"""
```

#### 3.7.4 气象站点验证

```python
# core/skills/builtin/in_situ_validation.py
class InSituValidationSkill:
    """气象站实测数据验证
    
    流程:
    1. 用户上传气象站 CSV（站名、lon、lat、温度）
    2. 从 LST 结果中提取对应位置像元值
    3. 计算 RMSE / MAE / 偏差
    4. 输出验证报告
    """
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 降尺度算法 | numpy + scipy | 纯数学计算 |
| 不确定性 | `scipy.stats` + 自建 | 不需要额外依赖 |
| 时间序列 | `statsmodels` | 季节分解、趋势检验 |

**估计代码量**: ~2500 行（5~8 个新文件）

---

### 3.8 [MEDIUM] 性能优化

#### 3.8.1 算法性能瓶颈分析

当前代码的性能瓶颈（通过代码阅读分析）：

| 模块 | 瓶颈 | 影响 |
|---|---|---|
| `data_acquisition._fetch_asset()` | 逐景下载，串行 | 慢（大景 > 1GB） |
| `evaluation.evaluate_spatial_consistency()` | 逐批扫描全图 | O(n) 但大影像慢 |
| `ttri.py` / `tcr.py` | 逐像素计算 | CPU 密集 |
| GDAL Warp | 多线程但 I/O 受限 | 磁盘 I/O 瓶颈 |

#### 3.8.2 优化方案

**并行下载**:
```python
# 改进: 用 concurrent.futures 并行下载
from concurrent.futures import ThreadPoolExecutor, as_completed

def _fetch_assets_parallel(self, items, bands_list):
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for item in items:
            for band in bands_list:
                future = executor.submit(self._fetch_asset, item, band)
                futures.append(future)
        
        for future in as_completed(futures):
            result = future.result()
            # 处理结果
```

**NumPy 向量化**:
```python
# 改进: 用 np.where 替代逐像素循环

# 当前（伪代码）
for i in range(n_rows):
    for j in range(n_cols):
        if mask[i, j] == 1:
            result[i, j] = compute(x[i, j], y[i, j])

# 改进后
result = np.where(mask == 1, compute_vec(x, y), np.nan)
```

**内存映射**:
```python
# 用 np.memmap 处理超大数组
fp = np.memmap('large_array.dat', dtype='float32', mode='w+', shape=(10000, 10000))
# 像操作普通 numpy 数组，但内存只保留必要的 page
```

#### 3.8.3 缓存策略

```python
# core/cache.py
class ProcessingCache:
    """中间结果缓存"""
    
    def __init__(self, cache_dir: str = "./.cache"):
        # 缓存结构:
        # .cache/
        #   ├── features/  (计算好的光谱指数)
        #   ├── ttri/      (TTRI 计算结果)
        #   └── aligned/   (对齐后的栅格)
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        
    def set(self, key: str, value: Any, ttl: int = 3600):
        """设置缓存（带过期时间）"""
        
    def invalidate(self, pattern: str):
        """清除匹配的缓存"""
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| 并行计算 | `concurrent.futures` / `joblib` | 已有 |
| 向量化 | NumPy / Numba `@jit` | Numba 需安装 |
| 缓存 | 文件系统 + SHA256 Key | 无额外依赖 |
| 性能分析 | `py-spy` / `cProfile` / `snakeviz` | 分析热点 |

**估计代码量**: ~800 行（2 个新文件 + 修改现有代码）

---

### 3.9 [LOW] 社区与生态

#### 3.9.1 Skill 在线市场

```python
# core/skills/marketplace.py
class SkillMarketplace:
    """Skill 在线市场"""
    
    MARKETPLACE_URL = "https://api.geothermo.ai/skills"
    
    def search(self, query: str) -> List[Dict]:
        """搜索可用 Skill"""
    
    def install(self, skill_id: str, version: str = "latest") -> bool:
        """安装远程 Skill"""
    
    def publish(self, skill_package: str, api_key: str) -> Dict:
        """发布自己的 Skill"""
```

#### 3.9.2 Python SDK

```python
# geothermo_sdk/__init__.py
from geothermo_sdk import GeoThermo

# 用户代码
client = GeoThermo(api_key="sk-xxx")

# 对话式 Agent
result = client.agent.run("处理武汉市 2024年7月的数据")

# 直接调用 Skill
result = client.skills.run(
    "rf_model",
    train_csv="train.csv",
    n_estimators=200,
)

# 查看结果
print(result.metrics["R2"])  # 0.87
```

#### 3.9.3 QGIS/ArcGIS 插件

```python
# qgis_plugin/geothermo_plugin.py
class GeoThermoPlugin:
    """QGIS 插件: 在 QGIS 界面中直接调用 GeoThermoAI"""
    
    def initGui(self):
        # 添加工具栏按钮
        self.action = QAction("GeoThermoAI", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        
    def run(self):
        # 获取当前选中的图层
        layer = self.iface.activeLayer()
        # 发送到 GeoThermoAI API
        result = client.process(layer.source())
        # 加载结果图层
        self.iface.addRasterLayer(result.path)
```

#### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| API 客户端 | `requests` / `httpx` | 已有 |
| 包管理 | PyPI (`twine` + `setuptools`) | 发布 SDK |
| QGIS 插件 | PyQGIS | QGIS 内置 |

**估计代码量**: ~2000 行（多个文件，跨项目）

---

## 四、完整升级对比总表

### 4.1 按优先级汇总

| 优先级 | 升级项 | 文件增量 | 依赖增量 | 代码量 | 核心价值 |
|---|---|---|---|---|---|
| **CRITICAL** | 工程化基础（测试+日志+配置+错误） | +4 | +4 (pytest, structlog, pydantic, sentry) | ~600 行 | 软件质量的根基 |
| **CRITICAL** | 多数据源支持 | +5~8 | 0~2 | ~1500 行 | 用户覆盖范围×3 |
| **CRITICAL** | AI/ML 能力升级（DL + 模型注册 + 诊断） | +5~10 | +3 (torch, torchgeo, shap) | ~3000 行 | 算法竞争力的未来 |
| **HIGH** | 任务管理与并行计算 | +3~5 | +2 (dask, celery) | ~2000 行 | 大规模处理能力 |
| **HIGH** | 数据管理与版本控制 | +3 | +1 (DVC) | ~1200 行 | 科研可复现性 |
| **MEDIUM** | Web 前后端分离 | +10~20 | +10+ (fastapi, react, ...) | ~5000 行 | 产品形态的升级 |
| **MEDIUM** | 专业遥感算法增强 | +5~8 | +1 (statsmodels) | ~2500 行 | 科学严谨性 |
| **MEDIUM** | 性能优化 | +2 | 0 | ~800 行 | 用户体验 |
| **LOW** | 社区与生态 | 多个项目 | +2~4 | ~2000 行 | 生态扩展 |

### 4.2 核心算法保持不变的

以下文件的算法逻辑**完全不受任何升级影响**：

- [core/data_preprocessing.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/data_preprocessing.py)
- [core/split_dataset.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/split_dataset.py)
- [core/rf_model.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/rf_model.py)
- [core/ttri.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/ttri.py)
- [core/tcr.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/tcr.py)
- [core/lst_final.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/lst_final.py)
- [core/export_geotiff.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/export_geotiff.py)
- [core/evaluation.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/evaluation.py)
- [core/agent/geo_thermo_agent.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/agent/geo_thermo_agent.py)（只需小幅适配）
- [core/skills/skill_registry.py](file:///d:/Files/研究和项目/10.GeoThermoAI/GeoThermoAI/core/skills/skill_registry.py)（只需小幅适配）

---

## 五、推荐分阶段实施路线

### Phase 0: 工程化固本（2~3 周）

```
目的: 为所有后续开发建立质量保障
输出: 测试通过、日志可查、错误可追踪

1. 引入 pytest，为核心算法写 20+ 个单元测试
2. 引入 structlog，替换所有 print/log_callback
3. 引入 pydantic，校验所有配置
4. 配置 GitHub Actions CI
```

**为什么优先做**：没有这些，后续所有功能迭代都是空中楼阁。测试覆盖率从 0% 到 60% 之后，重构和新增功能的信心会完全不同。

### Phase 1: 算法与数据能力（3~4 周）

```
目的: 补上最硬的技术短板
输出: DL 模型可用、多数据源可用、模型可对比

1. 实现 PyTorch UNet 降尺度模型
2. 实现模型注册表 + 实验管理
3. 实现本地数据导入 + Earth Search 支持
4. 实现 Benchmark 对比系统
```

### Phase 2: 可扩展与大规模（2~3 周）

```
目的: 支撑真实生产场景
输出: 任务可排队、大区域可切分、结果可复现

1. 实现异步任务队列（arq + Redis）
2. 实现 Dask 分布式处理
3. 实现实验管理系统 + DVC 集成
4. 实现不确定性量化
```

### Phase 3: 产品形态升级（3~5 周）

```
目的: 从"工具"到"产品"
输出: Web 版可用、API 可用、Docker 可部署

1. FastAPI 后端 + React 前端
2. 交互式地图可视化（MapLibre）
3. Docker + Docker Compose 部署方案
4. Python SDK 发布到 PyPI
```

### Phase 4: 生态建设（持续）

```
目的: 开源社区驱动
输出: 插件市场、QGIS 插件、贡献者生态

1. Skill 在线市场
2. QGIS 插件
3. 开发者文档和贡献指南
4. 与其它遥感开源项目集成
```

---

## 六、技术债务和技术风险

### 6.1 当前代码中的技术债务

| 问题 | 位置 | 风险 |
|---|---|---|
| `try/except` 捕获所有异常 | 多处 | 吞掉真正需要关注的错误 |
| 硬编码路径和模型名称 | `agent.py` | 数据源切换时需大面积修改 |
| importlib 动态加载无沙箱 | `skill_registry.py` | 第三方 Skill 可能执行恶意代码 |
| 全局 sys.path 修改 | `api.py` | 可能与其他库冲突 |
| 大量 pandas DataFrame 操作无类型提示 | 多个 Skill | 列名变更时静默失败 |
| GDAL 异常未完整处理 | `data_acquisition.py` | 下载失败时可能残留临时文件 |

### 6.2 关键技术风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| **GDAL 系统依赖** | Docker/云环境部署困难 | 使用 `osgeo/gdal:latest` 镜像为基础 |
| **DL 模型训练时间** | 用户等待时间长 | CUDA 加速 + 预训练权重 |
| **云计算成本** | Planetary Computer 免费但有速率限制 | 本地数据支持 + 多数据源 |
| **PyTorch 包体积** | Docker 镜像可能 > 5GB | 分层构建 + GPU/CPU 分开 |

---

## 七、总结

将 GeoThermoAI 升级为真正产品级 AI 遥感软件，本质上是**三个层面的升级**：

1. **工程层面**: 从"能运行的脚本"到"可测试、可部署、可维护的软件"
2. **算法层面**: 从"单一 RF 方法"到"多模型对比 + 深度学习 + 不确定性评估"
3. **产品层面**: 从"桌面工具"到"Web API + 插件生态 + 社区平台"

当前代码库的**核心架构（Agent + Skill 系统）本身就是产品级的**，这是最大的资产。所有升级都是在保持这个架构优势的基础上，补足工程化、数据能力、AI 能力和部署能力的短板。

这三个层面的升级互相独立，可以从任一层面开始，但建议严格按照 Phase 0 → 1 → 2 → 3 的顺序，因为每一层都建立在前一层的基础之上。
