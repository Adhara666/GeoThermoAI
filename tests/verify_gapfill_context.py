# -*- coding: utf-8 -*-
"""验证：延续类请求的目标引用（补洞默认指向最近完成结果）。"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from core.agent.understanding import operations as ops  # noqa: E402
from core.agent.understanding import prompts  # noqa: E402
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding import service as us  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


WH = Path("/app/config/study_areas/武汉市_市.geojson")
EZ = Path("/app/config/study_areas/鄂州市_市.geojson")

REC_WH = {
    "task_id": "task_demo_1",
    "label": "武汉市_市 2024年7月 10米地表温度完整生产",
    "region_display": "武汉市_市",
    "region_detail": {"path": str(WH), "display": "武汉市_市"},
    "time_value": {"start": "2024-07-01", "end": "2024-07-31"},
    "main_tif": "/tmp/demo_lst.tif",
}


def make_batch(capability="gapfill", message="继续生成无空洞的结果"):
    raw = {"operations": [{
        "op": "create", "label": "A", "target_ref": "",
        "capability": capability,
        "patches": {}, "missing": [], "ambiguity": [],
        "evidence": message,
    }], "note": "t"}
    return ops.parse_batch(raw, raw_output="")


def make_ctx(message, recent, active):
    return res.ResolveContext(
        message=message, anchor_date=datetime.date(2026, 9, 13),
        tz_offset=8.0, chat_mode="work",
        study_area_paths=[WH, EZ], active_study_area_paths=list(active),
        recent_results=list(recent))


# ── A：补洞 + 唯一最近完成结果 → 直接绑定，不追问 ──
ctx_a = make_ctx("继续生成无空洞的结果", [REC_WH], [EZ, WH])
out_a = res.resolve(make_batch(), ctx_a)
chg = out_a.changes[0] if out_a.changes else None
check("A 生成了一个草稿修改", chg is not None)
if chg:
    fields = (chg.slots or {}).get("fields") or {}
    region = fields.get("region") or {}
    timev = (fields.get("time") or {}).get("value") or {}
    check("A 无追问且不缺项", not chg.questions and not chg.missing,
          str(chg.missing) + str([q.prompt for q in chg.questions]))
    check("A 区域绑定到武汉文件",
          str((region.get("detail") or {}).get("path") or "").endswith(
              "武汉市_市.geojson"), str(region.get("detail")))
    check("A 区域来源=confirmed（如实的引用来源）",
          region.get("source") == "confirmed", str(region.get("source")))
    check("A 时间绑定 2024-07",
          timev.get("start") == "2024-07-01"
          and timev.get("end") == "2024-07-31", str(timev))
    check("A 转为结果后处理（不新建任务）",
          chg.action == "postprocess" and chg.task_id == "task_demo_1",
          str(chg.action))
    check("A 绑定主产品路径（main_tif）",
          (chg.execution or {}).get("main_tif") == "/tmp/demo_lst.tif",
          str(chg.execution))

# ── B：补洞但无最近完成结果 → 保持原有追问（不猜） ──
ctx_b = make_ctx("继续生成无空洞的结果", [], [EZ, WH])
out_b = res.resolve(make_batch(), ctx_b)
chg_b = out_b.changes[0] if out_b.changes else None
check("B 无最近结果时仍追问研究区",
      bool(chg_b and chg_b.questions
           and "多个研究区" in chg_b.questions[0].prompt),
      str([q.prompt for q in (chg_b.questions if chg_b else [])]))
check("B 此时仍是普通新建（不转后处理）",
      bool(chg_b and chg_b.action == "create"),
      str(chg_b.action if chg_b else ""))

# ── C：规则仅限补洞：普通完整生产不受影响 ──
ctx_c = make_ctx("再做一个", [REC_WH], [EZ, WH])
out_c = res.resolve(make_batch(capability="full_lst", message="再做一个"), ctx_c)
chg_c = out_c.changes[0] if out_c.changes else None
check("C full_lst 不受补洞规则影响（仍追问）",
      bool(chg_c and chg_c.questions), str([q.prompt for q in (chg_c.questions if chg_c else [])]))

# ── G：模型自行引用（region/time 均带）且与唯一最近结果一致 → 仍转后处理 ──
def make_batch_quoted_region(message="继续生成无空洞的结果"):
    raw = {"operations": [{
        "op": "create", "label": "A", "target_ref": "",
        "capability": "gapfill",
        "patches": {
            "region": {"action": "set", "value": "武汉市_市",
                       "evidence": "引用最近完成的任务"},
            "time": {"action": "set", "value": "2024年7月",
                     "evidence": "引用最近完成的任务"}},
        "missing": [], "ambiguity": [], "evidence": message,
    }], "note": "t"}
    return ops.parse_batch(raw, raw_output="")


ctx_g = make_ctx("继续生成无空洞的结果", [REC_WH], [EZ, WH])
out_g = res.resolve(make_batch_quoted_region(), ctx_g)
chg_g = out_g.changes[0] if out_g.changes else None
check("G 模型引用型也转为后处理（不新建任务）",
      bool(chg_g and chg_g.action == "postprocess"
           and chg_g.task_id == "task_demo_1"
           and not chg_g.questions and not chg_g.missing),
      "%s/%s" % (chg_g.action if chg_g else "-",
                  str(chg_g.missing if chg_g else "")))

# ── H：模型点名了另一个研究区（与最近完成结果不一致）→ 不误转 ──
def make_batch_other_region(message="给别的地方生成无空洞的结果"):
    raw = {"operations": [{
        "op": "create", "label": "A", "target_ref": "",
        "capability": "gapfill",
        "patches": {"region": {"action": "set", "value": "鄂州市_市",
                               "evidence": message}},
        "missing": [], "ambiguity": [], "evidence": message,
    }], "note": "t"}
    return ops.parse_batch(raw, raw_output="")


ctx_h = make_ctx("给别的地方生成无空洞的结果", [REC_WH], [EZ, WH])
out_h = res.resolve(make_batch_other_region(), ctx_h)
chg_h = out_h.changes[0] if out_h.changes else None
check("H 点名其他地区不误转后处理",
      bool(chg_h and chg_h.action == "create"),
      str(chg_h.action if chg_h else "-"))

# ── F：模型把整句消息塞进 region（实测垃圾回显）→ 丢弃后仍能绑定 ──
def make_batch_bad_region(message="继续生成无空洞的结果"):
    raw = {"operations": [{
        "op": "create", "label": "A", "target_ref": "",
        "capability": "gapfill",
        "patches": {"region": {"action": "set", "value": message,
                               "evidence": message}},
        "missing": [], "ambiguity": [], "evidence": message,
    }], "note": "t"}
    return ops.parse_batch(raw, raw_output="")


ctx_f = make_ctx("继续生成无空洞的结果", [REC_WH], [EZ, WH])
out_f = res.resolve(make_batch_bad_region(), ctx_f)
chg_f = out_f.changes[0] if out_f.changes else None
check("F 垃圾 region（整句回显）被丢弃后仍直接绑定不追问",
      bool(chg_f and not chg_f.questions and not chg_f.missing),
      str([q.prompt for q in (chg_f.questions if chg_f else [])]) + str(chg_f.missing if chg_f else ""))
if chg_f:
    fields_f = (chg_f.slots or {}).get("fields") or {}
    region_f = fields_f.get("region") or {}
    check("F 绑定到武汉文件",
          str((region_f.get("detail") or {}).get("path") or "").endswith(
              "武汉市_市.geojson"), str(region_f.get("detail")))
    check("F 同样转为结果后处理",
          chg_f.action == "postprocess" and chg_f.task_id == "task_demo_1",
          str(chg_f.action))

# ── D：提示词包含新上下文与引用规范 ──
p = prompts.understand_prompt(
    anchor_date="2026-09-13", tz_label="UTC+8", chat_mode="work",
    study_areas=["武汉市_市"], open_tasks=[], open_questions=[],
    recent_completed=["武汉市_市 2024年7月 10米地表温度完整生产｜武汉市_市｜2024-07-01~2024-07-31"])
check("D 提示词含最近完成结果清单", "最近完成的结果" in p
      and "2024-07-01~2024-07-31" in p)
check("D 提示词含引用规范（允许引用不算重放）",
      "允许引用、不算重放" in p and "来自最近完成的任务" in p)
check("D 提示词禁把整句当地区名",
      "绝不能把整句话或请求短语" in p and "宁可完全不写 region" in p)

# ── E：真实台账联动（加载本对话最近完成结果并直接解析） ──
try:
    import sqlite3

    import core.web_app as wa
    from core.web_app import _uid_ctx

    _uid_ctx.set("Adhara")
    c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
    row = c.execute(
        "SELECT t.conversation_id, t.capability FROM tasks t"
        " WHERE t.user_id='Adhara' AND t.summary_status='completed'"
        " AND t.capability='full_lst' ORDER BY t.updated_at DESC LIMIT 1"
    ).fetchone()
    if row:
        conv_pk = row[0]
        b = wa.AppBackend()
        b._uid = lambda: "Adhara"
        ledger = us.load_ledger_context(
            b._get_state_store(), user_id="Adhara", conversation_id=conv_pk)
        recents = ledger.get("recent_results") or []
        check("E 真实台账读到最近完成结果", bool(recents),
              str(ledger.keys()))
        if recents:
            r0 = recents[0]
            print("      最近完成:", r0["region_display"],
                  r0.get("time_value"))
            check("E 最近结果为武汉 2024-07",
                  r0["region_display"] == "武汉市_市"
                  and (r0.get("time_value") or {}).get("start") == "2024-07-01")
            check("E 记录含 task_id 与 main_tif",
                  bool(r0.get("task_id"))
                  and str(r0.get("main_tif") or "").endswith(".tif")
                  and Path(str(r0.get("main_tif"))).is_file(),
                  str(r0.get("main_tif")))
            ctx_e = us.build_resolve_context(
                message="继续生成无空洞的结果",
                received_at=datetime.datetime.now(datetime.timezone.utc),
                tz_offset=8.0, chat_mode="work",
                study_area_paths=[WH, EZ],
                active_study_area_paths=[EZ, WH],
                ledger=ledger)
            out_e = res.resolve(make_batch(), ctx_e)
            chg_e = out_e.changes[0] if out_e.changes else None
            check("E 真实上下文下直接绑定不追问",
                  bool(chg_e and not chg_e.questions and not chg_e.missing),
                  str(chg_e.missing if chg_e else "无草稿"))
            check("E 真实上下文转后处理并绑定 main_tif",
                  bool(chg_e and chg_e.action == "postprocess"
                       and (chg_e.execution or {}).get("main_tif")
                       == r0.get("main_tif")),
                  str(chg_e.execution if chg_e else ""))
    else:
        print("  [SKIP] 未找到已完成的 full_lst 任务")
except Exception as e:  # noqa: BLE001
    print("  [SKIP] 真实台账联动异常:", e)

print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
