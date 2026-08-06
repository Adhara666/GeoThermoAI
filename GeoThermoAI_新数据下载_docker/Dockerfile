# ─────────────────────────────────────────────────────────────────────
# GeoThermoAI Vue3 版 — ModelScope Studio Docker
#
# 基础镜像：OSGeo 官方 GDAL 镜像（发布在 ghcr.io，Docker Hub 上没有对应标签）
#   - Ubuntu 24.04 (noble) + Python 3.12
#   - GDAL C 库 + PROJ + osgeo Python 绑定 由镜像源码编译、版本自洽
#   - 前端为 Vue 3 构建产物（dist/），由 FastAPI 静态托管，无需 Node
# ─────────────────────────────────────────────────────────────────────
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.3

# 中文输出 / 日志实时刷新 / pip 国内镜像
# PIP 源可通过 --build-arg 覆盖：ModelScope 默认阿里云；本地构建可用清华镜像，如
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/ \
#                --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn -t geothermoai:test .
ARG PIP_INDEX_URL=https://mirrors.cloud.aliyuncs.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.cloud.aliyuncs.com
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=$PIP_INDEX_URL \
    PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST \
    HF_ENDPOINT=https://hf-mirror.com

# 安装 pip 与编译工具（部分依赖需要编译）；git/git-lfs 用于模型缺失时从 ModelScope 拉取 bge 模型
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-dev \
    build-essential \
    git \
    git-lfs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝依赖清单并安装（利用 Docker 层缓存，避免每次重新装）
# --break-system-packages：Ubuntu 24.04 (PEP 668) 禁止 pip 直接装系统 Python，容器内可安全绕过
# 注意：镜像 osgeo 绑定按 apt numpy 1.x 编译，requirements.txt 已锁 numpy<2，pip 不会升级 numpy
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# 构建期自检：确认 osgeo 可导入、GDAL 版本正常（把潜在运行期故障提前到构建期暴露）
RUN python3 -c "from osgeo import gdal; print('osgeo OK, GDAL', gdal.VersionInfo())"

# 拷贝项目代码（core/ 算法 + server.py 后端 + dist/ 前端产物 + config + models/）
COPY . .

# bge 嵌入模型就位（记忆系统 RAG，中文最优；ONNX 格式 ~95MB）。
# 优先级：1) 仓库 models/ 随 COPY 带入（用户上传的模型）→ 直接使用，不下载；
#         2) 缺失/文件过小（网页上传大文件可能被截断为 0 字节）才视为无效，
#            改从 ModelScope 下载（构建环境必达），失败回退 HF 镜像；
#         3) 仍无效 → 自检 fail-fast，构建失败，杜绝运行时静默回退内置嵌入。
RUN if [ -f /app/models/bge-small-zh-v1.5/onnx/model.onnx ] \
        && [ "$(stat -c%s /app/models/bge-small-zh-v1.5/onnx/model.onnx 2>/dev/null || echo 0)" -gt 50000000 ]; then \
        echo "bge model ready: 使用仓库自带 models/bge-small-zh-v1.5（用户上传）"; \
    else \
        echo "仓库未含有效 bge 模型（缺失或过小），尝试从 ModelScope 下载..."; \
        rm -rf /app/models/bge-small-zh-v1.5; \
        git lfs install >/dev/null 2>&1 || true; \
        git clone --depth 1 https://www.modelscope.cn/Xenova/bge-small-zh-v1.5.git /tmp/bge_dl >/dev/null 2>&1 \
            && mkdir -p /app/models/bge-small-zh-v1.5 \
            && cp -a /tmp/bge_dl/. /app/models/bge-small-zh-v1.5/ \
            && rm -rf /tmp/bge_dl; \
    fi

# HF 兜底（仅当仓库 COPY 与 ModelScope 下载均未就位时）
RUN if [ ! -s /app/models/bge-small-zh-v1.5/onnx/model.onnx ] \
        || [ "$(stat -c%s /app/models/bge-small-zh-v1.5/onnx/model.onnx 2>/dev/null || echo 0)" -le 50000000 ]; then \
        echo "ModelScope 下载未就位（缺失或过小），回退 huggingface 镜像下载..."; \
        python3 -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Xenova/bge-small-zh-v1.5', local_dir='/app/models/bge-small-zh-v1.5')"; \
    fi

# 构建期自检（fail-fast）：bge 嵌入模型必须有效（存在且 > 50MB）。
# 缺失或为 0 字节空文件（网页上传大文件截断）一律视为失败，
# 杜绝"运行时静默回退内置嵌入"导致中文检索质量降级。
RUN python3 - <<'PY'
import os
p = "/app/models/bge-small-zh-v1.5/onnx/model.onnx"
size = os.path.getsize(p) if os.path.isfile(p) else 0
if os.path.isfile(p) and size > 50 * 1024 * 1024:
    print(f"bge model ready: True ({size/1024/1024:.1f} MB)")
else:
    raise SystemExit(
        f"构建失败：bge 嵌入模型缺失或无效（{p}，{size/1024/1024:.1f} MB，需 > 50MB）。\n"
        "请用 Git 方式上传 models/bge-small-zh-v1.5（勿用网页上传大文件，可能被截断为空），\n"
        "或保证构建期从 ModelScope/HF 下载成功。"
    )
PY

# ModelScope 要求服务监听 7860 端口
EXPOSE 7860

# 启动 FastAPI 后端（托管 Vue 前端 + REST/SSE 接口）
CMD ["python3", "server.py"]
