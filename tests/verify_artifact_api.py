# -*- coding: utf-8 -*-
"""验证产物 content/meta/tile 三接口的应用层逻辑（隔离测试库，可删除）。

在容器内运行：构造最小台账（用户/任务/运行/节点/尝试/产物）+ 真实小 GeoTIFF
与 closure JSON，patch 后端使用临时库与测试用户，逐一调用后端方法验证：
  - artifact_content：JSON 内容读取正确
  - artifact_meta：bounds 输出（EPSG:4326）与样式推断
  - artifact_tile：瓦片 PNG 非空、内容为 PNG 头
  - 归属校验：他人用户不可见
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.state_kernel.store import StateStore  # noqa: E402
from core.state_kernel import schema as kschema  # noqa: E402
import core.web_app as wa  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="artverify_"))
db = tmp / "ledger.sqlite3"
store = StateStore(str(db))
store.close()

# 造真实文件：小 GeoTIFF（武汉市范围、EPSG:4326）+ closure JSON
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402

tif = tmp / "rf_10m_lst_final.tif"
with rasterio.open(
        str(tif), "w", driver="GTiff", width=64, height=64, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_bounds(114.0, 30.3, 114.6, 30.9, 64, 64)) as ds:
    ds.write(np.full((1, 64, 64), 300.0, dtype="float32"))

closure = tmp / "coarse_constraint_closure.json"
closure.write_text(json.dumps({
    "value_range": {"min_30m_K": 290.1, "max_30m_K": 310.2,
                    "min_10m_K": 291.3, "max_10m_K": 309.8,
                    "low_end_difference_K": -0.2, "high_end_difference_K": 0.3}}),
    encoding="utf-8")

# 最小台账骨架（遵循外键）：conversations → tasks → runs → nodes → attempts → artifacts
import uuid  # noqa: E402


def uid():
    return uuid.uuid4().hex


def sql(conn, q, args=()):
    conn.execute(q, args)


def seed(conn):
    now = "2026-09-11 12:00:00"
    sql(conn, "INSERT INTO conversations (id, user_id, project_id, legacy_conv_id, semantic_version, next_message_seq, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid(), "tester", "P", "cid1", 1, 1, now, now))
    conv = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
    sql(conn, "INSERT INTO tasks (id, user_id, project_id, conversation_id, capability, version, slots, ambiguity, summary_status, priority, created_at, updated_at, label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid(), "tester", "P", conv, "full_lst", 1, "{}", "{}", "running", 0, now, now, "武汉测试任务"))
    task = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
    sql(conn, "INSERT INTO runs (id, task_id, task_version, template_version, frozen_inputs, scenario_binding, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid(), task, 1, "v1", "{}", "{}", "running", now, now))
    run = conn.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    sql(conn, "INSERT INTO nodes (id, run_id, node_key, node_type, params, input_contract, output_contract, status, exec_order) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid(), run, "export", "export", "{}", "{}", "{}", "succeeded", 1))
    node = conn.execute("SELECT id FROM nodes LIMIT 1").fetchone()[0]
    sql(conn, "INSERT INTO attempts (id, node_id, attempt_no, status, task_version, started_at, finished_at) VALUES (?,?,?,?,?,?,?)",
        (uid(), node, 1, "succeeded", 1, now, now))
    attempt = conn.execute("SELECT id FROM attempts LIMIT 1").fetchone()[0]
    a1, a2 = uid(), uid()
    sql(conn, "INSERT INTO artifacts (id, attempt_id, type, path, content_hash, input_sources, availability, retention_class, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (a1, attempt, "geotiff", str(tif), "h1", "{}", "available", "keep_forever", now))
    sql(conn, "INSERT INTO artifacts (id, attempt_id, type, path, content_hash, input_sources, availability, retention_class, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (a2, attempt, "json", str(closure), "h2", "{}", "available", "keep_forever", now))
    return a1, a2


store2 = StateStore(str(db))
a_tif, a_json = store2.submit_write(lambda c: seed(c))

# 构造后端并隔离：使用临时库 + 测试用户 → 验证他人不可见
backend = wa.AppBackend()
backend._get_state_store = lambda: store2

import core.web_app  # 重新取模块级别名  # noqa: E402

def as_user(name):
    backend._uid = lambda: name  # type: ignore

ok = 0
fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}")


as_user("tester")
r = backend.artifact_content(a_json)
check("content 返回 JSON 且值正确",
      r.get("ok") and r["json"]["value_range"]["min_30m_K"] == 290.1)

m = backend.artifact_meta(a_tif)
check("meta 返回 bounds（EPSG:4326 范围正确）",
      m.get("ok") and abs(m["bounds"][0] - 114.0) < 1e-6
      and abs(m["bounds"][3] - 30.9) < 1e-6)
check("meta 样式推断为 lst_10m", m.get("style") == "lst_10m")

# z=12 下覆盖武汉（114.0-114.6E, 30.3-30.9N）的瓦片：x≈3346, y≈1682
png = backend.artifact_tile(a_tif, "lst_10m", 12, 3346, 1682)
check("tile 返回 PNG（非空且魔数正确）",
      isinstance(png, (bytes, bytearray)) and png[:8] == b"\x89PNG\r\n\x1a\n")

as_user("other")
check("他人用户不可见 content", not backend.artifact_content(a_json).get("ok"))
check("他人用户不可见 meta", not backend.artifact_meta(a_tif).get("ok"))
check("他人用户不可见 tile", backend.artifact_tile(a_tif, "", 12, 3346, 1682) is None)

store2.close()
print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
