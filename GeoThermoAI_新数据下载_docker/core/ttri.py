"""
TTRI（地形热响应指数）计算模块

TTRI = a * DEM + b * Slope + c * cos(Aspect)

系数 a, b, c（及截距）**只用固定 train split 拟合一次**（用户确认的规则），
固定保存为 ``ttri_coefficients.json``；validate/test/完整30m约束层/10m预测格网
全部无标签地复用同一组系数变换，禁止各自用自身 LST 重新拟合。

10m 预测数据的空间化插值，改为基于"完整30m约束层"（
``30m_constraint_grid.parquet``，覆盖全部有效30m像元，而不是 step=2 抽样点）构建稠密
TTRI 栅格，再通过 core.grid_mapping 的统一仿射映射双线性插值到10m网格
（不再使用 row/3.0, col/3.0 的隐式假设）。
"""

import os
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.interpolate import RegularGridInterpolator

from . import grid_mapping
from .atomic_io import atomic_write_json, write_verified
from .table_io import TableWriter, iter_chunks, read_table, sample_rows, write_empty

import pyarrow as pa
import pyarrow.parquet as pq

# ── 常量 ──────────────────────────────────────────────────────────────
FEATURE_COLS = ["DEM", "Slope", "cos(Aspect)"]
TARGET_COL = "LST"
TTRI_COL = "TTRI"
SPECTRAL_COLS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI"]

# 固定文件名（新增固定名允许，禁止模型自由起名/动态文件名）
COEFFICIENTS_FILENAME = "ttri_coefficients.json"


def _validate_columns(df: pd.DataFrame, required_cols: list, dataset_name: str) -> None:
    """校验DataFrame是否包含所有必需的列。"""
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{dataset_name} 缺少必需的列: {missing}. 现有列: {list(df.columns)}"
        )


def _is_valid_row(df_slice: pd.DataFrame, spectral_cols: Optional[List[str]] = None) -> np.ndarray:
    """判断有效行（所有光谱列均有值）。"""
    cols = spectral_cols if spectral_cols is not None else SPECTRAL_COLS
    return df_slice[cols].notna().all(axis=1)


# ======================================================================
#  仅 train 拟合一次
# ======================================================================


def _fit_regression_diagnostics(
    df: pd.DataFrame, feature_cols: list, target_col: str
) -> Dict:
    """拟合多元线性回归（含截距项），使用 np.linalg.lstsq，并保留 
    秩/条件数/样本数诊断，而不是只使用系数。

    模型: LST = intercept + a * DEM + b * Slope + c * cos(Aspect)
    TTRI  = a * DEM + b * Slope + c * cos(Aspect)（不含截距）
    """
    X = df[feature_cols].values.astype(np.float64)
    y = df[target_col].values.astype(np.float64)
    n_samples = len(X)

    A = np.column_stack([np.ones(n_samples), X])
    coeff, residuals, rank, singular_values = np.linalg.lstsq(A, y, rcond=None)

    n_params = A.shape[1]
    condition_number = None
    if singular_values is not None and len(singular_values) > 0 and singular_values[-1] > 0:
        condition_number = float(singular_values[0] / singular_values[-1])

    coef_ = coeff[1:]
    intercept = float(coeff[0])

    y_pred = A @ coeff
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    rank_deficient = int(rank) < n_params
    is_ill_conditioned = condition_number is not None and condition_number > 1e8

    return {
        "coefficients": coef_.tolist(),
        "intercept": round(intercept, 6),
        "r2": round(float(r2), 6),
        "n_samples": int(n_samples),
        "rank": int(rank),
        "n_params": int(n_params),
        "rank_deficient": bool(rank_deficient),
        "condition_number": None if condition_number is None else round(condition_number, 3),
        "ill_conditioned": bool(is_ill_conditioned),
        "feature_cols": list(feature_cols),
        "target_col": target_col,
    }


def fit_ttri_train(
    train_csv: str,
    output_dir: str,
    progress_callback=None,
    min_samples: int = 30,
) -> Dict:
    """只用固定 train split 拟合一次 TTRI 回归系数，固定保存 ``ttri_coefficients.json``。

    秩不足或病态（条件数 > 1e8）时明确失败，而不是静默输出不稳定系数。

    Args:
        train_csv:   训练集Parquet路径（只读，不覆盖）
        output_dir:  ttri_coefficients.json 的保存目录
        min_samples: 最小允许样本数，过少的 AOI 不应计算出"看似有效"的系数

    Returns:
        dict: 拟合诊断信息 + coefficients_path
    """
    if progress_callback:
        progress_callback("ttri_train", 0.0, "读取训练集，拟合 TTRI 回归系数（仅 train，一次）...")

    df = read_table(train_csv)
    _validate_columns(df, FEATURE_COLS + [TARGET_COL], "训练集")

    if len(df) < min_samples:
        raise ValueError(
            f"训练集有效样本仅 {len(df)} 行（< 最小要求 {min_samples}），"
            f"地形信息可能不足以拟合稳定的 TTRI 系数，已拒绝生成 {COEFFICIENTS_FILENAME}"
        )

    diag = _fit_regression_diagnostics(df, FEATURE_COLS, TARGET_COL)

    if diag["rank_deficient"]:
        raise ValueError(
            f"TTRI 回归秩亏（rank={diag['rank']} < 参数数 {diag['n_params']}），"
            f"可能地形（DEM/Slope/Aspect）在该 AOI 内几乎无变化，拒绝生成不稳定系数"
        )
    if diag["ill_conditioned"]:
        raise ValueError(
            f"TTRI 回归病态（condition_number={diag['condition_number']} > 1e8），"
            f"系数对样本噪声极度敏感，拒绝生成不可靠系数；请检查 AOI 地形特征是否近似共线"
        )

    coefficients_path = os.path.join(output_dir, COEFFICIENTS_FILENAME)
    payload = {
        "schema_version": 1,
        "source": "train_only_single_fit",
        "train_csv": os.path.abspath(train_csv),
        **diag,
        "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    from .manifest import sha256_file

    payload["train_csv_sha256"] = sha256_file(train_csv)
    atomic_write_json(coefficients_path, payload)

    if progress_callback:
        progress_callback(
            "ttri_train", 1.0,
            f"TTRI 系数拟合完成（仅 train，n={diag['n_samples']}）: "
            f"a(DEM)={diag['coefficients'][0]:.6f}, b(Slope)={diag['coefficients'][1]:.6f}, "
            f"c(cos)={diag['coefficients'][2]:.6f}, R²={diag['r2']:.4f}, "
            f"rank={diag['rank']}/{diag['n_params']}, cond={diag['condition_number']}",
        )

    return {**payload, "coefficients_path": coefficients_path}


def load_ttri_coefficients(coefficients: "str | Dict") -> Dict:
    """接受 ttri_coefficients.json 路径或已加载的 dict，统一返回 dict。"""
    if isinstance(coefficients, dict):
        return coefficients
    import json

    with open(coefficients, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_ttri_column(
    csv_path: str,
    coefficients: "str | Dict",
    *,
    feature_cols: Optional[List[str]] = None,
) -> Dict:
    """用给定（已拟合好的、不再重新拟合的）系数为某 30m Parquet 原地新增/覆盖 TTRI 列。

    用于 validate.parquet / test.parquet：不使用它们自身的 LST 重新拟合，只做无标签变换。
    写入采用 ``.partial`` + 原子替换，最终文件名保持固定不变。
    分块读取 + TableWriter 分块写，内存占用 O(块大小)，不再整表读入内存。

    Returns:
        dict: {rows, ttri_valid, ttri_invalid, coefficients_hash}
    """
    coef_dict = load_ttri_coefficients(coefficients)
    cols = feature_cols or coef_dict.get("feature_cols") or FEATURE_COLS
    coef_arr = np.asarray(coef_dict["coefficients"], dtype=np.float64)

    # 先校验首块列（避免校验失败时留下半成品 .partial）
    _it_probe = iter_chunks(csv_path)
    first_chunk = next(_it_probe, None)
    if first_chunk is not None:
        _validate_columns(first_chunk, cols, os.path.basename(csv_path))
    del _it_probe, first_chunk

    rows = 0
    ttri_valid = 0
    ttri_invalid = 0
    writer = None

    def _build(tmp_path: str) -> None:
        nonlocal rows, ttri_valid, ttri_invalid, writer
        for chunk in iter_chunks(csv_path):
            X = chunk[cols].values.astype(np.float64)
            ttri = X @ coef_arr

            if TTRI_COL in chunk.columns:
                chunk.drop(columns=[TTRI_COL], inplace=True)
            if TARGET_COL in chunk.columns:
                target_idx = list(chunk.columns).index(TARGET_COL)
                chunk.insert(target_idx, TTRI_COL, ttri)
            else:
                chunk[TTRI_COL] = ttri

            if writer is None:
                writer = TableWriter(tmp_path)
            writer.write(chunk)
            rows += len(chunk)
            ttri_valid += int(np.isfinite(ttri).sum())
            ttri_invalid += int((~np.isfinite(ttri)).sum())
        if writer is not None:
            writer.close()
        else:
            # 空输入：写出带原始 schema + TTRI 列的空 Parquet
            pf = pq.ParquetFile(csv_path)
            schema = pa.schema(list(pf.schema_arrow) + [pa.field(TTRI_COL, pa.float64(), nullable=True)])
            write_empty(tmp_path, schema)

    def _validator(tmp_path: str) -> Tuple[bool, str]:
        check = sample_rows(tmp_path, n=5)
        if TTRI_COL not in check.columns:
            return False, "写出的 Parquet 缺少 TTRI 列"
        return True, ""

    write_verified(_build, csv_path, _validator)

    return {
        "rows": int(rows),
        "ttri_valid": int(ttri_valid),
        "ttri_invalid": int(ttri_invalid),
        "coefficients_source": coef_dict.get("coefficients_path", "<inline>"),
    }


def compute_ttri_for_splits(
    train_csv: str,
    val_csv: str,
    test_csv: str,
    output_dir: str,
    progress_callback=None,
) -> Dict:
    """仅用 train_csv 拟合一次系数并固定保存 ``ttri_coefficients.json``，
    然后用同一组系数为 train/validate/test 三个固定文件原地新增 TTRI 列
    （train 自身的 TTRI 也是"应用同一组系数"，数值上等于拟合时的样本内变换，
    但代码路径与 validate/test 完全一致，杜绝"各自重新拟合"的实现分叉）。

    Returns:
        dict: {coefficients: {...}, output_files: {train,validate,test}, coefficients_path}
    """
    if progress_callback:
        progress_callback("ttri_train", 0.0, "开始计算 TTRI（仅 train 拟合一次）...")

    fit_result = fit_ttri_train(train_csv, output_dir, progress_callback=None)
    coefficients_path = fit_result["coefficients_path"]

    datasets = [(train_csv, "训练集", "train"), (val_csv, "验证集", "validate"), (test_csv, "测试集", "test")]
    output_files: Dict[str, str] = {}
    per_dataset_stats: Dict[str, Dict] = {}

    for idx, (path, label, key) in enumerate(datasets):
        if progress_callback:
            progress_callback(
                "ttri_train", 0.2 + 0.7 * (idx + 1) / len(datasets),
                f"为{label}应用同一组 TTRI 系数（不重新拟合）...",
            )
        stats = apply_ttri_column(path, fit_result)
        output_files[key] = path
        per_dataset_stats[key] = stats

    if progress_callback:
        progress_callback("ttri_train", 1.0, "TTRI 计算完成：train/validate/test 复用同一组系数")

    return {
        "coefficients": {
            "coefficients": fit_result["coefficients"],
            "intercept": fit_result["intercept"],
            "r2": fit_result["r2"],
            "rank": fit_result["rank"],
            "condition_number": fit_result["condition_number"],
            "n_samples": fit_result["n_samples"],
        },
        "output_files": output_files,
        "per_dataset_stats": per_dataset_stats,
        "coefficients_path": coefficients_path,
        # 向后兼容旧调用方读取 result["train"]["r2"] 等字段的用法
        "train": {
            "coefficients": fit_result["coefficients"],
            "intercept": fit_result["intercept"],
            "r2": fit_result["r2"],
        },
    }


# ======================================================================
#  完整30m约束层上的 TTRI（双流；不需要插值，本就在30m自身网格上）
# ======================================================================


def compute_ttri_for_constraint_grid(
    constraint_csv: str,
    coefficients: "str | Dict",
    output_path: Optional[str] = None,
) -> Dict:
    """在完整30m约束层（``30m_constraint_grid.parquet``，覆盖全部有效30m像元）上，
    用同一组 train 系数直接计算 TTRI（本身就在30m网格上，不需要插值）。
    分块读取 + TableWriter 分块写，内存占用 O(块大小)，不再整表读入内存。

    Returns:
        dict: {output_path, rows, ttri_valid}
    """
    coef_dict = load_ttri_coefficients(coefficients)
    cols = coef_dict.get("feature_cols") or FEATURE_COLS
    coef_arr = np.asarray(coef_dict["coefficients"], dtype=np.float64)

    # 先校验首块列（避免校验失败时留下半成品 .partial）
    _it_probe = iter_chunks(constraint_csv)
    first_chunk = next(_it_probe, None)
    if first_chunk is not None:
        _validate_columns(first_chunk, cols, "完整30m约束层")
    del _it_probe, first_chunk

    target_path = output_path or constraint_csv
    rows = 0
    ttri_valid = 0
    writer = None

    def _build(tmp_path: str) -> None:
        nonlocal rows, ttri_valid, writer
        for chunk in iter_chunks(constraint_csv):
            X = chunk[cols].values.astype(np.float64)
            ttri = X @ coef_arr
            chunk[TTRI_COL] = ttri
            if writer is None:
                writer = TableWriter(tmp_path)
            writer.write(chunk)
            rows += len(chunk)
            ttri_valid += int(np.isfinite(ttri).sum())
        if writer is not None:
            writer.close()
        else:
            # 空输入：写出带原始 schema + TTRI 列的空 Parquet
            pf = pq.ParquetFile(constraint_csv)
            schema = pa.schema(list(pf.schema_arrow) + [pa.field(TTRI_COL, pa.float64(), nullable=True)])
            write_empty(tmp_path, schema)

    def _validator(tmp_path: str) -> Tuple[bool, str]:
        check = sample_rows(tmp_path, n=5)
        return (TTRI_COL in check.columns, "缺少 TTRI 列")

    write_verified(_build, target_path, _validator)

    return {
        "output_path": target_path,
        "rows": int(rows),
        "ttri_valid": int(ttri_valid),
    }


def build_dense_ttri_grid(
    constraint_csv: str,
    height: int,
    width: int,
    coefficients: "str | Dict",
) -> Tuple[np.ndarray, RegularGridInterpolator, Dict]:
    """从完整30m约束层构建稠密 (height, width) TTRI 栅格数组 + 双线性插值器。

    与旧版 ``build_30m_ttri_grid`` 的关键差异：
        - 输入是覆盖全部有效30m像元的完整约束层，而不是 step=2 抽样 Parquet；
        - 网格按 [0, height) x [0, width) 的**绝对像元索引**稠密建立（未出现的行列
          仍是 NaN，但网格坐标轴不再是"样本中出现过的 unique row/col"这种稀疏轴，
          从而与 core.grid_mapping 的仿射插值约定完全对应）。

    Returns:
        (grid, interpolator, coverage_stats)
    """
    coef_dict = load_ttri_coefficients(coefficients)
    cols = coef_dict.get("feature_cols") or FEATURE_COLS
    coef_arr = np.asarray(coef_dict["coefficients"], dtype=np.float64)

    df = read_table(constraint_csv, columns=["row", "col"] + cols)
    _validate_columns(df, cols, "完整30m约束层")

    X = df[cols].values.astype(np.float64)
    ttri_values = X @ coef_arr

    grid = np.full((height, width), np.nan, dtype=np.float32)
    rows = df["row"].values.astype(np.int64)
    colsi = df["col"].values.astype(np.int64)
    in_range = (rows >= 0) & (rows < height) & (colsi >= 0) & (colsi < width)
    grid[rows[in_range], colsi[in_range]] = ttri_values[in_range]

    interp = grid_mapping.build_dense_grid_interpolator(grid)
    coverage = {
        "grid_height": int(height),
        "grid_width": int(width),
        "constraint_rows": int(len(df)),
        "constraint_rows_in_range": int(in_range.sum()),
        "constraint_rows_out_of_range": int((~in_range).sum()),
    }
    return grid, interp, coverage


def interpolate_grid_to_fine(
    fine_row: np.ndarray,
    fine_col: np.ndarray,
    fine_transform: list,
    coarse_transform: list,
    interpolator: RegularGridInterpolator,
    grid: Optional[np.ndarray] = None,
    nearest_index=None,
) -> np.ndarray:
    """统一仿射映射（core.grid_mapping）+ 双线性插值：把 30m 稠密栅格插值到给定的
    细格 (row, col) 位置。用于 10m TTRI 预测；TCR smooth_recentered 模式复用同一
    底层实现 grid_mapping.interpolate_dense_grid_to_fine。

    grid/nearest_index 给定时，约束层覆盖范围外的细像元用最近有效值回退，
    保证每个预测样本都有 TTRI（口径统一：预测数据只按 S2 去云，不再额外扣点）。
    """
    return grid_mapping.interpolate_dense_grid_to_fine(
        fine_row, fine_col, fine_transform, coarse_transform, interpolator,
        grid=grid, nearest_index=nearest_index,
    )


def compute_ttri_predict(
    constraint_csv: str,
    constraint_meta_json: str,
    predict_10m_csv: str,
    predict_10m_meta_json: str,
    coefficients: "str | Dict",
    output_path: str,
    batch_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """计算10m预测数据的TTRI：完整30m约束层稠密栅格 → 统一仿射双线性插值到10m。

    与旧接口的差异：不再从 data_30m_csv（step2抽样）构建稀疏"unique row/col"网格，
    也不再使用 row/3.0, col/3.0 假设；改用 core.grid_mapping 基于真实仿射变换的映射。

    Args:
        constraint_csv:        完整30m约束层Parquet路径（30m_constraint_grid.parquet）
        constraint_meta_json:  完整30m约束层元数据（含 height/width/transform/crs）
        predict_10m_csv:       10m预测数据Parquet路径
        predict_10m_meta_json: 10m元数据JSON路径（含 transform/crs）
        coefficients:          ttri_coefficients.json 路径或 dict（仅 train 拟合的系数）
        output_path:           输出Parquet路径
        batch_size:            批处理大小
        progress_callback:     进度回调

    Returns:
        dict: {output_path, total_valid, total_invalid, out_of_grid}
    """
    import json

    t_start = time.time()
    if progress_callback:
        progress_callback("ttri_predict", 0, "开始计算10m预测数据的TTRI（统一仿射映射）...")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with open(constraint_meta_json, "r", encoding="utf-8") as f:
        constraint_meta = json.load(f)
    with open(predict_10m_meta_json, "r", encoding="utf-8") as f:
        predict_meta = json.load(f)

    coarse_transform = constraint_meta["transform"]
    fine_transform = predict_meta["transform"]
    grid_mapping.assert_same_crs(
        constraint_meta.get("crs"), predict_meta.get("crs"), context="TTRI 10m 插值：30m约束层 vs 10m预测格网"
    )
    ratio_diag = grid_mapping.check_exact_ratio_grid(fine_transform, coarse_transform)

    if progress_callback:
        progress_callback("ttri_predict", 0.15, "构建完整30m TTRI稠密栅格...")

    grid, interp, coverage = build_dense_ttri_grid(
        constraint_csv, constraint_meta["height"], constraint_meta["width"], coefficients
    )
    # 约束层覆盖范围外的预测点用最近有效 TTRI 回退（口径统一：每个预测样本都有 TTRI）
    nearest_index = grid_mapping.nearest_valid_index(grid)

    writer = None
    total_valid = 0
    total_invalid = 0
    out_of_grid = 0
    batch_count = 0

    if progress_callback:
        progress_callback("ttri_predict", 0.3, "开始逐批插值10m数据...")

    for chunk in iter_chunks(predict_10m_csv, batch_size=batch_size):
        valid_mask = _is_valid_row(chunk)
        n_valid = int(valid_mask.sum())
        n_invalid = int((~valid_mask).sum())
        total_valid += n_valid
        total_invalid += n_invalid

        ttri_values = np.full(len(chunk), np.nan, dtype=np.float64)

        if n_valid > 0:
            valid_chunk = chunk.loc[valid_mask]
            fine_row = valid_chunk["row"].values.astype(np.float64)
            fine_col = valid_chunk["col"].values.astype(np.float64)
            interpolated = interpolate_grid_to_fine(
                fine_row, fine_col, fine_transform, coarse_transform, interp,
                grid=grid, nearest_index=nearest_index,
            )
            ttri_values[valid_mask.values] = interpolated
            out_of_grid += int(np.isnan(interpolated).sum())

        chunk[TTRI_COL] = ttri_values
        if writer is None:
            writer = TableWriter(output_path)
        writer.write(chunk)
        batch_count += 1

        if progress_callback and (batch_count % 20 == 0 or batch_count <= 5):
            progress_callback(
                "ttri_predict", 0.3 + 0.65 * min(batch_count * batch_size / max(total_valid + total_invalid, 1), 1.0),
                f"批次 {batch_count}: 有效 {n_valid:,}",
            )

    if writer is not None:
        writer.close()
    else:
        # 10m 预测数据为空文件时也要写出带 schema 的空 Parquet，避免下游读取报错
        pf = pq.ParquetFile(predict_10m_csv)
        schema = pa.schema(list(pf.schema_arrow) + [pa.field(TTRI_COL, pa.float64(), nullable=True)])
        write_empty(output_path, schema)

    elapsed = time.time() - t_start
    if progress_callback:
        progress_callback(
            "ttri_predict", 1.0,
            f"TTRI计算完成: {total_valid:,} 有效行, {total_invalid:,} 无效行, "
            f"{out_of_grid:,} 落在约束层覆盖范围外, 耗时 {elapsed:.1f}s",
        )

    return {
        "output_path": output_path,
        "total_valid": int(total_valid),
        "total_invalid": int(total_invalid),
        "out_of_grid": int(out_of_grid),
        "grid_ratio_diagnostics": ratio_diag,
        "constraint_coverage": coverage,
        "batch_count": batch_count,
        "elapsed_seconds": round(elapsed, 1),
    }
