# -*- coding: utf-8 -*-
"""验证：暂停/恢复/取消（迁移 + 理解层映射 + 提交事务 + 调度门控）。"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, "/app")

from core.agent.understanding import operations as ops  # noqa: E402
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding import service as uservice  # noqa: E402
from core.planning.compiler import compile_task_tx  # noqa: E402
from core.scheduling.scheduler import Scheduler  # noqa: E402
from core.state_kernel import tasks as t_store  # noqa: E402
from core.state_kernel.store import StateStore  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


tmp = Path(tempfile.mkdtemp(prefix="gtai_pause_"))
store = StateStore(tmp / "t.sqlite3")

# ── A) 迁移：v7 + 新列 ──────────────────────────────────────────
ver = store.read(lambda c: int(c.execute("PRAGMA user_version").fetchone()[0]))
check("schema 迁移到 v7", ver == 7, str(ver))
cols = store.read(lambda c: [r[1] for r in c.execute("PRAGMA table_info(tasks)")])
check("tasks.pause_requested 列存在", "pause_requested" in cols)

# ── 种子：对话 + 任务 ───────────────────────────────────────────
WH = "/app/config/study_areas/武汉市_市.geojson"
SLOTS = {"fields": {
    "region": {"value": "武汉市_市", "source": "user",
               "detail": {"path": WH, "display": "武汉市_市"}},
    "time": {"value": {"start": "2024-07-01", "end": "2024-07-31"},
             "source": "user"},
    "product_mode": {"value": "pair", "source": "user"}},
    "negations": []}

conv = store.submit_write(lambda c: __import__(
    "core.state_kernel.intake", fromlist=["_ensure_conversation"]
)._ensure_conversation(c, "u", "p", "c"))


def _mk_task():
    return store.submit_write(lambda c: t_store.create_task(
        c, user_id="u", project_id="p", conversation_id=conv,
        capability="full_lst", slots=SLOTS, label="武汉任务",
        summary_status="ready"))


# ── B) 理解层：『暂停』映射 ACT_PAUSE 且唯一绑定 ─────────────────
tid = _mk_task()
task = store.read(lambda c: t_store.load_task(c, tid))
ctx = res.ResolveContext(message="暂停这个任务", anchor_date=date(2026, 9, 14),
                         open_tasks=[task])
change = res._resolve_task_command(
    ops.CandidateOperation(op=ops.OP_PAUSE, target_ref=""), ctx)
check("『暂停』解析为 ACT_PAUSE 且绑定唯一任务",
      change is not None and change.action == res.ACT_PAUSE
      and change.task_id == tid,
      str(change and (change.action, change.task_id)))
check("_status_for(ACT_PAUSE) == paused",
      uservice._status_for(change) == "paused", "")

# ── C) 提交事务：暂停 ───────────────────────────────────────────
out = uservice._commit_change(store, change, user_id="u", project_id="p",
                              conversation_id=conv, command_id="c1",
                              message_id="m1")
t2 = store.read(lambda c: t_store.load_task(c, tid))
check("暂停提交：pause_requested=1 且状态 paused",
      int(t2.get("pause_requested") or 0) == 1
      and t2["summary_status"] == "paused",
      f"{t2.get('pause_requested')} / {t2['summary_status']}")
check("暂停 out.summary_status=paused", out.get("summary_status") == "paused")

# ── D) 恢复（continue）：清标记 + resumed ──────────────────────
change2 = res.DraftChange(action=res.ACT_CONTINUE, capability="full_lst",
                          label="武汉任务", slots=t2["slots"],
                          task_id=tid, expected_version=int(t2["version"]))
out2 = uservice._commit_change(store, change2, user_id="u", project_id="p",
                               conversation_id=conv, command_id="c2",
                               message_id="m2")
t3 = store.read(lambda c: t_store.load_task(c, tid))
check("恢复后：pause_requested=0 且 out.resumed=True",
      int(t3.get("pause_requested") or 0) == 0 and out2.get("resumed") is True,
      f"{t3.get('pause_requested')} / {out2.get('resumed')}")
check("恢复后状态回 ready", t3["summary_status"] == "ready",
      str(t3["summary_status"]))

# ── D2) 恢复「等待回答」中的暂停任务：回到等待回答而非误置 ready ──
store.submit_write(lambda c: c.execute(
    "UPDATE tasks SET summary_status='awaiting_info', pause_requested=1 WHERE id=?",
    (tid,)))
t_aw = store.read(lambda c: t_store.load_task(c, tid))
out2b = uservice._commit_change(
    store,
    res.DraftChange(action=res.ACT_CONTINUE, capability="full_lst",
                    label="武汉任务", slots=t_aw["slots"], task_id=tid,
                    expected_version=int(t_aw["version"])),
    user_id="u", project_id="p", conversation_id=conv,
    command_id="c2b", message_id="m2b")
t_aw2 = store.read(lambda c: t_store.load_task(c, tid))
check("恢复等待回答的任务：保持 awaiting_info 且 resumed=True",
      t_aw2["summary_status"] == "awaiting_info"
      and int(t_aw2.get("pause_requested") or 0) == 0
      and out2b.get("resumed") is True,
      f"{t_aw2['summary_status']} / {t_aw2.get('pause_requested')} / {out2b.get('resumed')}")

# ── E) 暂停中取消：清标记 + cancelled ──────────────────────────
store.submit_write(lambda c: c.execute(
    "UPDATE tasks SET pause_requested=1, summary_status='paused' WHERE id=?",
    (tid,)))
t4 = store.read(lambda c: t_store.load_task(c, tid))
change3 = res.DraftChange(action=res.ACT_CANCEL, capability="full_lst",
                          label="武汉任务", slots=t4["slots"],
                          task_id=tid, expected_version=int(t4["version"]))
uservice._commit_change(store, change3, user_id="u", project_id="p",
                        conversation_id=conv, command_id="c3", message_id="m3")
t5 = store.read(lambda c: t_store.load_task(c, tid))
check("暂停中取消：状态 cancelled 且 pause 标记被清",
      t5["summary_status"] == "cancelled"
      and int(t5.get("pause_requested") or 0) == 0,
      f"{t5['summary_status']} / {t5.get('pause_requested')}")

# ── F) 调度门控：暂停不派发 / 恢复派发 ──────────────────────────
tid2 = _mk_task()
settings = {"agent": {"default_exec_mode": "approval", "roles_enabled": True}}
made = store.submit_write(lambda c: compile_task_tx(
    c, task_id=tid2, expected_task_version=1, settings=settings,
    project_dir=None, run_label="门控验证"))

sched = Scheduler(store, tmp / "exec", handler=None)
launched = []
sched._launch = lambda row, claim, context, skipped: launched.append(row["id"])
sched._refresh()

node0 = store.read(lambda c: c.execute(
    "SELECT id,status FROM nodes WHERE run_id=? ORDER BY exec_order LIMIT 1",
    (made["run_id"],)).fetchone())
check("门控前置：首节点已是 ready", str(node0[1]) == "ready", str(node0[1]))

store.submit_write(lambda c: c.execute(
    "UPDATE tasks SET pause_requested=1, summary_status='paused' WHERE id=?",
    (tid2,)))
sched._refresh()
st = store.read(lambda c: c.execute("SELECT summary_status FROM tasks WHERE id=?",
                                    (tid2,)).fetchone()[0])
states = store.read(lambda c: [r[0] for r in c.execute(
    "SELECT status FROM nodes WHERE run_id=?", (made["run_id"],))])
check("暂停时任务保持 paused", st == "paused", str(st))
check("暂停时节点未被误标取消", "cancelled" not in states, str(states[:4]))
sched._dispatch_ready()
check("暂停时不派发任何节点", launched == [], str(launched))

store.submit_write(lambda c: c.execute(
    "UPDATE tasks SET pause_requested=0, summary_status='running' WHERE id=?",
    (tid2,)))
sched._refresh()
sched._dispatch_ready()
check("恢复后派发首个就绪节点", launched == [node0[0]], str(launched[:3]))

store.close()
print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
