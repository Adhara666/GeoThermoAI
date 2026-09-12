# -*- coding: utf-8 -*-
"""验证 kernel_qa_context：模型将看到的任务进展信息（Adhara 真实任务，可删除）。"""
import json
import sqlite3
import sys

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402

c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
# 取 Adhara 对话（武汉任务所在对话）
row = c.execute(
    "SELECT cv.legacy_conv_id FROM conversations cv"
    " WHERE cv.user_id='Adhara'"
    " ORDER BY cv.updated_at DESC LIMIT 1").fetchone()

backend = wa.AppBackend()
backend._uid = lambda: "Adhara"

ctx = backend.kernel_qa_context("", row[0])
print("== 模型将看到的上下文 ==")
print(json.dumps(ctx, ensure_ascii=False, indent=2)[:2200])

ok = bool(ctx.get("tasks")) and all(
    t.get("progress") for t in ctx["tasks"])
print()
print("含任务进展:", bool(ctx.get("tasks")),
      "| 每个任务有进度行:", all(t.get("progress") for t in ctx.get("tasks") or [{}]))
