# -*- coding: utf-8 -*-
"""验证 _conversation_exec_mode：无请求上下文也能读到对话当前模式（可删除）。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402

backend = wa.AppBackend()

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


# 造一个临时对话文件（写 exec_mode=auto）验证读取路径
from core.state_kernel.store import StateStore  # noqa: E402
import tempfile  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="modechk_"))
db = tmp / "ledger.sqlite3"
store = StateStore(str(db))
store.close()


def seed(conn):
    now = "2026-09-11 12:00:00"
    conn.execute(
        "INSERT INTO conversations (id, user_id, project_id, legacy_conv_id,"
        " semantic_version, next_message_seq, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("convpk1", "tester", "P", "legacy1", 1, 1, now, now))
    conn.execute(
        "INSERT INTO tasks (id, user_id, project_id, conversation_id,"
        " capability, version, slots, ambiguity, summary_status, priority,"
        " created_at, updated_at, label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t1", "tester", "P", "convpk1", "full_lst", 1, "{}", "{}",
         "ready", 0, now, now, "测试任务"))


store2 = StateStore(str(db))
store2.submit_write(seed)

backend._get_state_store = lambda: store2
backend._uid = lambda: "tester"

conv_dir = tmp / "conversations"
conv_dir.mkdir()
(conv_dir / "legacy1.json").write_text(
    json.dumps({"exec_mode": "auto", "title": "T"}), encoding="utf-8")
backend._conv_dir = lambda: conv_dir

task_row = {"id": "t1", "user_id": "tester", "conversation_id": "convpk1"}
mode = backend._conversation_exec_mode(task_row)
check("读到对话文件的 auto 模式", mode == "auto", repr(mode))

(conv_dir / "legacy1.json").write_text(
    json.dumps({"title": "无模式"}), encoding="utf-8")
check("文件无模式时返回空串（回退默认值）",
      backend._conversation_exec_mode(task_row) == "")

check("行缺 user_id 时安全返回空",
      backend._conversation_exec_mode({"id": "t1"}) == "")

store2.close()
print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
