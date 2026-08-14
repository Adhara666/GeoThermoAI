"""
分层反思

| 角色 | 反思强度 | 文件 |
|---|---|---|
| 规划 Agent | 轻量 | planner_rules.py（P1–P7） |
| 数据 Agent | 轻量 | data_rules.py（D1–D7） |
| 训练 Agent | 主反思 | train_rules.py（R1–R7 七规则兜底） |
| 评估 Agent | 轻量 | eval_rules.py（E-R1–E-R7 表述与结构把关） |

统一约定：确定性规则的结论**永远覆盖** LLM 结论。
"""

from .result import Action, ReflectionResult

__all__ = ["Action", "ReflectionResult"]
