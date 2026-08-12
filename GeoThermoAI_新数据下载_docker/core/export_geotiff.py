"""
GeoTIFF导出模块（重写）

严格按 Parquet 的 row,col 写入栅格，不再依赖表格文件偏移/行序推导像元位置。
旧实现虽然要求表含 row,col 列，但实际用 ``idx = csv_offset + i; row = idx // width``
按连续文件偏移重新计算行列，只在"全网格、严格行优先、未被重排"这一隐含前提下
碰巧正确；一旦只写有效行、并发合并、排序或断点续跑，栅格会静默错位（已用乱序
2x2 CSV 复现）。

本实现改为：读取表格的 row/col 列直接定位到二维数组对应位置；越界或重复
(row,col) 立即结构化失败，不静默覆盖或忽略。统计信息使用在线（Welford）算法
增量计算，不再对整幅数组做布尔索引复制。
"""

import json
import os
import time
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import rasterio

from .atomic_io import write_verified
from .table_io import iter_chunks


class _OnlineStats:
    """Welford 在线算法：单遍增量计算 count/mean/std/min/max，避免复制全幅有限值数组。"""

    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.vmin = np.inf
        self.vmax = -np.inf

    def update(self, values: np.ndarray) -> None:
        if values.size == 0:
            return
        self.vmin = min(self.vmin, float(values.min()))
        self.vmax = max(self.vmax, float(values.max()))
        # 分块 Welford 合并
        n_b = values.size
        mean_b = float(values.mean())
        m2_b = float(((values - mean_b) ** 2).sum())
        n_a = self.count
        delta = mean_b - self.mean
        new_count = n_a + n_b
        self.mean = self.mean + delta * n_b / new_count
        self.m2 = self.m2 + m2_b + delta ** 2 * n_a * n_b / new_count
        self.count = new_count

    def finalize(self) -> Dict:
        if self.count == 0:
            return {"min": None, "max": None, "mean": None, "std": None}
        variance = self.m2 / self.count
        return {
            "min": round(self.vmin, 6),
            "max": round(self.vmax, 6),
            "mean": round(self.mean, 6),
            "std": round(float(np.sqrt(max(variance, 0.0))), 6),
        }


def export_geotiff(
    lst_final_csv: str,
    meta_10m_json: str,
    output_path: str,
    value_column: str = "LST_final",
    progress_callback=None,
) -> Dict:
    """
    将Parquet中的 LST_final 列严格按 row,col 写入带地理参考的GeoTIFF影像。

    Args:
        lst_final_csv:   包含 LST_final、row、col 列的Parquet路径
        meta_10m_json:   10m数据元信息JSON路径（含height, width, transform, crs）
        output_path:     输出GeoTIFF路径（固定名，通过 .partial + 原子替换写入）
        value_column:    写入的数值列名，默认 LST_final
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含输出路径、影像统计信息
            - output_path:  输出文件路径
            - stats:        {min, max, mean, std, valid_percent}
            - image_size:   {height, width}
            - file_size_mb: 文件大小(MB)

    Raises:
        ValueError: 表中出现 row/col 越界或重复索引（结构化失败，不静默覆盖/忽略）
    """
    if progress_callback:
        progress_callback("export_geotiff", 0, "开始加载元数据...")

    # ── 1. 加载元数据 ──────────────────────────────────────────────────
    with open(meta_10m_json, "r", encoding="utf-8") as f:
        meta = json.load(f)

    height = meta["height"]
    width = meta["width"]
    transform = meta["transform"]
    crs_raw = meta.get("crs") or meta.get("target_epsg")
    crs = crs_raw

    if progress_callback:
        progress_callback(
            "export_geotiff", 0.1,
            f"影像尺寸: {height} × {width} ({height * width:,} 像素), CRS: {crs}",
        )

    # ── 2. 初始化空数组 ───────────────────────────────────────────────
    arr = np.full((height, width), np.nan, dtype=np.float32)
    seen = np.zeros((height, width), dtype=bool)

    if progress_callback:
        progress_callback("export_geotiff", 0.15, "开始按 row,col 读取Parquet并填充栅格...")

    # ── 3. 分批读取Parquet，严格按 row,col 定位写入 ──────────────────────
    chunk_size = 1_000_000
    total_rows = 0
    filled_rows = 0
    stats = _OnlineStats()

    for i, chunk in enumerate(iter_chunks(lst_final_csv, columns=["row", "col", value_column], batch_size=chunk_size)):
        n = len(chunk)

        # 空值必须先于 int64 转换检出：NaN 转 int64 会变成 INT64_MIN
        # (-9223372036854775808)，之后只能报「越界」，读者无法看出真实原因是
        # 「row/col 是空的」——这通常意味着上游中间表写入不完整或被截断。
        null_index = chunk["row"].isna() | chunk["col"].isna()
        if null_index.any():
            n_null = int(null_index.sum())
            first_bad = int(null_index.values.argmax()) + i * chunk_size
            raise ValueError(
                f"GeoTIFF 导出失败：第 {i} 个批次中有 {n_null} 行的 row 或 col 是空值"
                f"（首个出现在第 {first_bad:,} 行附近）。行列号本应是完整的整数，"
                f"出现空值通常说明上游中间表写入不完整（磁盘空间不足或写入被中断），"
                f"而不是模型或算法问题；拒绝导出以避免静默错位"
            )

        rows = chunk["row"].values.astype(np.int64)
        cols = chunk["col"].values.astype(np.int64)
        vals = chunk[value_column].values.astype(np.float32)

        out_of_bounds = (rows < 0) | (rows >= height) | (cols < 0) | (cols >= width)
        if out_of_bounds.any():
            n_bad = int(out_of_bounds.sum())
            examples = list(zip(rows[out_of_bounds][:5].tolist(), cols[out_of_bounds][:5].tolist()))
            raise ValueError(
                f"GeoTIFF 导出失败：第 {i} 个批次中有 {n_bad} 行 row/col 越界 "
                f"(height={height}, width={width})，例如 {examples}；拒绝导出以避免静默错位"
            )

        dup_mask = seen[rows, cols]
        if dup_mask.any():
            n_dup = int(dup_mask.sum())
            examples = list(zip(rows[dup_mask][:5].tolist(), cols[dup_mask][:5].tolist()))
            raise ValueError(
                f"GeoTIFF 导出失败：第 {i} 个批次中有 {n_dup} 行 row/col 与此前已写入的像元重复，"
                f"例如 {examples}；每个像元最多允许出现一次，拒绝导出以避免静默覆盖"
            )
        seen[rows, cols] = True

        finite = np.isfinite(vals)
        if finite.any():
            arr[rows[finite], cols[finite]] = vals[finite]
            stats.update(vals[finite])
            filled_rows += int(finite.sum())

        total_rows += n

        if progress_callback and (i + 1) % 10 == 0:
            progress_callback(
                "export_geotiff",
                0.15 + 0.35 * min(total_rows / (height * width), 1.0) if height * width > 0 else 0.5,
                f"已处理 {total_rows:,} 行, 填充 {filled_rows:,} 像素",
            )

    if progress_callback:
        progress_callback("export_geotiff", 0.55, "统计信息计算完成（在线算法，无需复制全幅数组）")

    stat_result = stats.finalize()
    # 有效占比口径：优先用研究区多边形内像元数（meta.region_pixels），
    # 未提供研究区时回退 bbox 全网格（height*width）
    region_pixels = int(meta.get("region_pixels") or 0)
    _denom = region_pixels if region_pixels > 0 else (height * width)
    valid_percent = (filled_rows / _denom) * 100 if _denom > 0 else 0

    if progress_callback:
        _min = stat_result["min"] if stat_result["min"] is not None else float("nan")
        _max = stat_result["max"] if stat_result["max"] is not None else float("nan")
        _mean = stat_result["mean"] if stat_result["mean"] is not None else float("nan")
        progress_callback(
            "export_geotiff", 0.65,
            f"统计: min={_min:.4f}, max={_max:.4f}, mean={_mean:.4f}, valid={valid_percent:.1f}%",
        )

    # ── 5. 写入GeoTIFF（.partial + 校验 + 原子替换）───────────
    if progress_callback:
        progress_callback("export_geotiff", 0.7, "写入GeoTIFF文件...")

    def _build(tmp_path: str) -> None:
        with rasterio.open(
            tmp_path, "w", driver="GTiff",
            height=height, width=width, count=1,
            dtype=rasterio.float32, crs=crs, transform=transform,
            compress="deflate", predictor=3, tiled=True, blockxsize=256, blockysize=256,
            nodata=np.nan,
        ) as dst:
            dst.write(arr, 1)
            dst.set_band_description(1, "LST_final: 10m grid downscaled LST estimate (Kelvin)")
            tag_update = {"UNITS": "K"}
            if region_pixels > 0:
                # 研究区口径：记录研究区多边形内像元数，供空洞填补等下游做同口径统计
                tag_update["REGION_PIXELS"] = str(region_pixels)
            if stat_result["min"] is not None:
                tag_update["STATISTICS_MINIMUM"] = f"{stat_result['min']:.15g}"
                tag_update["STATISTICS_MAXIMUM"] = f"{stat_result['max']:.15g}"
                tag_update["STATISTICS_MEAN"] = f"{stat_result['mean']:.15g}"
                tag_update["STATISTICS_STDDEV"] = f"{stat_result['std']:.15g}"
                tag_update["STATISTICS_VALID_PERCENT"] = f"{valid_percent:.2f}"
            dst.update_tags(**tag_update)
            dst.update_tags(1, units="K")

    def _validator(tmp_path: str) -> Tuple[bool, str]:
        try:
            with rasterio.open(tmp_path) as check:
                if check.count != 1:
                    return False, f"波段数异常: {check.count}"
                if (check.height, check.width) != (height, width):
                    return False, f"尺寸不匹配: {(check.height, check.width)} != {(height, width)}"
                if crs and check.crs is None:
                    return False, "CRS 丢失"
                sample = check.read(1, out_shape=(min(height, 256), min(width, 256)))
                if not np.isfinite(sample).any() and filled_rows > 0:
                    return False, "重新打开后采样窗口未发现任何有限值，可能写入损坏"
        except Exception as e:
            return False, f"重新打开校验失败: {e}"
        return True, ""

    write_verified(_build, output_path, _validator)

    # ── 6. 文件大小 ────────────────────────────────────────────────────
    file_size_mb = os.path.getsize(output_path) / 1024 / 1024

    if progress_callback:
        progress_callback(
            "export_geotiff", 1.0,
            f"GeoTIFF已保存: {output_path} ({file_size_mb:.2f} MB)",
        )

    return {
        "output_path": output_path,
        "stats": {**stat_result, "valid_percent": round(valid_percent, 2)},
        "image_size": {"height": height, "width": width},
        "file_size_mb": round(file_size_mb, 2),
        "total_rows_processed": total_rows,
        "filled_pixels": filled_rows,
    }
