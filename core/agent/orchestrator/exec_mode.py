"""
执行模式

默认值必须是 APPROVAL：现状代码在配对选择处一定会暂停问用户，
默认「由我批准」才能保证不改变既有功能。

模式由总调度统一掌握，子 Agent 只读不写。
"""

from typing import Dict, Optional


class ExecMode:
    APPROVAL = "approval"   # 由我批准：关键节点弹窗，由用户选择下一步
    AUTO = "auto"           # 完全执行：按默认策略走完全流程并输出结果


DEFAULT_EXEC_MODE = ExecMode.APPROVAL

ALL_MODES = (ExecMode.APPROVAL, ExecMode.AUTO)

# 前端上拉框与气泡说明用的中文标签（红线：一律中文、不带表情符号）
MODE_LABELS: Dict[str, str] = {
    ExecMode.APPROVAL: "由我批准",
    ExecMode.AUTO: "完全执行",
}

MODE_HINTS: Dict[str, str] = {
    ExecMode.APPROVAL: "关键节点会停下来问你",
    ExecMode.AUTO: "一次跑完，不打断",
}


def normalize(value: Optional[str], default: str = DEFAULT_EXEC_MODE) -> str:
    """把任意输入收敛到合法模式；非法值回落到 default。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ALL_MODES else default


def is_approval(mode: Optional[str]) -> bool:
    return normalize(mode) == ExecMode.APPROVAL


def is_auto(mode: Optional[str]) -> bool:
    return normalize(mode) == ExecMode.AUTO


def label(mode: Optional[str]) -> str:
    return MODE_LABELS[normalize(mode)]
