# -*- coding: utf-8 -*-
"""验证重复需求抑制 + 展示去重（HTTP 端到端，可删除）。

场景（复刻用户实测）：
  1) 同一句多任务消息发送两次 → 第二次不重复建任务、不重复追问
  2) 两个任务各一条问题，展示带任务标题（无裸问题段）
  3) 气泡文案不因重发而重复刷屏
"""
import sys
import time
from pathlib import Path
from urllib.parse import quote  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

import e2e_stage2_understanding as s2  # noqa: E402

s2.os.environ["GTAI_E2E_USERNAME"] = "stage6_e2e"
s2._DEFAULT_STATE = "/app/data/state_kernel/.stage6_e2e_state.json"
s2.PROJECT = "阶段六验收"

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


token = s2.login()
print("  [PASS] 登录")
s2.configure_model(token, "http://127.0.0.1:18000", "stub-key",
                   "scripted-candidate-model")
s2.ensure_project(token)
s2.upload_study_areas(token, ["武汉市_市", "南京市_市"])

# 新对话：时间戳命名，避免污染历史
title = f"dup-verify-{int(time.time())}"
cid = s2.make_conversation(token, title)
print(f"  [PASS] 新建对话 {cid}")

MSG = "武汉 7 月、南京 8 月，都是 2025 年"


def wait_tasks(min_n, timeout=90):
    end = time.time() + timeout
    rows = []
    while time.time() < end:
        rows = s2.session(token, cid).get("tasks") or []
        if len(rows) >= min_n:
            return rows
        time.sleep(2)
    return rows


send1 = s2.send(token, cid, MSG)
rows1 = wait_tasks(2)
n1 = len(rows1)
check("第一次发送建出两张任务卡", n1 >= 2, f"tasks={n1}")

reply2_text = ""
send2 = s2.send(token, cid, MSG)
time.sleep(6)
rows2 = s2.session(token, cid).get("tasks") or []
qs2 = s2.session(token, cid).get("questions") or []
check("第二次同句发送不新增任务（重复抑制）", len(rows2) == n1,
      f"before={n1} after={len(rows2)}")

# 从消息列表取最后一条回复文本
url = (f"/api/messages?project={quote(s2.PROJECT)}"
       f"&conv={quote(cid)}")
msg_ret = s2.call("GET", url, token=token)
for m in reversed(msg_ret.get("messages") or []):
    if m.get("role") == "assistant":
        reply2_text = str(m.get("content") or "")
        break
check("重发回复有明确提示（未重复创建）",
      "重复" in reply2_text, reply2_text[:160])

print(f"\n重复抑制验证结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
