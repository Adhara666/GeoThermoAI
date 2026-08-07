"""
流程状态机 RunState（技术方案 2.2 / 3.5）

记录：当前阶段、replan 计数、暂停点、断点（阶段内回退用）、审批选择轨迹、调优轮次。
只有总调度写它，子 Agent 只读。
"""

import time
from typing import Any, Dict, List, Optional

from .agent_config import REPLAN_MAX
from .exec_mode import DEFAULT_EXEC_MODE, normalize as normalize_exec_mode


class Stage:
    PLANNING = "planning"
    DATA = "data"
    TRAIN = "train"
    EVAL = "eval"
    DONE = "done"
    STOPPED = "stopped"


STAGE_ORDER = (Stage.PLANNING, Stage.DATA, Stage.TRAIN, Stage.EVAL, Stage.DONE)

# 阶段中文名（气泡与报告用）
STAGE_LABELS = {
    Stage.PLANNING: "需求规划",
    Stage.DATA: "数据准备",
    Stage.TRAIN: "模型训练",
    Stage.EVAL: "结果评估",
    Stage.DONE: "已完成",
    Stage.STOPPED: "已停止",
}


class RunState:
    """一次任务运行的流程状态。"""

    def __init__(self, exec_mode: str = DEFAULT_EXEC_MODE, replan_max: int = REPLAN_MAX,
                 plan_id: str = "", conv_id: str = "", project_id: str = ""):
        self.exec_mode = normalize_exec_mode(exec_mode)
        self.replan_max = int(replan_max)
        self.plan_id = plan_id
        self.conv_id = conv_id
        self.project_id = project_id

        self.stage = Stage.PLANNING
        self.replan_count = 0
        self.replan_reasons: List[str] = []
        self.pause_node = ""
        self.resume_point = ""          # 阶段内回退的断点（如 pair_selection）
        self.approval_choices: Dict[str, str] = {}
        self.tuning_rounds = 0
        self.stopped_reason = ""
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # ── 阶段 ───────────────────────────────────────────────────────

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    def advance_from_skill(self, skill_name: str) -> None:
        """按 skill 所属阶段推进状态（阶段只前进，不因单步失败倒退）。"""
        from ..plan_schema import stage_of

        mapped = stage_of(skill_name)
        if mapped in STAGE_ORDER and STAGE_ORDER.index(mapped) >= STAGE_ORDER.index(
                self.stage if self.stage in STAGE_ORDER else Stage.PLANNING):
            self.stage = mapped

    # ── replan ─────────────────────────────────────────────────────

    def can_replan(self) -> bool:
        """是否还允许自动 replan（技术方案 2.4 规则 4 / 规则 P6）。"""
        return self.replan_count < self.replan_max

    def note_replan(self, reason: str) -> int:
        """记一次 replan，返回记后的次数。"""
        self.replan_count += 1
        if reason:
            self.replan_reasons.append(reason)
        return self.replan_count

    def last_replan_reason(self) -> str:
        return self.replan_reasons[-1] if self.replan_reasons else ""

    # ── 暂停 / 审批 ────────────────────────────────────────────────

    def mark_paused(self, node: str) -> None:
        self.pause_node = node

    def clear_pause(self) -> None:
        self.pause_node = ""

    def record_approval(self, node: str, option_id: str) -> None:
        if node:
            self.approval_choices[node] = option_id

    def set_resume_point(self, point: str) -> None:
        """设置阶段内回退断点（如「重选影像组合」回到 pair_selection）。"""
        self.resume_point = point

    def take_resume_point(self) -> str:
        """取出并清空断点（一次性消费）。"""
        point, self.resume_point = self.resume_point, ""
        return point

    # ── 调优轮次 ───────────────────────────────────────────────────

    def next_tuning_round(self) -> int:
        self.tuning_rounds += 1
        return self.tuning_rounds

    def stop(self, reason: str = "") -> None:
        self.stage = Stage.STOPPED
        self.stopped_reason = reason

    # ── 序列化 ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exec_mode": self.exec_mode,
            "stage": self.stage,
            "plan_id": self.plan_id,
            "conv_id": self.conv_id,
            "project_id": self.project_id,
            "replan_count": self.replan_count,
            "replan_reasons": list(self.replan_reasons),
            "pause_node": self.pause_node,
            "resume_point": self.resume_point,
            "approval_choices": dict(self.approval_choices),
            "tuning_rounds": self.tuning_rounds,
            "stopped_reason": self.stopped_reason,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RunState":
        data = data if isinstance(data, dict) else {}
        state = cls(
            exec_mode=data.get("exec_mode", DEFAULT_EXEC_MODE),
            replan_max=data.get("replan_max", REPLAN_MAX),
            plan_id=data.get("plan_id", ""),
            conv_id=data.get("conv_id", ""),
            project_id=data.get("project_id", ""),
        )
        state.stage = data.get("stage", Stage.PLANNING)
        state.replan_count = int(data.get("replan_count", 0) or 0)
        state.replan_reasons = list(data.get("replan_reasons") or [])
        state.pause_node = data.get("pause_node", "")
        state.resume_point = data.get("resume_point", "")
        state.approval_choices = dict(data.get("approval_choices") or {})
        state.tuning_rounds = int(data.get("tuning_rounds", 0) or 0)
        state.stopped_reason = data.get("stopped_reason", "")
        state.started_at = data.get("started_at", state.started_at)
        return state
