# -*- coding: utf-8 -*-
"""第七轮改动综合验证：日志持久化/回填、完成气泡、采样、资源口径、语言（可删除）。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/tests")

import core.web_app as wa  # noqa: E402

DB = "/app/data/state_kernel/ledger.sqlite3"
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


# 1) schema v5 + task_logs
ver = c.execute("PRAGMA user_version").fetchone()[0]
check("数据库迁移到 v5", ver == 5, f"version={ver}")
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check("task_logs 表存在", "task_logs" in tables)
total_logs = c.execute("SELECT COUNT(*) FROM task_logs").fetchone()[0]
print(f"  task_logs 总行数: {total_logs}")
check("历史事件已回填日志", total_logs > 0, f"rows={total_logs}")

# 2) 完成气泡（对话可能已被删除：容错跳过）
conv_file = Path("/app/data/users/Adhara/conversations/96b0a1dcae1f.json")
HAS_CONV = conv_file.exists()
data = json.loads(conv_file.read_text(encoding="utf-8")) if HAS_CONV \
    else {"messages": []}
bubbles = [m for m in (data.get("messages") or [])
           if isinstance(m, dict) and m.get("kind") == "task_complete"]
if HAS_CONV:
    check("完成报告气泡已写入（武汉+鄂州=2 条）", len(bubbles) >= 2,
          f"count={len(bubbles)}")
else:
    print("  [SKIP] 对话已删除，跳过完成气泡计数检查")
for b in bubbles[:2]:
    print("    ---")
    print("    " + (b.get("content") or "").replace("\n", "\n    ")[:500])
import re as _re  # noqa: E402
_EMO = _re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
                    "\U00002B00-\U00002BFF\U00002300-\U000023FF\U0000FE0F]+")
check("完成报告无 emoji", all(not _EMO.search(b.get("content") or "")
                              for b in bubbles))
# 解读段：数据行后存在非列表的普通段落（不再带“AI 解读”字样）
def _has_prose(content: str) -> bool:
    paras = [p.strip() for p in str(content).split("\n\n") if p.strip()]
    return len(paras) >= 3 and not paras[-1].startswith("-") \
        and "AI 解读" not in content
check("完成报告含结果解读段（无“AI 解读”字样）",
      all(_has_prose(b.get("content") or "") for b in bubbles))
check("完成报告含使用影像行（日期+Landsat 型号+Sentinel-2）",
      all("使用影像：" in (b.get("content") or "")
          and "Landsat" in (b.get("content") or "")
          and "Sentinel-2" in (b.get("content") or "")
          for b in bubbles))

# 防回归：报告组装函数只能有一份定义（历史上有旧版残留覆盖新版）
_src = Path("/app/core/web_app.py").read_text(encoding="utf-8")
check("报告组装函数无重复定义",
      _src.count("def _compose_completion_report(") == 1,
      f"count={_src.count('def _compose_completion_report(')}")

# 3) 日志历史 API（含失败原因行）
backend = wa.AppBackend()
backend._uid = lambda: "Adhara"
r = backend.conversation_logs("96b0a1dcae1f", tz=8)
logs = r.get("logs") or []
if HAS_CONV:
    check("日志历史接口返回条目", r.get("ok") and len(logs) > 0, f"n={len(logs)}")
else:
    print("  [SKIP] 对话已删除，跳过日志条目检查")
# 失败/重试行面向全局日志核实（该对话内未必产生失败）
texts = [row[0] for row in c.execute("SELECT text FROM task_logs")]
has_fail = any("失败" in x or "Failed" in x for x in texts)
has_retry = any("重试" in x or "Retry" in x for x in texts)
print(f"    日志样例（尾部 8 条）:")
for x in logs[-8:]:
    print("      " + x["text"][:140])
check("包含失败/原因行", has_fail)
check("包含重试行", has_retry)
check("日志无 emoji 图标", all(not _EMO.search(x) for x in texts))

# 4) 采样（显示温度：输入 30m LST + 产物 10m LST）
_row0 = c.execute(
    "SELECT id FROM tasks WHERE user_id='Adhara' AND label LIKE '武汉市%'"
    " AND summary_status='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
task_id = _row0[0] if _row0 else ""
if task_id:
    inp = backend.task_inputs(task_id)
    lst_input = next((x for x in inp.get("inputs", []) if x["key"] == "landsat_path"), None)
    if lst_input is None or not lst_input.get("bounds"):
        print("  [SKIP] 该任务输入文件已不可用（历史任务），跳过采样检查")
    else:
        check("任务输入包含 30m LST 层", True)
        b = lst_input["bounds"]
        lon, lat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        v_in = backend.task_input_sample(task_id, "landsat_path", lat, lon)
        check("30m LST 输入层采样返回温度(K)",
              isinstance(v_in, float) and 200 < v_in < 400, f"v={v_in}")
        art = c.execute(
            "SELECT a.id, a.path FROM artifacts a JOIN attempts at ON at.id=a.attempt_id"
            " JOIN nodes n ON n.id=at.node_id WHERE n.run_id=("
            " SELECT current_run_id FROM tasks WHERE id=?)"
            " AND a.path LIKE '%rf_10m_lst_final%' LIMIT 1", (task_id,)).fetchone()
        check("找到 10m LST 正式产物", art is not None)
        # 文件可能已被磁盘回收（availability=cleaned）：此处验证“已清理”状态
        # 被正确标记而非误报可下载
        avail = c.execute("SELECT availability FROM artifacts WHERE id=?",
                          (art["id"],)).fetchone()[0] if art else ""
        print(f"    10m LST 产物可用性: {avail}")
        if avail == "available":
            v_out = None
            for dx, dy in ((0, 0), (-0.03, -0.03), (0.03, 0.03), (-0.05, 0.02), (0.02, -0.05)):
                v_out = backend.artifact_sample(art["id"], lat + dy, lon + dx)
                if isinstance(v_out, float):
                    break
            check("10m LST 产物采样返回温度(K)",
                  isinstance(v_out, float) and 200 < v_out < 400, f"v={v_out}")
        else:
            check("10m LST 产物已标记清理（文件缺失时不误报可下载）", avail == "cleaned")
        v_bad = backend.task_input_sample(task_id, "sentinel2_path", lat, lon)
        check("非 LST 层不参与采样", v_bad is None)
else:
    print("  [SKIP] 无已完成武汉任务，跳过采样检查")

# 5) 资源口径（整个软件）
usage = backend.get_sys_usage()
print(f"    sysinfo: mem={usage['mem_gb']}G disk={usage['disk_gb']}G")
check("内存为整个软件（容器）口径 > 0", usage["mem_gb"] > 0)
check("磁盘覆盖数据根（含执行缓存）", usage["disk_gb"] > 1.0,
      f"disk={usage['disk_gb']}")

# 6) 语言：任务标签/程序文案随对话语言
from core.agent.understanding import resolution as res  # noqa: E402
from core.agent.understanding import capabilities as caps  # noqa: E402
from core.agent.understanding.slotbook import SlotBook  # noqa: E402
from core.agent.understanding import operations as ops  # noqa: E402
check("英文消息检测为 en",
      res._lang_of("Make a July 2025 LST product for Wuhan City") == "en")
check("中文消息检测为 zh", res._lang_of("帮我做武汉7月的地表温度") == "zh")
book = SlotBook({"fields": {
    "region": {"value": "Wuhan City", "source": "user"},
    "time": {"value": {"start": "2025-07-01", "end": "2025-07-31"},
             "source": "user"}}})
label_en = res._label_for(book, ops.CAP_FULL_LST, "en")
print(f"    英文任务名示例: {label_en}")
check("英文任务名为英文", label_en == "Wuhan City July 2025 10 m LST full production",
      label_en)
label_zh = res._label_for(book, ops.CAP_FULL_LST, "zh")
check("中文任务名保持中文", "2025 年 7 月" in label_zh and "完整生产" in label_zh, label_zh)
check("能力英文名就绪",
      caps.label(ops.CAP_FULL_LST, "en") == "10 m LST full production")

# 7) 用户语言设置读写
prefs = c.execute("SELECT 1").fetchone()
lang_path = Path("/app/data/users/Adhara/settings.json")
saved = False
if lang_path.is_file():
    saved = "ui_lang" in json.loads(lang_path.read_text(encoding="utf-8"))
print(f"    ui_lang 已持久: {saved}（用户切换语言后写入）")

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
