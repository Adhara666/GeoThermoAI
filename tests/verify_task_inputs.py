# -*- coding: utf-8 -*-
"""最终验证：task_inputs + task_input_tile 全链路（Adhara 真实任务，可删除）。"""
import math
import sqlite3
import sys

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402

c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
row = c.execute(
    "SELECT t.id, t.label FROM tasks t WHERE t.user_id='Adhara'"
    " AND t.label LIKE '武汉市%' ORDER BY t.created_at DESC LIMIT 1").fetchone()
print("任务:", row[1][:30])

backend = wa.AppBackend()
backend._uid = lambda: "Adhara"

r = backend.task_inputs(row[0])
print("inputs:", r.get("ok"), "| 层数:", len(r.get("inputs") or []))
for it in r.get("inputs") or []:
    print("  ", it["label"], "|", it["key"], "| style:", it["style_id"])

first = next(it for it in r["inputs"] if it["key"] == "sentinel2_path")
b = first["bounds"]
lon, lat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
z = 12
n = 2 ** z
x = int((lon + 180) / 360 * n)
lat_r = math.radians(lat)
y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
for key in ("sentinel2_path", "landsat_path", "dem_path"):
    png = backend.task_input_tile(row[0], key, z, x, y)
    ok = isinstance(png, (bytes, bytearray)) and png[:8] == b"\x89PNG\r\n\x1a\n"
    print(f"  {key} 瓦片 PNG: {ok} | {len(png) if png else 0} 字节")

bad = backend.task_input_tile(row[0], "hack", z, x, y)
backend._uid = lambda: "stage6_e2e"
r2 = backend.task_inputs(row[0])
print("非法 key → None:", bad is None, "| 他人被拒:", r2.get("ok") is False)
