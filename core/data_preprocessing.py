"""
数据预处理模块

整合 Landsat LST、QA、Sentinel-2多光谱、SCL和DEM数据，
生成30m训练特征Parquet（step=2抽样）、完整30m约束层Parquet 和 10m预测特征Parquet。

处理流程:
    1. 加载所有栅格数据
    2. 将S2/SCL/DEM对齐到Landsat 30m网格
    3. 生成联合掩膜（Landsat QA + Sentinel SCL 4/5/6 + 热红外有效值）
    4. 计算光谱指数（NDVI, NDWI, NDBI）和地形特征（Slope, Aspect, cos(Aspect)）
    5. 输出30m step2 Parquet（训练抽样，继续保留）+ 完整30m约束层Parquet（
       覆盖全部有效30m像元，供 TTRI 空间化 / TCR / 闭合评价使用，不再把 step2
       抽样 Parquet 兼任"完整30m格网"）
    6. 将SCL对齐到Sentinel原生10m网格，输出10m全网格Parquet和元数据JSON

双流设计：
    完整30m输入栅格
      ├─ A. 固定 30m_features_step2.parquet → split → TTRI拟合 → RF训练/独立评价
      └─ B. 完整有效30m约束层 30m_constraint_grid.parquet → TTRI空间化/TCR/闭合评价
    两条流共享同一份 joint_mask、DEM/Slope/Aspect 计算，互不冲突；A 流的采样策略
    与列结构均未改变。
"""

import json
import os
import time
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

from .atomic_io import write_verified
from .geo_mask import rasterize_region as _rasterize_region
from .table_io import TableWriter, sample_rows

# ── 物理常量 ──────────────────────────────────────────────────────────
S2_SR_SCALE = 10000.0          # Sentinel-2地表反射率缩放因子 (DN / 10000)
LST_SCALE = 0.00341802          # Landsat ST_B10 辐射定标系数
LST_OFFSET_K = 149.0            # Landsat ST_B10 偏移量 (K)
EPS = 1e-8                      # 防止除零

# ── 波段索引（rasterio 1-based）──────────────────────────────────────
BAND_BLUE = 1
BAND_GREEN = 2
BAND_RED = 3
BAND_NIR = 4
BAND_SWIR1 = 5

# ── 输出列定义 ────────────────────────────────────────────────────────
_BASE_TRAIN_COLUMNS = [
    "row", "col",
    "R", "G", "B", "NIR", "SWIR1",
    "NDVI", "NDWI", "NDBI",
    "DEM", "Slope", "Aspect", "cos(Aspect)",
]
TARGET_COLUMN = "LST"

PREDICT_COLUMNS = [
    "row", "col",
    "R", "G", "B", "NIR", "SWIR1",
    "NDVI", "NDWI", "NDBI",
]

# 完整30m约束层：紧凑列（不复制全部15列特征，只保留 TCR/TTRI 空间化所需的最小集合）
_BASE_CONSTRAINT_COLUMNS = ["row", "col", "LST", "DEM", "Slope", "cos(Aspect)"]

# 固定文件名（工程写死，不允许模型自由起名；升级：中间产物表统一 Parquet）
CONSTRAINT_CSV_NAME = "30m_constraint_grid.parquet"
CONSTRAINT_META_NAME = "30m_constraint_grid_meta.json"

# ── Sentinel-2 L2A SCL 有效地物类别 ──────────────────────────────────
VALID_SCL_CLASSES = np.array([4, 5, 6])  # 植被、裸土、水体


def train_columns() -> List[str]:
    cols = list(_BASE_TRAIN_COLUMNS)
    cols.append(TARGET_COLUMN)
    return cols


def constraint_columns() -> List[str]:
    return list(_BASE_CONSTRAINT_COLUMNS)


# ======================================================================
#  地理处理辅助函数
# ======================================================================


def _transform_to_list(transform) -> List[float]:
    """将仿射变换对象转为float列表。"""
    return [float(transform[i]) for i in range(6)]


def _resolve_raster_path(path: str, progress_callback=None) -> str:
    """
    解析栅格路径：支持单文件、目录（自动拼接分块）。

    大型遥感影像可能被分块为多个 TIFF 文件。
    本函数检测输入是目录还是单文件：
    - 单文件 (.tif/.tiff): 直接返回路径
    - 目录: 查找所有 .tif 文件，用 rasterio.merge 合并后返回临时文件路径

    合并产生的临时文件固定命名为 ``_merged_vrt_temp.tif``，仅用于本次处理，
    调用方应在处理成功/失败后都清理该临时文件（见 process_preprocessing 的
    ``finally`` 清理，避免下一轮把残留的旧合并结果重新枚举进目录扫描）。

    Args:
        path: 文件或目录路径
        progress_callback: 进度回调

    Returns:
        str: 可用的单文件栅格路径
    """
    if os.path.isfile(path):
        return path

    if os.path.isdir(path):
        # 查找目录下所有 .tif 文件（明确排除上一轮可能残留的合并临时文件）
        tif_files = sorted([
            os.path.join(path, f) for f in os.listdir(path)
            if f.lower().endswith(('.tif', '.tiff')) and f != "_merged_vrt_temp.tif"
        ])
        if not tif_files:
            raise FileNotFoundError(f"目录 {path} 中未找到 .tif/.tiff 文件")
        if len(tif_files) == 1:
            return tif_files[0]

        # 多个分块文件：使用 rasterio.merge 合并
        if progress_callback:
            progress_callback("preprocessing", 0.02,
                              f"拼接 {len(tif_files)} 个分块文件...")

        from rasterio.merge import merge as rasterio_merge
        datasets = [rasterio.open(f) for f in tif_files]
        try:
            merged_arr, merged_transform = rasterio_merge(datasets)
            meta = datasets[0].meta.copy()
            meta.update({
                "driver": "GTiff",
                "height": merged_arr.shape[1],
                "width": merged_arr.shape[2],
                "transform": merged_transform,
            })

            # 写入临时合并文件（固定名，处理结束后由调用方清理，不长期驻留）
            merged_path = os.path.join(path, "_merged_vrt_temp.tif")
            with rasterio.open(merged_path, "w", **meta) as dst:
                for band_idx in range(merged_arr.shape[0]):
                    dst.write(merged_arr[band_idx], band_idx + 1)

            if progress_callback:
                progress_callback("preprocessing", 0.04,
                                  f"拼接完成: {meta['height']}x{meta['width']} 像素")
            return merged_path
        finally:
            for ds in datasets:
                ds.close()

    # 如果既不是文件也不是目录，可能是通配符模式
    import glob as glob_mod
    matched = sorted(glob_mod.glob(path))
    if matched and all(f.lower().endswith(('.tif', '.tiff')) for f in matched):
        return _resolve_raster_path(os.path.dirname(matched[0]), progress_callback)

    raise FileNotFoundError(f"无法解析路径: {path}")


def _resample_and_align(
    ref_path: str,
    source_path: str,
    out_path: str,
    is_categorical: bool = False,
) -> str:
    """
    将source栅格重投影/重采样到与ref栅格完全一致的网格。

    Args:
        ref_path:       参考栅格路径
        source_path:    源栅格路径
        out_path:       输出栅格路径
        is_categorical: 是否为分类数据（使用最近邻重采样）

    Returns:
        str: 输出路径
    """
    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    resampling_method = Resampling.nearest if is_categorical else Resampling.bilinear

    with rasterio.open(ref_path) as ref_ds, rasterio.open(source_path) as src_ds:
        out_profile = src_ds.profile.copy()
        out_profile.update({
            "crs": ref_ds.crs,
            "transform": ref_ds.transform,
            "width": ref_ds.width,
            "height": ref_ds.height,
            "count": src_ds.count,
        })

        src_nodata = src_ds.nodata
        dst_nodata = out_profile.get("nodata", src_nodata)
        if out_profile.get("nodata", None) is None and src_nodata is not None:
            out_profile["nodata"] = src_nodata

        with rasterio.open(out_path, "w", **out_profile) as dst_ds:
            for band_idx in range(1, src_ds.count + 1):
                dst_array = np.empty(
                    (ref_ds.height, ref_ds.width),
                    dtype=out_profile["dtype"],
                )
                if dst_nodata is not None:
                    dst_array.fill(dst_nodata)

                reproject(
                    source=rasterio.band(src_ds, band_idx),
                    destination=dst_array,
                    src_transform=src_ds.transform,
                    src_crs=src_ds.crs,
                    src_nodata=src_nodata,
                    dst_transform=ref_ds.transform,
                    dst_crs=ref_ds.crs,
                    dst_nodata=dst_nodata,
                    resampling=resampling_method,
                )
                dst_ds.write(dst_array, band_idx)

    return out_path


def _reproject_to_crs_grid(
    source_path: str,
    out_path: str,
    dst_crs: str,
    resolution: float,
    is_categorical: bool = False,
) -> str:
    """将栅格重投影到目标CRS和分辨率。"""
    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    resampling = Resampling.nearest if is_categorical else Resampling.bilinear

    with rasterio.open(source_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds,
            resolution=(resolution, resolution),
        )
        src_nodata = src.nodata

        profile = src.profile.copy()
        if is_categorical:
            dst_nodata = profile.get("nodata", src_nodata)
        else:
            profile["dtype"] = "float32"
            dst_nodata = np.nan

        profile.update(
            crs=dst_crs,
            transform=transform,
            width=width,
            height=height,
            count=src.count,
            nodata=dst_nodata,
        )

        with rasterio.open(out_path, "w", **profile) as dst:
            for band_idx in range(1, src.count + 1):
                dtype = np.float32 if not is_categorical else src.dtypes[band_idx - 1]
                dst_array = np.empty((height, width), dtype=dtype)
                if dst_nodata is not None:
                    dst_array.fill(dst_nodata)
                reproject(
                    source=rasterio.band(src, band_idx),
                    destination=dst_array,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src_nodata,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    dst_nodata=dst_nodata,
                    resampling=resampling,
                )
                dst.write(dst_array, band_idx)

    return out_path


# ======================================================================
#  掩膜生成
# ======================================================================


def _landsat_qa_mask(qa_array: np.ndarray) -> np.ndarray:
    """
    从Landsat Collection 2 QA_PIXEL波段提取晴空像元掩膜。

    清除位: bit 0 (fill), bit 1 (dilated cloud), bit 2 (cirrus),
           bit 3 (cloud), bit 4 (cloud shadow)
    """
    qa = np.asarray(qa_array, dtype=np.uint16)
    cloud_bits_mask = np.uint16((1 << 1) | (1 << 2) | (1 << 3) | (1 << 4))
    fill_bit = np.uint16(1 << 0)
    no_cloud_shadow = np.bitwise_and(qa, cloud_bits_mask) == 0
    not_fill = np.bitwise_and(qa, fill_bit) == 0
    return no_cloud_shadow & not_fill


def _sentinel_scl_mask(scl_array: np.ndarray) -> np.ndarray:
    """从Sentinel-2 L2A SCL波段提取有效像元（类别4/5/6：植被、裸土、水体）。"""
    scl = np.asarray(scl_array)
    return np.isin(scl, VALID_SCL_CLASSES)


def _thermal_valid_mask(lst_dn: np.ndarray) -> np.ndarray:
    """热红外波段有效值掩膜（> 0 且有限）。"""
    lst = np.asarray(lst_dn, dtype=np.float64)
    return (lst > 0) & np.isfinite(lst)


# ======================================================================
#  地形特征计算
# ======================================================================


def _terrain_features(
    dem_array: np.ndarray, dx: float = 30.0, dy: float = 30.0
) -> Tuple[np.ndarray, np.ndarray]:
    """
    从DEM计算坡度和坡向，使用numpy.gradient。

    像元间距从实际仿射变换的 dx/dy 读取，不再对所有输入写死 30.0，
    避免非 30m/非方形像元的 DEM 得到错误坡度量纲。要求 DEM 已是米制、北向、无旋转
    网格（标准 Landsat 参考格网满足该假设）。

    Args:
        dem_array:  DEM数据（float数组）
        dx:         列方向（东西向）像元间距（米，取绝对值）
        dy:         行方向（南北向）像元间距（米，取绝对值）

    Returns:
        tuple: (slope_degrees, aspect_degrees)
    """
    dem = np.asarray(dem_array, dtype=np.float64)
    gy, gx = np.gradient(dem, abs(dy), abs(dx))
    slope = np.degrees(np.arctan(np.sqrt(gx ** 2 + gy ** 2)))
    aspect = np.mod(np.degrees(np.arctan2(-gx, gy)) + 360.0, 360.0)
    return slope.astype(np.float32), aspect.astype(np.float32)


# ======================================================================
#  光谱指数计算
# ======================================================================


def _spectral_indices(
    r: np.ndarray, g: np.ndarray, b: np.ndarray,
    nir: np.ndarray, swir1: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    计算光谱指数。

    Returns:
        tuple: (NDVI, NDWI, NDBI)
    """
    ndvi = (nir - r) / (nir + r + EPS)
    ndwi = (g - nir) / (g + nir + EPS)
    ndbi = (swir1 - nir) / (swir1 + nir + EPS)
    return ndvi, ndwi, ndbi


# ======================================================================
#  主预处理函数
# ======================================================================


def process_preprocessing(
    landsat_path: str,
    sentinel2_path: str,
    qa_path: str,
    scl_path: str,
    dem_path: str,
    output_dir: str,
    progress_callback=None,
    step2_min_valid_samples: int = 750000,
    study_area_geojson: str = "",
) -> Dict:
    """
    数据预处理：生成30m训练特征Parquet（采样）、完整30m约束层Parquet 和 10m预测特征Parquet。

    Args:
        landsat_path:   Landsat L2 ST_B10栅格路径
        sentinel2_path: Sentinel-2 L2A多光谱栅格路径
        qa_path:        Landsat QA_PIXEL栅格路径
        scl_path:       Sentinel-2 SCL栅格路径
        dem_path:       DEM栅格路径（30m）
        output_dir:     输出目录
        progress_callback: 进度回调 callback(step_name, percent, message)
        step2_min_valid_samples: joint_mask 有效像元达到该数量才做 step=2 等间隔抽样；
                                 不足时自动降为 step=1（全像元），保住训练样本量。
        study_area_geojson: 研究区 GeoJSON 路径（可选）。给定时，像元占比统计的
                            分母改为「研究区多边形内」的像元数，而不是 bbox 外接
                            矩形全网格；缺失或栅格化失败时自动回退 bbox 口径。

    Returns:
        dict: 包含输出文件路径和元数据
            - train_csv:        30m训练集Parquet路径（step2，继续保留）
            - train_meta:       30m训练集元数据JSON路径
            - constraint_csv:   完整30m约束层Parquet路径
            - constraint_meta:  完整30m约束层元数据JSON路径
            - predict_csv:      10m预测集Parquet路径
            - predict_meta:     10m元数据JSON路径
            - aligned_s2/aligned_scl/aligned_dem: 对齐到30m的中间产物路径
    """
    t_start = time.time()
    os.makedirs(output_dir, exist_ok=True)

    # ── 自动解析分块文件（大影像可能被分块）────────────────
    if progress_callback:
        progress_callback("preprocessing", 0.01, "检查输入数据...")

    landsat_path = _resolve_raster_path(landsat_path, progress_callback)
    sentinel2_path = _resolve_raster_path(sentinel2_path, progress_callback)
    qa_path = _resolve_raster_path(qa_path, progress_callback)
    scl_path = _resolve_raster_path(scl_path, progress_callback)
    dem_path = _resolve_raster_path(dem_path, progress_callback)

    processed_dir = output_dir
    os.makedirs(processed_dir, exist_ok=True)

    # ── 临时对齐文件路径 ──────────────────────────────────────────────
    aligned_s2 = os.path.join(processed_dir, "Aligned_S2_30m.tif")
    aligned_scl = os.path.join(processed_dir, "Aligned_SCL_30m.tif")
    aligned_dem = os.path.join(processed_dir, "Aligned_DEM_30m.tif")
    scl_10m_aligned = os.path.join(processed_dir, "Aligned_SCL_to_S2_10m.tif")

    # 输出路径（固定命名；升级：中间产物表统一 Parquet）
    train_csv = os.path.join(output_dir, "30m_features_step2.parquet")
    train_meta = os.path.join(output_dir, "30m_features_step2_meta.json")
    constraint_csv = os.path.join(output_dir, CONSTRAINT_CSV_NAME)
    constraint_meta = os.path.join(output_dir, CONSTRAINT_META_NAME)
    predict_csv = os.path.join(output_dir, "10m_predict_features.parquet")
    predict_meta = os.path.join(output_dir, "10m_predict_features_meta.json")

    try:
        # ==================================================================
        #  步骤1: 对齐S2/SCL/DEM到Landsat 30m网格
        # ==================================================================
        if progress_callback:
            progress_callback("preprocessing", 0.05, "对齐S2到Landsat 30m网格...")

        _resample_and_align(landsat_path, sentinel2_path, aligned_s2, is_categorical=False)

        if progress_callback:
            progress_callback("preprocessing", 0.10, "对齐SCL到Landsat 30m网格...")

        _resample_and_align(landsat_path, scl_path, aligned_scl, is_categorical=True)

        if progress_callback:
            progress_callback("preprocessing", 0.15, "对齐DEM到Landsat 30m网格...")

        _resample_and_align(landsat_path, dem_path, aligned_dem, is_categorical=False)

        # ==================================================================
        #  步骤2: 读取30m栅格数据
        # ==================================================================
        if progress_callback:
            progress_callback("preprocessing", 0.20, "读取30m栅格数据...")

        # 获取30m栅格尺寸（以 Landsat 为唯一参考格网）
        with rasterio.open(landsat_path) as _ds:
            height_30m, width_30m = _ds.height, _ds.width
            landsat_transform = _ds.transform
            landsat_crs = str(_ds.crs) if _ds.crs else None

        # 预飞行校验：所有对齐后栅格必须与参考格网严格同尺寸，
        # 否则后续按窗口读取会静默错位/越界（此前该检查被整段注释掉）。
        for _label, _path in (("QA", qa_path), ("对齐DEM", aligned_dem),
                               ("对齐S2", aligned_s2), ("对齐SCL", aligned_scl)):
            with rasterio.open(_path) as _chk:
                if (_chk.height, _chk.width) != (height_30m, width_30m):
                    raise ValueError(
                        f"{_label} 栅格尺寸 {(_chk.height, _chk.width)} 与 Landsat 参考格网 "
                        f"{(height_30m, width_30m)} 不一致，拒绝继续（预飞行校验）"
                    )

        # 预分配数组
        lst = np.empty((height_30m, width_30m), dtype=np.float32)
        qa = np.empty((height_30m, width_30m), dtype=np.uint16)
        dem = np.empty((height_30m, width_30m), dtype=np.float32)
        blue = np.empty((height_30m, width_30m), dtype=np.float32)
        green = np.empty((height_30m, width_30m), dtype=np.float32)
        red = np.empty((height_30m, width_30m), dtype=np.float32)
        nir = np.empty((height_30m, width_30m), dtype=np.float32)
        swir1 = np.empty((height_30m, width_30m), dtype=np.float32)
        scl = np.empty((height_30m, width_30m), dtype=np.uint8)

        block_rows_30m = 512

        with rasterio.open(landsat_path) as lst_ds, \
             rasterio.open(qa_path) as qa_ds, \
             rasterio.open(aligned_dem) as dem_ds, \
             rasterio.open(aligned_s2) as s2_ds, \
             rasterio.open(aligned_scl) as scl_ds:

            lst_nodata = lst_ds.nodata
            dem_nodata = dem_ds.nodata
            s2_nodata = s2_ds.nodata

            for start in range(0, height_30m, block_rows_30m):
                    stop = min(start + block_rows_30m, height_30m)
                    window = rasterio.windows.Window(0, start, width_30m, stop - start)

                    # LST
                    lst_block = lst_ds.read(1, window=window).astype(np.float32)
                    if lst_nodata is not None:
                        lst_block[lst_block == lst_nodata] = np.nan
                    lst[start:stop, :] = lst_block

                    # QA
                    qa[start:stop, :] = qa_ds.read(1, window=window)

                    # DEM
                    dem_block = dem_ds.read(1, window=window).astype(np.float32)
                    if dem_nodata is not None:
                        dem_block[dem_block == dem_nodata] = np.nan
                    dem[start:stop, :] = dem_block

                    # SCL
                    scl[start:stop, :] = scl_ds.read(1, window=window)

                    # S2 多光谱波段
                    b_block = s2_ds.read(BAND_BLUE, window=window).astype(np.float32)
                    g_block = s2_ds.read(BAND_GREEN, window=window).astype(np.float32)
                    r_block = s2_ds.read(BAND_RED, window=window).astype(np.float32)
                    n_block = s2_ds.read(BAND_NIR, window=window).astype(np.float32)
                    s_block = s2_ds.read(BAND_SWIR1, window=window).astype(np.float32)

                    if s2_nodata is not None:
                        for arr in (b_block, g_block, r_block, n_block, s_block):
                            arr[arr == s2_nodata] = np.nan

                    for arr in (b_block, g_block, r_block, n_block, s_block):
                        arr /= S2_SR_SCALE

                    blue[start:stop, :] = b_block
                    green[start:stop, :] = g_block
                    red[start:stop, :] = r_block
                    nir[start:stop, :] = n_block
                    swir1[start:stop, :] = s_block

        dx = abs(float(landsat_transform.a))
        dy = abs(float(landsat_transform.e))
        slope, aspect = _terrain_features(dem, dx=dx, dy=dy)
        cos_aspect = np.cos(np.deg2rad(aspect))

        # ==================================================================
        #  步骤3: 生成联合掩膜
        # ==================================================================
        if progress_callback:
            progress_callback("preprocessing", 0.30, "生成联合掩膜...")

        clear_mask = _landsat_qa_mask(qa) & _sentinel_scl_mask(scl)
        joint_mask = clear_mask & _thermal_valid_mask(lst)

        # LST 全网格转开尔文（供 step2 抽样与完整约束层共用，只算一次）
        lst_k_full = lst * LST_SCALE + LST_OFFSET_K

        # 研究区多边形掩膜（占比口径统一：分母用「研究区多边形内」像元数，可选；
        # 缺失或栅格化失败时 region30=None，所有 region_* 统计回退为 0，口径回退 bbox）
        region30 = _rasterize_region(study_area_geojson, landsat_path)
        region_pixels30 = int(region30.sum()) if region30 is not None else 0

        # ==================================================================
        #  步骤4: 生成30m训练Parquet（采样）
        #  有效样本充足（默认 ≥750,000）时 step=2 等间隔抽样，节省算力与存储；
        #  不足时不采样（step=1 全像元直接进入划分），保住训练样本量。
        # ==================================================================
        valid_count = int(joint_mask.sum())
        step = 2 if valid_count >= step2_min_valid_samples else 1
        if progress_callback:
            progress_callback(
                "preprocessing", 0.35,
                f"生成30m训练特征Parquet (step={step}采样，有效像元 {valid_count:,} 个"
                f"{'' if step == 1 else '，每4取1'})..."
            )

        mask_ss = joint_mask[::step, ::step]
        rr, cc = np.nonzero(mask_ss)
        rows = rr.astype(np.int32) * step
        cols = cc.astype(np.int32) * step

        r = red[::step, ::step][mask_ss]
        g = green[::step, ::step][mask_ss]
        b = blue[::step, ::step][mask_ss]
        n = nir[::step, ::step][mask_ss]
        s = swir1[::step, ::step][mask_ss]
        dem_1d = dem[::step, ::step][mask_ss]
        slope_1d = slope[::step, ::step][mask_ss]
        aspect_1d = aspect[::step, ::step][mask_ss]
        cos_aspect_1d = cos_aspect[::step, ::step][mask_ss]
        lst_k = lst_k_full[::step, ::step][mask_ss]

        train_data = {
            "row": rows, "col": cols,
            "R": r, "G": g, "B": b, "NIR": n, "SWIR1": s,
            "NDVI": (n - r) / (n + r + EPS),
            "NDWI": (g - n) / (g + n + EPS),
            "NDBI": (s - n) / (s + n + EPS),
            "DEM": dem_1d, "Slope": slope_1d, "Aspect": aspect_1d, "cos(Aspect)": cos_aspect_1d,
        }
        train_data[TARGET_COLUMN] = lst_k

        df_train = pd.DataFrame(train_data)[train_columns()]
        df_train = df_train.replace([np.inf, -np.inf], np.nan).dropna(
            subset=train_columns()
        )
        df_train.to_parquet(train_csv, index=False, compression="zstd")

        with rasterio.open(landsat_path) as ds:
            train_meta_dict = {
                "height": int(ds.height),
                "width": int(ds.width),
                "rows_total_grid": int(ds.height * ds.width),
                "training_rows": int(len(df_train)),
                "crs": str(ds.crs) if ds.crs else None,
                "transform": _transform_to_list(ds.transform),
                "ravel_order": "C",
                "step": step,
                "columns": train_columns(),
                "sentinel2_sr_scale": (
                    f"exported reflectance = corrected DN / {S2_SR_SCALE:g}；"
                    f"corrected DN 已在数据获取阶段按各景 BOA_ADD_OFFSET 定标（"
                    f"sentinel2_provenance.json），此处不再对所有影像盲减固定值"
                ),
                "target": "LST = Landsat ST_B10 DN * 0.00341802 + 149.0",
                "output_csv": train_csv,
            }

        with open(train_meta, "w", encoding="utf-8") as f:
            json.dump(train_meta_dict, f, ensure_ascii=False, indent=2)

        if progress_callback:
            progress_callback(
                "preprocessing", 0.42,
                f"30m训练数据(step{step}): {len(df_train):,} 行",
            )

        # ==================================================================
        #  步骤4.5: 生成完整30m约束层
        #  覆盖全部 joint_mask 有效像元（step=1），供 TTRI 空间化 / TCR / 闭合评价使用，
        #  不再把 step2 抽样 Parquet 兼任"完整30m格网"。
        # ==================================================================
        if progress_callback:
            progress_callback("preprocessing", 0.45, "生成完整30m约束层 (30m_constraint_grid.parquet)...")

        full_rr, full_cc = np.nonzero(joint_mask)
        constraint_data = {
            "row": full_rr.astype(np.int32),
            "col": full_cc.astype(np.int32),
            "LST": lst_k_full[joint_mask],
            "DEM": dem[joint_mask],
            "Slope": slope[joint_mask],
            "cos(Aspect)": cos_aspect[joint_mask],
        }
        df_constraint = pd.DataFrame(constraint_data)[constraint_columns()]
        df_constraint = df_constraint.replace([np.inf, -np.inf], np.nan).dropna(
            subset=constraint_columns()
        )

        def _build_constraint(tmp_path: str) -> None:
            df_constraint.to_parquet(tmp_path, index=False, compression="zstd")

        def _validate_constraint(tmp_path: str) -> Tuple[bool, str]:
            check = sample_rows(tmp_path, n=5)
            missing = [c for c in constraint_columns() if c not in check.columns]
            if missing:
                return False, f"完整30m约束层缺少列: {missing}"
            return True, ""

        write_verified(_build_constraint, constraint_csv, _validate_constraint)

        if region30 is not None:
            region_valid30 = int(
                region30[df_constraint["row"].values, df_constraint["col"].values].sum()
            )
        else:
            region_valid30 = 0
        constraint_meta_dict = {
            "height": int(height_30m),
            "width": int(width_30m),
            "crs": landsat_crs,
            "transform": _transform_to_list(landsat_transform),
            "ravel_order": "C",
            "columns": constraint_columns(),
            "valid_pixels": int(len(df_constraint)),
            "total_pixels": int(height_30m * width_30m),
            "valid_ratio": round(float(len(df_constraint)) / float(height_30m * width_30m), 6)
            if height_30m * width_30m > 0 else 0.0,
            # 研究区口径（分母=研究区多边形内像元；region_pixels30=0 表示未提供研究区）
            "region_pixels": region_pixels30,
            "region_valid_pixels": region_valid30,
            "region_valid_ratio": round(region_valid30 / region_pixels30, 6)
            if region_pixels30 > 0 else 0.0,
            "description": (
                "覆盖全部有效30m像元（joint_mask，即 Landsat QA + Sentinel SCL 4/5/6 + "
                "热红外有效值），用于 TTRI 空间化插值网格 / TCR 聚合与回写 / 粗尺度闭合评价的"
                "统一参考；与 30m_features_step2.parquet（训练抽样）互为独立数据流。"
                "region_* 字段为研究区多边形口径的占比统计"
            ),
            "output_csv": constraint_csv,
        }
        with open(constraint_meta, "w", encoding="utf-8") as f:
            json.dump(constraint_meta_dict, f, ensure_ascii=False, indent=2)

        if progress_callback:
            progress_callback(
                "preprocessing", 0.50,
                f"完整30m约束层: {len(df_constraint):,} / {height_30m * width_30m:,} 像素有效",
            )

        # 30m 阶段大数组用后即释放（内存优化：核心表/meta 已落盘后不再需要）
        del lst, qa, dem, blue, green, red, nir, swir1, scl
        del joint_mask, clear_mask, lst_k_full, slope, aspect, cos_aspect

        # ==================================================================
        #  步骤5: 生成10m预测Parquet
        # ==================================================================
        if progress_callback:
            progress_callback("preprocessing", 0.55, "对齐SCL到Sentinel2 10m网格...")

        # 将SCL对齐到S2原始10m网格
        _resample_and_align(sentinel2_path, scl_path, scl_10m_aligned, is_categorical=True)

        if progress_callback:
            progress_callback("preprocessing", 0.58, "生成10m预测特征Parquet（全网格）...")

        block_rows = 512
        valid_pixels = 0
        scl_valid_pixels = 0
        black_zero_pixels = 0  # 仅诊断计数，不再用于剔除有效像元（见下方说明）
        # 研究区多边形掩膜（10m 网格，对齐 S2 参考格网；占比分母按研究区内像元）
        region10 = _rasterize_region(study_area_geojson, sentinel2_path)
        region_pixels10 = int(region10.sum()) if region10 is not None else 0
        region_valid_pixels = 0

        # Parquet 分块追加写（等价旧 to_csv(mode="a")，逐 block 一个 row group，内存可控）
        _predict_writer = None

        with rasterio.open(sentinel2_path) as s2_ds, rasterio.open(scl_10m_aligned) as scl_ds:
            profile = s2_ds.profile.copy()
            height, width = s2_ds.height, s2_ds.width
            nodata = s2_ds.nodata

            if (scl_ds.height, scl_ds.width) != (height, width):
                raise ValueError(
                    f"SCL栅格尺寸 {(scl_ds.height, scl_ds.width)} 与 Sentinel-2 10m 参考格网 "
                    f"{(height, width)} 不一致（预飞行校验）"
                )

            total_blocks = (height + block_rows - 1) // block_rows

            for block_idx, start in enumerate(range(0, height, block_rows)):
                stop = min(start + block_rows, height)
                window = rasterio.windows.Window(0, start, width, stop - start)

                b_arr = s2_ds.read(BAND_BLUE, window=window).astype(np.float32)
                g_arr = s2_ds.read(BAND_GREEN, window=window).astype(np.float32)
                r_arr = s2_ds.read(BAND_RED, window=window).astype(np.float32)
                n_arr = s2_ds.read(BAND_NIR, window=window).astype(np.float32)
                s_arr = s2_ds.read(BAND_SWIR1, window=window).astype(np.float32)

                if nodata is not None:
                    for arr in (b_arr, g_arr, r_arr, n_arr, s_arr):
                        arr[arr == nodata] = np.nan

                # 转换为地表反射率（数据获取阶段已按景应用 BOA_ADD_OFFSET 定标）
                for arr in (b_arr, g_arr, r_arr, n_arr, s_arr):
                    arr /= S2_SR_SCALE

                scl_block = scl_ds.read(1, window=window)
                scl_valid = _sentinel_scl_mask(scl_block)
                finite = (
                    np.isfinite(b_arr) & np.isfinite(g_arr) & np.isfinite(r_arr)
                    & np.isfinite(n_arr) & np.isfinite(s_arr)
                )
                # 诊断计数：全零仅用于统计展示，不再从 valid 中剔除。
                # 定标修正后合法反射率也可能出现单波段甚至（极小概率）全部为0，
                # 真正的"无数据"已经通过 nodata → NaN（finite 检查）正确识别，
                # 不应再用"数值恰好等于0"这一启发式二次剔除合法暗色像元。
                zero_outside = (
                    (b_arr == 0) & (g_arr == 0) & (r_arr == 0)
                    & (n_arr == 0) & (s_arr == 0)
                )
                valid = scl_valid & finite

                valid_pixels += int(valid.sum())
                scl_valid_pixels += int(scl_valid.sum())
                black_zero_pixels += int((zero_outside & finite).sum())
                if region10 is not None:
                    region_valid_pixels += int((valid & region10[start:stop, :]).sum())

                # 计算光谱指数
                ndvi_arr = (n_arr - r_arr) / (n_arr + r_arr + EPS)
                ndwi_arr = (g_arr - n_arr) / (g_arr + n_arr + EPS)
                ndbi_arr = (s_arr - n_arr) / (s_arr + n_arr + EPS)

                # 无效像元设NaN
                for arr in (r_arr, g_arr, b_arr, n_arr, s_arr, ndvi_arr, ndwi_arr, ndbi_arr):
                    arr[~valid] = np.nan

                block_height = stop - start
                df_predict = pd.DataFrame({
                    "row": np.repeat(np.arange(start, stop, dtype=np.int32), width),
                    "col": np.tile(np.arange(width, dtype=np.int32), block_height),
                    "R": r_arr.ravel(order="C"),
                    "G": g_arr.ravel(order="C"),
                    "B": b_arr.ravel(order="C"),
                    "NIR": n_arr.ravel(order="C"),
                    "SWIR1": s_arr.ravel(order="C"),
                    "NDVI": ndvi_arr.ravel(order="C"),
                    "NDWI": ndwi_arr.ravel(order="C"),
                    "NDBI": ndbi_arr.ravel(order="C"),
                }, columns=PREDICT_COLUMNS).replace([np.inf, -np.inf], np.nan)

                if _predict_writer is None:
                    # 先写 .partial，全部块成功后再原子替换为正式文件名：
                    # 中途异常不会在 predict_csv 留下截断的正式产物
                    _predict_writer = TableWriter(predict_csv + ".partial")
                _predict_writer.write(df_predict)

                if progress_callback:
                    progress_callback(
                        "preprocessing",
                        0.58 + 0.35 * (block_idx + 1) / total_blocks,
                        f"10m预测数据: block {block_idx + 1}/{total_blocks} (rows {start}-{stop - 1})",
                    )

        if _predict_writer is not None:
            _predict_writer.close()
            os.replace(predict_csv + ".partial", predict_csv)

        # 生成10m元数据
        predict_meta_dict = {
            "height": int(height),
            "width": int(width),
            "rows_total": int(height * width),
            "crs": str(profile.get("crs")) if profile.get("crs") else None,
            "transform": _transform_to_list(profile["transform"]),
            "ravel_order": "C",
            "description": "Parquet data row index = row * width + col; row/col columns are preserved",
            "columns": PREDICT_COLUMNS,
            "sentinel2_sr_scale": (
                f"exported reflectance = corrected DN / {S2_SR_SCALE:g}（数据获取阶段已按景定标）"
            ),
            "valid_pixels": int(valid_pixels),
            "scl_valid_pixels": int(scl_valid_pixels),
            "black_zero_pixels_diagnostic_only": int(black_zero_pixels),
            "invalid_rule": "feature columns are NaN when SCL is not class 4/5/6 or any band is non-finite (nodata)",
            # 研究区口径（分母=研究区多边形内像元；region_pixels=0 表示未提供研究区）
            "region_pixels": region_pixels10,
            "region_valid_pixels": int(region_valid_pixels),
            "region_valid_ratio": round(region_valid_pixels / region_pixels10, 6)
            if region_pixels10 > 0 else 0.0,
            "output_csv": predict_csv,
        }

        with open(predict_meta, "w", encoding="utf-8") as f:
            json.dump(predict_meta_dict, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - t_start

        if progress_callback:
            progress_callback(
                "preprocessing",
                1.0,
                f"预处理完成: 30m训练(step2) {len(df_train):,} 行, "
                f"完整30m约束层 {len(df_constraint):,} 行, "
                f"10m预测 {height * width:,} 像素 (有效 {valid_pixels:,}), "
                f"耗时 {elapsed:.1f}s",
            )

        return {
            "train_csv": train_csv,
            "train_meta": train_meta,
            "constraint_csv": constraint_csv,
            "constraint_meta": constraint_meta,
            "predict_csv": predict_csv,
            "predict_meta": predict_meta,
            "aligned_s2": aligned_s2,
            "aligned_scl": aligned_scl,
            "aligned_dem": aligned_dem,
            "train_rows": int(len(df_train)),
            "constraint_rows": int(len(df_constraint)),
            "predict_total_pixels": int(height * width),
            "predict_valid_pixels": int(valid_pixels),
            # 研究区口径统计（region_pixels10=0 表示未提供研究区，下游应回退 bbox 口径）
            "region_pixels_10m": region_pixels10,
            "region_valid_pixels_10m": int(region_valid_pixels),
            "region_pixels_30m": region_pixels30,
            "region_valid_pixels_30m": region_valid30,
            "elapsed_seconds": round(elapsed, 1),
        }
    finally:
        # aligned_* 中间产物按既定策略保留供 debug，仅清理明确无核心价值的合并临时文件
        merged_tmp_candidates = set()
        for _p in (landsat_path, sentinel2_path, qa_path, scl_path, dem_path):
            _d = os.path.dirname(_p)
            if _d:
                merged_tmp_candidates.add(os.path.join(_d, "_merged_vrt_temp.tif"))
        for _tmp in merged_tmp_candidates:
            if os.path.isfile(_tmp):
                try:
                    os.remove(_tmp)
                except OSError:
                    pass
