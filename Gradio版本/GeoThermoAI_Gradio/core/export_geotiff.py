"""
GeoTIFF导出模块

将包含 LST_final 列的CSV文件导出为GeoTIFF栅格影像。

CSV中row/col列指示像素位置。
"""

import json
import os
import time
from typing import Dict, Optional

import numpy as np
import pandas as pd
import rasterio


def export_geotiff(
    lst_final_csv: str,
    meta_10m_json: str,
    output_path: str,
    progress_callback=None,
) -> Dict:
    """
    将CSV中的 LST_final 列写入带地理参考的GeoTIFF影像。

    Args:
        lst_final_csv:   包含 LST_final、row、col 列的CSV路径
        meta_10m_json:   10m数据元信息JSON路径（含height, width, transform, crs）
        output_path:     输出GeoTIFF路径
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含输出路径、影像统计信息
            - output_path:  输出文件路径
            - stats:        {min, max, mean, std, valid_percent}
            - image_size:   {height, width}
            - file_size_mb: 文件大小(MB)
    """
    if progress_callback:
        progress_callback("export_geotiff", 0, "开始加载元数据...")

    # ── 1. 加载元数据 ──────────────────────────────────────────────────
    with open(meta_10m_json, "r", encoding="utf-8") as f:
        meta = json.load(f)

    height = meta["height"]
    width = meta["width"]
    transform = meta["transform"]
    # 处理crs格式
    crs_raw = meta.get("crs") or meta.get("target_epsg")
    if crs_raw and isinstance(crs_raw, str) and crs_raw.startswith("EPSG:"):
        crs = crs_raw
    else:
        crs = crs_raw

    if progress_callback:
        progress_callback(
            "export_geotiff", 0.1,
            f"影像尺寸: {height} × {width} ({height * width:,} 像素), CRS: {crs}",
        )

    # ── 2. 初始化空数组 ───────────────────────────────────────────────
    arr = np.full((height, width), np.nan, dtype=np.float32)

    if progress_callback:
        progress_callback("export_geotiff", 0.15, "开始读取CSV并填充栅格...")

    # ── 3. 分批读取CSV并填充数组 ───────────────────────────────────────
    chunk_size = 1_000_000
    total_rows = 0
    filled_rows = 0
    csv_offset = 0  # 绝对CSV行索引（0-based, 不含表头）

    for i, chunk in enumerate(pd.read_csv(lst_final_csv, chunksize=chunk_size)):
        n = len(chunk)

        # 根据CSV位置计算绝对row/col（row = idx // width, col = idx % width）
        idx = np.arange(csv_offset, csv_offset + n, dtype=np.int64)
        rows = idx // width
        cols = idx % width

        vals = chunk["LST_final"].values.astype(np.float32)
        mask = np.isfinite(vals)

        if mask.any():
            arr[rows[mask], cols[mask]] = vals[mask]
            filled_rows += mask.sum()

        total_rows += n
        csv_offset += n

        if progress_callback and (i + 1) % 10 == 0:
            progress_callback(
                "export_geotiff",
                0.15 + 0.35 * min(total_rows / (height * width), 1.0) if height * width > 0 else 0.5,
                f"已处理 {total_rows:,} 行, 填充 {filled_rows:,} 像素",
            )

    if progress_callback:
        progress_callback("export_geotiff", 0.55, "计算影像统计信息...")

    # ── 4. 计算统计信息 ────────────────────────────────────────────────
    nan_count = np.isnan(arr).sum()
    valid_percent = (1 - nan_count / (height * width)) * 100 if (height * width) > 0 else 0

    finite_vals = arr[np.isfinite(arr)]
    if len(finite_vals) > 0:
        stats_min = float(np.min(finite_vals))
        stats_max = float(np.max(finite_vals))
        stats_mean = float(np.mean(finite_vals))
        stats_std = float(np.std(finite_vals))
    else:
        stats_min = stats_max = stats_mean = stats_std = float("nan")

    if progress_callback:
        progress_callback(
            "export_geotiff", 0.65,
            f"统计: min={stats_min:.4f}, max={stats_max:.4f}, "
            f"mean={stats_mean:.4f}, valid={valid_percent:.1f}%",
        )

    # ── 5. 写入GeoTIFF ────────────────────────────────────────────────
    if progress_callback:
        progress_callback("export_geotiff", 0.7, "写入GeoTIFF文件...")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=rasterio.float32,
        crs=crs,
        transform=transform,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        nodata=np.nan,
    ) as dst:
        dst.write(arr, 1)

        # 写入 STATISTICS_ 标签
        tag_update = {}
        if not np.isnan(stats_min):
            tag_update["STATISTICS_MINIMUM"] = f"{stats_min:.15g}"
            tag_update["STATISTICS_MAXIMUM"] = f"{stats_max:.15g}"
            tag_update["STATISTICS_MEAN"] = f"{stats_mean:.15g}"
            tag_update["STATISTICS_STDDEV"] = f"{stats_std:.15g}"
            tag_update["STATISTICS_VALID_PERCENT"] = f"{valid_percent:.2f}"
        dst.update_tags(**tag_update)

    # ── 6. 文件大小 ────────────────────────────────────────────────────
    file_size_mb = os.path.getsize(output_path) / 1024 / 1024

    if progress_callback:
        progress_callback(
            "export_geotiff", 1.0,
            f"GeoTIFF已保存: {output_path} ({file_size_mb:.2f} MB)",
        )

    return {
        "output_path": output_path,
        "stats": {
            "min": round(stats_min, 6) if not np.isnan(stats_min) else None,
            "max": round(stats_max, 6) if not np.isnan(stats_max) else None,
            "mean": round(stats_mean, 6) if not np.isnan(stats_mean) else None,
            "std": round(stats_std, 6) if not np.isnan(stats_std) else None,
            "valid_percent": round(valid_percent, 2),
        },
        "image_size": {"height": height, "width": width},
        "file_size_mb": round(file_size_mb, 2),
        "total_rows_processed": total_rows,
        "filled_pixels": filled_rows,
    }
