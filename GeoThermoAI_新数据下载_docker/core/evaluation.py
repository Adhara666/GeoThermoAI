"""
评价模块（A-07 重写 + 第6节 5K判据删除 / 用户确认第4、5条）

旧版 ``evaluate_spatial_consistency`` 把两件不同的事混在一份 JSON 里：
    - RF 对测试集的独立预测精度；
    - TCR 用了拆分前完整30m标签做约束后，10m结果回聚合到30m是否闭合。
    第二组指标其实用了包含 test 标签的完整参考做闭合，再拿同一个 test 对比，
    容易被误当成"独立精度"。

本模块拆成两个函数、两个固定 JSON schema，互不混用：
    - evaluate_independent_prediction():   独立预测协议（TCR 前，未见标签）
    - evaluate_coarse_constraint_closure(): 粗尺度闭合协议（TCR 后，生产模式）

同时按用户确认第5条彻底删除 5K 阈值判据：不再输出 max_abs_deviation /
threshold_K / passed 字段及"通过/超出"文案；主展示只保留"各自完整有效输出
范围"的最低/最高端**有符号**温差；共同覆盖端点差作为可选后台诊断单独保存，
不在前端默认展示的字段路径下。
"""

import json
import os
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import r2_score

from . import grid_mapping
from .atomic_io import atomic_write_json
from .rf_model import load_model_and_features

INDEPENDENT_PREDICTION_FILENAME = "independent_prediction.json"
COARSE_CONSTRAINT_CLOSURE_FILENAME = "coarse_constraint_closure.json"


def _safe_r2(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[Optional[float], Optional[str]]:
    """R² 在样本数<2或参考方差为0时返回 (None, 原因)，不把 NaN 写进 JSON（A-07要求）。"""
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
#  协议一：独立预测（TCR 前，未见标签的 30m 测试集）
# ======================================================================


def evaluate_independent_prediction(
    test_csv: str,
    model_path: str,
    output_dir: str,
    split_info: Optional[dict] = None,
    progress_callback=None,
) -> Dict:
    """独立预测协议：test 不参与 TTRI 拟合、调参或 TCR，报告 R²/RMSE/MAE/MB、
    样本数和空间范围。固定输出 ``independent_prediction.json``。

    Args:
        test_csv:     30m测试集CSV路径（空间块+guard buffer 划分产物）
        model_path:   已训练RF模型.pkl路径
        output_dir:   输出目录
        split_info:   可选，split_dataset.py 产出的 split_info.json 内容，
                      用于在报告中注明所用划分方法与 guard buffer
        progress_callback: 进度回调

    Returns:
        dict: 写入的完整 JSON 内容 + output_path
    """
    if progress_callback:
        progress_callback("evaluation_independent", 0, "开始独立预测评估...")

    os.makedirs(output_dir, exist_ok=True)

    model, feature_cols, model_meta = load_model_and_features(model_path)
    df_test = pd.read_csv(test_csv)
    missing = [c for c in feature_cols + ["LST"] if c not in df_test.columns]
    if missing:
        raise ValueError(f"测试集缺少必需的列: {missing}")

    X_test = df_test[feature_cols].values
    y_test = df_test["LST"].values.astype(np.float64)
    y_pred = model.predict(X_test).astype(np.float64)

    r2, r2_reason = _safe_r2(y_test, y_pred)
    rmse = float(np.sqrt(np.mean((y_pred - y_test) ** 2)))
    mae = float(np.mean(np.abs(y_pred - y_test)))
    mb = float(np.mean(y_pred - y_test))

    spatial_extent = None
    if "row" in df_test.columns and "col" in df_test.columns:
        spatial_extent = {
            "min_row": int(df_test["row"].min()), "max_row": int(df_test["row"].max()),
            "min_col": int(df_test["col"].min()), "max_col": int(df_test["col"].max()),
        }

    result = {
        "schema_version": 1,
        "protocol": "independent_prediction",
        "description": (
            "test 集在空间上与 train/validate 隔离（含 guard buffer），且不参与 "
            "TTRI 拟合、调参或 TCR；本结果反映模型在未见空间区域的30m预测泛化能力，"
            "不含任何粗尺度约束信息"
        ),
        "mb_definition": "MB = mean(prediction - reference)，单位 K；正值表示预测整体偏暖",
        "n_samples": int(len(df_test)),
        "metrics": {
            "R2": r2, "r2_null_reason": r2_reason,
            "RMSE_K": round(rmse, 6),
            "MAE_K": round(mae, 6),
            "MB_K": round(mb, 6),
        },
        "spatial_extent_rowcol": spatial_extent,
        "split_method": (split_info or {}).get("method", "unknown"),
        "guard_buffer_m": (split_info or {}).get("guard_buffer_m"),
        "block_size_px": (split_info or {}).get("block_size_px"),
        "model_path": model_path,
        "features": feature_cols,
    }

    output_path = os.path.join(output_dir, INDEPENDENT_PREDICTION_FILENAME)
    atomic_write_json(output_path, result)
    result["output_path"] = output_path

    if progress_callback:
        progress_callback(
            "evaluation_independent", 1.0,
            f"独立预测评估完成: R²={r2}, RMSE={rmse:.4f}K, MAE={mae:.4f}K, MB={mb:.4f}K",
        )

    return result


# ======================================================================
#  协议二：粗尺度闭合（生产模式，TCR 后）
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

    同时按用户确认第5条计算"各自完整有效输出范围"的最低/最高端有符号温差
    （主展示口径），以及仅供后台的 common_valid_footprint 诊断（不建议前端默认展示）。

    Args:
        constraint_csv:        完整30m约束层CSV路径（30m_constraint_grid.csv）
        constraint_meta_json:  完整30m约束层元数据JSON路径
        lst_final_csv:         TCR阶段输出的最终结果CSV路径（含 row,col,LST_final）
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

    df_30m = pd.read_csv(constraint_csv, usecols=["row", "col", "LST"])
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

    for chunk in pd.read_csv(lst_final_csv, chunksize=chunk_size, usecols=["row", "col", "LST_final"]):
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

    # ── 步骤4: 值域范围（用户确认第5条：各自完整有效输出范围的有符号端点差）──
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
            "不代表辐射或能量守恒（A-06/A-07）"
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
