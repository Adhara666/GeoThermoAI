# -*- coding: utf-8 -*-
"""验证：执行模式运行时实时生效（内存态优先/多键对齐/调度读取器）。"""
import sqlite3
import sys

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402
from core.web_app import _uid_ctx  # noqa: E402
from core.scheduling.scheduler import Scheduler  # noqa: E402

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

c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
c.row_factory = sqlite3.Row
# 选一个没有开放问题的对话（避免 set_exec_mode 触发真实自动代选副作用）
conv = None
for r in c.execute("SELECT id, project_id, legacy_conv_id FROM conversations"
                   " WHERE user_id='Adhara' ORDER BY updated_at DESC"
                   " LIMIT 40"):
    n = c.execute("SELECT COUNT(*) FROM questions WHERE conversation_id=?"
                  " AND status='open'", (r["id"],)).fetchone()[0]
    if n == 0:
        conv = r
        break
check("找到无开放问题的测试对话", conv is not None)
pk, pid = conv["id"], conv["project_id"]
legacy = str(conv["legacy_conv_id"] or "")
print("  测试对话:", pk[:12], "| project:", pid, "| legacy:", legacy or "-")

before = b._live_exec_mode(pk)
print("  当前模式:", before or "(空)")

r = b.set_exec_mode(pid, pk, "auto")
check("set_exec_mode 返回 ok", bool(r.get("ok")))
check("内存态（pk 键）读到 auto", b._live_exec_mode(pk) == "auto")
if legacy:
    check("内存态（legacy 键）读到 auto", b._live_exec_mode(legacy) == "auto")
check("conversation_exec_mode(pk) == auto",
      b.conversation_exec_mode(pk) == "auto")
if legacy:
    check("conversation_exec_mode(legacy) == auto",
          b.conversation_exec_mode(legacy) == "auto")
row_task = {"user_id": "Adhara", "conversation_id": pk}
check("_conversation_exec_mode(task_row) == auto（编译入口实时读取）",
      b._conversation_exec_mode(row_task) == "auto")

s = Scheduler.__new__(Scheduler)
s._exec_mode_reader = b._live_exec_mode
check("调度运行时读取器（不依赖文件）返回 auto",
      s._runtime_exec_mode({"user_id": "Adhara", "conversation_id": pk})
      == "auto")

back = before if before else "approval"
b.set_exec_mode(pid, pk, back)
check("还原为 %s" % back, b._live_exec_mode(pk) == back)
print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
