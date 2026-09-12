# -*- coding: utf-8 -*-
"""P1-P3 研究区多选（启用集）验证脚本。

覆盖：激活集存储/迁移/别名、理解层无名解析、旧链路解析收口、
自动启用、完成报告研究区名、删除保护、轮廓接口。
使用真实用户目录 + 临时测试文件，结束后恢复原状态。
"""
import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402
from core.web_app import _uid_ctx  # noqa: E402
from core.agent.understanding import operations as ops  # noqa: E402
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding import service as svc  # noqa: E402
from core.agent.understanding.slotbook import SlotBook  # noqa: E402
from core.agent.geo_thermo_agent import GeoThermoAgent  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f"  {detail}" if detail else ""))


_uid_ctx.set("Adhara")
b = wa.AppBackend()
b._uid = lambda: "Adhara"
d = b._study_dir()
d.mkdir(parents=True, exist_ok=True)

# ── 保存原始状态（结束后恢复） ──
orig_active = b.get_active_study_areas()
act_p = d / ".active.json"
cur_p = d / ".current.txt"
orig_act_raw = act_p.read_text(encoding="utf-8") if act_p.exists() else None
orig_cur_raw = cur_p.read_text(encoding="utf-8") if cur_p.exists() else None

Q = ops.F_REGION
GJ = {"type": "FeatureCollection", "features": [{
    "type": "Feature", "properties": {"name": "测试"},
    "geometry": {"type": "Polygon", "coordinates": [
        [[114.0, 30.0], [114.2, 30.0], [114.2, 30.2],
         [114.0, 30.2], [114.0, 30.0]]]}}]}
A = d / "_pytest_区域A.geojson"
BF = d / "_pytest_区域B.geojson"
C = d / "_pytest_区域A二.geojson"
for f in (A, BF, C):
    f.write_text(json.dumps(GJ, ensure_ascii=False), encoding="utf-8")

TASK_ID = "pytest_task_" + str(int(time.time()))
RUN_ID = "pytest_run_" + str(int(time.time()))


def _ctx(study, active, msg="帮我做地表温度"):
    return svc.build_resolve_context(
        message=msg,
        received_at=datetime.datetime.now(datetime.timezone.utc),
        tz_offset=8, chat_mode="work",
        study_area_paths=list(study),
        active_study_area_paths=list(active),
        ledger={})


try:
    print("== 1) 激活集存储 ==")
    r = b.set_active_study_areas([A.name, BF.name])
    check("设置启用集(2)", r.get("ok") and len(r.get("active") or []) == 2)
    check("读取启用集（保持顺序）",
          b.get_active_study_areas() == [A.name, BF.name])
    check("多选时 current 别名=空串", b.get_current_study_area() == "")
    r2 = b.set_active_study_areas(["不存在.geojson"])
    check("无效文件整体拒绝", not r2.get("ok"))
    b.set_active_study_areas([A.name])
    check("单选时 current 别名=A", b.get_current_study_area() == A.name)
    check(".current.txt 兼容标记写入",
          cur_p.exists() and cur_p.read_text(encoding="utf-8").strip() == A.name)
    act_p.unlink()
    check("迁移：仅 .current.txt → 单元素启用集",
          b.get_active_study_areas() == [A.name])
    check(".current.txt 缺失时不误报", True)
    b.set_active_study_areas([A.name, BF.name])

    print("== 2) 理解层：无名解析按启用集 ==")
    book = SlotBook({"fields": {}})
    _, q, _ = res._resolve_region(book, _ctx([A, BF, C], [A, BF]), "zh")
    check("多启用+无名→追问", q is not None and "多个研究区" in (q.prompt or ""))
    check("追问候选=启用集（顺序一致）",
          [x["label"] for x in (q.candidates or [])] == [A.stem, BF.stem])
    book1 = SlotBook({"fields": {}})
    book1, q1, note1 = res._resolve_region(book1, _ctx([A, BF, C], [A]), "zh")
    check("唯一启用+无名→直接采用",
          q1 is None and book1.value(Q) == A.stem and A.stem in (note1 or ""))
    book2_in = SlotBook({"fields": {Q: {"value": BF.stem, "source": "user"}}})
    book2, q2, _ = res._resolve_region(book2_in, _ctx([A, BF, C], [A]), "zh")
    detail = (book2.get(Q).get("detail") or {})
    check("点名（不在启用集）→ 直接绑定",
          q2 is None and str(detail.get("path") or "").endswith(BF.name))
    book3 = SlotBook({"fields": {Q: {"value": "区域A", "source": "user"}}})
    _, q3, _ = res._resolve_region(book3, _ctx([A, BF, C], [C]), "zh")
    check("模糊多候选时启用项置顶",
          q3 is not None and (q3.candidates or [{}])[0]["label"] == C.stem,
          f"first={(q3.candidates or [{}])[0].get('label') if q3 else None}")
    book4_in = SlotBook({"fields": {}})
    book4, q4, note4 = res._resolve_region(book4_in, _ctx([A], []), "zh")
    check("无启用+全库唯一→采用（原行为）",
          q4 is None and book4.value(Q) == A.stem)

    print("== 3) 旧链路解析收口（不再取最新） ==")
    class _Stub:
        _active_study_area_files = staticmethod(
            GeoThermoAgent._active_study_area_files)
    finder = GeoThermoAgent._find_study_area_file
    b.set_active_study_areas([A.name, BF.name])
    check("多启用+无名→None（不再猜测）",
          finder(_Stub(), str(d), preferred_name="") is None)
    b.set_active_study_areas([A.name])
    check("唯一启用→返回该文件",
          str(finder(_Stub(), str(d), preferred_name="")).endswith(A.name))
    check("点名优先于启用集",
          str(finder(_Stub(), str(d), preferred_name="区域B")).endswith(BF.name))
    b.set_active_study_areas([])
    check("多文件+无启用+无名→None",
          finder(_Stub(), str(d), preferred_name="") is None)
    tmp = Path("/tmp/_pytest_sa")
    tmp.mkdir(exist_ok=True)
    (tmp / "single.geojson").write_text(json.dumps(GJ), encoding="utf-8")
    check("单文件+无启用→返回它",
          str(finder(_Stub(), str(tmp), preferred_name="")).endswith("single.geojson"))
    (tmp / "single.geojson").unlink()
    tmp.rmdir()

    print("== 4) 指定未启用 → 自动启用（P2） ==")
    b.set_active_study_areas([A.name])
    fake = type("R", (), {"tasks": [{"slots": {"fields": {
        Q: {"detail": {"path": str(BF)}}}}}]})()
    b._auto_activate_bound_regions(fake)
    check("自动并入启用集",
          set(b.get_active_study_areas()) == {A.name, BF.name})

    print("== 5) 完成报告研究区名 ==")
    c = sqlite3.connect(str(b._get_state_store().db_path))
    conv_pk = c.execute("SELECT id FROM conversations WHERE user_id='Adhara'"
                        " LIMIT 1").fetchone()
    if conv_pk:
        c.execute(
            "INSERT INTO tasks (id,user_id,project_id,conversation_id,"
            " capability,slots,summary_status,current_run_id,created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (TASK_ID, "Adhara", "pytest", conv_pk[0], "full_lst",
             json.dumps({"fields": {Q: {
                 "value": BF.stem,
                 "detail": {"display": BF.stem, "path": str(BF)}}}},
                 ensure_ascii=False),
             "running", RUN_ID, "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"))
        c.commit()
        check("_run_region_display 解析显示名",
              b._run_region_display(RUN_ID) == BF.stem)
        print("== 6) 删除保护 ==")
        msg = b.delete_study_area(BF.name)
        check("被未结束任务引用→拒绝删除",
              msg.startswith("「") and BF.exists())
        c.execute("DELETE FROM tasks WHERE id=?", (TASK_ID,))
        c.commit()
    else:
        print("  [SKIP] 无对话行，跳过报告/删除保护测试")

    print("== 7) 轮廓接口 ==")
    outs = b.study_area_outlines([A.name, BF.name])
    check("轮廓返回 GeoJSON 内容",
          len(outs) == 2 and outs[0]["geojson"].get("type") == "FeatureCollection")

finally:
    print("== 清理 ==")
    try:
        c2 = sqlite3.connect(str(b._get_state_store().db_path))
        c2.execute("DELETE FROM tasks WHERE id LIKE 'pytest_task_%'")
        c2.commit()
    except Exception as e:
        print("  清理任务行失败:", e)
    for f in (A, BF, C):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass
    if orig_act_raw is not None:
        act_p.write_text(orig_act_raw, encoding="utf-8")
    else:
        try:
            act_p.unlink(missing_ok=True)
        except OSError:
            pass
    if orig_cur_raw is not None:
        cur_p.write_text(orig_cur_raw, encoding="utf-8")
    else:
        try:
            cur_p.unlink(missing_ok=True)
        except OSError:
            pass
    print("  已恢复原启用集:", orig_active)

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
for name in FAIL:
    print("  - " + name)
sys.exit(1 if FAIL else 0)
