"""
总调度编排层（技术方案第 3 章）

- exec_mode：执行模式常量与归一化
- agent_config：`settings.agent` 特性开关与可配置项
- approval：审批节点定义、暂停载荷构造、恢复结果解析
- run_state：流程状态机（阶段 / replan 计数 / 暂停点 / 断点）
- hooks：StepDecision + StageHooks 协议（执行引擎的扩展点）
"""

from . import agent_config, approval, exec_mode
from .exec_mode import DEFAULT_EXEC_MODE, ExecMode
from .hooks import StageHooks, StepDecision
from .run_state import RunState, Stage

__all__ = [
    "agent_config",
    "approval",
    "exec_mode",
    "ExecMode",
    "DEFAULT_EXEC_MODE",
    "StageHooks",
    "StepDecision",
    "RunState",
    "Stage",
]
