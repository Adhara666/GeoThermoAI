# -*- coding: utf-8 -*-
"""验证配对选择：富候选（info/云量）+ 运行时模式自动代选（隔离，可删除）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.scheduling.acquisition import select  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="pairsel_"))
region = tmp / "region.geojson"
region.write_text(json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "测试区"},
     "geometry": {"type": "Polygon", "coordinates": [[
         [114.0, 30.4], [114.6, 30.4], [114.6, 30.8], [114.0, 30.8],
         [114.0, 30.4]]]}}]}), encoding="utf-8")

candidates = {
    "image_pairs": [
        {"landsat_date": "2025-08-18", "sentinel2_date": "2025-08-16",
         "landsat_satellite": "L9", "landsat_count": 1, "sentinel2_count": 1,
         "landsat_coverage": 99.5, "sentinel2_coverage": 98.1,
         "landsat_cloud_cover": 4.2, "sentinel2_cloud_cover": 11.3,
         "time_diff_days": 2},
        {"landsat_date": "2025-07-24", "sentinel2_date": "2025-07-24",
         "landsat_satellite": "L8", "landsat_count": 1, "sentinel2_count": 1,
         "landsat_coverage": 97.0, "sentinel2_coverage": 91.5,
         "landsat_cloud_cover": 18.6, "sentinel2_cloud_cover": 27.9,
         "time_diff_days": 0},
    ],
    "landsat_items": [
        {"id": "LC08_20250724", "properties": {"datetime": "2025-07-24T10:00:00"}, "assets": {}},
        {"id": "LC09_20250818", "properties": {"datetime": "2025-08-18T10:00:00"}, "assets": {}},
    ],
    "sentinel2_items": [
        {"id": "S2_20250724", "properties": {"datetime": "2025-07-24T10:00:00"}, "assets": {}},
        {"id": "S2_20250816", "properties": {"datetime": "2025-08-16T10:00:00"}, "assets": {}},
    ],
    "dem_items": [
        {"id": "DEM1", "properties": {"datetime": "2020-01-01T00:00:00"}, "assets": {}},
    ],
}
cpath = tmp / "candidates.json"
cpath.write_text(json.dumps(candidates), encoding="utf-8")


def make_spec(runtime_mode):
    return {
        "snapshot": {"params": {"product_mode": {"value": "pair"},
                                "exec_mode": {"value": "approval"}},
                     "slot_values": {
                         "fields": {"region": {"value": "测试区",
                                     "detail": {"path": str(region)}}},
                         "negations": []}},
        "node": {"frozen_inputs": json.dumps({"capability": "full_lst"}),
                 "params": "{}"},
        "staging_dir": str(tmp / "staging"),
        "context": {"candidates_path": str(cpath)},
        "runtime_exec_mode": runtime_mode,
    }


(tmp / "staging").mkdir(exist_ok=True)


ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


# 1) approval（无运行时模式）：返回富候选问题
r1 = select(make_spec(""))
q = r1.get("question") or {}
cands = q.get("candidates") or []
check("approval 返回配对选择问题", q.get("prompt") == "请选择本次影像配对", str(r1)[:120])
check("问题带 pair_select 类型（供完全执行自动代选识别）",
      (q.get("payload") or {}).get("kind") == "pair_select")
check("候选 2 组", len(cands) == 2, str(len(cands)))
c0 = next((c for c in cands
           if c.get("label") == "Landsat 2025-08-18 · Sentinel-2 2025-08-16"),
          cands[0] if cands else {})
info = c0.get("info") or {}
check("候选带 info（云量/覆盖/时差）",
      (info.get("landsat") or {}).get("cloud") == 4.2
      and (info.get("sentinel2") or {}).get("coverage") == 98.1
      and info.get("time_diff_days") == 2, json.dumps(info, ensure_ascii=False)[:160])
check("label 可读（Landsat … · Sentinel-2 …）",
      c0.get("label") == "Landsat 2025-08-18 · Sentinel-2 2025-08-16",
      str(c0.get("label")))

# 2) runtime_exec_mode=auto（快照是 approval）：自动选最高分，不再提问
r2 = select(make_spec("auto"))
check("完全执行（运行时）自动代选、不提问",
      "question" not in r2, str(r2)[:120])

# 3) 快照 auto 且无运行时值：保持自动（旧行为不破坏）
spec3 = make_spec("")
spec3["snapshot"]["params"]["exec_mode"] = {"value": "auto"}
r3 = select(spec3)
check("快照 auto 仍自动（旧行为保持）", "question" not in r3, str(r3)[:120])

print(f"\n配对选择验证结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
