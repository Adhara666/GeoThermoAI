# -*- coding: utf-8 -*-
"""验证候选编号续编闭环：创建→显示→按编号解析（隔离临时库，可删除）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.state_kernel.store import StateStore  # noqa: E402
from core.state_kernel import questions as q_store  # noqa: E402
from core.agent.understanding.service import _compose_ask  # noqa: E402
from core.agent.understanding.answers import match_candidate  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="qnum_"))
store = StateStore(str(tmp / "l.sqlite3"))
CONV = "conv-1"

SPEC = [{"id": "1", "label": "配对模式", "value": "pair"},
        {"id": "2", "label": "月度合成模式", "value": "monthly"}]


def seed(conn):
    now = "2026-09-11 12:00:00"
    conn.execute(
        "INSERT INTO conversations (id, user_id, project_id, legacy_conv_id,"
        " semantic_version, next_message_seq, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (CONV, "u", "P", CONV, 1, 1, now, now))
    for tid in ("t1", "t2"):
        conn.execute(
            "INSERT INTO tasks (id, user_id, project_id, conversation_id,"
            " capability, version, slots, ambiguity, summary_status, priority,"
            " created_at, updated_at, label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "u", "P", CONV, "full_lst", 1, "{}", "{}",
             "awaiting_info", 0, now, now, f"任务-{tid}"))


def tx(conn):
    seed(conn)
    q1 = q_store.create_question(
        conn, user_id="u", conversation_id=CONV, qtype="missing_field",
        prompt="你的时间范围是2025 年 7 月（整月），要按哪种方式做？",
        targets=[{"task_id": "t1", "task_version": 1, "field": "product_mode"}],
        candidates=SPEC)
    q2 = q_store.create_question(
        conn, user_id="u", conversation_id=CONV, qtype="missing_field",
        prompt="你的时间范围是2025 年 8 月（整月），要按哪种方式做？",
        targets=[{"task_id": "t2", "task_version": 1, "field": "product_mode"}],
        candidates=SPEC)
    return q1, q2


q1, q2 = store.submit_write(tx)

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


def _try(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return f"ERROR:{type(e).__name__}:{e}"


qq1 = store.read(lambda c: q_store.get_question(c, q1)) \
    if hasattr(q_store, "get_question") else None
qqs = store.read(lambda c: q_store.list_open_questions(
    c, user_id="u", conversation_id=CONV))

ids1 = [x["id"] for x in qqs[0]["candidates"]]
ids2 = [x["id"] for x in qqs[1]["candidates"]]
check("问题1 候选编号 1,2", ids1 == ["1", "2"], str(ids1))
check("问题2 候选编号 3,4（对话内全局续编）", ids2 == ["3", "4"], str(ids2))

text = _compose_ask(
    qqs, [{"task_id": "t1", "label": "武汉市_市 2025 年 7 月 10 米地表温度完整生产"},
          {"task_id": "t2", "label": "鄂州市_市 2025 年 8 月 10 米地表温度完整生产"}])
check("显示包含“3. 配对模式”（与编号一致）",
      "\n3. 配对模式" in text, text[-160:])
check("提示语与编号自洽", "编号在各任务间连续" in text)

hit = match_candidate(qqs[1]["candidates"], "3")
check("回复“3”解析到问题2的配对模式（无歧义）",
      bool(hit) and hit["label"] == "配对模式", str(hit))
hit1 = match_candidate(qqs[0]["candidates"], "3")
check("问题1 对“3”无命中（歧义消除）", hit1 is None, str(hit1))

store.close()
print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
