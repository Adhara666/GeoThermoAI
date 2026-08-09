"""
TCR（热约束残差，Thermal Constraint Residual）+ LST_final 计算模块（A-06 重写）

与旧实现的关键差异：
    - 30m 参考不再是 step2 抽样 CSV（约60m锚点间距），而是完整30m约束层
      ``30m_constraint_grid.csv``（A-05），粗格就是真实30m像元，不是稀疏锚点；
    - 细→粗映射统一改用 core.grid_mapping 的仿射逆变换（Phase 1 聚合、Phase 2
      回写使用同一个映射函数），不再是 Phase 1 用 KDTree 角点最近邻、Phase 2 用
      row/3、evaluation 又用另一套手工除法这种三套不一致算子；
    - 聚合使用 np.bincount 稠密数组，不用 Python dict 存全部粗格（B-03/7.2）；
    - 提供两种命名清晰的模式（用户确认第3条，默认 block_constant）：
        * block_constant（默认）：每个30m格内所有细像元加同一残差，在同一父格/
          同一有效像元集合/同一权重下精确满足算术均值闭合，边界可能块状；
        * smooth_recentered（可选，未过验收前不作默认）：先生成平滑残差场，
          再按同一父格重中心化，格内均值闭合同样精确成立，但需额外给出
          格内误差与边界跳变统计，不能预先承诺全局连续；
    - 越界/掩膜洞不再被无最大距离的最近邻硬吸附到边界锚点；
    - 输出 spectral/ttri/tcr/lst_final 有效性与 out_of_grid 计数（B-03）。

TCR_30m = LST_true_30m - mean(LST_pred_in_30m_cell)
LST_final = LST_pred + TCR

本模块只讨论"30m 产品格网算术均值闭合"，不宣称辐射或能量守恒（A-06/A-07）。
"""

import json
import os
import time
from typing import Dict, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

from . import grid_mapping
from .atomic_io import write_verified

# ── 常量 ──────────────────────────────────────────────────────────────
TARGET_COL = "LST"
TCR_COL = "TCR"
LST_PRED_COL = "LST_pred"
LST_FINAL_COL = "LST_final"
SPECTRAL_COLS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI", "TTRI"]

MODE_BLOCK_CONSTANT = "block_constant"
MODE_SMOOTH_RECENTERED = "smooth_recentered"
VALID_MODES = (MODE_BLOCK_CONSTANT, MODE_SMOOTH_RECENTERED)


# ======================================================================
#  辅助函数
# ======================================================================


def _load_meta(meta_path: str) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


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
    """判断有效行（所有光谱+TTRI列均有值）。"""
    return df_slice[SPECTRAL_COLS].notna().all(axis=1)


class _BoundaryJumpAccumulator:
    """在不要求整幅栅格常驻内存的前提下，逐块累积"格内相邻像元差异"统计。

    利用预测CSV按行优先(row-major)写出的约定：在同一个 chunk 内，
    - 索引 i, i+1 若 row 相同且 col 相邻，即为水平相邻像元对；
    - 索引 i, i+width 若 col 相同且 row 相邻，即为垂直相邻像元对。
    分别按"落在同一30m格" vs "跨30m格边界"分类，用 sum/sumsq/count/max
    做在线累积，避免保存全部差值（对应7.2节的内存约束）。
    """

    def __init__(self, width: int):
        self.width = width
        self.stats = {
            "same_cell": {"sum": 0.0, "sumsq": 0.0, "count": 0, "max": 0.0},
            "cross_cell": {"sum": 0.0, "sumsq": 0.0, "count": 0, "max": 0.0},
        }

    def _accumulate(self, key: str, diffs: np.ndarray) -> None:
        if diffs.size == 0:
            return
        d = np.abs(diffs)
        s = self.stats[key]
        s["sum"] += float(d.sum())
        s["sumsq"] += float((d ** 2).sum())
        s["count"] += int(d.size)
        s["max"] = max(s["max"], float(d.max()))

    def add_chunk(self, row: np.ndarray, col: np.ndarray, coarse_row: np.ndarray,
                  coarse_col: np.ndarray, values: np.ndarray) -> None:
        n = len(row)
        if n > 1:
            horiz_adjacent = (row[:-1] == row[1:]) & (col[1:] == col[:-1] + 1)
            if horiz_adjacent.any():
                diffs = values[1:][horiz_adjacent] - values[:-1][horiz_adjacent]
                same = (
                    (coarse_row[1:][horiz_adjacent] == coarse_row[:-1][horiz_adjacent])
                    & (coarse_col[1:][horiz_adjacent] == coarse_col[:-1][horiz_adjacent])
                )
                self._accumulate("same_cell", diffs[same])
                self._accumulate("cross_cell", diffs[~same])
        if n > self.width:
            vert_adjacent = (row[self.width:] == row[:-self.width] + 1) & (
                col[self.width:] == col[:-self.width]
            )
            if vert_adjacent.any():
                diffs = values[self.width:][vert_adjacent] - values[:-self.width][vert_adjacent]
                same = (
                    (coarse_row[self.width:][vert_adjacent] == coarse_row[:-self.width][vert_adjacent])
                    & (coarse_col[self.width:][vert_adjacent] == coarse_col[:-self.width][vert_adjacent])
                )
                self._accumulate("same_cell", diffs[same])
                self._accumulate("cross_cell", diffs[~same])

    def finalize(self) -> Dict:
        out = {}
        for key, s in self.stats.items():
            if s["count"] > 0:
                mean = s["sum"] / s["count"]
                var = max(s["sumsq"] / s["count"] - mean ** 2, 0.0)
                out[key] = {
                    "n_pairs": s["count"],
                    "mean_abs_diff_K": round(mean, 6),
                    "std_abs_diff_K": round(float(np.sqrt(var)), 6),
                    "max_abs_diff_K": round(s["max"], 6),
                }
            else:
                out[key] = {"n_pairs": 0, "mean_abs_diff_K": None, "std_abs_diff_K": None, "max_abs_diff_K": None}
        return out


# ======================================================================
#  主函数
# ======================================================================


def compute_tcr(
    constraint_csv: str,
    constraint_meta_json: str,
    predict_10m_csv: str,
    meta_10m_json: str,
    model_path: str,
    output_path: str,
    mode: str = MODE_BLOCK_CONSTANT,
    batch_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """计算TCR（热约束残差）+ LST_final。

    Args:
        constraint_csv:        完整30m约束层CSV路径（30m_constraint_grid.csv，含LST列）
        constraint_meta_json:  完整30m约束层元数据JSON路径
        predict_10m_csv:       10m预测数据CSV路径（含TTRI列）
        meta_10m_json:         10m元数据JSON路径
        model_path:            训练好的RF模型.pkl路径
        output_path:           输出CSV路径（含 LST_pred, TCR, LST_final）
        mode:                  "block_constant"（默认，精确满足算术均值闭合，边界可能块状）
                                或 "smooth_recentered"（可选，先平滑后按格重中心化，
                                仍满足格内均值闭合，但不预先承诺全局连续，附加格内
                                误差与边界跳变诊断）
        batch_size:            批处理大小
        progress_callback:     进度回调

    Returns:
        dict: 输出路径、TCR统计信息、有效性诊断（B-03）
    """
    if mode not in VALID_MODES:
        raise ValueError(f"未知 TCR 模式: {mode}，可选: {VALID_MODES}")

    t_start = time.time()
    if progress_callback:
        progress_callback("tcr", 0, f"开始TCR + LST_final 计算（模式={mode}）...")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    metrics_path = model_path.replace("_model_", "_metrics_").replace(".pkl", ".json")

    coarse_meta = _load_meta(constraint_meta_json)
    fine_meta = _load_meta(meta_10m_json)
    coarse_transform = coarse_meta["transform"]
    fine_transform = fine_meta["transform"]
    coarse_height, coarse_width = int(coarse_meta["height"]), int(coarse_meta["width"])
    fine_width = int(fine_meta["width"])
    grid_mapping.assert_same_crs(coarse_meta.get("crs"), fine_meta.get("crs"), context="TCR: 30m约束层 vs 10m预测格网")
    ratio_diag = grid_mapping.check_exact_ratio_grid(fine_transform, coarse_transform)

    model, feature_cols = _load_model_meta(model_path, metrics_path)

    # ─── 1. 读取完整30m约束层，构建稠密 LST 参考栅格 ──────────────────
    if progress_callback:
        progress_callback("tcr", 0.05, "加载完整30m约束层...")

    df_30m = pd.read_csv(constraint_csv, usecols=["row", "col", TARGET_COL])
    lst_true_dense = np.full(coarse_height * coarse_width, np.nan, dtype=np.float64)
    c_row = df_30m["row"].values.astype(np.int64)
    c_col = df_30m["col"].values.astype(np.int64)
    in_range_30m = (c_row >= 0) & (c_row < coarse_height) & (c_col >= 0) & (c_col < coarse_width)
    lst_true_dense[c_row[in_range_30m] * coarse_width + c_col[in_range_30m]] = df_30m[TARGET_COL].values[in_range_30m]
    n_30m = int(in_range_30m.sum())
    del df_30m

    sum_pred = np.zeros(coarse_height * coarse_width, dtype=np.float64)
    count_pred = np.zeros(coarse_height * coarse_width, dtype=np.int64)
    lst_pred_cache = []
    out_of_grid = 0
    total_spectral_valid = 0
    total_invalid = 0

    # ==================================================================
    #  Phase 1: 10m预测 + 30m格聚合（统一仿射映射，替代 KDTree/row3）
    # ==================================================================
    if progress_callback:
        progress_callback("tcr", 0.12, "Phase 1: 逐批预测10m + 统一仿射映射聚合到30m格...")

    t_phase1 = time.time()
    batch_count_1 = 0

    for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
        valid_mask = _is_valid_row(chunk)
        n_valid = int(valid_mask.sum())
        total_spectral_valid += n_valid
        total_invalid += int((~valid_mask).sum())

        if n_valid > 0:
            valid_chunk = chunk.loc[valid_mask]
            X = valid_chunk[feature_cols].values.astype(np.float64)
            lst_pred = model.predict(X)
            lst_pred_cache.append(lst_pred.astype(np.float32))

            fine_row = valid_chunk["row"].values.astype(np.float64)
            fine_col = valid_chunk["col"].values.astype(np.float64)
            coarse_row, coarse_col, inside = grid_mapping.fine_to_coarse_index(
                fine_row, fine_col, fine_transform, coarse_transform, coarse_height, coarse_width
            )
            out_of_grid += int((~inside).sum())

            if inside.any():
                s_arr, c_arr = grid_mapping.aggregate_by_coarse_cell(
                    coarse_row[inside], coarse_col[inside], lst_pred[inside], coarse_height, coarse_width
                )
                sum_pred += s_arr
                count_pred += c_arr

        batch_count_1 += 1
        if progress_callback and batch_count_1 % 20 == 0:
            progress_callback(
                "tcr", 0.12 + 0.20 * min(batch_count_1 / 100, 1.0),
                f"Phase 1 批次 {batch_count_1}: {n_valid:,} 有效, "
                f"耗时 {time.time() - t_phase1:.0f}s",
            )

    t_phase1_elapsed = time.time() - t_phase1

    # ─── 计算 TCR_30m（稠密数组，覆盖真实30m格网，不是稀疏锚点）───────
    if progress_callback:
        progress_callback("tcr", 0.35, "计算TCR_30m（真实30m格网）...")

    valid_cells = (count_pred > 0) & np.isfinite(lst_true_dense)
    n_valid_cells = int(valid_cells.sum())
    if n_valid_cells == 0:
        raise RuntimeError("TCR 计算失败：没有任何30m格同时具有有效参考LST和至少一个10m预测子像元")

    mean_pred = np.zeros_like(sum_pred)
    mean_pred[valid_cells] = sum_pred[valid_cells] / count_pred[valid_cells]
    tcr_dense = np.full(coarse_height * coarse_width, np.nan, dtype=np.float64)
    tcr_dense[valid_cells] = lst_true_dense[valid_cells] - mean_pred[valid_cells]

    valid_tcr_values = tcr_dense[valid_cells]
    subpixel_counts = count_pred[valid_cells]
    tcr_stats = {
        "min": float(valid_tcr_values.min()),
        "max": float(valid_tcr_values.max()),
        "mean": float(valid_tcr_values.mean()),
        "std": float(valid_tcr_values.std()),
        "n_valid_blocks": int(n_valid_cells),
        "n_reference_30m_pixels": n_30m,
        "subpixel_count_per_cell": {
            "min": int(subpixel_counts.min()),
            "median": float(np.median(subpixel_counts)),
            "p95": float(np.percentile(subpixel_counts, 95)),
            "max": int(subpixel_counts.max()),
        },
    }

    tcr_grid_2d = tcr_dense.reshape(coarse_height, coarse_width)
    # 无 TCR 的 30m 格（约束层外/无子像元）用最近有效 TCR 回退，
    # 保证每个预测样本都有 TCR（预测数据只按 S2 去云，不再额外扣点）
    tcr_nearest = grid_mapping.nearest_valid_index(tcr_grid_2d)
    lst_pred_all = np.concatenate(lst_pred_cache) if lst_pred_cache else np.array([], dtype=np.float32)
    del lst_pred_cache, sum_pred, count_pred, mean_pred, lst_true_dense

    smooth_interp = None
    correction_dense = None
    pre_recenter_diag = None
    if mode == MODE_SMOOTH_RECENTERED:
        if progress_callback:
            progress_callback("tcr", 0.40, "smooth_recentered: 构建平滑残差场插值器...")
        smooth_interp = grid_mapping.build_dense_grid_interpolator(tcr_grid_2d)

        # 第二遍：计算平滑场在每个有效细像元处的取值，按格聚合均值，
        # 得到"约束前"（未重中心化）的格内均值，用于与真值求 correction，
        # 并作为"约束前"格内误差/边界跳变诊断的基准。
        smooth_sum = np.zeros(coarse_height * coarse_width, dtype=np.float64)
        smooth_count = np.zeros(coarse_height * coarse_width, dtype=np.int64)
        pre_boundary = _BoundaryJumpAccumulator(fine_width)
        lst_pred_offset = 0

        for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
            valid_mask = _is_valid_row(chunk)
            n_valid = int(valid_mask.sum())
            if n_valid == 0:
                continue
            valid_chunk = chunk.loc[valid_mask]
            fine_row = valid_chunk["row"].values.astype(np.float64)
            fine_col = valid_chunk["col"].values.astype(np.float64)
            smooth_vals = grid_mapping.interpolate_dense_grid_to_fine(
                fine_row, fine_col, fine_transform, coarse_transform, smooth_interp
            )
            coarse_row, coarse_col, inside = grid_mapping.fine_to_coarse_index(
                fine_row, fine_col, fine_transform, coarse_transform, coarse_height, coarse_width
            )
            finite = inside & np.isfinite(smooth_vals)
            if finite.any():
                s_arr, c_arr = grid_mapping.aggregate_by_coarse_cell(
                    coarse_row[finite], coarse_col[finite], smooth_vals[finite], coarse_height, coarse_width
                )
                smooth_sum += s_arr
                smooth_count += c_arr
                pre_boundary.add_chunk(
                    valid_chunk["row"].values.astype(np.int64)[finite],
                    valid_chunk["col"].values.astype(np.int64)[finite],
                    coarse_row[finite], coarse_col[finite], smooth_vals[finite],
                )

        smooth_mean_dense = np.full(coarse_height * coarse_width, np.nan, dtype=np.float64)
        has_smooth = smooth_count > 0
        smooth_mean_dense[has_smooth] = smooth_sum[has_smooth] / smooth_count[has_smooth]

        correction_dense = np.zeros(coarse_height * coarse_width, dtype=np.float64)
        can_correct = valid_cells & has_smooth
        correction_dense[can_correct] = tcr_dense[can_correct] - smooth_mean_dense[can_correct]
        correction_dense = correction_dense.reshape(coarse_height, coarse_width)

        cell_error_pre = np.abs(smooth_mean_dense[valid_cells & has_smooth] - tcr_dense[valid_cells & has_smooth])
        pre_recenter_diag = {
            "cell_mean_abs_error_K": {
                "mean": None if cell_error_pre.size == 0 else round(float(cell_error_pre.mean()), 6),
                "max": None if cell_error_pre.size == 0 else round(float(cell_error_pre.max()), 6),
                "n_cells": int(cell_error_pre.size),
            },
            "boundary_jump": pre_boundary.finalize(),
        }
        del smooth_sum, smooth_count, smooth_mean_dense

    # ==================================================================
    #  Phase 2: 回写 TCR + 计算LST_final
    # ==================================================================
    if progress_callback:
        progress_callback("tcr", 0.55, f"Phase 2: 回写TCR（{mode}）+ 计算LST_final...")

    t_phase2 = time.time()
    lst_pred_offset = 0
    output_written = False
    total_valid_2 = 0
    total_invalid_2 = 0
    tcr_valid_count = 0
    lst_final_valid_count = 0
    batch_count_2 = 0
    post_boundary = _BoundaryJumpAccumulator(fine_width) if mode == MODE_SMOOTH_RECENTERED else None
    post_cell_sum = np.zeros(coarse_height * coarse_width, dtype=np.float64) if mode == MODE_SMOOTH_RECENTERED else None
    post_cell_count = np.zeros(coarse_height * coarse_width, dtype=np.int64) if mode == MODE_SMOOTH_RECENTERED else None

    for chunk in pd.read_csv(predict_10m_csv, chunksize=batch_size):
        valid_mask = _is_valid_row(chunk)
        n_valid = int(valid_mask.sum())
        n_invalid = int((~valid_mask).sum())
        total_invalid_2 += n_invalid
        total_valid_2 += n_valid

        tcr_vals = np.full(len(chunk), np.nan, dtype=np.float64)
        lst_pred_vals = np.full(len(chunk), np.nan, dtype=np.float32)

        if n_valid > 0:
            valid_chunk = chunk.loc[valid_mask]
            fine_row_i = valid_chunk["row"].values.astype(np.int64)
            fine_col_i = valid_chunk["col"].values.astype(np.int64)
            fine_row_f = fine_row_i.astype(np.float64)
            fine_col_f = fine_col_i.astype(np.float64)

            coarse_row, coarse_col, inside = grid_mapping.fine_to_coarse_index(
                fine_row_f, fine_col_f, fine_transform, coarse_transform, coarse_height, coarse_width
            )

            local_tcr = np.full(n_valid, np.nan, dtype=np.float64)
            if mode == MODE_BLOCK_CONSTANT:
                if inside.any():
                    local_tcr[inside] = tcr_grid_2d[coarse_row[inside], coarse_col[inside]]
                # 非 inside 或对应30m格无 TCR（约束层外/无子像元）的预测像元，
                # 用最近有效 TCR 回退，保证每个预测样本都有 TCR
                # （预测数据只按 S2 去云，不再额外扣点）。
                missing = ~np.isfinite(local_tcr)
                if tcr_nearest is not None and missing.any():
                    r_c = np.clip(coarse_row[missing], 0, coarse_height - 1)
                    c_c = np.clip(coarse_col[missing], 0, coarse_width - 1)
                    local_tcr[missing] = tcr_grid_2d[
                        tcr_nearest[0][r_c, c_c], tcr_nearest[1][r_c, c_c]
                    ]
            else:  # smooth_recentered
                smooth_vals = grid_mapping.interpolate_dense_grid_to_fine(
                    fine_row_f, fine_col_f, fine_transform, coarse_transform, smooth_interp,
                    grid=tcr_grid_2d, nearest_index=tcr_nearest,
                )
                # 插值 NaN 已被最近邻回退填平，非 inside 像元直接取回退后的平滑值
                local_tcr = np.where(np.isfinite(smooth_vals), smooth_vals, np.nan)
                if inside.any():
                    block_fallback = tcr_grid_2d[coarse_row[inside], coarse_col[inside]]
                    corr = correction_dense[coarse_row[inside], coarse_col[inside]]
                    smooth_ok = np.isfinite(smooth_vals[inside])
                    combined = np.where(smooth_ok, smooth_vals[inside] + corr, block_fallback)
                    # 双线性插值在30m栅格边缘半个像元内天然无法定义（超出插值凸包），
                    # 该处退化为与 block_constant 相同的整格常数，保证 smooth_recentered
                    # 的覆盖率不低于 block_constant，不引入额外空洞（仍标注见 validity 诊断）。
                    local_tcr[inside] = combined

                    finite_local = np.isfinite(local_tcr[inside])
                    if finite_local.any():
                        idx_inside = np.flatnonzero(inside)
                        idx_finite = idx_inside[finite_local]
                        post_cell_sum_idx = (
                            coarse_row[idx_finite] * coarse_width + coarse_col[idx_finite]
                        )
                        np.add.at(post_cell_sum, post_cell_sum_idx, local_tcr[idx_finite])
                        np.add.at(post_cell_count, post_cell_sum_idx, 1)
                        post_boundary.add_chunk(
                            fine_row_i[idx_finite], fine_col_i[idx_finite],
                            coarse_row[idx_finite], coarse_col[idx_finite], local_tcr[idx_finite],
                        )

            tcr_vals[valid_mask.values] = local_tcr
            tcr_valid_count += int(np.isfinite(local_tcr).sum())

            batch_lst_pred = lst_pred_all[lst_pred_offset:lst_pred_offset + n_valid]
            lst_pred_vals[valid_mask.values] = batch_lst_pred
            lst_pred_offset += n_valid

        lst_final_vals = lst_pred_vals + tcr_vals
        lst_final_valid_count += int(np.isfinite(lst_final_vals).sum())

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
                "tcr", 0.55 + 0.40 * min(batch_count_2 / 100, 1.0),
                f"Phase 2 批次 {batch_count_2}: {n_valid:,} 有效",
            )

    t_phase2_elapsed = time.time() - t_phase2
    total_elapsed = time.time() - t_start

    post_recenter_diag = None
    if mode == MODE_SMOOTH_RECENTERED:
        has_post = post_cell_count > 0
        post_mean = np.full(coarse_height * coarse_width, np.nan, dtype=np.float64)
        post_mean[has_post] = post_cell_sum[has_post] / post_cell_count[has_post]
        cell_error_post = np.abs(post_mean[valid_cells & has_post] - tcr_dense[valid_cells & has_post])
        post_recenter_diag = {
            "cell_mean_abs_error_K": {
                "mean": None if cell_error_post.size == 0 else round(float(cell_error_post.mean()), 8),
                "max": None if cell_error_post.size == 0 else round(float(cell_error_post.max()), 8),
                "n_cells": int(cell_error_post.size),
            },
            "boundary_jump": post_boundary.finalize(),
        }

    if progress_callback:
        progress_callback("tcr", 1.0,
            f"TCR+LST_final完成（{mode}）: 有效 {total_valid_2:,} 行, "
            f"总耗时 {total_elapsed:.1f}s")

    validity = {
        "spectral_valid": int(total_spectral_valid),
        "spectral_invalid": int(total_invalid_2),
        "tcr_valid": int(tcr_valid_count),
        "lst_final_valid": int(lst_final_valid_count),
        "out_of_grid": int(out_of_grid),
        "reference_30m_valid_cells": int(n_valid_cells),
        "reference_30m_total_pixels": int(coarse_height * coarse_width),
    }

    return {
        "output_path": output_path,
        "mode": mode,
        "tcr_statistics": tcr_stats,
        "validity": validity,
        "grid_ratio_diagnostics": ratio_diag,
        "smooth_recentered_diagnostics": (
            {"pre_recenter": pre_recenter_diag, "post_recenter": post_recenter_diag}
            if mode == MODE_SMOOTH_RECENTERED else None
        ),
        "total_valid_10m": total_valid_2,
        "total_invalid_10m": total_invalid_2,
        "phase1_seconds": round(t_phase1_elapsed, 1),
        "phase2_seconds": round(t_phase2_elapsed, 1),
        "total_seconds": round(total_elapsed, 1),
        "energy_conservation_disclaimer": (
            "TCR 反映的是10m预测回聚合到30m产品格网后的算术均值闭合程度，"
            "不代表辐射或能量守恒（A-06/A-07）。"
        ),
    }
