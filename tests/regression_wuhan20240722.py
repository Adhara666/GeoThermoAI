# -*- coding: utf-8 -*-
"""科学回归复算：武汉 2024-07-22 配对模式（升级验收共用）。

依据 docs/回归用例_武汉20240722.md §7 两级复跑办法：

  --raw-dir <目录>   第 1 级「输入冻结复算」：以已有 raw 输入为起点复跑主链
  --download         第 2 级「完整复跑」：调用程序自己的数据获取技能，
                     从 STAC 检索 → 选配对 → 下载 → 主链 → 对比，
                     全流程一次不落（第二阶段验收用）
  --compare-only     跳过复算，仅对既有输出目录做对比（复测用）

放行标准（§7）：主链各阶段统计与登记值一致（在明确容差内）；
任何阶段对不上先停下查因，**不允许放宽容差直到通过**。

在容器内运行（需 GDAL/镜像环境）：
    python3 tests/regression_wuhan20240722.py --download \\
        --region /app/input/武汉市_市.geojson --out /app/data/regression_stage2
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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

# 登记用例的场景身份（§1、§6）：完整复跑必须落到同一对影像上，
# 否则比的就不是同一份输入
PAIR_LANDSAT_DATE = "2024-07-21"
PAIR_SENTINEL_DATE = "2024-07-22"
SEARCH_START = "2024-07-01"
SEARCH_END = "2024-07-31"
CLOUD_THRESHOLD = 30

# 第一阶段的默认路径（Docker 卷内冻结输入），保持向后兼容
DEFAULT_PAIR = Path("/app/data/users/Adhara/workspace/最后测试/convs/"
                    "8e55b8d22ffe/pairs/L20240721_S20240722")
DEFAULT_OUT_ROOT = Path("/app/data/regression_stage1")

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""),
          flush=True)
    if not cond:
        FAILS.append((name, detail))


def close(a, b, tol):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ── 第 2 级：完整复跑（含下载） ─────────────────────────────────


def acquire(region: str, raw_dir: Path) -> dict:
    """调用程序自己的数据获取技能：检索 → 选定登记配对 → 下载。

    不另写下载代码，走的就是生产路径（`DataAcquisitionSkill`），
    这样「下载环节是否被编排层改动破坏」也在回归覆盖范围内。
    """
    from core.skills.builtin.data_acquisition import DataAcquisitionSkill

    skill = DataAcquisitionSkill()
    raw_dir.mkdir(parents=True, exist_ok=True)

    def on_log(level, msg):
        print(f"  [{level}] {msg}", flush=True)

    def on_progress(step, pct, msg):
        print(f"  [进度] {step} {pct * 100:5.1f}% {msg}", flush=True)

    base = {"region": region, "start_date": SEARCH_START, "end_date": SEARCH_END,
            "output_dir": str(raw_dir), "cloud_threshold": CLOUD_THRESHOLD,
            "dem_source": "copernicus"}

    print("\n== 数据获取①：检索候选影像配对 ==", flush=True)
    found = skill.execute(dict(base), on_progress, on_log)
    check("检索成功", found.success, found.message)
    if not found.success:
        raise SystemExit("检索失败，无法继续完整复跑")

    pairs = found.data.get("image_pairs") or []
    check(f"检索到候选配对（{len(pairs)} 组）", bool(pairs))
    target = next((p for p in pairs
                   if str(p.get("landsat_date")) == PAIR_LANDSAT_DATE
                   and str(p.get("sentinel2_date")) == PAIR_SENTINEL_DATE), None)
    check(f"候选中包含登记配对 L{PAIR_LANDSAT_DATE} + S{PAIR_SENTINEL_DATE}",
          target is not None,
          "实际候选：" + "；".join(
              f"L{p.get('landsat_date')}+S{p.get('sentinel2_date')}" for p in pairs))
    if target is None:
        raise SystemExit("远程数据源没有登记用例的那对影像，先核对场景身份再判断")

    print("\n== 数据获取②：按登记配对下载原始输入 ==", flush=True)
    got = skill.execute({**base, "selected_pair": target}, on_progress, on_log)
    check("下载成功", got.success, got.message)
    if not got.success:
        raise SystemExit("下载失败，无法继续完整复跑")

    expected = [f"landsat_lst_{PAIR_LANDSAT_DATE.replace('-', '')}.tif",
                f"landsat_qa_pixel_{PAIR_LANDSAT_DATE.replace('-', '')}.tif",
                f"sentinel2_bands_{PAIR_SENTINEL_DATE.replace('-', '')}.tif",
                f"sentinel2_scl_{PAIR_SENTINEL_DATE.replace('-', '')}.tif",
                "dem.tif"]
    for name in expected:
        path = raw_dir / name
        check(f"原始输入 {name} 已就位",
              path.is_file() and path.stat().st_size > 0,
              f"{path}（{path.stat().st_size if path.exists() else 0} 字节）")
    return got.data


# ── 主链复算 ────────────────────────────────────────────────────


def prepare_input(pair_dir: Path, reg_root: Path) -> Path:
    """把冻结 raw 输入复制到独立回归目录（绝不写原 pair 目录）。"""
    src = pair_dir / "raw"
    dst = reg_root / "input"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        target = dst / f.name
        if not target.exists():
            shutil.copy2(f, target)
    return dst


def run_pipeline(raw_dir: Path, out_dir: Path) -> None:
    from core.pipeline import EasyLSTPipeline

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    ldate = PAIR_LANDSAT_DATE.replace("-", "")
    sdate = PAIR_SENTINEL_DATE.replace("-", "")
    pipeline = EasyLSTPipeline()
    pipeline.configure(
        landsat_path=str(raw_dir / f"landsat_lst_{ldate}.tif"),
        sentinel2_path=str(raw_dir / f"sentinel2_bands_{sdate}.tif"),
        qa_path=str(raw_dir / f"landsat_qa_pixel_{ldate}.tif"),
        scl_path=str(raw_dir / f"sentinel2_scl_{sdate}.tif"),
        dem_path=str(raw_dir / "dem.tif"),
        output_dir=str(out_dir),
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
    print(f"复算完成，耗时 {time.time() - t0:.0f}s", flush=True)


# ── 对比 ────────────────────────────────────────────────────────


def load_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def compare(out_dir: Path) -> None:
    print("\n== 数据划分（登记 §2）==")
    split = load_json(out_dir / "for_train" / "split_info.json")
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
        str(out_dir / "30m_features_step2.parquet")).num_rows
    check(f"训练抽样总行数={BASELINE['train_sample_rows']}",
          n_sampled == BASELINE["train_sample_rows"], f"实际 {n_sampled}")
    # 划分后的训练集（应等于 split counts.train）
    n_train = pq.read_metadata(
        str(out_dir / "for_train" / "train.parquet")).num_rows
    check(f"划分后 train 行数={BASELINE['split_counts']['train']}",
          n_train == BASELINE["split_counts"]["train"], f"实际 {n_train}")
    n_constraint = pq.read_metadata(
        str(out_dir / "30m_constraint_grid.parquet")).num_rows
    check(f"30m 约束格网={BASELINE['constraint_rows']}",
          n_constraint == BASELINE["constraint_rows"], f"实际 {n_constraint}")

    print("\n== TTRI（登记 §3）==")
    coef = load_json(out_dir / "for_train" / "ttri_coefficients.json")
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
    for mf in sorted((out_dir / "results" / "test").glob("*.json")):
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
    tif = out_dir / "results" / "predict_10m" / "rf_10m_lst_final.tif"
    if not tif.exists():
        candidates = sorted(out_dir.rglob("*lst_final*.tif"))
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
    closure_files = sorted(out_dir.rglob("coarse_constraint_closure.json"))
    check("闭合评估 JSON 存在", bool(closure_files), "未找到 coarse_constraint_closure.json")
    if not closure_files:
        return
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
    parser.add_argument("--download", action="store_true",
                        help="第 2 级完整复跑：先调用数据获取技能检索并下载原始输入")
    parser.add_argument("--region", default="",
                        help="研究区 GeoJSON 路径（--download 时必填）")
    parser.add_argument("--raw-dir", default="",
                        help="已有 raw 输入目录（第 1 级输入冻结复算）")
    parser.add_argument("--pair-dir", default=str(DEFAULT_PAIR),
                        help="Docker 卷内冻结 pair 目录（第 1 级默认来源）")
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT),
                        help="回归输出根目录（独立目录，不覆盖历史结果）")
    args = parser.parse_args()

    reg_root = Path(args.out)
    out_dir = reg_root / "wuhan20240722"
    print("科学回归复算：武汉 2024-07-22 配对模式")
    print(f"输出目录：{out_dir}", flush=True)

    if not args.compare_only:
        if args.download:
            if not args.region or not os.path.isfile(args.region):
                raise SystemExit("--download 需要 --region 指向真实的研究区 GeoJSON")
            raw_dir = reg_root / "input"
            acquire(args.region, raw_dir)
        elif args.raw_dir:
            raw_dir = Path(args.raw_dir)
        else:
            raw_dir = prepare_input(Path(args.pair_dir), reg_root)
        print(f"\n原始输入已就位：{raw_dir}", flush=True)
        run_pipeline(raw_dir, out_dir)
    compare(out_dir)

    print(f"\n回归对比结果：{len(FAILS)} 项不一致")
    for name, detail in FAILS:
        print(f"  - {name}: {detail}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
