"""
执行引擎扩展点协议（技术方案 2.5 / 10.2）

`StepDecision` 与 `StageHooks` 的**规范定义在本文件**，`core/agent/executor.py`
从这里导入（技术方案 10.2 的代码片段以本文件为准）。

依赖方向固定为 `executor → orchestrator.hooks`，本文件不导入 executor，
避免循环依赖。
"""

from typing import Any, Dict, List, Optional


class StepDecision:
    """执行引擎在每个钩子点接受的决策。"""

    CONTINUE = "continue"   # 继续下一步
    RETRY = "retry"         # 用 new_params 重跑当前步（训练调优用）
    PAUSE = "pause"         # 弹审批节点
    ABORT = "abort"         # 停止，向用户说明
    REPLAN = "replan"       # 交回总调度发起 replan

    ACTIONS = (CONTINUE, RETRY, PAUSE, ABORT, REPLAN)

    def __init__(self, action: str = CONTINUE, new_params: Optional[dict] = None,
                 reason: str = "", payload: Optional[dict] = None,
                 message: str = ""):
        if action not in self.ACTIONS:
            raise ValueError(f"未知的执行决策：{action}")
        self.action = action
        self.new_params = dict(new_params or {})
        self.reason = reason
        self.payload = dict(payload or {})
        self.message = message

    # 便捷构造器（可读性优于到处写字符串常量）
    @classmethod
    def cont(cls) -> "StepDecision":
        return cls(cls.CONTINUE)

    @classmethod
    def retry(cls, new_params: dict, reason: str = "") -> "StepDecision":
        return cls(cls.RETRY, new_params=new_params, reason=reason)

    @classmethod
    def abort(cls, reason: str = "", message: str = "") -> "StepDecision":
        return cls(cls.ABORT, reason=reason, message=message)

    @classmethod
    def replan(cls, reason: str = "", payload: Optional[dict] = None,
               message: str = "") -> "StepDecision":
        return cls(cls.REPLAN, reason=reason, payload=payload, message=message)

    @classmethod
    def pause(cls, payload: dict, reason: str = "") -> "StepDecision":
        return cls(cls.PAUSE, payload=payload, reason=reason)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"StepDecision({self.action}, reason={self.reason!r})"


class StageHooks:
    """执行引擎扩展点协议；默认实现全部短路（等价于无 hooks）。"""

    def before_step(self, skill_name: str, step: dict, ctx: Any) -> Optional[StepDecision]:
        """执行前：可就地改 `step["params"]`，或返回 PAUSE/ABORT/REPLAN 打断。"""
        return None

    def after_step(self, skill_name: str, result: Any, ctx: Any) -> Optional[StepDecision]:
        """执行后：返回 StepDecision 决定 继续/重跑/暂停/中止/replan。"""
        return None

    def rank_pairs(self, pairs: List[dict], ctx: Any) -> Optional[List[dict]]:
        """影像配对排序与推荐标记；返回 None 表示不介入（保持数据源原顺序）。"""
        return None

    def select_pair(self, pairs: List[dict], ctx: Any) -> Optional[dict]:
        """完全执行模式下代替用户选配对；返回 None 表示交还给原有交互逻辑。"""
        return None

    def on_no_pair(self, detail: dict, ctx: Any) -> Optional[StepDecision]:
        """搜索不到合格配对时的处置；返回 None 表示沿用原有「终止并报告」。"""
        return None
