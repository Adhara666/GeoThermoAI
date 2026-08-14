"""
反思结果数据类

各角色的反思统一返回 `ReflectionResult`；确定性规则的结论**永远覆盖** LLM 结论，
`rule_hits` 记录命中的规则编号（如 `P2` / `D3` / `R3` / `E-R4`），供气泡与报告标注
「[规则] R3」以便追溯。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


class Action:
    """反思结论对应的下一步动作。"""

    PROCEED = "proceed"       # 放行，交总调度继续
    ASK = "ask"               # 反问用户，本轮不跑任何 Skill
    CHAT_ONLY = "chat_only"   # 判为纯聊天/问答，转流式对话
    ADJUST = "adjust"         # 改参数再来一轮（训练阶段内优化）
    STOP = "stop"             # 停止（接受当前结果或终止）
    REPLAN = "replan"         # 交回总调度发起 replan
    REWRITE = "rewrite"       # 打回重写（评估文本把关）

    ALL = (PROCEED, ASK, CHAT_ONLY, ADJUST, STOP, REPLAN, REWRITE)


@dataclass
class ReflectionResult:
    """一次反思的结论。"""

    ok: bool
    action: str = Action.PROCEED
    question: str = ""                                  # action=ask 时要问用户的话
    note: str = ""                                      # 一句话理由（中文）
    violations: List[str] = field(default_factory=list)  # 未通过的检查项（人话）
    suggestions: List[str] = field(default_factory=list)  # 建议动作，按可行性排序
    rule_hits: List[str] = field(default_factory=list)   # 命中的确定性规则编号
    data: Dict[str, Any] = field(default_factory=dict)   # 结构化附带信息

    def __post_init__(self):
        if self.action not in Action.ALL:
            raise ValueError(f"未知的反思动作：{self.action}")

    @classmethod
    def passed(cls, note: str = "", **kwargs) -> "ReflectionResult":
        return cls(ok=True, action=Action.PROCEED, note=note, **kwargs)

    @classmethod
    def ask(cls, question: str, note: str = "", **kwargs) -> "ReflectionResult":
        return cls(ok=False, action=Action.ASK, question=question, note=note, **kwargs)

    @classmethod
    def chat_only(cls, note: str = "", **kwargs) -> "ReflectionResult":
        return cls(ok=True, action=Action.CHAT_ONLY, note=note, **kwargs)

    @classmethod
    def failed(cls, action: str, note: str = "", **kwargs) -> "ReflectionResult":
        return cls(ok=False, action=action, note=note, **kwargs)
