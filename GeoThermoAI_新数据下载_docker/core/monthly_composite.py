"""
月度影像合成模块（数据获取阶段：先把该月全部无云影像合成为一张月度影像）

月度合成 = 对同一月份内多景影像做像元级合成：
- 每景先被 gdal.Warp 到统一网格（研究区 bbox + UTM + 目标分辨率），
- 再按像元聚合（默认中位数，对云/异常稳健）；
- 可选的逐景有效掩膜（如 SCL/QA 云掩膜）用于只统计无云像元。

设计要点：
- 分块处理（block_rows 行一块），峰值内存 = 块内像元数 × 景数，与总像元数无关；
- 输入各景必须已网格对齐（同 CRS / 同分辨率 / 同 extent / 同行列数）；
- 输出 Float32 月度合成 tif + 有效观测计数 tif（uint16）；
- 无掩膜时全像元视为有效；掩膜约定 1=有效，0=无效（NaN/越界由 warp 填 nodata）。
"""

import os
import logging
import threading
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def composite_median(
    tile_paths: List[str],
    output_path: str,
    mask_paths: Optional[List[str]] = None,
    block_rows: int = 256,
    nodata: float = -9999.0,
    progress_callback=None,
) -> dict:
    """对 N 个已网格对齐的同波段 GeoTIFF 做像元级中位数合成。

    Args:
        tile_paths:  每景波段 tif 路径（已对齐）。
        output_path: 合成输出 tif 路径。
        mask_paths:  每景有效掩膜路径（1=有效，0=无效）；长度须与 tile_paths 一致，
                     可为 None 表示全部有效。
        block_rows:  每块处理的行数（控制内存峰值）。
        nodata:      合成输出的 NoData 值（无任何有效观测的像元）。
        progress_callback: 可选回调 (step, percent, message)。

    Returns:
        {"output_path":..., "count_path":..., "valid_percent":..., "width":..., "height":...}
    """
    if not tile_paths:
        raise ValueError("月度合成至少需要一景影像")
    from osgeo import gdal

    src = gdal.Open(tile_paths[0])
    if src is None:
        raise ValueError(f"无法打开合成输入: {tile_paths[0]}")
    width, height = src.RasterXSize, src.RasterYSize
    geotransform = src.GetGeoTransform()
    projection = src.GetProjection()
    band1 = src.GetRasterBand(1)
    src_nodata = band1.GetNoDataValue()
    src_dtype = band1.DataType
    src = None

    masks = list(mask_paths) if mask_paths else []
    if masks and len(masks) != len(tile_paths):
        masks = []
        logger.warning("[monthly] 掩膜数量与影像数量不一致，忽略掩膜按全有效处理")

    n = len(tile_paths)
    out_dir = os.path.dirname(output_path)
    os.makedirs(out_dir, exist_ok=True)
    count_path = os.path.splitext(output_path)[0] + "_count.tif"

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(output_path, width, height, 1, gdal.GDT_Float32,
                           options=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=YES"])
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)
    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(nodata)

    cnt_ds = driver.Create(count_path, width, height, 1, gdal.GDT_UInt16,
                           options=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])
    cnt_ds.SetGeoTransform(geotransform)
    cnt_ds.SetProjection(projection)
    cnt_band = cnt_ds.GetRasterBand(1)
    cnt_band.SetNoDataValue(0)

    total_valid = 0
    total_pixels = 0
    try:
        row0 = 0
        while row0 < height:
            block_h = min(block_rows, height - row0)
            # 读取所有景的当前块（Float64 累积，避免中位数受 nodata 污染）
            stack = np.full((n, block_h, width), np.nan, dtype=np.float64)
            valid = np.ones((n, block_h, width), dtype=bool)
            for i, tp in enumerate(tile_paths):
                ds = gdal.Open(tp)
                if ds is None:
                    valid[i, :, :] = False
                    continue
                arr = ds.GetRasterBand(1).ReadAsArray(0, row0, width, block_h)
                ds = None
                if arr is None:
                    valid[i, :, :] = False
                    continue
                arr = np.asarray(arr, dtype=np.float64)
                if src_nodata is not None:
                    arr = np.where(arr == src_nodata, np.nan, arr)
                stack[i] = arr

            # 掩膜（约定 1=有效/晴空，0=云/无效）。某景无掩膜（None 或文件缺失）
            # 时视为该景全部有效，避免个别景缺掩膜导致整批掩膜被禁用。
            if masks:
                for i, mp in enumerate(masks):
                    if not mp or not os.path.isfile(mp):
                        continue
                    ds = gdal.Open(mp)
                    if ds is None:
                        continue
                    m = ds.GetRasterBand(1).ReadAsArray(0, row0, width, block_h)
                    ds = None
                    if m is None:
                        continue
                    valid[i, :, :] &= np.asarray(m) > 0
                stack = np.where(valid, stack, np.nan)

            stack = np.where(np.isfinite(stack), stack, np.nan)
            count = np.sum(~np.isnan(stack), axis=0).astype(np.uint16)
            with np.errstate(invalid="ignore", over="ignore"):
                med = np.nanmedian(stack, axis=0)
            med = np.where(count > 0, med, nodata)
            med = np.asarray(med, dtype=np.float32)

            out_band.WriteArray(med, 0, row0)
            cnt_band.WriteArray(count, 0, row0)
            total_valid += int(np.sum(count > 0))
            total_pixels += block_h * width
            row0 += block_h

            if progress_callback:
                progress_callback("monthly_composite", min(row0 / height, 1.0),
                                  f"月度合成 {min(row0, height)}/{height} 行")
        out_band.FlushCache()
        cnt_band.FlushCache()
    finally:
        out_ds = None
        cnt_ds = None

    return {
        "output_path": output_path,
        "count_path": count_path,
        "valid_percent": round(100.0 * total_valid / max(total_pixels, 1), 2),
        "width": width,
        "height": height,
        "scenes": n,
    }
