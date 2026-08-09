"""
结果生成与评估 Agent（技术方案第 7 章）

覆盖 `tcr_compute` + `lst_export` + `accuracy_eval` 三个 Skill，加上**基于记忆先验的
结果解读**。评估方法完全复用现有实现，本 Agent 不重算任何指标，只读结果。

轻反思是「表述把关」：确定性规则 E-R1 – E-R6 命中即打回重写（最多 2 次），
两次仍不过则降级为模板化报告，绝不输出未通过检查的文案。
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..orchestrator import approval as approval_proto
from ..orchestrator.approval import Node, Option
from ..orchestrator.exec_mode import is_auto
from ..orchestrator.hooks import StepDecision
from ..reflection import eval_rules
from ..reflection.train_rules import grade
from .base_role import RoleAgent
from .prompts import evaluation as eval_prompts

logger = logging.getLogger(__name__)


def _flatten_metrics(source: Optional[dict]) -> Dict[str, Any]:
    """把 `{"n_samples":…, "metrics": {"R2":…}}` 摊平成一层。

    `core/evaluation.evaluate_independent_prediction` 把 R²/RMSE/MAE/MB 放在
    `metrics` 子字典里，而 n_samples 在顶层。不摊平的话报告里会出现
    「样本数有值、决定系数却显示未计算」这种自相矛盾的输出。
    """
    data = dict(source or {})
    nested = data.pop("metrics", None)
    if isinstance(nested, dict):
        for key, value in nested.items():
            data.setdefault(key, value)
    return data


def _cut_at_sentence(text: str, limit: int) -> str:
    """按句子边界截断，绝不在句子中间断开（气泡不能出现半截话）。"""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    head = body[:limit]
    for sep in ("。", "；", "！", "？"):
        pos = head.rfind(sep)
        if pos > 0:
            return head[:pos + 1]
    return head.rstrip("，、 ") + "……"


class EvalAgent(RoleAgent):
    """结果读取 + 解读生成 + 表述把关 + 工作流写回。"""

    role = "eval"
    role_name = "评估"

    def __init__(self, assistant, memory_manager=None, project_id: str = "",
                 on_log=None, on_thinking=None):
        super().__init__(assistant, memory_manager=memory_manager,
                         project_id=project_id, on_log=on_log,
                         on_thinking=on_thinking)
        self.bundle: Dict[str, Any] = {}
        self.report = ""
        self.degraded = False
        self.degrade_reason = ""
        self.rewrites = 0

    # ── 执行引擎回调 ───────────────────────────────────────────────

    def on_eval_step(self, skill_name: str, result: Any, ctx: Any,
                     hooks=None) -> StepDecision:
        data = getattr(result, "data", None)
        data = data if isinstance(data, dict) else {}

        if skill_name == "tcr_compute":
            self.bundle["tcr_statistics"] = data.get("tcr_statistics") or {}
            return StepDecision.cont()
        if skill_name == "lst_export":
            # total_valid 在 lst_export 结果的**顶层**，不在 stats 里（见其 result_data），
            # 合并进来才能在报告里给出有效像元数
            stats = dict(data.get("stats") or {})
            if data.get("total_valid") is not None:
                stats.setdefault("total_valid", data["total_valid"])
            self.bundle["lst_stats"] = stats
            self.bundle["image_size"] = data.get("image_size") or {}
            return StepDecision.cont()
        if skill_name != "accuracy_eval":
            return StepDecision.cont()

        self._collect(ctx, data)
        self.report = self.build_report(ctx)
        ctx.emit(self.report + "\n")
        self._write_workflow(ctx, hooks)

        exec_mode = getattr(hooks, "exec_mode", "")
        if hooks is not None:
            hooks.final_report = self.report
        if not is_auto(exec_mode):
            payload = approval_proto.build_final_report(self._final_summary())
            choice = hooks._ask(payload) if hooks is not None else None
            if choice is None:
                return StepDecision.pause(payload, reason="等待用户确认最终报告")
            option_id = choice.get("option_id", Option.DONE)
            if getattr(hooks, "run_state", None) is not None:
                hooks.run_state.record_approval(Node.FINAL_REPORT, option_id)
            try:
                ctx.exp_state.setdefault("approval_choices", {})[Node.FINAL_REPORT] = option_id
            except Exception:
                pass
            if option_id == Option.MORE_ANALYSIS:
                ctx.emit("好的，请说明你想做的分析。\n")

        # ── 结果后处理（可选）：询问是否对 10m LST 空洞做填补 ──
        return self._handle_postprocess(ctx, hooks)

    # ── 读取真实结果（不重算任何指标） ─────────────────────────────

    def _collect(self, ctx: Any, accuracy_data: Dict[str, Any]) -> None:
        rf = ctx.exp_state.get("rf_data") or {}
        self.bundle["test_metrics"] = rf.get("test_metrics") or {}
        self.bundle["train_metrics"] = (rf.get("train_metrics") or {}).get("train") \
            or rf.get("train_metrics") or {}
        self.bundle["feature_importance"] = rf.get("feature_importance") or []
        self.bundle["params"] = rf.get("params") or {}

        results_dir = getattr(ctx, "results_dir", "") or ""
        indep = rf.get("independent_prediction") or self._read_json(
            os.path.join(results_dir, "independent_prediction.json"))
        self.bundle["independent_prediction"] = _flatten_metrics(indep)

        closure_full = accuracy_data.get("closure_metrics") or self._read_json(
            os.path.join(results_dir, "coarse_constraint_closure.json"))
        closure_full = closure_full or {}
        closure = dict(closure_full.get("closure") or {})
        if closure_full.get("value_range"):
            closure["value_range"] = closure_full["value_range"]
        self.bundle["closure"] = closure

        for source in (self.bundle["test_metrics"], indep or {}):
            if isinstance(source, dict) and source.get("r2_null_reason"):
                self.bundle["r2_null_reason"] = source["r2_null_reason"]

        plan = getattr(ctx, "plan", {}) or {}
        self.bundle["region"] = (plan.get("region") or {}).get("name", "")
        self.bundle["time_range"] = plan.get("time_range") or {}

    @staticmethod
    def _read_json(path: str) -> Dict[str, Any]:
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[eval] 结果文件读取失败（按缺失处理）: {e}")
            return {}

    # ── 解读生成 + 表述把关 ────────────────────────────────────────

    def build_report(self, ctx: Any) -> str:
        expected = grade((self.bundle.get("test_metrics") or {}).get("R2"))
        knowledge = self._knowledge_block()
        facts = self.facts_text()

        draft = self._generate(knowledge, facts, expected)
        last_hits: List[str] = []
        for attempt in range(eval_rules.EVAL_REWRITE_MAX + 1):
            if not draft:
                break
            reflection = eval_rules.check(draft, bundle=self.bundle,
                                          expected_grade=expected,
                                          require_structure=True)
            if reflection.ok:
                self.rewrites = attempt
                return draft
            last_hits = list(reflection.rule_hits)
            # 详细违规项只进日志面板（气泡红线 4），且可能含被编造的数字，不进报告
            self.log(f"表述检查未通过（{'/'.join(last_hits)}），第 {attempt + 1} 次重写；"
                     f"具体：{'；'.join(reflection.violations[:4])}")
            if attempt >= eval_rules.EVAL_REWRITE_MAX:
                break
            draft = self._rewrite(knowledge, facts, draft, reflection.violations, expected)

        self.degraded = True
        self.degrade_reason = self._degrade_reason(bool(draft), last_hits)
        self.log(f"降级为模板化报告：{self.degrade_reason}")
        return self.template_report(expected)

    @staticmethod
    def _degrade_reason(had_draft: bool, rule_hits: List[str]) -> str:
        """降级原因的一句话说明。

        进报告的内容只放**规则中文短名**：既不引用可能被编造的原文，
        也避免「E-R2」这类编号里的数字被 E-R1 当成指标数值（气泡红线 1 与 3）。
        """
        if not had_draft:
            return "大模型没有返回解读内容（接口不可用或生成预算耗尽）"
        if rule_hits:
            labels = "、".join(eval_rules.rule_labels(rule_hits))
            return f"自动解读连续两次未通过表述检查，未过项为{labels}"
        return "自动解读未通过表述检查"

    def _generate(self, knowledge: str, facts: str, expected: str) -> str:
        # 实现期修订 v1.5：关闭思考链（thinking={"type":"disabled"}）。
        # 结果解读是「单步结构化输出」任务，不需要推理链；思考链的 reasoning_content
        # 计入 max_tokens，会占满预算导致正文为空而降级。数值准确性由 E-R1~E-R7
        # 确定性规则兜底，不依赖模型思考。budget 保留 8192 作双保险。
        text = self.call_text(eval_prompts.report_prompt(knowledge, facts, expected),
                             "请撰写结果说明。", temperature=0.2, max_tokens=8192,
                             thinking={"type": "disabled"})
        from .base_role import is_api_failure

        return "" if is_api_failure(text) else text.strip()

    def _rewrite(self, knowledge: str, facts: str, draft: str, violations: List[str],
                 expected: str) -> str:
        # 与 _generate 同因：关闭思考链，只输出重写正文
        text = self.call_text(
            eval_prompts.rewrite_prompt(knowledge, facts, draft,
                                        "\n".join(f"- {v}" for v in violations), expected),
            "请按修改要求重写。", temperature=0.1, max_tokens=8192,
            thinking={"type": "disabled"})
        from .base_role import is_api_failure

        return "" if is_api_failure(text) else text.strip()

    def _knowledge_block(self) -> str:
        block = self.memory_block("评估 协议 闭合 精度 解读 禁用表述")
        if block:
            return block
        from ...memory.knowledge_eval import eval_knowledge_text

        return eval_knowledge_text()

    # ── 事实清单与模板化报告（降级兜底） ───────────────────────────

    def facts_text(self) -> str:
        """逐项列出真实数值，作为 LLM 的唯一数字来源。"""
        from .. import presentation

        test = self.bundle.get("test_metrics") or {}
        indep = self.bundle.get("independent_prediction") or {}
        closure = self.bundle.get("closure") or {}
        cm = closure.get("metrics") or {}
        stats = self.bundle.get("lst_stats") or {}
        size = self.bundle.get("image_size") or {}
        value_range = closure.get("value_range") or {}

        lines = [
            f"- 研究区：{self.bundle.get('region') or '未记录'}",
            f"- 时间范围：{(self.bundle.get('time_range') or {}).get('start', '')} 至 "
            f"{(self.bundle.get('time_range') or {}).get('end', '')}",
            f"- 产品分辨率：十米",
            f"- 有效像元数：{presentation.fmt_count(stats.get('total_valid'))} 个",
            f"- 影像尺寸：{presentation.fmt_count(size.get('height'))} 行 × "
            f"{presentation.fmt_count(size.get('width'))} 列",
            f"- 有效像元占比：{presentation.fmt_percent(stats.get('valid_percent'))}",
            f"- 测试集决定系数：{presentation.fmt_num(test.get('R2'))}",
            f"- 测试集均方根误差：{presentation.fmt_num(test.get('RMSE'))} 开尔文",
            f"- 独立预测决定系数：{presentation.fmt_num(indep.get('R2'))}",
            f"- 独立预测均方根误差：{presentation.fmt_num(indep.get('RMSE_K'))} 开尔文",
            f"- 独立预测样本数：{presentation.fmt_count(indep.get('n_samples'))} 个",
            f"- 闭合平均偏差：{presentation.fmt_num(cm.get('MB_K'))} 开尔文",
            f"- 闭合平均绝对误差：{presentation.fmt_num(cm.get('MAE_K'))} 开尔文",
            f"- 闭合均方根误差：{presentation.fmt_num(cm.get('RMSE_K'))} 开尔文",
            f"- 闭合匹配格网数：{presentation.fmt_count(closure.get('n_matched_cells'))} 个",
        ]
        if value_range:
            lines.append(
                f"- 低端温差：{presentation.fmt_num(value_range.get('low_end_difference_K'))}"
                f" 开尔文；高端温差："
                f"{presentation.fmt_num(value_range.get('high_end_difference_K'))} 开尔文")
        tcr = self.bundle.get("tcr_statistics") or {}
        if tcr:
            lines.append(
                f"- 热约束残差平均值：{presentation.fmt_num(tcr.get('mean'))} 开尔文；"
                f"标准差：{presentation.fmt_num(tcr.get('std'))} 开尔文；"
                f"有效格网：{presentation.fmt_count(tcr.get('n_valid_blocks'))} 个")
        top = self._top_features()
        if top:
            lines.append(f"- 贡献最大的特征：{top}")
        if self.bundle.get("r2_null_reason"):
            lines.append(f"- 决定系数无法计算的原因：{self.bundle['r2_null_reason']}")
        return "\n".join(lines)

    def template_report(self, expected_grade: str) -> str:
        """模板化报告：只填数字，不含任何 LLM 生成的评价性语句（技术方案 7.3）。"""
        from .. import presentation
        from ...memory.knowledge_eval import EVAL_SEED_ITEMS

        closure_note = next((i["content"] for i in EVAL_SEED_ITEMS if i["id"] == "E01"), "")
        test = self.bundle.get("test_metrics") or {}
        lines = [
            "**结果说明（模板化）**",
            "",
            "产品概况",
            self.facts_text(),
            "",
            "模型精度",
            f"- 按分档基准，本次测试集精度评级为{expected_grade}"
            f"（决定系数 {presentation.fmt_num(test.get('R2'))}）。",
            "",
            "闭合情况",
            "- 闭合指标是十米结果回聚合到三十米产品格网的算术均值闭合度，"
            "不是十米独立精度，也不代表能量或辐射守恒。",
            "",
            "局限性",
            "- 本段为模板化说明，未包含自动生成的解读文字。",
            f"- 降级原因：{self.degrade_reason or '未记录'}。详细的未通过项见日志面板。",
            f"- 口径依据：{_cut_at_sentence(closure_note, 180)}",
        ]
        return "\n".join(lines)

    # ── 结果后处理（可选，升级点：10m LST 空洞填补） ─────────────

    def _handle_postprocess(self, ctx: Any, hooks=None) -> StepDecision:
        """全流程结果生成后：询问是否对 10m LST 空洞做填补。

        由我批准模式：弹审批卡片让用户选择（执行填洞 / 不需要结束流程）；
        完全执行模式：默认跳过不询问，并在气泡中提示默认不执行、需要时可告知。
        """
        exec_mode = getattr(hooks, "exec_mode", "") if hooks is not None else ""

        if is_auto(exec_mode):
            # 完全执行模式：默认不执行结果后处理，完成后提示用户可随时告知
            try:
                ctx.exp_state.setdefault("approval_choices", {})[Node.POSTPROCESS] = Option.SKIP_POSTPROCESS
            except Exception:
                pass
            ctx.emit("（本次为完全执行模式，默认不执行结果后处理；"
                     "如需要无空洞的 10m 地表温度产品，告诉我即可对结果做空洞填补。）\n")
            return StepDecision.cont()

        summary = self._postprocess_summary()
        payload = approval_proto.build_postprocess(summary)
        choice = hooks._ask(payload) if hooks is not None else None
        if choice is None:
            return StepDecision.pause(payload, reason="等待用户确认是否结果后处理")
        option_id = choice.get("option_id", Option.SKIP_POSTPROCESS)
        if getattr(hooks, "run_state", None) is not None:
            hooks.run_state.record_approval(Node.POSTPROCESS, option_id)
        try:
            ctx.exp_state.setdefault("approval_choices", {})[Node.POSTPROCESS] = option_id
        except Exception:
            pass

        if option_id != Option.RUN_POSTPROCESS:
            ctx.emit("好的，保留当前带空洞的原始 10m 地表温度产品，本次流程到此结束。\n")
            return StepDecision.cont()
        return self._run_gapfill(ctx)

    def _postprocess_summary(self) -> str:
        stats = self.bundle.get("lst_stats") or {}
        valid_pct = stats.get("valid_percent")
        head = "当前 10m 地表温度产品存在因云像元扣除造成的空洞。"
        if valid_pct is not None:
            head = f"当前 10m 地表温度产品有效像元占比约 {valid_pct:.1f}%，其余为云像元造成的空洞。"
        return (head + "是否对这些空洞做填补，生成无空洞的 10m 地表温度产品？"
                "（只估计空洞像元，不改变无云区数值）")

    def _run_gapfill(self, ctx: Any) -> StepDecision:
        """执行 lst_gapfill skill（空洞填补），结果写入气泡与阶段清单。"""
        skill = ctx.registry.get("lst_gapfill")
        if skill is None:
            ctx.emit("结果后处理组件未注册，本次跳过。\n")
            return StepDecision.cont()
        try:
            from ..executor import build_skill_paths, pair_dates

            dates = pair_dates(ctx.exp_state.get("pair") or {})
            paths = build_skill_paths(
                getattr(ctx, "raw_dir", ""), getattr(ctx, "processed_dir", ""),
                getattr(ctx, "results_dir", ""), dates=dates,
                project_dir=getattr(ctx, "project_dir", ""),
            )
            params = paths.get("lst_gapfill") or {}
        except Exception as e:
            ctx.emit(f"结果后处理路径解析失败：{e}\n")
            return StepDecision.cont()

        ctx.emit("开始结果后处理：填补 10m 地表温度产品中的云像元空洞…\n")
        try:
            result = skill.execute(
                params,
                progress_callback=lambda sn, pct, msg: None,
                log_callback=lambda lvl, msg: self.log(msg),
            )
        except Exception as e:
            ctx.emit(f"结果后处理执行失败：{e}\n")
            return StepDecision.cont()

        try:
            ctx.exp_state.setdefault("step_success", {})["lst_gapfill"] = bool(getattr(result, "success", False))
        except Exception:
            pass

        if getattr(result, "success", False):
            data = getattr(result, "data", None)
            data = data if isinstance(data, dict) else {}
            ctx.emit(getattr(result, "message", "结果后处理完成") + "\n")
            filled_tif = data.get("filled_tif", "")
            mask_tif = data.get("mask_tif", "")
            ctx.emit(f"无空洞的 10m 地表温度产品已生成（{filled_tif}）"
                     f"{f'，空洞掩膜：{mask_tif}' if mask_tif else ''}\n")
        else:
            ctx.emit(f"结果后处理未完成：{getattr(result, 'message', '未知原因')}\n")
        return StepDecision.cont()

    def _final_summary(self) -> str:
        from .. import presentation

        test = self.bundle.get("test_metrics") or {}
        closure = (self.bundle.get("closure") or {}).get("metrics") or {}
        return (f"测试集决定系数 {presentation.fmt_num(test.get('R2'))}，"
                f"闭合平均偏差 {presentation.fmt_num(closure.get('MB_K'))} 开尔文。"
                f"产品与报告已生成。")

    def _top_features(self, limit: int = 3) -> str:
        items = [i for i in (self.bundle.get("feature_importance") or [])
                 if isinstance(i, dict)]
        items.sort(key=lambda i: i.get("importance", 0), reverse=True)
        labels = {"NDVI": "植被指数", "NDBI": "建筑指数", "NDWI": "水体指数",
                  "DEM": "高程", "TTRI": "地形热响应指数", "NIR": "近红外",
                  "SWIR1": "短波红外", "R": "红光", "G": "绿光", "B": "蓝光"}
        return "、".join(labels.get(i.get("feature"), str(i.get("feature")))
                        for i in items[:limit])

    # ── 工作流写回记忆（技术方案 7.5 / 8.3） ──────────────────────

    def _write_workflow(self, ctx: Any, hooks=None) -> None:
        from ...memory import workflow_experience

        if self.memory is None or not self.project_id:
            return
        test_r2 = (self.bundle.get("test_metrics") or {}).get("R2")
        failed = ctx.exp_state.get("failed")
        status = "failed" if failed else "success"
        if not workflow_experience.should_write(status=status,
                                                eval_passed=not self.degraded,
                                                test_r2=test_r2):
            self.log("未满足工作流写回的三个条件，跳过写入")
            return

        closure = (self.bundle.get("closure") or {}).get("metrics") or {}
        pair = dict(ctx.exp_state.get("pair") or {})
        pair["selected_by"] = ctx.exp_state.get("pair_selected_by", "")
        plan = getattr(ctx, "plan", {}) or {}
        time_range = plan.get("time_range") or {}
        record = workflow_experience.build_record(
            project_id=self.project_id,
            experiment_id="",
            conv_id=getattr(ctx, "conv_id", ""),
            region=(plan.get("region") or {}).get("name", "")
            or self.bundle.get("region", ""),
            date_range=[time_range.get("start", ""), time_range.get("end", "")],
            exec_mode=getattr(hooks, "exec_mode", "") or ctx.exp_state.get("exec_mode", ""),
            pair=pair,
            final_params=ctx.exp_state.get("final_params") or self.bundle.get("params") or {},
            tuning_trace=ctx.exp_state.get("tuning_trace") or [],
            metrics={"test_r2": test_r2,
                     "rmse": (self.bundle.get("test_metrics") or {}).get("RMSE"),
                     "closure_mb": closure.get("MB_K"),
                     "closure_mae": closure.get("MAE_K")},
            approval_choices=ctx.exp_state.get("approval_choices") or {},
            verdict="good",
        )
        self.memory.save_workflow(self.project_id, record)
        ctx.exp_state["eval_verdict"] = "good"
        self.log(f"本次流程已写入可复用工作流经验：{record['workflow_id']}")
