# -*- coding: utf-8 -*-
"""验证查询句路由判断 + 复核防护常量（可删除）。"""
import sys

sys.path.insert(0, "/app")

import core.web_app as wa  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


CASES = [
    ("我当前武汉市的配对用的是哪一天的 Sentinel-2 和 Landsat？", True),
    ("当前任务跑到哪一步了？", True),
    ("为什么失败了？", True),
    ("进度如何？", True),
    ("怎么做地表温度降尺度？", True),
    ("武汉 7 月、鄂州 8 月，都是 2025 年，月度产品", False),
    ("帮我重试一下？", False),
    ("把武汉任务取消？", False),
    ("你好", False),
    ("武汉去年 7 月做 LST，配对模式，下载数据", False),
    ("武汉 7 月做 LST 吗？", False),
    ("下载完成了吗？", False),
]
for msg, expected in CASES:
    got = wa._is_query_only(msg)
    check(f"“{msg[:22]}…”→ {expected}", got == expected, f"got={got}")

check("正文/思考硬上限已生效（3000）", wa._STREAM_MAX_CHARS == 3000,
      str(wa._STREAM_MAX_CHARS))

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
