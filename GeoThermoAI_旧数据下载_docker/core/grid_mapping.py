"""
统一细→粗像元几何映射模块（对应审查文档 A-06）

背景问题（已在 small_pipeline 实证中确认，见审查文档 §12.3）：
    - ttri.py 用 row/3, col/3
    - tcr.py Phase 1 用像元角点坐标 + KDTree 最近邻（无最大距离/有效父格检查）
    - tcr.py Phase 2 又回到 row/3, col/3
    - evaluation.py 用仿射 a/c/e/f 手工除法 + floor，忽略旋转项 b/d
四套算子互不一致，且实测 30m/10m 像元比例为 3.0059328343（不是精确 3），
约 30% 有效像元的父格归属因此不同。

本模块提供唯一的 fine_pixel_to_coarse_cell 系列函数，TTRI 空间化插值网格、
TCR 聚合与回写、evaluation 30m 块聚合必须统一调用本模块，禁止各自实现
row/3、手工仿射除法等隐式假设。默认始终使用真实仿射逆变换（对 3:1 精确对齐
网格同样正确，只是不走"捷径"），只有 check_exact_ratio_grid() 判定网格严格
3:1 对齐时才允许调用方选用整数除法快捷路径作为性能优化——当前实际数据不满足
该条件，因此默认路径就是全量仿射映射。
"""

import math
from typing import Dict, Optional, Tuple

import numpy as np


def pixel_center_xy(row, col, transform) -> Tuple[np.ndarray, np.ndarray]:
    """像元 (row, col)（标量或 ndarray）→ 像元中心地理坐标 (x, y)。

    仿射约定（与 GDAL GeoTransform / 项目 meta.json 的 transform 字段一致）：
        x = a*col + b*row + c
        y = d*col + e*row + f
    像元中心取 (col+0.5, row+0.5)。
    """
    a, b, c, d, e, f = (float(v) for v in transform)
    row_arr = np.asarray(row, dtype=np.float64)
    col_arr = np.asarray(col, dtype=np.float64)
    x = a * (col_arr + 0.5) + b * (row_arr + 0.5) + c
    y = d * (col_arr + 0.5) + e * (row_arr + 0.5) + f
    return x, y


def xy_to_pixel_corner(x, y, transform) -> Tuple[np.ndarray, np.ndarray]:
    """地理坐标 (x, y) → 该仿射变换下的连续像元角点坐标 (row_corner, col_corner)。

    对结果取 floor 即为该点所在的整数像元索引。使用通用 2x2 仿射矩阵求逆，
    正确处理旋转项 b/d（不像旧 evaluation.py 那样忽略旋转）。
    """
    a, b, c, d, e, f = (float(v) for v in transform)
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ValueError("仿射变换奇异（行列式≈0），无法求逆；请检查 transform 是否损坏")
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    dx = x_arr - c
    dy = y_arr - f
    col = (e * dx - b * dy) / det
    row = (a * dy - d * dx) / det
    return row, col


def fine_to_coarse_index(
    fine_row,
    fine_col,
    fine_transform,
    coarse_transform,
    coarse_height: Optional[int] = None,
    coarse_width: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """细格像元 (row,col) → 细像元中心地理坐标 → 粗格逆仿射 → floor → 粗格整数索引。

    Returns:
        (coarse_row, coarse_col, inside_footprint)
        inside_footprint: 提供 coarse_height/coarse_width 时，标记该细像元映射出的
        粗格索引是否落在粗栅格有效范围内（超出范围的像元不应被硬分配给最近边界格）；
        未提供尺寸时恒为 True，调用方需自行核对。
    """
    x, y = pixel_center_xy(fine_row, fine_col, fine_transform)
    row_c, col_c = xy_to_pixel_corner(x, y, coarse_transform)
    coarse_row = np.floor(row_c).astype(np.int64)
    coarse_col = np.floor(col_c).astype(np.int64)
    if coarse_height is not None and coarse_width is not None:
        inside = (
            (coarse_row >= 0)
            & (coarse_row < coarse_height)
            & (coarse_col >= 0)
            & (coarse_col < coarse_width)
        )
    else:
        inside = np.ones_like(coarse_row, dtype=bool)
    return coarse_row, coarse_col, inside


def check_exact_ratio_grid(
    fine_transform,
    coarse_transform,
    expected_ratio: float = 3.0,
    atol_ratio: float = 1e-6,
    atol_origin_px: float = 1e-6,
) -> Dict:
    """诊断细/粗栅格是否满足"无旋转、原点对齐、分辨率恰为 expected_ratio:1"的
    快捷条件（只有满足时，row//ratio, col//ratio 才与仿射精确映射等价）。

    仅用于诊断/manifest 记录，不控制主映射逻辑分支——A-06 要求所有模块统一改为
    真实仿射映射；当前实测数据（30m/10m 像元比 3.0059328343）本就不满足该快捷
    条件，因此默认路径必须始终是精确仿射映射。
    """
    fa, fb, fc, fd, fe, ff = (float(v) for v in fine_transform)
    ca, cb, cc, cd, ce, cf = (float(v) for v in coarse_transform)
    no_rotation = abs(fb) < 1e-9 and abs(fd) < 1e-9 and abs(cb) < 1e-9 and abs(cd) < 1e-9
    fine_res_x, fine_res_y = abs(fa), abs(fe)
    coarse_res_x, coarse_res_y = abs(ca), abs(ce)
    ratio_x = coarse_res_x / fine_res_x if fine_res_x > 0 else float("nan")
    ratio_y = coarse_res_y / fine_res_y if fine_res_y > 0 else float("nan")
    ratio_ok = (
        math.isfinite(ratio_x)
        and math.isfinite(ratio_y)
        and abs(ratio_x - expected_ratio) <= atol_ratio * expected_ratio
        and abs(ratio_y - expected_ratio) <= atol_ratio * expected_ratio
    )
    try:
        fine_row_corner, fine_col_corner = xy_to_pixel_corner(cc, cf, fine_transform)
        origin_ok = bool(
            abs(float(fine_row_corner) - round(float(fine_row_corner))) < atol_origin_px
            and abs(float(fine_col_corner) - round(float(fine_col_corner))) < atol_origin_px
        )
    except Exception:
        origin_ok = False
    return {
        "no_rotation": bool(no_rotation),
        "resolution_ratio_x": None if not math.isfinite(ratio_x) else round(ratio_x, 10),
        "resolution_ratio_y": None if not math.isfinite(ratio_y) else round(ratio_y, 10),
        "resolution_ratio_matches_expected": bool(ratio_ok),
        "origin_aligned": bool(origin_ok),
        "fast_path_eligible": bool(no_rotation and ratio_ok and origin_ok),
    }


def assert_same_crs(crs_a: Optional[str], crs_b: Optional[str], context: str = "") -> None:
    """核查两个栅格 CRS 字符串一致；不一致时结构化失败，而不是静默按 transform 数值硬算。"""
    if crs_a and crs_b and str(crs_a) != str(crs_b):
        suffix = f"（{context}）" if context else ""
        raise ValueError(f"CRS 不一致{suffix}: {crs_a} != {crs_b}")


def build_dense_grid_interpolator(grid: np.ndarray):
    """把稠密 (height, width) 数组包装为按"像元索引"为坐标轴的双线性插值器。

    供 TTRI 10m 空间化插值、TCR smooth_recentered 平滑场共用（A-06 统一实现，
    避免各模块各自构造 RegularGridInterpolator 的坐标轴假设不一致）。
    """
    from scipy.interpolate import RegularGridInterpolator

    h, w = grid.shape
    return RegularGridInterpolator(
        (np.arange(h, dtype=np.float64), np.arange(w, dtype=np.float64)),
        grid,
        method="linear",
        bounds_error=False,
        fill_value=np.nan,
    )


def interpolate_dense_grid_to_fine(
    fine_row, fine_col, fine_transform, coarse_transform, interpolator
) -> np.ndarray:
    """统一仿射映射 + 双线性插值：把稠密粗格栅格插值到给定细格 (row,col) 位置。

    RegularGridInterpolator 的采样点坐标 i 对应"像元中心"，而 xy_to_pixel_corner
    返回的是角点坐标，需要减 0.5 换算到中心坐标系，才能与稠密栅格的索引坐标对齐。
    """
    x, y = pixel_center_xy(fine_row, fine_col, fine_transform)
    row_corner, col_corner = xy_to_pixel_corner(x, y, coarse_transform)
    query_row = row_corner - 0.5
    query_col = col_corner - 0.5
    pts = np.column_stack([query_row, query_col])
    return interpolator(pts)


def aggregate_by_coarse_cell(
    coarse_row: np.ndarray,
    coarse_col: np.ndarray,
    values: np.ndarray,
    coarse_height: int,
    coarse_width: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """用 np.bincount 对 (coarse_row, coarse_col, value) 按粗格线性索引做 sum/count
    聚合，避免 Python dict 存全部粗格对象开销（对应 B-03/7.2 节建议）。

    调用方需先用 fine_to_coarse_index 返回的 inside_footprint 过滤越界像元。

    Returns:
        (sum_arr, count_arr): 形状 (coarse_height*coarse_width,) 的数组，
        sum_arr[idx]/count_arr[idx] 即为该粗格算术均值；idx = row*width + col。
    """
    idx = coarse_row.astype(np.int64) * int(coarse_width) + coarse_col.astype(np.int64)
    n_cells = int(coarse_height) * int(coarse_width)
    sum_arr = np.bincount(idx, weights=values.astype(np.float64), minlength=n_cells)[:n_cells]
    count_arr = np.bincount(idx, minlength=n_cells)[:n_cells]
    return sum_arr, count_arr
