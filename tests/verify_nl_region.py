# -*- coding: utf-8 -*-
"""验证：自然语言研究区（边界库检索 + 上传优先，临时）。"""
import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.agent.understanding import operations as ops  # noqa: E402
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding.slotbook import SlotBook  # noqa: E402


def ctx_with(areas):
    return res.ResolveContext(
        message="我想要洪山区2024年7月的10m地表温度数据",
        anchor_date=datetime.date(2026, 9, 14),
        tz_offset=8.0, chat_mode="work",
        study_area_paths=[Path(p) for p in areas],
        active_study_area_paths=[Path(p) for p in areas])


# 1) 无上传：洪山区 → 库检索
book = SlotBook().set(ops.F_REGION, "洪山区", "user")
b2, q, note = res._resolve_region(book, ctx_with([]))
detail = (b2.get(ops.F_REGION).get("detail") or {})
print("[1] 洪山区(无上传)")
print("    value:", b2.value(ops.F_REGION))
print("    path :", detail.get("path"))
print("    hash :", str(detail.get("content_hash"))[:12])
print("    note :", note)
print("    q    :", q)

# 2) 无上传：武汉（去后缀匹配）
book = SlotBook().set(ops.F_REGION, "武汉", "user")
b2, q, note = res._resolve_region(book, ctx_with([]))
detail = (b2.get(ops.F_REGION).get("detail") or {})
print("[2] 武汉(无上传)")
print("    value:", b2.value(ops.F_REGION))
print("    path :", detail.get("path"))

# 3) 上传优先：造一个用户自己的「洪山区」geojson
tmp = Path(tempfile.mkdtemp(prefix="gtai_area_"))
own = tmp / "洪山区.geojson"
own.write_text('{"type":"FeatureCollection","features":[{"type":"Feature",'
               '"properties":{"name":"洪山区"},"geometry":{"type":"Polygon",'
               '"coordinates":[[[114.3,30.4],[114.5,30.4],[114.5,30.6],'
               '[114.3,30.6],[114.3,30.4]]]}}]}', encoding="utf-8")
book = SlotBook().set(ops.F_REGION, "洪山区", "user")
b2, q, note = res._resolve_region(book, ctx_with([str(own)]))
detail = (b2.get(ops.F_REGION).get("detail") or {})
print("[3] 洪山区(有上传的同名文件)")
print("    path :", detail.get("path"))
print("    = 上传文件?", detail.get("path") == str(own.resolve()))

# 4) 库外地名：库无 + 无上传 → 原有追问路径
book = SlotBook().set(ops.F_REGION, "火星基地", "user")
b2, q, note = res._resolve_region(book, ctx_with([]))
print("[4] 火星基地(库外)")
print("    value:", b2.value(ops.F_REGION))
print("    q    :", getattr(q, "prompt", q))
