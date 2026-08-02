# ─────────────────────────────────────────────────────────────────────
# GeoThermoAI — ModelScope Studio Docker 版（GDAL 方案）
#
# 基础镜像：OSGeo 官方 GDAL 镜像（发布在 ghcr.io，Docker Hub 上没有对应标签）
#   - Ubuntu 24.04 (noble) + Python 3.12（构建日志可确认）
#   - GDAL C 库 + PROJ + osgeo Python 绑定 由镜像源码编译、版本自洽
#   - 彻底规避平台 gradio 类型下 "apt python3-gdal 遮蔽 wheel osgeo" 的问题
# ─────────────────────────────────────────────────────────────────────
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.3

# 中文输出 / 日志实时刷新 / pip 国内镜像
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.cloud.aliyuncs.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.cloud.aliyuncs.com

# 安装 pip 与编译工具（部分依赖需要编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝依赖清单并安装（利用 Docker 层缓存，避免每次重新装）
# --break-system-packages：Ubuntu 24.04 (PEP 668) 禁止 pip 直接装系统 Python，容器内可安全绕过
# 注意：镜像 osgeo 绑定按 apt numpy 1.x 编译，requirements.txt 已锁 numpy<2，pip 不会升级 numpy
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# 构建期自检：确认 osgeo 可导入、GDAL 版本正常（把潜在运行期故障提前到构建期暴露）
RUN python3 -c "from osgeo import gdal; print('osgeo OK, GDAL', gdal.VersionInfo())"

# 拷贝项目代码
COPY . .

# ModelScope 要求服务监听 7860 端口
EXPOSE 7860

# 启动 Gradio 应用（镜像系统 python3 即自带 osgeo 绑定，app.py 里无需安装 GDAL）
CMD ["python3", "app.py"]
