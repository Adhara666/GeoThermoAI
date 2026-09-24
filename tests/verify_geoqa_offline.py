# -*- coding: utf-8 -*-
"""验证 geoqa 引擎（临时）：解析/统计/覆盖守卫/海外中文。"""
import sys

sys.path.insert(0, "/app")

from core.geoqa import service as gqs  # noqa: E402
from core.boundaries import get_library  # noqa: E402

lib = get_library()
print("库条目:", len(lib.load()["entries"]))

print("\n== 1) 目标解析：洪山区 ==")
r = gqs.resolve_target(target_text="洪山区", library=lib)
print("status:", r["status"], "| kind:", r.get("kind"), "| label:", r.get("label"))

print("\n== 2) 面统计：洪山区平均温度 ==")
out = gqs.answer_geo_query(target_text="洪山区", library=lib)
print("status:", out["status"])
print("reply:", out.get("reply", "")[:300])
if out.get("stats"):
    s = out["stats"]
    print("stats: mean=%.2fK min=%.2f max=%.2f valid=%d count=%d coverage=%s"
          % (s["mean_k"], s["min_k"], s["max_k"], s["valid"], s["count"],
             s["coverage"]))

print("\n== 3) 点缓冲：武汉中心 300m ==")
out = gqs.answer_geo_query(lon=114.3055, lat=30.5928, radius_m=300, library=lib)
print("status:", out["status"])
print("reply:", out.get("reply", "")[:300])

print("\n== 4) 覆盖守卫：北京点 ==")
out = gqs.answer_geo_query(lon=116.4074, lat=39.9042, radius_m=500,
                           library=lib)
print("status:", out["status"])
print("reply:", out.get("reply", "")[:200])

print("\n== 5) 海外中文：纽约百老汇 ==")
out = gqs.answer_geo_query(target_text="纽约百老汇", library=lib)
print("status:", out["status"])
print("reply:", out.get("reply", "")[:220])
print("target:", {k: out.get("target", {}).get(k) for k in ("kind", "label")})
