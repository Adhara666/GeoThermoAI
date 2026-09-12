# -*- coding: utf-8 -*-
"""验证：完整上下文恢复 + 路径深度重定向（真实数据，可删除）。"""
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from core.artifacts.publisher import _rebase_paths, merge_published_result  # noqa: E402
from core.scheduling.scheduler import Scheduler  # noqa: E402
from core.state_kernel.store import StateStore  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


# 1) _rebase_paths 单元
import tempfile  # noqa: E402
tmp = Path(tempfile.mkdtemp(prefix="rebasechk_"))
old = tmp / "staging/work"; new = tmp / "committed"
(old / "for_train").mkdir(parents=True); (new / "for_train").mkdir(parents=True)
(new / "for_train/train.parquet").write_text("x")
ctx = {"files": {"for_train/train.parquet": str(old / "for_train/train.parquet"),
                 "missing.bin": str(old / "missing.bin")},
       "pipeline_data": {"train_csv": str(old / "for_train/train.parquet")},
       "steps": [{"p": str(old / "for_train/train.parquet")}]}
out = _rebase_paths(ctx, str(old), str(new))
check("已发布文件重定向", out["files"]["for_train/train.parquet"] == str(new / "for_train/train.parquet"))
check("未发布文件保留原样", out["files"]["missing.bin"] == str(old / "missing.bin"))
check("嵌套 dict 重定向", out["pipeline_data"]["train_csv"] == str(new / "for_train/train.parquet"))
check("嵌套 list 重定向", out["steps"][0]["p"] == str(new / "for_train/train.parquet"))

# 2) 真实数据：调度器 _context 对 prep_check 的组装（历史兼容恢复）
import sqlite3  # noqa: E402
c = sqlite3.connect("/app/data/state_kernel/ledger.sqlite3")
RUN = "4d661355cf924003a612a2592ab86477"
prep_node = c.execute(
    "SELECT id FROM nodes WHERE run_id=? AND node_key='prep_check'",
    (RUN,)).fetchone()
prep_node2 = c.execute(
    "SELECT id FROM nodes WHERE run_id=? AND node_key='ttri'",
    (RUN,)).fetchone()

s = Scheduler.__new__(Scheduler)
s.store = StateStore("/app/data/state_kernel/ledger.sqlite3")
ctx2 = s._context({"id": prep_node[0]})
pd_ = ctx2.get("pipeline_data") or {}
files = ctx2.get("files") or {}
check("恢复出 pipeline_data（不再 KeyError）", bool(pd_),
      f"keys={sorted(pd_)[:5]}")
train_csv = str(pd_.get("train_csv") or "")
check("pipeline_data 内路径指向发布目录且存在",
      "committed" in train_csv and Path(train_csv).is_file(), train_csv[-70:])
split = str(files.get("for_train/split_info.json") or "")
check("files 指向发布目录且存在",
      "committed" in split and Path(split).is_file(), split[-70:])

ctx3 = s._context({"id": prep_node2[0]})
check("ttri 节点的上下文同样含 pipeline_data",
      bool((ctx3.get("pipeline_data") or {})),
      sorted((ctx3.get("context") or {}).keys()) if False else "")
s.store.close()

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
