"""
TTRI（地形热响应指数）计算模块

TTRI = a * DEM + b * Slope + c * cos(Aspect)

其中系数 a, b, c 由多元线性回归 LST ~ DEM + Slope + cos(Aspect) 拟合得到。

包含两个函数：
    - compute_ttri_train: 对训练/验证/测试集各自独立拟合并计算TTRI
    - compute_ttri_predict: 使用训练集系数，通过30m规则网格双线性插值计算10m数据的TTRI
"""

import os
import time
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.interpolate import RegularGridInterpolator


# ── 常量 ──────────────────────────────────────────────────────────────
FEATURE_COLS = ["DEM", "Slope", "cos(Aspect)"]
TARGET_COL = "LST"
TTRI_COL = "TTRI"
SPECTRAL_COLS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI"]


def _validate_columns(df: pd.DataFrame, required_cols: list, dataset_name: str) -> None:
    """校验DataFrame是否包含所有必需的列。"""
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{dataset_name} 缺少必需的列: {missing}. 现有列: {list(df.columns)}"
        )


def _fit_regression(
    df: pd.DataFrame, feature_cols: list, target_col: str
) -> Tuple[np.ndarray, float, float]:
    """
    拟合多元线性回归（含截距项），使用 np.linalg.lstsq。

    模型: LST = intercept + a * DEM + b * Slope + c * cos(Aspect)
    TTRI  = a * DEM + b * Slope + c * cos(Aspect)（不含截距）

    Returns:
        tuple: (coef_, intercept, r2_score)
    """
    X = df[feature_cols].values.astype(np.float64)
    y = df[target_col].values.astype(np.float64)

    A = np.column_stack([np.ones(len(X)), X])
    coeff, residuals, rank, s = np.linalg.lstsq(A, y, rcond=None)

    coef_ = coeff[1:]
    intercept = float(coeff[0])

    # 计算R²
    y_pred = A @ coeff
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    return coef_, intercept, float(r2)


def _compute_ttri(df: pd.DataFrame, coef_: np.ndarray, feature_cols: list) -> np.ndarray:
    """计算 TTRI = a * DEM + b * Slope + c * cos(Aspect)。"""
    X = df[feature_cols].values.astype(np.float64)
    return X @ coef_


def _is_valid_row(df_slice: pd.DataFrame) -> np.ndarray:
    """判断有效行（所有光谱列均有值）。"""
    return df_slice[SPECTRAL_COLS].notna().all(axis=1)


# ======================================================================
#  训练集 TTRI 计算
# ======================================================================


def compute_ttri_train(
    train_csv: str,
    val_csv: str,
    test_csv: str,
    output_dir: str,
    progress_callback=None,
) -> Dict:
    """
    对训练集、验证集、测试集各自独立拟合TTRI回归，并计算TTRI列。

    每个数据集独立拟合LinearRegression:
        X = [DEM, Slope, cos(Aspect)]
        Y = [LST]
        TTRI = a * DEM + b * Slope + c * cos(Aspect)

    结果直接覆盖原始CSV文件（原地添加TTRI列），不产生中间文件。

    Args:
        train_csv:          训练集CSV路径（原地覆盖）
        val_csv:            验证集CSV路径（原地覆盖）
        test_csv:           测试集CSV路径（原地覆盖）
        output_dir:         输出目录（仅用于定位，文件已到位）
        progress_callback:  进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含各数据集的系数和R²
            - train:    {coefficients: [a, b, c], intercept: ..., r2: ...}
            - validate: {coefficients: [a, b, c], intercept: ..., r2: ...}
            - test:     {coefficients: [a, b, c], intercept: ..., r2: ...}
            - output_files: {train: ..., validate: ..., test: ...}
    """
    if progress_callback:
        progress_callback("ttri_train", 0, "开始计算TTRI...")

    datasets = [
        (train_csv, "训练集"),
        (val_csv, "验证集"),
        (test_csv, "测试集"),
    ]

    results = {}
    output_files = {}

    for idx, (input_path, name) in enumerate(datasets):
        if progress_callback:
            progress_callback(
                "ttri_train", (idx + 1) / len(datasets) * 0.8,
                f"处理{name}...",
            )

        df = pd.read_csv(input_path)
        _validate_columns(df, FEATURE_COLS + [TARGET_COL], name)

        coef_, intercept, r2 = _fit_regression(df, FEATURE_COLS, TARGET_COL)

        ttri = _compute_ttri(df, coef_, FEATURE_COLS)

        # 在 LST 列之前插入 TTRI 列，原地覆盖
        # 如果 TTRI 列已存在（上次运行残留），先删除
        if TTRI_COL in df.columns:
            df.drop(columns=[TTRI_COL], inplace=True)
        target_idx = list(df.columns).index(TARGET_COL)
        df.insert(target_idx, TTRI_COL, ttri)
        df.to_csv(input_path, index=False, encoding="utf-8-sig")

        key = "train" if "训练" in name else "validate" if "验证" in name else "test"
        results[key] = {
            "coefficients": coef_.tolist(),
            "intercept": round(intercept, 6),
            "r2": round(r2, 6),
        }
        output_files[key] = input_path

    results["output_files"] = output_files

    if progress_callback:
        progress_callback("ttri_train", 1.0, "TTRI计算完成")

    return results


# ======================================================================
#  预测集 TTRI 计算（双线性插值）
# ======================================================================


def build_30m_ttri_grid(
    data_30m_csv: str, coef_: np.ndarray, feature_cols: list
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, RegularGridInterpolator]:
    """
    从30m数据计算TTRI并构建规则网格 + 双线性插值器。

    Returns:
        tuple: (grid_rows, grid_cols, grid_values, interpolator)
    """
    df = pd.read_csv(data_30m_csv)
    _validate_columns(df, feature_cols, "30m全量数据")

    # 计算 TTRI_30m
    X = df[feature_cols].values.astype(np.float64)
    ttri_30m = X @ coef_

    # 构建规则网格
    grid_rows = np.sort(df["row"].unique())
    grid_cols = np.sort(df["col"].unique())

    row_to_idx = {r: i for i, r in enumerate(grid_rows)}
    col_to_idx = {c: j for j, c in enumerate(grid_cols)}

    grid = np.full((len(grid_rows), len(grid_cols)), np.nan, dtype=np.float64)
    ri = df["row"].map(row_to_idx).values
    ci = df["col"].map(col_to_idx).values
    grid[ri, ci] = ttri_30m

    interp = RegularGridInterpolator(
        (grid_rows, grid_cols),
        grid,
        method="linear",
        bounds_error=False,
        fill_value=np.nan,
    )

    return grid_rows, grid_cols, grid, interp


def compute_ttri_predict(
    data_30m_csv: str,
    predict_10m_csv: str,
    output_path: str,
    train_csv: str = None,
    batch_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """
    计算10m预测数据的TTRI（通过30m网格双线性插值）。

    流程:
        1. 从训练数据拟合TTRI系数（使用训练集的DEM/Slope/cos(Aspect)→LST回归系数）
        2. 构建30m规则网格 + RegularGridInterpolator
        3. 逐批读取10m数据，对有效像元使用 row/3.0, col/3.0 进行双线性插值
        4. 输出带TTRI列的CSV

    Args:
        data_30m_csv:    30m全量数据CSV路径（用于构建30m TTRI网格）
        predict_10m_csv: 10m预测数据CSV路径
        output_path:     输出CSV路径
        train_csv:       训练集CSV路径（用于拟合回归系数，不提供则使用data_30m_csv）
        batch_size:      批处理大小（默认500000）
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含输出路径、总行数、有效行数
            - output_path: 输出文件路径
            - total_valid:  有效行数
            - total_invalid: 无效行数
    """
    t_start = time.time()

    if progress_callback:
        progress_callback("ttri_predict", 0, "开始计算10m预测数据的TTRI...")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # ─── 步骤1: 从训练集拟合TTRI系数 ─────────────────────────────────
    if progress_callback:
        progress_callback("ttri_predict", 0.1, "从训练集拟合TTRI系数...")

    fit_csv = train_csv if train_csv else data_30m_csv
    df_fit = pd.read_csv(fit_csv)
    _validate_columns(df_fit, FEATURE_COLS + [TARGET_COL], "拟合用数据")

    coef_, intercept, r2 = _fit_regression(df_fit, FEATURE_COLS, TARGET_COL)

    if progress_callback:
        progress_callback(
            "ttri_predict", 0.2,
            f"TTRI系数: a(DEM)={coef_[0]:.6f}, b(Slope)={coef_[1]:.6f}, c(cos)={coef_[2]:.6f}, R²={r2:.4f}",
        )

    # ─── 步骤2: 构建30m规则网格 + 插值器 ──────────────────────────────
    if progress_callback:
        progress_callback("ttri_predict", 0.25, "构建30m TTRI规则网格...")

    grid_rows, grid_cols, grid_ttri, interp = build_30m_ttri_grid(
        data_30m_csv, coef_, FEATURE_COLS
    )
    del grid_ttri, df_fit

    if progress_callback:
        progress_callback("ttri_predict", 0.35, "开始逐批处理10m数据...")

    # ─── 步骤3: 逐批处理10m数据 ───────────────────────────────────────
    output_written = False
    total_valid = 0
    total_invalid = 0
    batch_count = 0

    # 先统计总行数用于进度估算（快速扫描行数）
    total_rows_est = 0
    try:
        for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
            total_rows_est += len(chunk)
            batch_count += 1
            if batch_count >= 10:
                break
    except Exception:
        total_rows_est = 220_000_000  # 默认估计

    batch_count = 0

    for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
        t_chunk = time.time()
        valid_mask = _is_valid_row(chunk)
        n_valid = valid_mask.sum()
        n_invalid = (~valid_mask).sum()
        total_valid += n_valid
        total_invalid += n_invalid

        ttri_values = np.full(len(chunk), np.nan, dtype=np.float64)

        if n_valid > 0:
            # 10m → 30m 网格坐标映射: row/3.0, col/3.0
            valid_chunk = chunk.loc[valid_mask]
            pts = np.column_stack([
                valid_chunk["row"].values.astype(np.float64) / 3.0,
                valid_chunk["col"].values.astype(np.float64) / 3.0,
            ])
            ttri_values[valid_mask] = interp(pts)

        chunk[TTRI_COL] = ttri_values

        chunk.to_csv(
            output_path,
            mode="w" if not output_written else "a",
            header=not output_written,
            index=False,
            encoding="utf-8-sig",
            na_rep="",
        )
        output_written = True
        batch_count += 1

        if progress_callback and (batch_count % 20 == 0 or batch_count <= 5):
            progress_callback(
                "ttri_predict",
                0.35 + 0.6 * min(batch_count * batch_size / total_rows_est, 1.0),
                f"批次 {batch_count}: 有效 {n_valid:,}, 耗时 {time.time() - t_chunk:.1f}s",
            )

    elapsed = time.time() - t_start

    if progress_callback:
        progress_callback(
            "ttri_predict",
            1.0,
            f"TTRI计算完成: {total_valid:,} 有效行, {total_invalid:,} 无效行, 耗时 {elapsed:.1f}s",
        )

    return {
        "output_path": output_path,
        "total_valid": total_valid,
        "total_invalid": total_invalid,
        "batch_count": batch_count,
        "elapsed_seconds": round(elapsed, 1),
    }
