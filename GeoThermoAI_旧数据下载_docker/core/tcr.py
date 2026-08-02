"""
TCR（热约束残差，Thermal Constraint Residual）+ LST_final 计算模块

计算流程:
  Phase 1: 加载模型 → 批次预测10m LST_pred → 按30m块聚合 → 计算TCR_30m
           → 构建30m TCR规则网格 + 双线性插值器
           （LST_pred 缓存在内存中，消除临时文件）
  Phase 2: 批次读取10m数据 → 双线性插值TCR → 计算 LST_final = LST_pred + TCR
           → 写入最终CSV（无中间文件）

TCR_30m = LST_true_30m - mean(LST_pred_in_30m_block)
LST_final = LST_pred + TCR
"""

import json
import os
import time
import warnings
from typing import Dict, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree

warnings.filterwarnings("ignore")

# ── 常量 ──────────────────────────────────────────────────────────────
TARGET_COL = "LST"
TCR_COL = "TCR"
LST_PRED_COL = "LST_pred"
LST_FINAL_COL = "LST_final"
SPECTRAL_COLS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI", "TTRI"]


# ======================================================================
#  辅助函数
# ======================================================================


def _load_transform(meta_path: str) -> list:
    """从meta.json加载仿射变换参数。"""
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    return meta["transform"]


def _col_row_to_utm(
    col: np.ndarray, row: np.ndarray, transform: list
) -> Tuple[np.ndarray, np.ndarray]:
    a, b, c, d, e, f = transform
    x = a * col + b * row + c
    y = d * col + e * row + f
    return x, y


def _load_model_meta(model_path: str, metrics_path: str) -> Tuple:
    """加载训练好的模型和特征列表。"""
    model = joblib.load(model_path)
    if os.path.exists(metrics_path):
        with open(metrics_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        feature_cols = meta.get("features", SPECTRAL_COLS)
    else:
        feature_cols = SPECTRAL_COLS
    return model, feature_cols


def _is_valid_row(df_slice: pd.DataFrame) -> np.ndarray:
    """判断有效行（所有光谱列均有值）。"""
    return df_slice[SPECTRAL_COLS].notna().all(axis=1)


def _build_tcr_grid(
    df_30m: pd.DataFrame, tcr_values: np.ndarray
) -> RegularGridInterpolator:
    grid_rows = np.sort(df_30m["row"].unique())
    grid_cols = np.sort(df_30m["col"].unique())

    row_to_idx = {r: i for i, r in enumerate(grid_rows)}
    col_to_idx = {c: j for j, c in enumerate(grid_cols)}

    grid = np.full((len(grid_rows), len(grid_cols)), np.nan, dtype=np.float64)
    ri = df_30m["row"].map(row_to_idx).values
    ci = df_30m["col"].map(col_to_idx).values
    grid[ri, ci] = tcr_values

    interp = RegularGridInterpolator(
        (grid_rows, grid_cols), grid,
        method="linear", bounds_error=False, fill_value=np.nan,
    )
    return interp


# ======================================================================
#  主函数（TCR + LST_final 一步完成）
# ======================================================================


def compute_tcr(
    data_30m_csv: str,
    meta_30m_json: str,
    predict_10m_csv: str,
    meta_10m_json: str,
    model_path: str,
    output_path: str,
    batch_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """
    计算TCR（热约束残差）+ LST_final，一步完成。

    Phase 1: 10m预测 + 30m块聚合（LST_pred缓存在内存中）
    Phase 2: 双线性插值TCR + 计算LST_final → 写入最终CSV

    LST_final = LST_pred + TCR

    Args:
        data_30m_csv:    30m全量数据CSV路径
        meta_30m_json:   30m元数据JSON路径
        predict_10m_csv: 10m预测数据CSV路径（含TTRI列）
        meta_10m_json:   10m元数据JSON路径
        model_path:      训练好的RF模型.pkl路径
        output_path:     输出CSV路径（最终输出，含LST_pred, TCR, LST_final）
        batch_size:      批处理大小（默认500000）
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含输出路径和统计信息
    """
    t_start = time.time()
    if progress_callback:
        progress_callback("tcr", 0, "开始TCR + LST_final 计算...")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    metrics_path = model_path.replace("_model_", "_metrics_").replace(".pkl", ".json")

    # ─── 1. 加载仿射变换 ──────────────────────────────────────────────
    transform_30m = _load_transform(meta_30m_json)
    transform_10m = _load_transform(meta_10m_json)

    # ─── 2. 加载模型 ──────────────────────────────────────────────────
    model, feature_cols = _load_model_meta(model_path, metrics_path)

    # ==================================================================
    #  Phase 1: 10m预测 + 30m块聚合（LST_pred 缓存内存，无临时文件）
    # ==================================================================
    if progress_callback:
        progress_callback("tcr", 0.08, "Phase 1: 加载30m全量数据...")

    df_30m = pd.read_csv(data_30m_csv, usecols=["row", "col", "LST"])
    n_30m = len(df_30m)

    x_30m, y_30m = _col_row_to_utm(
        df_30m["col"].values.astype(np.float64),
        df_30m["row"].values.astype(np.float64),
        transform_30m,
    )
    utm_30m = np.column_stack([x_30m, y_30m])
    lst_true_30m = df_30m[TARGET_COL].values.astype(np.float64)
    tree_30m = cKDTree(utm_30m)

    sum_pred = np.zeros(n_30m, dtype=np.float64)
    count_pred = np.zeros(n_30m, dtype=np.int32)

    # LST_pred 缓存（避免临时文件）
    lst_pred_cache = []

    if progress_callback:
        progress_callback("tcr", 0.15, "Phase 1: 逐批预测10m + 30m块聚合...")

    batch_count_1 = 0
    total_valid_1 = 0
    t_phase1 = time.time()

    for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
        valid_mask = _is_valid_row(chunk)
        n_valid = valid_mask.sum()
        total_valid_1 += n_valid

        if n_valid > 0:
            valid_chunk = chunk.loc[valid_mask]
            X = valid_chunk[feature_cols].values.astype(np.float64)
            lst_pred = model.predict(X)

            # 缓存到内存（float32 ~280MB for 70M pixels）
            lst_pred_cache.append(lst_pred.astype(np.float32))

            ux, uy = _col_row_to_utm(
                valid_chunk["col"].values.astype(np.float64),
                valid_chunk["row"].values.astype(np.float64),
                transform_10m,
            )
            _, indices = tree_30m.query(np.column_stack([ux, uy]), k=1)
            np.add.at(sum_pred, indices, lst_pred)
            np.add.at(count_pred, indices, 1)

        batch_count_1 += 1
        if progress_callback and batch_count_1 % 20 == 0:
            progress_callback(
                "tcr", 0.15 + 0.25 * min(batch_count_1 / 100, 1.0),
                f"Phase 1 批次 {batch_count_1}: {n_valid:,} 有效, "
                f"耗时 {time.time() - t_phase1:.0f}s",
            )

    t_phase1_elapsed = time.time() - t_phase1

    # ─── 4. 计算TCR_30m ───────────────────────────────────────────────
    if progress_callback:
        progress_callback("tcr", 0.42, "计算TCR_30m...")

    valid_blocks = count_pred > 0
    n_valid_blocks = valid_blocks.sum()
    tcr_30m = np.full(n_30m, np.nan, dtype=np.float64)
    mean_pred = sum_pred[valid_blocks] / count_pred[valid_blocks]
    tcr_30m[valid_blocks] = lst_true_30m[valid_blocks] - mean_pred
    valid_tcr = tcr_30m[valid_blocks]

    tcr_stats = {
        "min": float(valid_tcr.min()), "max": float(valid_tcr.max()),
        "mean": float(valid_tcr.mean()), "std": float(valid_tcr.std()),
        "n_valid_blocks": int(n_valid_blocks),
    }

    # ─── 5. 构建插值器 ───────────────────────────────────────────────
    tcr_interp = _build_tcr_grid(df_30m, tcr_30m)
    del df_30m, x_30m, y_30m, utm_30m, lst_true_30m
    del tree_30m, sum_pred, count_pred, mean_pred, tcr_30m

    # ─── 6. 合并 LST_pred 缓存 ────────────────────────────────────────
    lst_pred_all = np.concatenate(lst_pred_cache) if lst_pred_cache else np.array([], dtype=np.float32)
    del lst_pred_cache

    # ==================================================================
    #  Phase 2: 双线性插值TCR + 计算LST_final（一次性写入）
    # ==================================================================
    if progress_callback:
        progress_callback("tcr", 0.50, "Phase 2: 双线性插值TCR + LST_final...")

    t_phase2 = time.time()
    lst_pred_offset = 0
    output_written = False
    total_invalid = 0
    total_valid_2 = 0
    batch_count_2 = 0

    for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
        valid_mask = _is_valid_row(chunk)
        n_valid = valid_mask.sum()
        n_invalid = (~valid_mask).sum()
        total_invalid += n_invalid
        total_valid_2 += n_valid

        tcr_vals = np.full(len(chunk), np.nan, dtype=np.float64)
        lst_pred_vals = np.full(len(chunk), np.nan, dtype=np.float32)

        if n_valid > 0:
            valid_chunk = chunk.loc[valid_mask]
            pts = np.column_stack([
                valid_chunk["row"].values.astype(np.float64) / 3.0,
                valid_chunk["col"].values.astype(np.float64) / 3.0,
            ])
            tcr_vals[valid_mask] = tcr_interp(pts)

            batch_lst_pred = lst_pred_all[lst_pred_offset:lst_pred_offset + n_valid]
            lst_pred_vals[valid_mask] = batch_lst_pred
            lst_pred_offset += n_valid

        # 构建输出行：LST_pred, TCR, LST_final
        lst_final_vals = lst_pred_vals + tcr_vals

        out = pd.DataFrame({
            "row": chunk["row"].values.astype(int),
            "col": chunk["col"].values.astype(int),
            LST_PRED_COL: np.round(lst_pred_vals, 4),
            TCR_COL: np.round(tcr_vals, 4),
            LST_FINAL_COL: np.round(lst_final_vals, 4),
        })
        out.to_csv(output_path, mode="w" if not output_written else "a",
                   header=not output_written, index=False, encoding="utf-8-sig",
                   na_rep="")
        output_written = True
        batch_count_2 += 1

        if progress_callback and (batch_count_2 % 20 == 0 or batch_count_2 <= 5):
            progress_callback(
                "tcr", 0.50 + 0.45 * min(batch_count_2 / 100, 1.0),
                f"Phase 2 批次 {batch_count_2}: {n_valid:,} 有效",
            )

    t_phase2_elapsed = time.time() - t_phase2
    total_elapsed = time.time() - t_start

    if progress_callback:
        progress_callback("tcr", 1.0,
            f"TCR+LST_final完成: 有效 {total_valid_2:,} 行, "
            f"总耗时 {total_elapsed:.1f}s")

    return {
        "output_path": output_path,
        "tcr_statistics": tcr_stats,
        "total_valid_10m": total_valid_2,
        "total_invalid_10m": total_invalid,
        "phase1_seconds": round(t_phase1_elapsed, 1),
        "phase2_seconds": round(t_phase2_elapsed, 1),
        "total_seconds": round(total_elapsed, 1),
    }
