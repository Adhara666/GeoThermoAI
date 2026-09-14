# -*- coding: utf-8 -*-
"""验证：卡片回答通道编译时显式携带对话实时模式（可删除）。"""
import sys

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402
from core.web_app import _uid_ctx  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


_uid_ctx.set("Adhara")
b = wa.AppBackend()
b._uid = lambda: "Adhara"

# 模拟卡片回答场景：不触碰真实 DB 与调度器
record = {}
b._resolve_context = lambda pid, cid, **kw: ("conv_pk_1", object())
b._scheduler = None
b.session_snapshot = lambda pid, cid: {}
b._ack_reply_text = lambda: "答案已接收"
b._append_answer_bubbles = lambda pid, cid, text, reply: []


def fake_enqueue(rows, exec_mode=None):
    record["exec_mode"] = exec_mode
    record["rows"] = list(rows or [])


b._enqueue_ready = fake_enqueue
wa.understanding.answer_question = (
    lambda *a, **k: {"summary_status": "ready", "task_id": "t1"})

# ① 对话当前模式 = auto → 编译显式携带 auto
record.clear()
b._live_exec_mode = lambda cid: "auto"
res = b.answer_question("测试阶段7", "1948eae2edb2", "qid_1", "配对模式")
check("回答接口返回 ok", bool(res.get("ok")))
check("编译被调用", bool(record.get("rows")))
check("① 编译显式携带 auto（修复点）", record.get("exec_mode") == "auto",
      repr(record.get("exec_mode")))

# ② 对话模式读不到（空）→ 传 None，保留旧兜底行为
record.clear()
b._live_exec_mode = lambda cid: ""
b.answer_question("测试阶段7", "1948eae2edb2", "qid_2", "配对模式")
check("② 读不到模式时传 None（不写入空串）", record.get("exec_mode") is None,
      repr(record.get("exec_mode")))

# ③ 印证隐患来源：applied 字典缺字段时 _conversation_exec_mode 返回空
check("③ 任务字典缺字段时兜底为空（必须显式传值的理由）",
      b._conversation_exec_mode({"task_id": "x"}) == "")

print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
