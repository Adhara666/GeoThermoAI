"""
角色编排钩子（把三个执行 Agent 接到执行引擎的扩展点上）

技术方案 2.2 的「Solve」侧实现：执行引擎在每个钩子点回调这里，本类再委托给
数据 / 训练 / 评估 Agent，并按执行模式决定「继续 / 暂停审批 / 重跑本步 / 中止 / 交回 replan」。

replan 只能由总调度发起（技术方案 2.4 规则 1）：本类只把 `replan_request` 记下来，
由 `GeoThermoAgent.process_command_with_roles` 决定是否真的交给规划 Agent。
"""

import logging
from typing import Any, Dict, List, Optional

from .. import plan_schema, presentation
from . import approval as approval_proto
from .approval import Node, Option
from .exec_mode import is_auto
from .hooks import StageHooks, StepDecision

logger = logging.getLogger(__name__)

# no_pair / data_quality 节点上，各选项对应的 replan 提示
_REPLAN_HINTS = {
    Option.RELAX_CLOUD: "放宽云量阈值后重新搜索影像",
    Option.WIDEN_TIME: "扩大时间窗后重新搜索影像",
    Option.CHANGE_SOURCE: "更换数据源后重新搜索影像",
    Option.RESELECT_PAIR: "重新选择影像组合",
    Option.REPLAN: "用户要求更换时间或地区",
}


class RoleHooks(StageHooks):
    """把角色 Agent 接到执行引擎上的适配器。"""

    def __init__(self, *, exec_mode: str, run_state, pause_callback=None,
                 data_agent=None, train_agent=None, eval_agent=None,
                 agent_cfg: Optional[dict] = None, on_log=None,
                 data_probes: Optional[dict] = None):
        self.exec_mode = exec_mode
        self.run_state = run_state
        self.pause_callback = pause_callback
        self.data_agent = data_agent
        self.train_agent = train_agent
        self.eval_agent = eval_agent
        self.agent_cfg = dict(agent_cfg or {})
        self._on_log = on_log
        # 数据检查探针注入点（生产环境留空走真实文件系统，合成测试可替换）
        self.data_probes = dict(data_probes or {})

        # 供总调度读取的结果
        self.replan_request: Optional[Dict[str, Any]] = None
        self.resume_point = ""
        self.pipeline_data: Dict[str, Any] = {}
        self.ranked_pairs: List[dict] = []
        self.final_report = ""

    # ── 工具 ───────────────────────────────────────────────────────

    def log(self, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(f"  [orchestrator] {message}\n")
            except Exception:
                pass

    def _ask(self, payload: dict) -> Optional[Dict[str, Any]]:
        """弹审批节点并等待用户选择；返回 None 表示挂起（超时或对话被删）。"""
        if not self.pause_callback:
            return None
        response = self.pause_callback(payload)
        if not isinstance(response, dict) or response.get("paused"):
            return None
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    def _request_replan(self, reason: str, payload: Optional[dict] = None) -> StepDecision:
        self.replan_request = {"reason": reason, "payload": dict(payload or {})}
        return StepDecision.replan(reason=reason, payload=payload)

    # ── 影像配对 ───────────────────────────────────────────────────

    def rank_pairs(self, pairs: List[dict], ctx: Any) -> Optional[List[dict]]:
        if self.data_agent is None:
            return None
        ranked = self.data_agent.rank(pairs)
        self.ranked_pairs = ranked
        try:
            ctx.exp_state["pair_candidates"] = self.data_agent.pair_candidates_digest(ranked)
        except Exception:
            pass
        return ranked

    def select_pair(self, pairs: List[dict], ctx: Any) -> Optional[dict]:
        """完全执行模式自动选最高分；由我批准模式交回配对卡片让用户决定。"""
        if self.data_agent is None or not is_auto(self.exec_mode):
            return None
        chosen = self.data_agent.choose(pairs)
        if chosen is None:
            return None
        index = next((i + 1 for i, p in enumerate(pairs)
                      if p.get("quality_score") == chosen.get("quality_score")), 1)
        ctx.emit(self.data_agent.auto_select_note(chosen, index) + "\n")
        return chosen

    def on_no_pair(self, detail: dict, ctx: Any) -> Optional[StepDecision]:
        """搜不到合格配对：两种模式都不许硬跑（技术方案 5.2）。"""
        summary = (self.data_agent.no_pair_summary(detail) if self.data_agent
                   else presentation.no_pair_reason(detail).strip())
        ctx.emit(summary + "\n")

        if is_auto(self.exec_mode) and self.run_state.can_replan():
            self.run_state.note_replan("搜索不到合格的影像组合")
            return self._request_replan("搜索不到合格的影像组合，需要放宽云量或扩大时间窗",
                                       {"relax_cloud": True, "widen_time": True})
        return self._approval_branch(Node.NO_PAIR, summary, ctx)

    # ── 执行后 ─────────────────────────────────────────────────────

    def after_step(self, skill_name: str, result: Any, ctx: Any) -> Optional[StepDecision]:
        self.run_state.advance_from_skill(skill_name)

        if skill_name == "data_pipeline" and getattr(result, "success", False):
            data = getattr(result, "data", None)
            self.pipeline_data = dict(data) if isinstance(data, dict) else {}

        stage = plan_schema.stage_of(skill_name)
        if stage == "data":
            if not getattr(result, "success", False):
                return self._data_step_failed(skill_name, result, ctx)
            if skill_name == "ttri_compute":
                return self._data_reflection(ctx)
            return StepDecision.cont()

        if not getattr(result, "success", False):
            # 执行引擎在调用本钩子前已经把结果摘要打进气泡，这里只给决策带理由，
            # 不再重复输出同一句话（message 留空）
            return StepDecision.abort(reason=presentation.summarize(skill_name, result))

        if stage == "train" and self.train_agent is not None:
            return self.train_agent.on_trained(result, ctx, hooks=self)
        if stage == "eval" and self.eval_agent is not None:
            return self.eval_agent.on_eval_step(skill_name, result, ctx, hooks=self)
        return StepDecision.cont()

    def before_step(self, skill_name: str, step: dict, ctx: Any) -> Optional[StepDecision]:
        if plan_schema.stage_of(skill_name) == "train" and self.train_agent is not None:
            return self.train_agent.before_train(step, ctx, hooks=self)
        return None

    # ── 数据阶段的失败与反思 ───────────────────────────────────────

    def _data_step_failed(self, skill_name: str, result: Any, ctx: Any) -> StepDecision:
        """数据阶段任一步失败 → 禁止往下跑（修复 1.5(4)）。

        结果摘要由执行引擎在调用本钩子前输出，这里不重复打印。
        """
        reason = presentation.summarize(skill_name, result)
        if is_auto(self.exec_mode) and self.run_state.can_replan():
            self.run_state.note_replan(reason)
            return self._request_replan(reason, {"failed_stage": skill_name})
        return self._approval_branch(Node.DATA_QUALITY, reason, ctx)

    def _data_reflection(self, ctx: Any) -> StepDecision:
        """三步数据阶段跑完后统一做一次轻反思。"""
        if self.data_agent is None:
            return StepDecision.cont()

        manifest = self._load_manifest(ctx.project_dir)
        reflection = self.data_agent.reflect(
            raw_dir=ctx.raw_dir, processed_dir=ctx.processed_dir,
            pipeline_data=self.pipeline_data, manifest=manifest,
            raster_probe=self.data_probes.get("raster_probe"),
            csv_probe=self.data_probes.get("csv_probe"),
            meta_probe=self.data_probes.get("meta_probe"),
        )
        if reflection.ok:
            ctx.emit("数据检查通过，进入模型训练\n")
            return StepDecision.cont()

        summary = self._reflection_summary(reflection)
        ctx.emit(summary + "\n")
        self.log(f"数据反思未通过：{'/'.join(reflection.rule_hits)}")

        if is_auto(self.exec_mode) and self.run_state.can_replan():
            self.run_state.note_replan(reflection.note)
            return self._request_replan(reflection.note,
                                       {"violations": reflection.violations})
        return self._approval_branch(Node.DATA_QUALITY, summary, ctx)

    @staticmethod
    def _reflection_summary(reflection) -> str:
        lines = [f"数据检查未通过：{reflection.note}"]
        if reflection.violations:
            lines.append("具体问题：" + "；".join(reflection.violations[:4]))
        if reflection.suggestions:
            lines.append("建议：" + "；".join(reflection.suggestions[:3]))
        if reflection.rule_hits:
            lines.append(presentation.rule_note("/".join(reflection.rule_hits), "判定不合格"))
        return "\n".join(lines)

    @staticmethod
    def _load_manifest(project_dir: str) -> Optional[dict]:
        if not project_dir:
            return None
        try:
            from ... import manifest as run_manifest

            return run_manifest.load_manifest(project_dir)
        except Exception as e:
            logger.warning(f"[orchestrator] 读取阶段清单失败（跳过该项检查）: {e}")
            return None

    # ── 审批分支（no_pair / data_quality 共用） ────────────────────

    def _approval_branch(self, node: str, summary: str, ctx: Any) -> StepDecision:
        builder = (approval_proto.build_no_pair if node == Node.NO_PAIR
                   else approval_proto.build_data_quality)
        payload = builder(summary)
        choice = self._ask(payload)
        if choice is None:
            return StepDecision.pause(payload, reason="等待用户在数据阶段做出选择")

        option_id = choice.get("option_id", Option.STOP)
        self.run_state.record_approval(node, option_id)
        try:
            ctx.exp_state.setdefault("approval_choices", {})[node] = option_id
        except Exception:
            pass

        if option_id == Option.STOP:
            # 气泡由 _handle_control_decision 统一输出，此处不重复打印
            return StepDecision.abort(reason="用户选择停止",
                                     message="已按你的要求停下，随时可以重新下达指令。")

        if option_id == Option.ACCEPT:
            # v1.2 新增：用户明确要求忽略检查未通过的提示，直接放行进入下一步
            message = "好的，已按你的确认继续执行后续步骤。"
            ctx.emit(message + "\n")
            return StepDecision.cont()

        if option_id == Option.RESELECT_PAIR:
            self.resume_point = Node.PAIR_SELECTION
            self.run_state.set_resume_point(Node.PAIR_SELECTION)
            return self._request_replan(_REPLAN_HINTS[Option.RESELECT_PAIR],
                                       {"reselect_pair": True})

        hint = _REPLAN_HINTS.get(option_id, "用户要求重新规划")
        self.run_state.note_replan(hint)
        payload_hint = {
            Option.RELAX_CLOUD: {"relax_cloud": True},
            Option.WIDEN_TIME: {"widen_time": True},
            Option.CHANGE_SOURCE: {"change_source": True},
        }.get(option_id, {"user_replan": True})
        return self._request_replan(hint, payload_hint)
