# -*- coding: utf-8 -*-
"""验证 merge_published_result + 发布目录完整性（可删除）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from core.artifacts.publisher import merge_published_result  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


tmp = Path(tempfile.mkdtemp(prefix="mergechk_"))
target = tmp / "committed"
(target / "for_train").mkdir(parents=True)
(target / "for_train/split_info.json").write_text("{}")
(target / "30m_constraint_grid.parquet").write_text("x")

prior = {
    "context": {
        "files": {
            "for_train/split_info.json": "/old/staging/work/for_train/split_info.json",
            "30m_constraint_grid.parquet": "/old/staging/work/30m_constraint_grid.parquet",
            "not_published.bin": "/old/staging/work/not_published.bin",
        },
        "scale": {"pixels": 100},
    },
}
merged = merge_published_result(prior, str(target), ["a1", "a2"])
files = merged["context"]["files"]
check("已发布文件重定向到发布目录",
      files["for_train/split_info.json"] == str(target / "for_train/split_info.json")
      and files["30m_constraint_grid.parquet"] == str(target / "30m_constraint_grid.parquet"),
      json.dumps(files))
check("未发布路径保留原样（不误伤）",
      files["not_published.bin"] == "/old/staging/work/not_published.bin")
check("其它 context 键保留", merged["context"].get("scale") == {"pixels": 100})
check("提交信息保留", merged.get("published_dir") == str(target)
      and merged.get("artifacts") == ["a1", "a2"])

# 空 prior 不崩
merged2 = merge_published_result({}, str(target), [])
check("空 prior 安全", merged2.get("published_dir") == str(target)
      and merged2["context"].get("files") in (None, {}))

# 发布目录完整性（真实）：preprocess 的 for_train parquet 是否都在
committed = Path("/app/data/state_kernel/executions/runs/"
                 "4d661355cf924003a612a2592ab86477/committed/preprocess_split/attempt-1")
if committed.is_dir():
    names = sorted(p.relative_to(committed).as_posix()
                   for p in committed.rglob("*") if p.is_file())
    print("  发布目录文件:", len(names), "个")
    needed = ["for_train/split_info.json", "for_train/train.parquet",
              "for_train/validate.parquet", "for_train/test.parquet",
              "30m_constraint_grid.parquet", "10m_predict_features.parquet"]
    missing = [n for n in needed if n not in names]
    check("下游所需文件均在发布目录（ttri/prep_check 输入完整）",
          not missing, f"缺失: {missing}")
    print("  清单:", names[:20])

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
