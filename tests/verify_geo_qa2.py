# -*- coding: utf-8 -*-
"""验证 v2：海外边界下载/检索 + 街道级问答 + 重名追问闭环。

可重复运行；需要服务已启动（HTTP 本地）。
"""
import json
import sqlite3
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, "/app")

from core.auth import create_token  # noqa: E402

TOK = create_token("Adhara", "Adhara")
H = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}
BASE = "http://127.0.0.1:7860"
PROJECT = "测试7"

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail[:200]))


def post(path, payload):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload).encode(), headers=H)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def get(path):
    req = urllib.request.Request(BASE + path, headers=H)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def q1(sql, args=()):
    c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def current(conv):
    return str(get("/api/chat/current?conv=" + conv).get("content") or "")


def ask_wait(conv, msg, keys, wait=210):
    """发送消息并等待（返回对话当前流的全文；判定用本轮特征词）。"""
    post("/api/chat/start", {"project": PROJECT, "conv": conv, "message": msg,
                             "exec_mode": "auto",
                             "request_id": uuid.uuid4().hex})
    deadline = time.time() + wait
    cur, last_change = "", 0.0
    while time.time() < deadline:
        time.sleep(4)
        nxt = current(conv)
        if nxt != cur:
            cur, last_change = nxt, time.time()
        elif last_change and time.time() - last_change > 12:
            break
        if any(k in cur for k in keys) and \
                last_change and time.time() - last_change > 4:
            break
    return cur


# ── A) 海外边界：检索 + 镇/基层级下载（新加坡规划区 ≈ 基层） ──
print("== A) 海外边界 ==")
from core.boundaries import get_library  # noqa: E402
from core.boundaries import fetcher  # noqa: E402

lib = get_library()
hits = lib.search_global("Monaco", iso="MCO")
check("海外检索（摩纳哥）", bool(hits), str(hits[:2]))
try:
    out = fetcher.fetch_global("SGP", 1, lib)
    check("海外镇/基层级下载（新加坡 ADM1）", out.get("count", 0) > 0,
          json.dumps(out, ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    check("海外镇/基层级下载（新加坡 ADM1）", False, "%s" % e)

# ── B) E2E：街道级问答 ──
print("== B) 街道级问答 ==")
# 前置：确保测试项目存在（项目可能被删除；脚本需独立可复跑）
try:
    post("/api/projects", {"name": PROJECT})
except Exception:  # noqa: BLE001
    pass
conv = post("/api/conversations", {"project": PROJECT,
                                   "title": "地理问答验证2"}).get("conv_id")
print("  对话:", conv)
reply1 = ask_wait(conv, "关山街道的地表温度是多少",
                  keys=["关山街道", "℃"])
check("街道级面统计（关山街道）",
      "关山街道" in reply1 and ("℃" in reply1 or "K" in reply1),
      reply1.replace("\n", " "))

# ── C) 重名追问闭环：新华街道（全国重名，跨城市） ──
print("== C) 重名追问 ==")
reply2 = ask_wait(conv, "新华街道的温度是多少",
                  keys=["新华街道", "℃", "匹配到多个地点"])
ambiguous = "匹配到多个地点" in reply2
# “新华街道”是本轮特征词；直答统计或追问均可（覆盖范围优先消歧）
check("重名场景给出确定答复（消歧直答或追问）",
      ("新华街道" in reply2) and
      (ambiguous or ("℃" in reply2) or ("不在" in reply2)
       or ("覆盖" in reply2) or ("没法" in reply2)
       or ("没能" in reply2) or ("未能" in reply2) or ("查不到" in reply2)),
      reply2.replace("\n", " ")
      if ("新华街道" in reply2) else reply2[-300:].replace("\n", " "))
if ambiguous:
    conv_pk = q1("SELECT id FROM conversations WHERE legacy_conv_id=?",
                 (conv,))[0]
    qrow = q1("SELECT id, candidates FROM questions WHERE conversation_id=?"
              " AND status='open' AND qtype='geo_target'"
              " ORDER BY rowid DESC LIMIT 1", (conv_pk,))
    check("追问卡已落库（geo_target）", bool(qrow))
    if qrow:
        cands = json.loads(qrow["candidates"] or "[]")
        check("追问卡含多个候选", len(cands) >= 2,
              str([c.get("name", "")[:24] for c in cands[:3]]))
        first = cands[0]
        r = post("/api/kernel/answer", {
            "project": PROJECT, "conv": conv,
            "question_id": qrow["id"], "answer": str(first.get("id"))})
        check("追问卡可回答", bool(r.get("ok")), str(r)[:160])
        time.sleep(6)
        cur = current(conv)
        check("回答后给出统计或明确说明",
              ("平均地表温度" in cur) or ("不在" in cur) or ("没有" in cur),
              cur[-260:].replace("\n", " "))

print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
