"""
评价模块：粗尺度闭合协议

本模块只保留 TCR 后 10m 结果回聚合到 30m 是否闭合的评估
（evaluate_coarse_constraint_closure），测试集精度由 rf_model 阶段评估，
不在本模块重复。同时彻底删除 5K 阈值判据：不再输出 max_abs_deviation /
threshold_K / passed 字段及"通过/超出"文案；主展示只保留"各自完整有效输出
范围"的最低/最高端**有符号**温差；共同覆盖端点差作为可选后台诊断单独保存，
不在前端默认展示的字段路径下。
"""

import json
import os
from typing import Dict, Optional, Tuple

import numpy as np
from sklearn.metrics import r2_score

from . import grid_mapping
from .atomic_io import atomic_write_json
from .table_io import iter_chunks, read_table

COARSE_CONSTRAINT_CLOSURE_FILENAME = "coarse_constraint_closure.json"


def _safe_r2(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[Optional[float], Optional[str]]:
    """R² 在样本数<2或参考方差为0时返回 (None, 原因)，不把 NaN 写进 JSON。"""
    n = len(y_true)
    if n < 2:
        return None, f"n={n} < 2，R² 无法定义"
    var = float(np.var(y_true))
    if var <= 1e-12:
        return None, "参考值方差为0，R² 无法定义"
    return float(r2_score(y_true, y_pred)), None


def _load_transform_from_meta(meta_path: str) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ======================================================================
#  协议：粗尺度闭合（生产模式，TCR 后）
# ======================================================================


def evaluate_coarse_constraint_closure(
    constraint_csv: str,
    constraint_meta_json: str,
    lst_final_csv: str,
    meta_10m_json: str,
    output_dir: str,
    tcr_mode: Optional[str] = None,
    chunk_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """粗尺度闭合协议：生产 TCR 使用完整30m参考，报告10m结果回聚合到30m产品格网
    的算术均值闭合情况；不称为独立10m精度，不称能量/辐射守恒。
    固定输出 ``coarse_constraint_closure.json``。

    同时计算"各自完整有效输出范围"的最低/最高端有符号温差
    （主展示口径），以及仅供后台的 common_valid_footprint 诊断（不建议前端默认展示）。

    Args:
        constraint_csv:        完整30m约束层Parquet路径（30m_constraint_grid.parquet）
        constraint_meta_json:  完整30m约束层元数据JSON路径
        lst_final_csv:         TCR阶段输出的最终结果Parquet路径（含 row,col,LST_final）
        meta_10m_json:         10m元数据JSON路径
        output_dir:            输出目录
        tcr_mode:              可选，记录本轮使用的 TCR 模式（block_constant / smooth_recentered）
        chunk_size:            批处理大小
        progress_callback:     进度回调

    Returns:
        dict: 写入的完整 JSON 内容 + output_path
    """
    if progress_callback:
        progress_callback("evaluation_closure", 0, "开始粗尺度闭合评估...")

    os.makedirs(output_dir, exist_ok=True)

    coarse_meta = _load_transform_from_meta(constraint_meta_json)
    fine_meta = _load_transform_from_meta(meta_10m_json)
    coarse_transform = coarse_meta["transform"]
    fine_transform = fine_meta["transform"]
    coarse_height, coarse_width = int(coarse_meta["height"]), int(coarse_meta["width"])
    grid_mapping.assert_same_crs(
        coarse_meta.get("crs"), fine_meta.get("crs"), context="闭合评价: 30m约束层 vs 10m最终结果"
    )

    # ── 步骤1: 完整30m参考（用于"各自完整有效范围"的30m一侧 + 闭合参考） ──
    if progress_callback:
        progress_callback("evaluation_closure", 0.1, "加载完整30m约束层参考...")

    df_30m = read_table(constraint_csv, columns=["row", "col", "LST"])
    if len(df_30m) == 0:
        raise ValueError("30m 约束层为空表，无法做粗尺度闭合评估（上游约束层未生成有效像元）")
    full_30m_min = float(df_30m["LST"].min())
    full_30m_max = float(df_30m["LST"].max())
    n_valid_30m = int(len(df_30m))

    lst_true_dense = np.full(coarse_height * coarse_width, np.nan, dtype=np.float64)
    r30 = df_30m["row"].values.astype(np.int64)
    c30 = df_30m["col"].values.astype(np.int64)
    in_range = (r30 >= 0) & (r30 < coarse_height) & (c30 >= 0) & (c30 < coarse_width)
    lst_true_dense[r30[in_range] * coarse_width + c30[in_range]] = df_30m["LST"].values[in_range]
    del df_30m

    # ── 步骤2: 流式扫描10m最终结果，同时统计"完整10m范围"与按30m格聚合 ──
    if progress_callback:
        progress_callback("evaluation_closure", 0.2, "扫描10m最终结果并按统一仿射映射聚合到30m格...")

    sum_agg = np.zeros(coarse_height * coarse_width, dtype=np.float64)
    count_agg = np.zeros(coarse_height * coarse_width, dtype=np.int64)
    full_10m_min, full_10m_max = np.inf, -np.inf
    n_valid_10m = 0
    out_of_grid = 0
    chunk_idx = 0

    for chunk in iter_chunks(lst_final_csv, columns=["row", "col", "LST_final"], batch_size=chunk_size):
        mask = chunk["LST_final"].notna()
        n_valid = int(mask.sum())
        if n_valid == 0:
            chunk_idx += 1
            continue
        valid = chunk[mask]
        lst_vals = valid["LST_final"].values.astype(np.float64)
        full_10m_min = min(full_10m_min, float(lst_vals.min()))
        full_10m_max = max(full_10m_max, float(lst_vals.max()))
        n_valid_10m += n_valid

        fine_row = valid["row"].values.astype(np.float64)
        fine_col = valid["col"].values.astype(np.float64)
        coarse_row, coarse_col, inside = grid_mapping.fine_to_coarse_index(
            fine_row, fine_col, fine_transform, coarse_transform, coarse_height, coarse_width
        )
        out_of_grid += int((~inside).sum())
        if inside.any():
            s_arr, c_arr = grid_mapping.aggregate_by_coarse_cell(
                coarse_row[inside], coarse_col[inside], lst_vals[inside], coarse_height, coarse_width
            )
            sum_agg += s_arr
            count_agg += c_arr

        chunk_idx += 1
        if progress_callback and chunk_idx % 50 == 0:
            progress_callback(
                "evaluation_closure", 0.2 + 0.5 * min(chunk_idx / 500, 1.0),
                f"已处理 {chunk_idx} 批, {n_valid_10m:,} 有效像素",
            )

    if n_valid_10m == 0:
        raise RuntimeError("粗尺度闭合评估失败：10m最终结果没有任何有效像素")

    # ── 步骤3: 闭合指标（MB/MAE/RMSE/R²，matched = 同时有聚合值与30m参考的格）──
    if progress_callback:
        progress_callback("evaluation_closure", 0.75, "计算闭合指标...")

    has_agg = count_agg > 0
    matched = has_agg & np.isfinite(lst_true_dense)
    n_matched = int(matched.sum())
    if n_matched == 0:
        raise RuntimeError("粗尺度闭合评估失败：10m聚合结果与30m参考没有任何共同覆盖的格")

    mean_agg = np.zeros_like(sum_agg)
    mean_agg[has_agg] = sum_agg[has_agg] / count_agg[has_agg]

    agg_matched = mean_agg[matched]
    ref_matched = lst_true_dense[matched]
    diff = agg_matched - ref_matched  # MB 定义：正值=回聚合结果偏暖
    mb = float(np.mean(diff))
    mae = float(np.mean(np.abs(diff)))
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    r2, r2_reason = _safe_r2(ref_matched, agg_matched)

    n_reference_valid_cells = int(np.isfinite(lst_true_dense).sum())
    coverage_ratio = round(n_matched / n_reference_valid_cells, 6) if n_reference_valid_cells > 0 else 0.0

    subpixel_counts = count_agg[matched]

    # ── 步骤4: 值域范围（各自完整有效输出范围的有符号端点差）──
    low_end_diff = full_10m_min - full_30m_min
    high_end_diff = full_10m_max - full_30m_max

    # ── 步骤5: common_valid_footprint（仅供后台诊断，前端默认不展示）───────
    common_min_30m = float(ref_matched.min())
    common_max_30m = float(ref_matched.max())
    common_min_10m = float(agg_matched.min())
    common_max_10m = float(agg_matched.max())

    result = {
        "schema_version": 2,
        "protocol": "coarse_constraint_closure",
        "description": (
            "生产模式下 TCR 使用完整30m参考格网，10m最终结果回聚合到30m产品格网后"
            "与参考的算术均值闭合度；不是独立10m精度，不是未见标签预测，"
            "不代表辐射或能量守恒"
        ),
        "mb_definition": "MB = aggregate_10m_K - reference_30m_K；正值表示回聚合结果偏暖，负值偏冷",
        "tcr_mode": tcr_mode,
        "closure": {
            "n_matched_cells": n_matched,
            "n_reference_valid_cells": n_reference_valid_cells,
            "coverage_ratio": coverage_ratio,
            "out_of_grid_fine_pixels": int(out_of_grid),
            "metrics": {
                "MB_K": round(mb, 6),
                "MAE_K": round(mae, 6),
                "RMSE_K": round(rmse, 6),
                "R2": r2,
                "r2_null_reason": r2_reason,
            },
            "subpixel_count_per_matched_cell": {
                "min": int(subpixel_counts.min()),
                "median": float(np.median(subpixel_counts)),
                "p95": float(np.percentile(subpixel_counts, 95)),
                "max": int(subpixel_counts.max()),
            },
        },
        "value_range": {
            "comparison_scope": "each_output_full_valid_extent",
            "min_30m_K": round(full_30m_min, 4),
            "min_10m_K": round(full_10m_min, 4),
            "low_end_difference_K": round(low_end_diff, 4),
            "max_30m_K": round(full_30m_max, 4),
            "max_10m_K": round(full_10m_max, 4),
            "high_end_difference_K": round(high_end_diff, 4),
            "n_valid_30m": n_valid_30m,
            "n_valid_10m": n_valid_10m,
            "sign_convention": "正值表示10m端点温度更高，负值表示更低（10m端点 − 30m端点）",
        },
        "common_valid_footprint_diagnostic": {
            "note": "仅供后台/开发排查的可比性诊断，前端默认不展示；两侧均取自同一 matched 父格集合",
            "min_30m_K": round(common_min_30m, 4),
            "min_10m_K": round(common_min_10m, 4),
            "low_end_difference_K": round(common_min_10m - common_min_30m, 4),
            "max_30m_K": round(common_max_30m, 4),
            "max_10m_K": round(common_max_10m, 4),
            "high_end_difference_K": round(common_max_10m - common_max_30m, 4),
            "n": n_matched,
        },
    }

    output_path = os.path.join(output_dir, COARSE_CONSTRAINT_CLOSURE_FILENAME)
    atomic_write_json(output_path, result)
    result["output_path"] = output_path

    if progress_callback:
        progress_callback(
            "evaluation_closure", 1.0,
            f"粗尺度闭合评估完成: MB={mb:.4f}K, MAE={mae:.4f}K, RMSE={rmse:.4f}K, "
            f"匹配 {n_matched:,} 格; 低端差={low_end_diff:+.4f}K, 高端差={high_end_diff:+.4f}K",
        )

    return result
