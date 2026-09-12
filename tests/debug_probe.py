# -*- coding: utf-8 -*-
"""时间线诊断：prep_check 各尝试 vs 服务重启时间（可删除）。"""
import sqlite3
import sys

sys.path.insert(0, "/app")

c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
row = c.execute(
    "SELECT t.current_run_id FROM tasks t WHERE t.user_id='Adhara'"
    " AND t.label LIKE '武汉市%' ORDER BY t.created_at DESC LIMIT 1").fetchone()
run_id = row[0]
print("== prep_check 所有尝试（UTC）==")
for r in c.execute(
        "SELECT a.attempt_no, a.status, a.error, a.started_at, a.finished_at"
        " FROM attempts a JOIN nodes n ON n.id=a.node_id"
        " WHERE n.run_id=? AND n.node_key='prep_check' ORDER BY a.attempt_no",
        (run_id,)):
    print(f"  尝试{r[0]}: {r[1]} | 起 {r[3]} 止 {r[4]}")
    if r[2]:
        print(f"     {str(r[2])[:80]}")

# 服务进程启动时间（容器内 /proc/1 的启动时刻 = server 进程）
import os
import time
try:
    stat = os.stat("/proc/1")
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    started = time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.localtime(boot))
    print(f"\n容器启动时间(本地): {started}")
except Exception as e:
    print("读取启动时间失败:", e)

# 当前调度器文件是否为新版（无条件恢复）
src = open("/app/core/scheduling/scheduler.py", encoding="utf-8").read()
print("scheduler.py 含无条件恢复:", 'if published:' in src
      and '# 缺什么补什么' in src)
# __pycache__ 时间
import glob
for p in glob.glob("/app/core/scheduling/__pycache__/scheduler*.pyc"):
    print("pyc:", p.split("/")[-1],
          time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
print("scheduler.py mtime:",
      time.strftime("%m-%d %H:%M:%S",
                    time.localtime(os.path.getmtime(
                        "/app/core/scheduling/scheduler.py"))))
print("当前时间(本地):", time.strftime("%m-%d %H:%M:%S"))
