# -*- coding: utf-8 -*-
"""科学回归复算：武汉 2024-07-22 配对模式（升级第一阶段验收）。

依据 docs/回归用例_武汉20240722.md §7 复跑办法第 1 级（输入冻结复算，首选）：
以 Docker 卷内冻结的 raw 输入为起点，用旧串行路径（EasyLSTPipeline，总体方案
§15.5 认可的对照路径）从 data_pipeline 复跑到导出，关键数值与登记值逐项对比。

放行标准（§7）：主链各阶段统计与登记值一致（在明确容差内）；
任何阶段对不上先停下查因，不允许放宽容差直到通过。

在容器内运行（需 GDAL/镜像环境）：
    python3 tests/regression_wuhan20240722.py            # 复算 + 对比
    python3 tests/regression_wuhan20240722.py --compare-only
        # 跳过复算，仅对上次运行输出目录做对比（用于复测）

输出目录：/app/data/regression_stage1/wuhan20240722/（独立目录，不污染原结果）
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# ── 登记基线（docs/回归用例_武汉20240722.md §2–§5） ──────────────
BASELINE = {
    "split_counts": {"train": 925468, "validate": 228098, "test": 243225,
                     "buffer_excluded": 543349},
    "train_sample_rows": 1940140,
    "constraint_rows": 7761388,
    "predict_valid_pixels": 70461903,
    "ttri_coef": [-0.026030299968184845, 0.17060905722308478, -1.6882347347447544],
    "ttri_intercept": 314.790633,
    "ttri_fit_r2": 0.072584,
    "rf_test": {"r2": 0.812657, "rmse": 2.320824, "mae": 1.690762, "mb": 0.001273},
    "lst_final": {"min": 287.167, "max": 349.080, "mean": 314.177, "std": 5.502},
    "closure": {"coverage": 1.0, "mb": 0.0, "mae": 8e-06, "rmse": 1e-05},
}
TOL = {"r2": 1e-4, "rmse": 1e-3, "mae": 1e-3, "mb": 1e-3,
       "lst_abs": 1e-2, "closure_abs": 1e-5}

PAIR = Path("/app/data/users/Adhara/workspace/最后测试/convs/8e55b8d22ffe/pairs/L20240721_S20240722")
REG_ROOT = Path("/app/data/regression_stage1")
OUT_DIR = REG_ROOT / "wuhan20240722"

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append((name, detail))


def close(a, b, tol):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ── 复算 ────────────────────────────────────────────────────────

def prepare_input() -> Path:
    """把冻结 raw 输入复制到独立回归目录（绝不写原 pair 目录）。"""
    src = PAIR / "raw"
    dst = REG_ROOT / "input"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        target = dst / f.name
        if not target.exists():
            shutil.copy2(f, target)
    return dst


def run_pipeline(raw_dir: Path) -> None:
    from core.pipeline import EasyLSTPipeline

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    pipeline = EasyLSTPipeline()
    pipeline.configure(
        landsat_path=str(raw_dir / "landsat_lst_20240721.tif"),
        sentinel2_path=str(raw_dir / "sentinel2_bands_20240722.tif"),
        qa_path=str(raw_dir / "landsat_qa_pixel_20240721.tif"),
        scl_path=str(raw_dir / "sentinel2_scl_20240722.tif"),
        dem_path=str(raw_dir / "dem.tif"),
        output_dir=str(OUT_DIR),
        # 冻结参数（登记用例 §2 + 原 run_manifest）：划分与缓冲不变
        seed=42,
        block_size_px=30,
        guard_buffer_m=100.0,
        train_ratio=0.6, val_ratio=0.2, test_ratio=0.2,
        tcr_mode="block_constant",
        # RF 最佳轮参数（登记用例 §4；n_jobs 线程额度随容器，不参与科学等价性）
        rf_params={
            "n_estimators": 200, "max_depth": 25,
            "min_samples_split": 16, "min_samples_leaf": 8,
            "max_features": 0.5, "random_state": 42,
        },
        batch_size=500000, chunk_size=500000,
    )
    t0 = time.time()

    def on_progress(step, pct, msg):
        print(f"  [进度] {step} {pct:5.1f}% {msg}", flush=True)

    def on_log(level, msg):
        print(f"  [{level}] {msg}", flush=True)

    pipeline.run_full(on_progress, on_log)
    print(f"复算完成，耗时 {time.time() - t0:.0f}s")


# ── 对比 ────────────────────────────────────────────────────────

def load_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def compare() -> None:
    print("\n== 数据划分（登记 §2）==")
    split = load_json(OUT_DIR / "for_train" / "split_info.json")
    counts = split["counts"]
    for k, v in BASELINE["split_counts"].items():
        check(f"划分 {k}={v}", counts.get(k) == v, f"实际 {counts.get(k)}")
    check("划分方法 spatial_block_guard_buffer / seed=42 / 块30 缓冲100m",
          split.get("method") == "spatial_block_guard_buffer"
          and split.get("seed") == 42
          and split.get("block_size_px") == 30
          and split.get("guard_buffer_m") == 100.0,
          f"实际 {split.get('method')}/{split.get('seed')}/"
          f"{split.get('block_size_px')}/{split.get('guard_buffer_m')}")

    import pyarrow.parquet as pq
    # 预处理训练抽样（登记 §2：训练抽样总行数）
    n_sampled = pq.read_metadata(
        str(OUT_DIR / "30m_features_step2.parquet")).num_rows
    check(f"训练抽样总行数={BASELINE['train_sample_rows']}",
          n_sampled == BASELINE["train_sample_rows"], f"实际 {n_sampled}")
    # 划分后的训练集（应等于 split counts.train）
    n_train = pq.read_metadata(
        str(OUT_DIR / "for_train" / "train.parquet")).num_rows
    check(f"划分后 train 行数={BASELINE['split_counts']['train']}",
          n_train == BASELINE["split_counts"]["train"], f"实际 {n_train}")
    n_constraint = pq.read_metadata(
        str(OUT_DIR / "30m_constraint_grid.parquet")).num_rows
    check(f"30m 约束格网={BASELINE['constraint_rows']}",
          n_constraint == BASELINE["constraint_rows"], f"实际 {n_constraint}")

    print("\n== TTRI（登记 §3）==")
    coef = load_json(OUT_DIR / "for_train" / "ttri_coefficients.json")
    got = coef.get("coefficients") or coef.get("coef")
    check("TTRI 系数逐位一致", list(map(float, got)) == BASELINE["ttri_coef"],
          f"实际 {got}")
    intercept = coef.get("intercept")
    check("TTRI 截距逐位一致", float(intercept) == BASELINE["ttri_intercept"],
          f"实际 {intercept}")
    fit_r2 = coef.get("fit_r2", coef.get("r2"))
    check(f"TTRI 拟合 R²={BASELINE['ttri_fit_r2']}",
          close(fit_r2, BASELINE["ttri_fit_r2"], 1e-6), f"实际 {fit_r2}")

    print("\n== RF test 指标（登记 §4，选优口径）==")
    # 新路径输出 results/test/rf_ttri_predict_*.json，指标在 metrics 子字典
    rf_test = {}
    for mf in sorted((OUT_DIR / "results" / "test").glob("*.json")):
        d = load_json(mf)
        metrics = d.get("metrics") or {}
        if metrics:
            rf_test = {k.lower(): v for k, v in metrics.items()}
            break
    for key in ("r2", "rmse", "mae", "mb"):
        got_v = rf_test.get(key)
        base_v = BASELINE["rf_test"][key]
        tol = TOL["r2"] if key == "r2" else TOL[key]
        check(f"RF test {key.upper()}≈{base_v}（容差 {tol}）",
              close(got_v, base_v, tol), f"实际 {got_v}")

    print("\n== 最终 LST 统计（登记 §5，K 级容差）==")
    tif = OUT_DIR / "results" / "predict_10m" / "rf_10m_lst_final.tif"
    if not tif.exists():
        candidates = sorted(OUT_DIR.rglob("*lst_final*.tif"))
        check("导出 GeoTIFF 存在", bool(candidates), "未找到 lst_final 产物")
        tif = candidates[0]
    from osgeo import gdal
    import numpy as np
    ds = gdal.Open(str(tif))
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    # 有效像元：有限值且不等于 nodata（nodata 可能为 None/nan）
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    vals = arr[mask]
    stats = {"min": float(vals.min()), "max": float(vals.max()),
             "mean": float(vals.mean()), "std": float(vals.std())}
    for k, v in BASELINE["lst_final"].items():
        check(f"LST {k}≈{v}", close(stats[k], v, TOL["lst_abs"]),
              f"实际 {stats[k]:.6f}")
    ds = None

    print("\n== 30m 闭合（登记 §5）==")
    closure_files = sorted(OUT_DIR.rglob("coarse_constraint_closure.json"))
    check("闭合评估 JSON 存在", bool(closure_files), "未找到 coarse_constraint_closure.json")
    cl = load_json(closure_files[0])
    inner = cl.get("closure") or {}
    metrics = inner.get("metrics") or {}
    got_coverage = inner.get("coverage_ratio", cl.get("coverage"))
    check(f"闭合 coverage={BASELINE['closure']['coverage']}",
          close(got_coverage, BASELINE["closure"]["coverage"], 1e-9),
          f"实际 {got_coverage}")
    for k, base_k in (("MB_K", "mb"), ("MAE_K", "mae"), ("RMSE_K", "rmse")):
        got_v = metrics.get(k)
        check(f"闭合 {k}≈{BASELINE['closure'][base_k]}",
              close(got_v, BASELINE["closure"][base_k], TOL["closure_abs"]),
              f"实际 {got_v}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare-only", action="store_true",
                        help="跳过复算，仅对既有输出目录做对比")
    args = parser.parse_args()

    print("科学回归复算：武汉 2024-07-22 配对模式（升级第一阶段验收）")
    if not args.compare_only:
        raw_dir = prepare_input()
        print(f"冻结输入已就位：{raw_dir}")
        run_pipeline(raw_dir)
    compare()

    print(f"\n回归对比结果：{len(FAILS)} 项不一致")
    for name, detail in FAILS:
        print(f"  - {name}: {detail}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
