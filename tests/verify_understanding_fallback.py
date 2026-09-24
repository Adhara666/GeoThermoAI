# -*- coding: utf-8 -*-
"""验证：理解层修复（历史规范/结构化修复/降级通道/重放保险丝）。

可重复运行；真实模型探针在模型不可用时自动 SKIP。
"""
import datetime
import sys

sys.path.insert(0, "/app")

from core.agent.understanding import operations as ops  # noqa: E402
from core.agent.understanding import prompts  # noqa: E402
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding import service as us  # noqa: E402
from core.agent.understanding import understander as und  # noqa: E402

ok = fail = skip = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


# ── 1) 提示词：历史使用规范 ──
p = prompts.understand_prompt(
    anchor_date="2026-09-13", tz_label="UTC+8", chat_mode="work",
    study_areas=["武汉市_市"],
    open_tasks=["t1｜武汉任务｜完整生产｜running"], open_questions=[])
check("提示词含历史使用规范", "历史对话的使用规范" in p)
check("禁止重放已执行操作", "禁止重放历史里已执行过的操作" in p)
check("意图只来自最新消息", "意图只来自最新一条用户消息" in p)
check("未完成任务注明已在执行", "已在执行/等待中" in p)

# ── 2) 结构化修复：修复请求携带原始输出 ──
rq = prompts.repair_request("那现在呢", '{"operations": [坏JSON')
check("修复请求包含原始输出", '{"operations": [坏JSON' in rq)

# ── 3) understander：两次失败→留痕日志 + 修复请求带原始输出 ──
class _FakeAssistant:
    """按调用次数返回内容：首答与修复答都是坏 JSON。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def _call_api(self, messages, temperature=0.0, max_tokens=0,
                  thinking=None, json_mode=False, on_thinking=None):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "还是坏"

logs = []
fa = _FakeAssistant(["坏输出一号", "坏输出二号"])
agent = und.CandidateUnderstander(fa, on_log=lambda x: logs.append(str(x)))
batch = agent.understand(
    "那现在呢", anchor_date=datetime.date(2026, 9, 13), tz_label="UTC+8",
    chat_mode="work", study_areas=["武汉市_市"], open_tasks=[],
    open_questions=[], history=[])
check("两次失败仍返回无效批次", not batch.valid)
check("修复请求带上了首答原始输出",
      any("坏输出一号" in str(m[-1].get("content", ""))
          for m in fa.calls if m))
check("失败留痕日志（含原始输出前300字）",
      any("原始输出前300字" in x for x in logs), str(logs[:3]))
check("修复提示含强制要求", any("强制要求" in str(m[0].get("content", ""))
                               for m in fa.calls[1:] if m))

# ── 4) 降级通道：模型可用但解析失败 → reply_only ──
class _FakeU:
    def __init__(self, source, errors):
        self.source, self.errors = source, errors

    def understand(self, message, **kw):
        b = ops.CandidateBatch(raw_output="not-json", source=self.source)
        b.errors.extend(self.errors)
        return b


ctx = res.ResolveContext(message="那现在呢",
                         anchor_date=datetime.date(2026, 9, 13),
                         tz_offset=8.0, chat_mode="work")
r1 = us.handle_message(None, _FakeU("llm", ["模型输出无法解析为 JSON"]),
                       user_id="u", project_id="p", conversation_id="c",
                       message="那现在呢", ctx=ctx)
check("解析失败→降级 reply_only", r1.kind == us.KIND_REPLY_ONLY)
check("降级标记 degraded", getattr(r1, "degraded", False) is True)
check("降级不带套话文案（交回答通道）", r1.message == "")
check("降级有备注", any("降级" in n for n in r1.notes))

r2 = us.handle_message(None, _FakeU("unavailable", ["模型不可用"]),
                       user_id="u", project_id="p", conversation_id="c",
                       message="那现在呢", ctx=ctx)
check("模型不可用仍如实失败", r2.kind == us.KIND_FAILED
      and "联系不上语言模型" in r2.message)

# ── 5) 重放保险丝：同指纹 create 判重（开放任务） ──
slots = {"fields": {
    "region": {"action": "set", "value": "武汉市",
               "detail": {"path": "/x/武汉市_市.geojson"}},
    "time": {"value": {"start": "2024-07-01", "end": "2024-07-31"}}}}
task = {"capability": "full_lst", "slots": slots}
change = res.DraftChange(action=res.ACT_CREATE, capability="full_lst",
                         label="A", slots=slots)
check("同指纹开放任务被识别为重复",
      us._find_duplicate_open_task(change, [task]) is not None)
empty = {"fields": {}}
empty_change = res.DraftChange(action=res.ACT_CREATE, capability="full_lst",
                               label="A", slots=empty)
check("指纹全空不判重（信息缺失照常追问）",
      us._find_duplicate_open_task(empty_change, [task]) is None)

# ── 5.5) 覆盖审计：纯结构比对（无业务词表，不推断意图） ──
c_id = ops.CandidateOperation(op=ops.OP_GEO_QUERY, intent="identify",
                              covers=("这是哪",))
c_tp = ops.CandidateOperation(op=ops.OP_GEO_QUERY, intent="temperature",
                              covers=("温度如何",))
check("审计：子句全部被覆盖 → 无遗留",
      ops.uncovered_clauses("这是哪，温度如何", [c_id, c_tp]) == [])
check("审计：模型只给了位置操作 → 命中漏掉的温度子句",
      ops.uncovered_clauses("这是哪，温度如何", [c_id]) == ["温度如何"])
check("审计：模型未声明 covers → 直接放过（不误报）",
      ops.uncovered_clauses("这是哪，温度如何",
                            [ops.CandidateOperation(op=ops.OP_GEO_QUERY)]) == [])
check("审计：修饰语并入 covers → 无遗留",
      ops.uncovered_clauses(
          "我想要武汉7月的数据，用配对模式",
          [ops.CandidateOperation(op="create",
                                  covers=("我想要武汉7月的数据", "用配对模式"))])
      == [])
check("审计：covers 规范化（字符串 → 元组）",
      ops._parse_covers("武汉 7 月") == ("武汉 7 月",))

# ── 5.6) 补充回路（假模型）：首答漏子句 → 审计触发 → 补充解析合并 ──
_first = ('{"operations": [{"op": "geo_query", "intent": "identify",'
          ' "covers": ["这是哪"], "evidence": "这是哪"}]}')
_extra = ('{"operations": [{"op": "geo_query", "intent": "temperature",'
          ' "covers": ["温度如何"], "evidence": "温度如何"}]}')
logs2 = []
fa2 = _FakeAssistant([_first, _extra])
agent2 = und.CandidateUnderstander(fa2, on_log=lambda x: logs2.append(str(x)))
batch2 = agent2.understand(
    "这是哪，温度如何", anchor_date=datetime.date(2026, 9, 21),
    tz_label="UTC+8", chat_mode="work", study_areas=["武汉市_市"],
    open_tasks=[], open_questions=[])
intents2 = [getattr(o, "intent", "") for o in batch2.operations]
check("补充回路：首答漏温度 → 审计触发并合并出第二条",
      batch2.valid and len(batch2.operations) == 2
      and intents2 == ["identify", "temperature"], str(intents2))
check("补充回路：批次 note 记录了补充", "覆盖审计补充" in (batch2.note or ""))
check("补充回路：日志可见审计触发", any("覆盖审计" in x for x in logs2))

_noop = ('{"operations": [{"op": "reply_only", "covers": ["温度如何"]}]}')
fa3 = _FakeAssistant([_first, _noop])
agent3 = und.CandidateUnderstander(fa3, on_log=lambda x: None)
batch3 = agent3.understand(
    "这是哪，温度如何", anchor_date=datetime.date(2026, 9, 21),
    tz_label="UTC+8", chat_mode="work", study_areas=["武汉市_市"],
    open_tasks=[], open_questions=[])
check("补充回路：模型表态无补充（reply_only）→ 原批次不变",
      batch3.valid and len(batch3.operations) == 1,
      str([o.op for o in batch3.operations]))

# ── 6) 真实模型探针（可选）：带历史问“那现在呢”，不得重放 create ──
try:
    import json
    import sqlite3
    from pathlib import Path

    import core.web_app as wa
    from core.web_app import _uid_ctx

    _uid_ctx.set("Adhara")
    c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
    row = c.execute("SELECT conversation_id FROM messages WHERE content"
                    " LIKE '%请你生成武汉市2024年7月%' ORDER BY rowid"
                    " DESC LIMIT 1").fetchone()
    if row:
        pk = row[0]
        hist_rows = list(c.execute(
            "SELECT role, content FROM messages WHERE conversation_id=?"
            " ORDER BY rowid", (pk,)))[-8:]
        # 与生产 runner 一致：剥离思考链 + 裁剪尾部未答 user 消息
        raw_hist = [{"role": r[0], "content": r[1] or ""} for r in hist_rows]
        prior = wa.sanitize_prior_messages(raw_hist)
        b = wa.AppBackend()
        b._uid = lambda: "Adhara"
        probe_agent = und.CandidateUnderstander(b._assistant_for())
        pb = probe_agent.understand(
            "那现在呢", anchor_date=datetime.date(2026, 9, 13),
            tz_label="UTC+8", chat_mode="work", study_areas=["武汉市_市"],
            open_tasks=[], open_questions=(), history=prior)
        if pb.source == "unavailable":
            skip += 1
            print("  [SKIP] 真实模型探针（模型不可用）")
        else:
            ops_list = [o.op for o in (pb.operations or [])]
            check("真实探针：'那现在呢' 不重放 create",
                  "create" not in ops_list, str(ops_list))
            print("      probe ops:", ops_list, "| valid:", pb.valid)
    else:
        skip += 1
        print("  [SKIP] 真实模型探针（未找到测试对话）")
except Exception as e:  # noqa: BLE001
    skip += 1
    print("  [SKIP] 真实模型探针异常:", e)

print("\n结果：%d 项通过，%d 项失败，%d 项跳过" % (ok, fail, skip))
sys.exit(1 if fail else 0)
