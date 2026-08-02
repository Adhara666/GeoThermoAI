"""
空间一致性评估模块

基于技术文档 §5.4.2 的方法:
    1. 空间一致性: 将10m LST_final聚合到测试集对应的30m位置，计算MB/MAE/RMSE
    2. 值域范围: 统计10m LST_final与30m LST全量数据的min/max，验证偏差 < 5K
"""

import json
import os
import warnings
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


def _load_transform_from_meta(meta_path: str) -> Tuple[float, float, float, float, float, float]:
    """从 meta JSON 文件读取仿射变换参数 (a, b, c, d, e, f)"""
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    t = meta.get("transform", [])
    if len(t) >= 6:
        return tuple(t[:6])
    raise ValueError(f"meta 文件 {meta_path} 中缺少有效的 transform 参数")


def evaluate_spatial_consistency(
    test_csv: str,
    full_30m_csv: str,
    predict_csv: str,
    output_dir: str,
    meta_30m_json: str = None,
    meta_10m_json: str = None,
    progress_callback=None,
    t_30m: tuple = None,
    t_10m: tuple = None,
    chunk_size: int = 500000,
) -> Dict:
    """
    降尺度结果的空间一致性评估 + 值域范围分析。

    仿射变换参数优先从 meta JSON 文件读取，也可通过 t_30m/t_10m 直接传入。

    Args:
        test_csv:      30m测试集CSV路径（含 row, col, LST 列）
        full_30m_csv:  30m全量数据CSV路径（用于LST范围扫描）
        predict_csv:   10m预测结果CSV路径（含 row, col, LST_final 列）
        output_dir:    输出目录
        meta_30m_json: 30m元数据JSON路径（含transform参数）
        meta_10m_json: 10m元数据JSON路径（含transform参数）
        progress_callback: 进度回调 callback(step_name, percent, message)
        t_30m:         30m仿射变换参数 (a, b, c, d, e, f)，若提供则优先使用
        t_10m:         10m仿射变换参数 (a, b, c, d, e, f)，若提供则优先使用
        chunk_size:    批处理大小

    Returns:
        dict: 包含所有评估指标
            - spatial_consistency: {n_test_samples, n_matched, metrics: {MB, MAE, RMSE}}
            - value_range: {full_30m, full_10m, test_area_30m, test_area_10m_aggregated, deviation}
            - output_path: 评估结果JSON路径
    """
    # 读取仿射变换参数：优先使用直接传入的值，其次从 meta 文件读取
    if t_30m is None and meta_30m_json and os.path.exists(meta_30m_json):
        t_30m = _load_transform_from_meta(meta_30m_json)
    if t_10m is None and meta_10m_json and os.path.exists(meta_10m_json):
        t_10m = _load_transform_from_meta(meta_10m_json)

    if t_30m is None or t_10m is None:
        raise ValueError(
            "无法获取仿射变换参数：请提供 meta_30m_json/meta_10m_json 或直接传入 t_30m/t_10m"
        )
    if progress_callback:
        progress_callback("evaluation", 0, "开始空间一致性评估...")

    os.makedirs(output_dir, exist_ok=True)

    # ==================================================================
    #  步骤1: 扫描30m全量数据LST范围
    # ==================================================================
    if progress_callback:
        progress_callback("evaluation", 0.05, "扫描30m全量LST范围...")

    lst_min_30m, lst_max_30m = np.inf, -np.inf
    chunks_30m = 0
    for chunk in pd.read_csv(full_30m_csv, chunksize=chunk_size, usecols=["LST"]):
        c = chunk["LST"]
        lst_min_30m = min(lst_min_30m, c.min())
        lst_max_30m = max(lst_max_30m, c.max())
        chunks_30m += 1

    full_30m_min = float(lst_min_30m)
    full_30m_max = float(lst_max_30m)

    if progress_callback:
        progress_callback(
            "evaluation", 0.12,
            f"30m全量LST范围: {full_30m_min:.4f} ~ {full_30m_max:.4f}",
        )

    # ==================================================================
    #  步骤2: 加载测试集
    # ==================================================================
    if progress_callback:
        progress_callback("evaluation", 0.15, "加载30m测试集...")

    df_test = pd.read_csv(test_csv, usecols=["row", "col", "LST"])
    test_map = df_test.set_index(["row", "col"])["LST"]
    test_30m_min = float(df_test["LST"].min())
    test_30m_max = float(df_test["LST"].max())

    if progress_callback:
        progress_callback(
            "evaluation", 0.20,
            f"测试集: {len(df_test):,} 样本, LST范围: {test_30m_min:.4f} ~ {test_30m_max:.4f}",
        )

    # ==================================================================
    #  步骤3: 扫描10m结果并聚合到30m块
    # ==================================================================
    if progress_callback:
        progress_callback("evaluation", 0.25, "扫描10m结果并聚合到30m块...")

    agg: Dict[tuple, tuple] = {}  # {(r30, c30): (sum, count)}
    total_valid = 0
    chunk_count = 0
    pred_min, pred_max = np.inf, -np.inf

    for df_chunk in pd.read_csv(
        predict_csv, chunksize=chunk_size,
        usecols=["row", "col", "LST_final"],
    ):
        mask = df_chunk["LST_final"].notna()
        n_valid = mask.sum()
        if n_valid == 0:
            chunk_count += 1
            continue

        chunk = df_chunk[mask]
        r10 = chunk["row"].values
        c10 = chunk["col"].values
        lst = chunk["LST_final"].values

        pred_min = min(pred_min, lst.min())
        pred_max = max(pred_max, lst.max())

        # 10m像元中心UTM → 30m行列
        x_center = t_10m[2] + (c10 + 0.5) * t_10m[0]
        y_center = t_10m[5] + (r10 + 0.5) * t_10m[4]

        c30 = np.floor((x_center - t_30m[2]) / t_30m[0]).astype(int)
        r30 = np.floor((y_center - t_30m[5]) / t_30m[4]).astype(int)

        # 聚合
        chunk_agg = (
            pd.DataFrame({"r30": r30, "c30": c30, "LST_final": lst})
            .groupby(["r30", "c30"])["LST_final"]
            .agg(["sum", "count"])
        )
        for (r, c), row in chunk_agg.iterrows():
            key = (r, c)
            if key in agg:
                agg[key] = (agg[key][0] + row["sum"], agg[key][1] + row["count"])
            else:
                agg[key] = (row["sum"], row["count"])

        total_valid += n_valid
        chunk_count += 1

        if progress_callback and chunk_count % 50 == 0:
            progress_callback(
                "evaluation",
                0.25 + 0.25 * min(chunk_count / 500, 1.0),
                f"已处理 {chunk_count} 批, {total_valid:,} 有效像素",
            )

    if progress_callback:
        progress_callback(
            "evaluation", 0.55,
            f"扫描完成: {chunk_count} 批, {total_valid:,} 有效像素, "
            f"聚合到 {len(agg)} 个30m像素",
        )

    # ==================================================================
    #  步骤4: 空间一致性计算（与测试集对比）
    # ==================================================================
    if progress_callback:
        progress_callback("evaluation", 0.60, "计算空间一致性指标...")

    agg_test_min, agg_test_max = np.inf, -np.inf
    matched = 0
    total_mb = 0.0
    total_mae = 0.0
    total_sq = 0.0

    for (r30, c30), (s, n) in agg.items():
        lst_10m_agg = s / n
        if (r30, c30) in test_map.index:
            lst_30m = test_map.loc[(r30, c30)]
            agg_test_min = min(agg_test_min, lst_10m_agg)
            agg_test_max = max(agg_test_max, lst_10m_agg)
            diff = lst_10m_agg - lst_30m
            total_mb += diff
            total_mae += abs(diff)
            total_sq += diff * diff
            matched += 1

    if matched == 0:
        raise RuntimeError("测试集与10m结果无重叠像素，无法评估空间一致性")

    mb = total_mb / matched
    mae = total_mae / matched
    rmse = np.sqrt(total_sq / matched)

    if progress_callback:
        progress_callback(
            "evaluation", 0.75,
            f"空间一致性: MB={mb:.4f}, MAE={mae:.4f}, RMSE={rmse:.4f}, 匹配 {matched:,} 像素",
        )

    # ==================================================================
    #  步骤5: 值域偏差分析
    # ==================================================================
    if progress_callback:
        progress_callback("evaluation", 0.80, "分析值域偏差...")

    dev_min_10m_vs_30m_full = pred_min - full_30m_min
    dev_max_10m_vs_30m_full = pred_max - full_30m_max
    dev_min_10m_vs_30m_test = agg_test_min - test_30m_min
    dev_max_10m_vs_30m_test = agg_test_max - test_30m_max
    max_dev = max(abs(dev_min_10m_vs_30m_full), abs(dev_max_10m_vs_30m_full))
    passed = max_dev < 5.0

    if progress_callback:
        progress_callback(
            "evaluation", 0.90,
            f"值域偏差最大绝对值: {max_dev:.4f}K ({'通过' if passed else '超出'}, 阈值<5K)",
        )

    # ==================================================================
    #  步骤6: 保存结果
    # ==================================================================
    existing = [
        f for f in os.listdir(output_dir)
        if f.startswith("spatial_consistency_") and f.endswith(".json")
    ]
    run_id = len(existing) + 1
    output_path = os.path.join(output_dir, f"spatial_consistency_run{run_id:03d}.json")

    result = {
        "task": "spatial_consistency_and_range_evaluation",
        "data": {
            "test_set": test_csv,
            "full_30m": full_30m_csv,
            "predict_10m": predict_csv,
        },
        "spatial_consistency": {
            "n_test_samples": int(len(df_test)),
            "n_matched": int(matched),
            "metrics": {
                "MB": round(mb, 4),
                "MAE": round(mae, 4),
                "RMSE": round(rmse, 4),
            },
        },
        "value_range": {
            "full_30m": {
                "min": round(full_30m_min, 4),
                "max": round(full_30m_max, 4),
            },
            "full_10m": {
                "min": round(pred_min, 4),
                "max": round(pred_max, 4),
            },
            "test_area_30m": {
                "min": round(test_30m_min, 4),
                "max": round(test_30m_max, 4),
            },
            "test_area_10m_aggregated": {
                "min": round(agg_test_min, 4),
                "max": round(agg_test_max, 4),
            },
            "deviation": {
                "full_10m_vs_full_30m_min": round(dev_min_10m_vs_30m_full, 4),
                "full_10m_vs_full_30m_max": round(dev_max_10m_vs_30m_full, 4),
                "test_10m_vs_test_30m_min": round(dev_min_10m_vs_30m_test, 4),
                "test_10m_vs_test_30m_max": round(dev_max_10m_vs_30m_test, 4),
                "max_abs_deviation": round(max_dev, 4),
                "threshold_K": 5,
                "passed": bool(passed),
            },
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if progress_callback:
        progress_callback(
            "evaluation",
            1.0,
            f"评估完成: MB={mb:.4f}, MAE={mae:.4f}, RMSE={rmse:.4f}, "
            f"值域偏差={max_dev:.4f}K ({'通过' if passed else '超出'})",
        )

    return result
