"""
结果生成与评估 Agent

覆盖 `tcr_compute` + `lst_export` + `accuracy_eval` 三个 Skill，加上**基于记忆先验的
结果解读**。评估方法完全复用现有实现，本 Agent 不重算任何指标，只读结果。

结果解读采用「系统组装 + LLM 定性短句」：
- 报告里的数字、评级、闭合口径句全部由系统从真实结果确定性渲染（assemble_report），
  不经过 LLM 之手，因此不存在「编造数字」「口径混用」的失败路径；
- LLM 只负责「关键特征与局限」一节 1~3 句定性说明（_qualitative_note），
  通过三项轻校验（无数字 / 无禁用表述 / 无极值负面判断）；不通过或接口不可用时
  用固定兜底句替代，**报告永远完整、永不降级**。
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from ..orchestrator import approval as approval_proto
from ..orchestrator.approval import Node, Option
from ..orchestrator.exec_mode import is_auto
from ..orchestrator.hooks import StepDecision
from .. import presentation
from ..reflection import eval_rules
from ..reflection.train_rules import grade
from .base_role import RoleAgent
from .prompts import evaluation as eval_prompts

logger = logging.getLogger(__name__)


# 定性说明未通过轻校验或接口不可用时的固定兜底句：报告永远完整，永不降级
_FALLBACK_NOTE = "本次产品的关键特征与局限需结合研究区、天气与云掩膜情况理解。"


def _normalize_note(note: str) -> str:
    """把 LLM 定性短句里的中文度量单位规范为 10m/30m 写法（先替换更长词防重叠）。"""
    return (note or "").replace("三十米", "30m").replace("十米", "10m").strip()


class EvalAgent(RoleAgent):
    """结果读取 + 系统组装报告 + LLM 定性短句 + 工作流写回。"""

    role = "eval"
    role_name = "评估"

    def __init__(self, assistant, memory_manager=None, project_id: str = "",
                 on_log=None, on_thinking=None):
        super().__init__(assistant, memory_manager=memory_manager,
                         project_id=project_id, on_log=on_log,
                         on_thinking=on_thinking)
        self.bundle: Dict[str, Any] = {}
        self.report = ""
        # 定性说明未通过轻校验或接口不可用时的原因（空串 = 采用了 LLM 原文）
        self.note_reason = ""

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
        # 结果说明与上一步之间用与步骤间一致的留白隔开
        ctx.emit(presentation.step_gap())
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
        closure_full = accuracy_data.get("closure_metrics") or self._read_json(
            os.path.join(results_dir, "coarse_constraint_closure.json"))
        closure_full = closure_full or {}
        closure = dict(closure_full.get("closure") or {})
        if closure_full.get("value_range"):
            closure["value_range"] = closure_full["value_range"]
        self.bundle["closure"] = closure

        for source in (self.bundle["test_metrics"],):
            if isinstance(source, dict) and source.get("r2_null_reason"):
                self.bundle["r2_null_reason"] = source["r2_null_reason"]

        plan = getattr(ctx, "plan", {}) or {}
        self.bundle["region"] = (plan.get("region") or {}).get("name", "")
        self.bundle["time_range"] = plan.get("time_range") or {}
        # 实际用到的影像组合（配对模式的卫星与日期；月度合成的代表日+composite=monthly）
        self.bundle["pair"] = (getattr(ctx, "exp_state", None) or {}).get("pair") or {}

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

    # ── 结果解读：系统组装 + LLM 定性短句 ──────────────────────────

    def build_report(self, ctx: Any) -> str:
        """生成最终结果说明：数字/评级/口径句由系统渲染，LLM 只补定性短句。

        不再有「检查 → 重写 → 降级」循环：报告永远是完整的，
        定性短句不通过轻校验就用固定兜底句，不会丢任何真实信息。
        """
        expected = grade((self.bundle.get("test_metrics") or {}).get("R2"))
        knowledge = self._knowledge_block()
        facts = self.facts_text()

        note = self._qualitative_note(knowledge, facts, expected)
        return self.assemble_report(expected, note)

    def _qualitative_note(self, knowledge: str, facts: str, expected: str) -> str:
        """让 LLM 生成「关键特征与局限」的 1~3 句定性说明。

        返回空串表示未采用 LLM 原文（接口不可用或未通过轻校验），
        由 assemble_report 用固定兜底句替代，报告仍然完整。
        """
        # 与规划/调优同因：关闭思考链，防止推理内容占满 max_tokens 截断输出
        text = self.call_text(eval_prompts.qualitative_prompt(knowledge, facts, expected),
                              "请撰写关键特征与局限的说明。", temperature=0.2,
                              max_tokens=1024, thinking={"type": "disabled"})
        from .base_role import is_api_failure

        if is_api_failure(text):
            self.note_reason = "大模型接口调用失败"
            self.log(f"定性说明未生成（{self.note_reason}），使用固定兜底句")
            return ""
        text = (text or "").strip()
        ok, reason = self._qualitative_ok(text)
        if not ok:
            self.note_reason = reason
            self.log(f"定性说明未通过轻校验（{reason}），使用固定兜底句")
            return ""
        self.note_reason = ""
        return text

    def _qualitative_ok(self, text: str) -> tuple:
        """定性短句的三项轻校验：无数字 / 无禁用表述 / 无极值负面判断。

        数字一票否决：定性说明本就不该出现任何数值（提示词已要求），
        出现数字即视为未遵守约定，宁可兜底也不冒险放行。
        唯一例外是分辨率单位写法 10m/30m（提示词要求用 10m/30m 而非 十米/三十米）：
        先剥掉 `数字+m` 的单位 token 再查数字，其余数字（指标数值）仍一票否决。
        """
        if len(text) < 10:
            return False, "内容过短"
        stripped_units = re.sub(r"\d+(?:\.\d+)?m(?![a-zA-Z])", "", text)
        if re.search(r"\d", stripped_units):
            return False, "出现了数字（定性说明不得引用数值）"
        closure = self.bundle.get("closure") or {}
        closure_ok = eval_rules.closure_is_normal(closure.get("metrics"))
        if eval_rules.check_disallowed(text, closure_ok):
            return False, "使用了禁用表述"
        if eval_rules.check_extreme_negativity(text, closure_ok):
            return False, "以极值差为依据下了负面结论"
        if not text.endswith(eval_rules.SENTENCE_ENDINGS):
            return False, "未以句子收尾符号结束"
        return True, ""

    def _knowledge_block(self) -> str:
        block = self.memory_block("评估 协议 闭合 精度 解读 禁用表述")
        if block:
            return block
        from ...memory.knowledge_eval import eval_knowledge_text

        return eval_knowledge_text()

    # ── 事实清单（系统渲染报告的数字来源） ────────────────────────

    def _data_time_line(self) -> str:
        """数据时间一行：配对模式写实际用到的 Landsat 卫星与日期、Sentinel-2 日期，
        月度合成模式写月份，都不再写「起止范围」；无配对信息时回退原时间范围。"""
        from .. import presentation

        pair = self.bundle.get("pair") or {}
        if str(pair.get("composite") or "") == "monthly":
            rep = pair.get("landsat_date") or pair.get("sentinel2_date") or ""
            return f"- 数据时间：{presentation.month_label(rep)}"
        l_date = pair.get("landsat_date")
        s_date = pair.get("sentinel2_date")
        if l_date and s_date:
            sat = presentation.satellite_label(pair.get("landsat_satellite"))
            return (f"- 数据时间：{sat} {l_date} 与 Sentinel-2 {s_date}")
        tr = self.bundle.get("time_range") or {}
        return (f"- 数据时间：{tr.get('start', '')} 至 {tr.get('end', '')}")

    def facts_text(self) -> str:
        """逐项列出真实数值，作为 LLM 的唯一数字来源。"""
        from .. import presentation

        test = self.bundle.get("test_metrics") or {}
        closure = self.bundle.get("closure") or {}
        cm = closure.get("metrics") or {}
        stats = self.bundle.get("lst_stats") or {}
        size = self.bundle.get("image_size") or {}
        value_range = closure.get("value_range") or {}

        lines = [
            f"- 研究区：{self.bundle.get('region') or '未记录'}",
            self._data_time_line(),
            f"- 产品分辨率：10m",
            f"- 有效像元数：{presentation.fmt_count(stats.get('total_valid'))} 个",
            f"- 影像尺寸：{presentation.fmt_count(size.get('height'))} 行 × "
            f"{presentation.fmt_count(size.get('width'))} 列",
            f"- 有效像元占比：{presentation.fmt_percent(stats.get('valid_percent'))}",
            f"- 测试集决定系数：{presentation.fmt_num(test.get('R2'))}",
            f"- 测试集平均绝对误差：{presentation.fmt_num(test.get('MAE'))} K",
            f"- 测试集均方根误差：{presentation.fmt_num(test.get('RMSE'))} K",
            f"- 闭合平均偏差：{presentation.fmt_num(cm.get('MB_K'))} K",
            f"- 闭合平均绝对误差：{presentation.fmt_num(cm.get('MAE_K'))} K",
            f"- 闭合均方根误差：{presentation.fmt_num(cm.get('RMSE_K'))} K",
            f"- 闭合匹配格网数：{presentation.fmt_count(closure.get('n_matched_cells'))} 个",
        ]
        if value_range:
            lines.append(
                f"- 低端温差：{presentation.fmt_num(value_range.get('low_end_difference_K'))}"
                f" K；高端温差："
                f"{presentation.fmt_num(value_range.get('high_end_difference_K'))} K")
        tcr = self.bundle.get("tcr_statistics") or {}
        if tcr:
            lines.append(
                f"- 热约束残差平均值：{presentation.fmt_num(tcr.get('mean'))} K；"
                f"标准差：{presentation.fmt_num(tcr.get('std'))} K；"
                f"有效格网：{presentation.fmt_count(tcr.get('n_valid_blocks'))} 个")
        top = self._top_features()
        if top:
            lines.append(f"- 贡献最大的特征：{top}")
        if self.bundle.get("r2_null_reason"):
            lines.append(f"- 决定系数无法计算的原因："
                         f"{self.bundle['r2_null_reason'].rstrip('。')}")
        return "\n".join(l.rstrip("。") for l in lines)

    def assemble_report(self, expected_grade: str, note: str) -> str:
        """确定性组装最终报告：全部数字、评级、闭合口径句由系统给出。

        层级规则（与气泡排版约定一致）：只有「结果说明」是二级标题（前端主色
        竖线），其下的产品概况/模型精度/闭合情况/关键特征与局限全部用三级标题
        + 缩进区分层级，不再各自带竖线；数字一律用阿拉伯数字（10m 而非十米）。
        """
        from .. import presentation

        test = self.bundle.get("test_metrics") or {}
        lines = [
            "## 结果说明",
            "",
            "### 产品概况",
            self.facts_text(),
            "",
            "### 模型精度",
            f"- 按分档基准，本次测试集精度评级为{expected_grade}"
            f"（决定系数 {presentation.fmt_num(test.get('R2'))}）",
            "- 可在工作面板中的地图页面查看每个像元的温度情况，并与30m的地表温度对比",
            "",
            "### 闭合情况",
            "- 闭合指标是10m结果回聚合到30m产品格网的算术均值闭合度，"
            "不是10m独立精度，也不代表能量或辐射守恒",
            "",
            "### 关键特征与局限",
            f"- {(_normalize_note(note) or _FALLBACK_NOTE).rstrip('。')}",
        ]
        return "\n".join(l.rstrip("。") for l in lines)

    # ── 结果后处理（可选，10m LST 空洞填补） ─────────────

    def _plan_has_gapfill(self, ctx: Any) -> bool:
        """当前执行计划是否已显式包含结果后处理步骤（lst_gapfill）。

        用户要求「从头执行并包含结果后处理」时，规划 Agent 会在完整流程末尾带上
        lst_gapfill 步骤——此时评估完成后由执行引擎直接执行该步骤，不再弹询问/提示。
        """
        plan = getattr(ctx, "plan", None)
        if not isinstance(plan, dict):
            return False
        return any(str((s or {}).get("skill") or "") == "lst_gapfill"
                   for s in (plan.get("steps") or []))

    def _handle_postprocess(self, ctx: Any, hooks=None) -> StepDecision:
        """全流程结果生成后：询问是否对 10m LST 空洞做填补。

        由我批准模式：弹审批卡片让用户选择（执行填洞 / 不需要结束流程）；
        完全执行模式：默认跳过不询问，并在气泡中提示默认不执行、需要时可告知。
        若执行计划本身已包含 lst_gapfill 步骤，则不重复询问/提示，直接交给执行引擎。
        """
        exec_mode = getattr(hooks, "exec_mode", "") if hooks is not None else ""

        if self._plan_has_gapfill(ctx):
            return StepDecision.cont()

        if is_auto(exec_mode):
            # 完全执行模式：默认不执行结果后处理，完成后提示用户可随时告知。
            # 用引用块（前端灰字 + 主色竖线）与最终报告同一层级展示，
            # 前面用与步骤间一致的 step_gap 留白与「关键特征与局限」分隔
            # （等价「执行方案已确定」与「第 1 步」之间的空行大小）。
            try:
                ctx.exp_state.setdefault("approval_choices", {})[Node.POSTPROCESS] = Option.SKIP_POSTPROCESS
            except Exception:
                pass
            ctx.emit(presentation.step_gap())
            ctx.emit("> 本次为完全执行模式，默认不执行结果后处理；"
                     "如需要无空洞的 10m 地表温度产品，告诉我即可对结果做空洞填补\n")
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

    def _run_gapfill(self, ctx: Any, params: Optional[dict] = None) -> StepDecision:
        """执行 lst_gapfill skill（空洞填补），结果写入气泡与阶段清单。

        params 为 None 时按 ctx 的 raw/processed/results 目录拼接标准路径
        （全流程结束后询问路径）；否则用调用方（执行引擎对 postprocess 计划
        动态查找）已解析好的输入/输出路径。
        """
        skill = ctx.registry.get("lst_gapfill")
        if skill is None:
            ctx.emit("结果后处理组件未注册，本次跳过。\n")
            return StepDecision.cont()
        if params is None:
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
        else:
            params = dict(params)

        # 只填研究区矢量范围内的空洞：从计划的研究区 GeoJSON 路径注入
        # （独立请求路径的 _PostCtx 不带 plan，靠上层把 region_geojson 放进 ctx）。
        # 调用方（executor）已按影像对记录的研究区填好 params["region_geojson"] 时不覆盖。
        if not params.get("region_geojson"):
            region_geojson = getattr(ctx, "region_geojson", "") or ""
            if not region_geojson:
                plan = getattr(ctx, "plan", None)
                if isinstance(plan, dict):
                    region_geojson = str((plan.get("region") or {}).get("study_area_file") or "")
            if region_geojson:
                params = dict(params)
                params["region_geojson"] = region_geojson

        ctx.emit("开始结果后处理：填补 10m 地表温度产品中的云像元空洞…\n")
        try:
            result = skill.execute(
                params,
                # 填洞进度同时进日志面板（保留工作面板进度由阶段清单驱动）
                progress_callback=lambda sn, pct, msg: self.log(msg),
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
            # 气泡只给完成统计（skill 的 message 已含填充像元数/占比/未改无云区），
            # 产物路径细节走日志面板，不泄漏路径进气泡
            ctx.emit(getattr(result, "message", "结果后处理完成") + "\n")
            self.log(f"结果后处理产物：filled_tif={data.get('filled_tif', '')}，"
                     f"mask_tif={data.get('mask_tif', '')}")
        else:
            ctx.emit(f"结果后处理未完成：{getattr(result, 'message', '未知原因')}\n")
        return StepDecision.cont()

    def _final_summary(self) -> str:
        from .. import presentation

        test = self.bundle.get("test_metrics") or {}
        closure = (self.bundle.get("closure") or {}).get("metrics") or {}
        return (f"测试集决定系数 {presentation.fmt_num(test.get('R2'))}，"
                f"闭合平均偏差 {presentation.fmt_num(closure.get('MB_K'))} K。"
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

    # ── 工作流写回记忆 ──────────────────────

    def _write_workflow(self, ctx: Any, hooks=None) -> None:
        from ...memory import workflow_experience

        if self.memory is None or not self.project_id:
            return
        test_r2 = (self.bundle.get("test_metrics") or {}).get("R2")
        failed = ctx.exp_state.get("failed")
        status = "failed" if failed else "success"
        # 报告永远完整（数字/评级/口径由系统渲染，定性短句兜底），
        # 不存在「解读失败降级」的状态，评估始终视为通过。
        if not workflow_experience.should_write(status=status, eval_passed=True,
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
