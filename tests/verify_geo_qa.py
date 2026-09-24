# -*- coding: utf-8 -*-
"""现场 E2E：自然语言研究区 + 地点温度问答（可删除）。"""
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


def post(path, payload):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload).encode(), headers=H)
    with urllib.request.urlopen(req, timeout=120) as r:
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


def send(conv, msg, point=None):
    payload = {"project": PROJECT, "conv": conv, "message": msg,
               "exec_mode": "auto", "request_id": uuid.uuid4().hex}
    if point:
        payload["selected_point"] = point
    return post("/api/chat/start", payload)


def wait_reply(conv, timeout=180, must=None):
    """等待对话当前流内容出现非空回复。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = get("/api/chat/current?conv=" + conv)
            last = str(r.get("content") or r.get("reply") or "")
        except Exception:  # noqa: BLE001
            last = ""
        if last.strip() and (must is None or must in last):
            return last
        time.sleep(3)
    return last


def wait_any(conv, words, timeout=180):
    """等待当前轮流内容变化且包含任一词（兼容多种措辞/数据来源）。"""
    prev = ""
    try:
        prev = str(get("/api/chat/current?conv=" + conv).get("content") or "")
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + timeout
    cur = prev
    while time.time() < deadline:
        time.sleep(3)
        try:
            cur = str(get("/api/chat/current?conv=" + conv).get("content") or "")
        except Exception:  # noqa: BLE001
            continue
        if cur != prev and any(w in cur for w in words):
            time.sleep(2)
            try:
                cur = str(get("/api/chat/current?conv=" + conv).get("content") or "")
            except Exception:  # noqa: BLE001
                pass
            return cur
    return cur


results = []


def report(name, ok, detail=""):
    results.append(ok)
    print("%s %s  %s" % ("[PASS]" if ok else "[FAIL]", name, detail[:160]))


# 前置：确保测试项目存在（项目可能被删除；脚本需独立可复跑）
try:
    post("/api/projects", {"name": PROJECT})
except Exception:  # noqa: BLE001
    pass

r = post("/api/conversations", {"project": PROJECT, "title": "地理问答验证"})
conv = r.get("conv_id")
print("对话:", conv, "|", r.get("message"))

# ── 1) 自然语言研究区：洪山区（无上传）建任务 ──
print(">> 1) 发送：我想要洪山区2024年7月的10m地表温度数据，用配对模式")
send(conv, "我想要洪山区2024年7月的10m地表温度数据，用配对模式")
task = None
deadline = time.time() + 150
while time.time() < deadline:
    task = q1("SELECT id, summary_status, slots FROM tasks WHERE conversation_id="
              " (SELECT id FROM conversations WHERE legacy_conv_id=?)"
              " ORDER BY rowid DESC LIMIT 1", (conv,))
    if task:
        break
    time.sleep(3)
ok1 = bool(task)
region_path = ""
if task:
    try:
        slots = json.loads(task["slots"] or "{}")
        detail = ((slots.get("fields") or {}).get("region") or {}).get("detail") or {}
        region_path = str(detail.get("path") or "")
    except (ValueError, TypeError):
        pass
report("洪山区任务已创建（不依赖上传）",
       ok1 and "boundary_library" in region_path and "洪山区" in region_path,
       "task=%s | region=%s" % (task["id"][:10] if task else "-", region_path))

# 等任务进入执行/排队后（供后面 geo 查询到结果前，先做无结果守卫？——此处已有一个更早完成结果）
time.sleep(10)

# ── 2) 区级问答：洪山区平均温度 ──
print(">> 2) 发送：洪山区的平均地表温度是多少度")
send(conv, "洪山区的平均地表温度是多少度")
reply2 = wait_reply(conv, timeout=180, must="℃")
# 回答需带具体观测日期（Sentinel-2 2024-07-22，非仅月份）
has_date = any(s in reply2 for s in ("22 日", "22日", "07-22", "7-22"))
ok2 = ("℃" in reply2 or "C)" in reply2) and \
      ("42" in reply2 or "315" in reply2) and has_date
report("区级面统计问答", ok2, reply2.replace("\n", " "))

# ── 3) 选点提问：这个点周围300米（点取洪山区内地大，保证被最新结果覆盖；
#       武汉全域与洪山区自身两种产物均能命中，避免多任务完成后的数据漂移） ──
print(">> 3) 发送（带选点）：这个点周围300米范围的地表温度是多少")
send(conv, "这个点周围300米范围的地表温度是多少",
     point={"lon": 114.39381, "lat": 30.52532})
reply3 = wait_reply(conv, timeout=180, must="300")
ok3 = ("300 m" in reply3 or "300米" in reply3 or "300 米" in reply3
       or "300m" in reply3) and \
      ("℃" in reply3 or "K" in reply3 or "C)" in reply3)
report("选点缓冲统计问答", ok3, reply3.replace("\n", " "))

# ── 4) 海外点守卫：网络可用→“不在覆盖范围”；不可用→如实说查不到；都不得给数字 ──
print(">> 4) 发送：纽约百老汇的温度是多少")
send(conv, "纽约百老汇的温度是多少")
reply4 = wait_any(conv, ["百老汇", "百老匯", "没能", "未能", "查不到", "覆盖", "不在"])
ok4 = ("℃" not in reply4) and \
      any(k in reply4 for k in ("百老汇", "百老匯", "没能", "未能",
                                 "查不到", "覆盖", "不在"))
report("海外点覆盖守卫（不编数字）", ok4, reply4.replace("\n", " "))

# ── 5) 位置识别：带选点问“这是哪里”（网络可用→道路级；不可用→本地库行政区级） ──
print(">> 5) 发送（带选点）：这是哪里，具体什么地方")
send(conv, "这是哪里，具体什么地方",
     point={"lon": 114.39381, "lat": 30.52532})
reply5 = wait_any(conv, ["位于", "一带", "附近", "没能识别"])
ok5 = ("没能识别" not in reply5) and \
      any(k in reply5 for k in ("一带", "位于", "附近", "中国地质大学",
                                 "洪山", "武汉", "东湖", "关东", "鲁磨路"))
report("位置识别（带选点）", ok5, reply5.replace("\n", " "))

# ── 6) 复合问句：位置+温度（一句话应同时答出两者，不能只答一个） ──
print(">> 6) 发送（带选点）：这是哪，温度如何")
send(conv, "这是哪，温度如何",
     point={"lon": 114.39381, "lat": 30.52532})
reply6 = wait_any(conv, ["℃"])
ok6 = ("℃" in reply6) and any(
    k in reply6 for k in ("区", "街道", "路", "中心", "位于",
                          "点选", "位置", "一带"))
report("复合问句（位置+温度）", ok6, reply6.replace("\n", " "))

# ── 7) 收尾：取消洪山区任务 ──
if task:
    try:
        post("/api/tasks/%s/commands" % task["id"],
             {"operation": "cancel", "request_id": uuid.uuid4().hex})
        print("已取消任务:", task["id"][:10])
    except Exception as e:  # noqa: BLE001
        print("取消任务失败:", e)

print("\nE2E 结果：%d/%d 通过" % (sum(results), len(results)))
print("演示对话:", conv)
