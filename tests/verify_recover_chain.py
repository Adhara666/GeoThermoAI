# -*- coding: utf-8 -*-
"""验证历史提交恢复链：completion → files → 重定向发布目录（可删除）。"""
import json
import sqlite3
import sys
from pathlib import Path

RUN = "4d661355cf924003a612a2592ab86477"
c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")

# 上游 preprocess_split 节点
node = c.execute(
    "SELECT n.id, n.result FROM nodes n WHERE n.run_id=? AND n.node_key='preprocess_split'",
    (RUN,)).fetchone()
result = json.loads(node[1] or "{}")
published = result.get("published_dir")
print("published_dir:", published)
assert published, "无发布目录"

# 按其修复前逻辑：result.context.files 应为空（被覆盖）
print("修复前 result.context.files:", len((result.get("context") or {}).get("files") or {}))

# 模拟 _recover_input_files：从 completion 读回
attempt = c.execute(
    "SELECT result_path FROM attempts WHERE node_id=? AND status='succeeded'"
    " ORDER BY attempt_no DESC LIMIT 1", (node[0],)).fetchone()
env = json.loads(Path(attempt[0]).read_text(encoding="utf-8"))
files = ((env.get("result") or {}).get("context") or {}).get("files") or {}
print("completion 读回 files:", len(files), "项")

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


redirected = {
    rel: (str(Path(published) / rel) if (Path(published) / rel).is_file() else path)
    for rel, path in files.items()}
split = redirected.get("for_train/split_info.json", "")
check("恢复链能拿到 split_info.json 且指向存在文件",
      bool(split) and Path(split).is_file(), split)
train = redirected.get("for_train/train.parquet", "")
check("train.parquet 重定向有效（ttri 输入）",
      bool(train) and Path(train).is_file(), train)

# 另外确认：prep_check 的失败节点可重试（状态 failed）
pc = c.execute(
    "SELECT n.status FROM nodes n WHERE n.run_id=? AND n.node_key='prep_check'",
    (RUN,)).fetchone()
check("prep_check 处于可重试状态", pc and pc[0] == "failed", str(pc))

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
