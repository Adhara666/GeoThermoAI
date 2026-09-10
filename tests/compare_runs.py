# -*- coding: utf-8 -*-
"""A/B 对比两次主链复算的科学结果（升级验收：证明编排层改动没碰算法）。

用法：
    python3 tests/compare_runs.py --a <旧代码输出目录> --b <新代码输出目录>

两个目录必须来自**同一份原始输入**。逐项比较划分成员数、TTRI 系数、
RF 指标、TCR/最终 LST 统计与粗尺度闭合；任何一项不同都视为不通过。

这是回归用例 §7 之外的补充证据：当远程数据源发生漂移、无法与历史登记值
逐位对齐时，「同输入 / 旧代码 vs 新代码」才是判定「算法有没有被改坏」的
直接手段（原文档 §15.5「与原串行路径对照」的同一思路）。
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""),
          flush=True)
    if not cond:
        FAILS.append((name, detail))


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def raster_stats(path: Path):
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(str(path))
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    geo = ds.GetGeoTransform()
    shape = (ds.RasterYSize, ds.RasterXSize)
    proj = ds.GetProjection()
    mask = np.isfinite(arr)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    vals = arr[mask]
    stats = {
        "shape": shape, "geotransform": tuple(round(g, 9) for g in geo),
        "proj_hash": hashlib.sha256((proj or "").encode()).hexdigest()[:16],
        "valid": int(mask.sum()),
        "min": float(vals.min()), "max": float(vals.max()),
        "mean": float(vals.mean()), "std": float(vals.std()),
    }
    ds = None
    return stats


def first(root: Path, pattern: str):
    hits = sorted(root.rglob(pattern))
    return hits[0] if hits else None


def compare(a: Path, b: Path) -> None:
    import pyarrow.parquet as pq

    print("== 数据划分 ==")
    sa = load_json(a / "for_train" / "split_info.json")
    sb = load_json(b / "for_train" / "split_info.json")
    check("划分计数完全一致", sa["counts"] == sb["counts"],
          f"A={sa['counts']} B={sb['counts']}")
    for key in ("method", "seed", "block_size_px", "guard_buffer_m"):
        check(f"划分参数 {key} 一致", sa.get(key) == sb.get(key),
              f"A={sa.get(key)} B={sb.get(key)}")

    print("\n== 中间表行数 ==")
    for rel in ("30m_features_step2.parquet", "30m_constraint_grid.parquet",
                "for_train/train.parquet", "for_train/validate.parquet",
                "for_train/test.parquet"):
        pa, pb = a / rel, b / rel
        if not (pa.exists() and pb.exists()):
            check(f"{rel} 两侧都存在", False, f"A={pa.exists()} B={pb.exists()}")
            continue
        na = pq.read_metadata(str(pa)).num_rows
        nb = pq.read_metadata(str(pb)).num_rows
        check(f"{rel} 行数一致（{na}）", na == nb, f"A={na} B={nb}")

    print("\n== 训练集内容指纹（逐字节） ==")
    for rel in ("for_train/train.parquet", "for_train/test.parquet"):
        pa, pb = a / rel, b / rel
        if pa.exists() and pb.exists():
            ha, hb = sha256(pa), sha256(pb)
            check(f"{rel} 内容完全相同", ha == hb, f"A={ha[:16]} B={hb[:16]}")

    print("\n== TTRI ==")
    ca = load_json(a / "for_train" / "ttri_coefficients.json")
    cb = load_json(b / "for_train" / "ttri_coefficients.json")
    check("TTRI 系数逐位一致",
          (ca.get("coefficients") or ca.get("coef"))
          == (cb.get("coefficients") or cb.get("coef")),
          f"A={ca.get('coefficients')} B={cb.get('coefficients')}")
    check("TTRI 截距逐位一致", ca.get("intercept") == cb.get("intercept"),
          f"A={ca.get('intercept')} B={cb.get('intercept')}")
    check("TTRI 拟合 R² 逐位一致",
          ca.get("fit_r2", ca.get("r2")) == cb.get("fit_r2", cb.get("r2")),
          f"A={ca.get('fit_r2')} B={cb.get('fit_r2')}")

    print("\n== RF 指标 ==")

    def rf_metrics(root: Path):
        for mf in sorted((root / "results" / "test").glob("*.json")):
            metrics = (load_json(mf).get("metrics") or {})
            if metrics:
                return {k.lower(): v for k, v in metrics.items()}
        return {}

    ma, mb = rf_metrics(a), rf_metrics(b)
    for key in ("r2", "rmse", "mae", "mb"):
        check(f"RF test {key.upper()} 逐位一致", ma.get(key) == mb.get(key),
              f"A={ma.get(key)} B={mb.get(key)}")

    print("\n== 最终 LST 栅格 ==")
    ta, tb = first(a, "*lst_final*.tif"), first(b, "*lst_final*.tif")
    check("两侧都有导出产物", bool(ta and tb), f"A={ta} B={tb}")
    if ta and tb:
        ra, rb = raster_stats(ta), raster_stats(tb)
        for key in ("shape", "geotransform", "proj_hash", "valid",
                    "min", "max", "mean", "std"):
            check(f"LST {key} 一致", ra[key] == rb[key],
                  f"A={ra[key]} B={rb[key]}")

    print("\n== 粗尺度闭合 ==")
    fa, fb = first(a, "coarse_constraint_closure.json"), \
        first(b, "coarse_constraint_closure.json")
    check("两侧都有闭合评价", bool(fa and fb), f"A={fa} B={fb}")
    if fa and fb:
        ja, jb = load_json(fa), load_json(fb)
        check("闭合结果逐字段一致", ja == jb,
              f"A={json.dumps(ja, ensure_ascii=False)[:200]} "
              f"B={json.dumps(jb, ensure_ascii=False)[:200]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True, help="对照组输出目录（旧代码）")
    parser.add_argument("--b", required=True, help="实验组输出目录（新代码）")
    args = parser.parse_args()
    a, b = Path(args.a), Path(args.b)
    print(f"A（旧代码）：{a}\nB（新代码）：{b}\n")
    compare(a, b)
    print(f"\nA/B 对比结果：{len(FAILS)} 项不一致")
    for name, detail in FAILS:
        print(f"  - {name}: {detail}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
