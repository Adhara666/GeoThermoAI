# -*- coding: utf-8 -*-
"""验证：结果后处理续跑（同一任务下的 gapfill 运行）编译链路。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.planning.catalog import validate_step_names  # noqa: E402
from core.planning.compiler import compile_task_tx  # noqa: E402
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


# ── 1) 步骤白名单：postprocess（gapfill 节点的技能名）必须被认可 ──
okv, unknown = validate_step_names(["postprocess", "rf_model"])
check("白名单认可 postprocess", okv and not unknown, str(unknown))

# ── 2) 编译单测：postprocess=True 生成“单节点 gapfill”续跑 ──
WH = "/app/config/study_areas/武汉市_市.geojson"
tmp = Path(tempfile.mkdtemp())
store = StateStore(tmp / "t.sqlite3")
slots = {"fields": {
    "region": {"value": "武汉市_市", "source": "user",
               "detail": {"path": WH, "display": "武汉市_市"}},
    "time": {"value": {"start": "2024-07-01", "end": "2024-07-31"},
             "source": "user"},
}, "negations": []}
def _seed(c):
    from core.state_kernel.intake import _ensure_conversation
    conv = _ensure_conversation(c, "u", "p", "c")
    return t_store.create_task(
        c, user_id="u", project_id="p", conversation_id=conv,
        capability="full_lst", slots=slots, label="武汉任务",
        summary_status="ready")


tid = store.submit_write(_seed)
settings_file = tmp / "settings.json"
settings_file.write_text("{}", encoding="utf-8")
settings = {
    "agent": {"default_exec_mode": "approval", "roles_enabled": True},
    "_execution": {"settings_path": str(settings_file),
                   "main_tif": "/tmp/main_lst.tif"},
}
made = store.submit_write(lambda c: compile_task_tx(
    c, task_id=tid, expected_task_version=1, settings=settings,
    project_dir=None, run_label="结果后处理", postprocess=True))
check("续跑运行能力=gapfill", made["capability"] == "gapfill",
      str(made.get("capability")))
check("续跑只含 gapfill 节点",
      [n["type"] for n in made["nodes"]] == ["gapfill"],
      str([n["type"] for n in made["nodes"]]))
run = store.read(lambda c: c.execute(
    "SELECT frozen_inputs,status FROM runs WHERE id=?",
    (made["run_id"],)).fetchone())
fi = json.loads(run[0])
snap = fi.get("snapshot") or {}
execu = snap.get("execution") or {}
check("快照标记 postprocess 且能力为 gapfill",
      fi.get("postprocess") is True and fi.get("capability") == "gapfill")
check("主产品路径冻结进快照 execution.main_tif",
      execu.get("main_tif") == "/tmp/main_lst.tif", str(execu))
task = store.read(lambda c: t_store.load_task(c, tid))
check("同一任务转为 queued 且指向新运行",
      task["summary_status"] == "queued"
      and task["current_run_id"] == made["run_id"],
      "%s / %s" % (task["summary_status"], task["current_run_id"][:10]))
check("任务能力未被改写（仍是 full_lst）",
      task["capability"] == "full_lst", str(task["capability"]))
store.close()
print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
