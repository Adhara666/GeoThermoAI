"""
训练与调优 Agent（技术方案第 6 章）· 主反思

只做**阶段内优化**：改超参再训一轮属于本 Agent 内部循环，不发起 replan。
唯一上报总调度请求 replan 的情况：用户在审批节点明确选择「换时间或地区」。

两个工程问题的解决（技术方案 6.4）：
- 问题 A：调优期给 `rf_model` 传 `defer_cleanup=True`，避免每轮重建 train/validate/test；
  接受最终结果后再显式清理一次。
- 问题 B：每轮输出到 `results/tuning/round_{i}/`，选定最佳轮后把产物**复制**到规范位置
  `results/`（保留调优轨迹供审计），并确保复制后的模型是 `results/train/` 下最新的，
  这样下游 `tcr_compute` / `accuracy_eval` 的现有推断逻辑无需改动。
"""

import glob
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from ..orchestrator import approval as approval_proto
from ..orchestrator.approval import Node, Option
from ..orchestrator.exec_mode import is_auto
from ..orchestrator.hooks import StepDecision
from ..reflection import train_rules
from ..reflection.train_rules import Decision
from .base_role import RoleAgent
from .prompts import train as train_prompts

logger = logging.getLogger(__name__)


class TrainAgent(RoleAgent):
    """七规则约束下的多轮调参 + 主反思。"""

    role = "train"
    role_name = "训练"

    def __init__(self, assistant, registry, memory_manager=None, project_id: str = "",
                 on_log=None, max_rounds: Optional[int] = None, on_thinking=None):
        super().__init__(assistant, memory_manager=memory_manager,
                         project_id=project_id, on_log=on_log,
                         on_thinking=on_thinking)
        self.registry = registry
        self.max_rounds = train_rules.resolve_max_rounds(max_rounds)

        self.rounds: List[Dict[str, Any]] = []
        self.tuning_started = False
        self.decided_no_tuning = False
        self.manual_params: Dict[str, Any] = {}
        self.finalized = False

    # ── 执行前：分轮目录 + 延迟清理 ─────────────────────────────────

    def before_train(self, step: dict, ctx: Any, hooks=None) -> Optional[StepDecision]:
        """第一轮训练前把输出重定向到 round_0，并要求 Skill 延迟清理中间产物。"""
        params = step.setdefault("params", {})
        params["output_dir"] = self._round_dir(ctx, 0)
        params["defer_cleanup"] = True
        self.log(f"第 1 轮训练输出目录已设为 round_0，中间产物延迟清理")
        return None

    # ── 执行后：主反思与调优循环 ───────────────────────────────────

    def on_trained(self, result: Any, ctx: Any, hooks=None) -> StepDecision:
        data = getattr(result, "data", None)
        data = data if isinstance(data, dict) else {}
        index = len(self.rounds)
        record = self._round_record(index, data, ctx)
        self.rounds.append(record)
        self._emit_round_summary(ctx, record)

        exec_mode = getattr(hooks, "exec_mode", "")
        run_state = getattr(hooks, "run_state", None)

        # 首次训练完成 → tuning_decision
        if not self.tuning_started and not self.decided_no_tuning:
            decision = self._first_decision(ctx, record, exec_mode, run_state, hooks)
            if decision is not None:
                return decision

        return self._tuning_step(ctx, record, exec_mode, run_state, hooks)

    # ── tuning_decision 节点 ───────────────────────────────────────

    def _first_decision(self, ctx: Any, record: dict, exec_mode: str, run_state,
                        hooks) -> Optional[StepDecision]:
        if is_auto(exec_mode):
            # 完全执行模式：直接进入七规则调优循环
            self.tuning_started = True
            return None

        ranked = getattr(hooks, "ranked_pairs", None) or []
        all_tried = bool(ranked) and all(bool(p.get("tried")) for p in ranked)
        payload = approval_proto.build_tuning_decision(
            summary=self._decision_summary(record),
            fields=self._manual_fields(),
            max_rounds=self.max_rounds,
            exclude_reselect=all_tried,  # 升级点 12：全部已尝试时不再提示换对
        )
        choice = self._ask(hooks, payload)
        if choice is None:
            return StepDecision.pause(payload, reason="等待用户选择是否调优")

        option_id = choice.get("option_id", Option.AI_TUNE)
        self._record_choice(ctx, run_state, Node.TUNING_DECISION, option_id)

        if option_id == Option.AI_TUNE:
            self.tuning_started = True
            return None
        if option_id == Option.MANUAL_TUNE:
            self.tuning_started = True
            self.manual_params = dict(choice.get("values") or {})
            params, _ = train_rules.clamp_params(self.manual_params)
            ctx.emit("已按你设置的参数再训练一轮\n")
            return StepDecision.retry(self._retry_params(ctx, params),
                                     reason="按你设置的参数重新训练")
        if option_id == Option.ACCEPT:
            self.decided_no_tuning = True
            self._finalize(ctx, record)
            return StepDecision.cont()
        if option_id == Option.RESELECT_PAIR:
            if run_state is not None:
                run_state.set_resume_point(Node.PAIR_SELECTION)
            return self._replan(hooks, "用户要求重新选择影像组合", {"reselect_pair": True})
        if option_id == Option.REPLAN:
            return self._replan(hooks, "用户不接受当前精度，要求更换时间或地区",
                               {"user_replan": True})
        self.tuning_started = True
        return None

    # ── 七规则调优循环 ─────────────────────────────────────────────

    def _tuning_step(self, ctx: Any, record: dict, exec_mode: str, run_state,
                     hooks) -> StepDecision:
        if self.decided_no_tuning:
            return StepDecision.cont()

        llm_decision = self._llm_decision(ctx, record)
        final = train_rules.rule_safeguard(llm_decision, {
            "rounds": self.rounds[:-1],
            "current": record,
            "max_rounds": self.max_rounds,
        })
        if final["note"]:
            # 升级点 10：不向前端展示「[规则] R7 …」类字眼，直接用自然语言说明调优结论
            ctx.emit(final["note"] + "\n")
        record["decision"] = final["action"]
        record["rule_hits"] = final["rule_hits"]

        # 由我批准模式：每一轮**调优**都报告并询问（无论精度好坏）。
        # 首次训练（round 0）由 tuning_decision 节点负责，不重复弹 tuning_round。
        if not is_auto(exec_mode) and self.tuning_started and record["round"] > 0:
            is_last = (final["action"] != Decision.ADJUST
                       or len(self.rounds) >= self.max_rounds)
            payload = approval_proto.build_tuning_round(
                summary=self._round_summary_text(record, final),
                is_last_round=is_last)
            choice = self._ask(hooks, payload)
            if choice is None:
                return StepDecision.pause(payload, reason="等待用户选择本轮调优结果")
            option_id = choice.get("option_id", Option.ACCEPT)
            self._record_choice(ctx, run_state, Node.TUNING_ROUND, option_id)
            if option_id in (Option.ACCEPT, Option.STOP_TUNING):
                self._finalize(ctx, self._best())
                return StepDecision.cont()
            if train_rules.hard_stopped(final["rule_hits"]):
                # 用户要继续，但命中了硬停止规则（R2/R5/R6/R7）→ 规则优先
                ctx.emit("按硬性规则已不宜继续调优，取误差最小的一轮作为最终结果\n")
                self._finalize(ctx, self._best())
                return StepDecision.cont()
            if final["action"] != Decision.ADJUST:
                # 没有硬停止理由，用户又明确要求继续 → 用规则兜底方向真的再训一轮
                final = {**final, "action": Decision.ADJUST,
                         "new_params": train_rules.fallback_direction(
                             record.get("params") or {})}

        if final["action"] == Decision.ADJUST:
            if run_state is not None:
                run_state.next_tuning_round()
            next_index = len(self.rounds)
            params = self._retry_params(ctx, final["new_params"], round_index=next_index)
            ctx.emit(f"开始第 {next_index + 1} 轮调优训练\n")
            return StepDecision.retry(params, reason=final.get("reason", ""))

        self._finalize(ctx, self._best())
        return StepDecision.cont()

    # ── 收尾：提升最佳轮并清理 ─────────────────────────────────────

    def _finalize(self, ctx: Any, record: Optional[dict]) -> None:
        if self.finalized:
            return
        self.finalized = True
        best = record or self._best()
        if best is None:
            return

        ctx.exp_state["tuning_trace"] = [
            {"round": r["round"], "test_r2": r.get("test_r2"), "rmse": r.get("rmse")}
            for r in self.rounds
        ]
        ctx.exp_state["final_params"] = dict(best.get("params") or {})
        ctx.exp_state["rf_data"] = dict(best.get("raw") or {})

        promoted = self._promote_round(ctx, best)
        rounds_text = f"共 {len(self.rounds)} 轮" if len(self.rounds) > 1 else "未调优"
        from .. import presentation

        ctx.emit(f"调优结束（{rounds_text}），采用第 {best['round'] + 1} 轮的结果："
                 f"测试集决定系数 {presentation.fmt_num(best.get('test_r2'))}，"
                 f"均方根误差 {presentation.fmt_num(best.get('rmse'))} 开尔文\n")
        if not promoted:
            self.log("最佳轮产物复制未完成，下游将使用最近一次训练的模型")

    def _promote_round(self, ctx: Any, best: dict) -> bool:
        """把最佳轮的产物复制到规范位置 `results/`（复制而非移动，保留调优轨迹）。"""
        round_dir = best.get("output_dir") or ""
        results_dir = getattr(ctx, "results_dir", "") or ""
        if not round_dir or not results_dir or not os.path.isdir(round_dir):
            return False
        if os.path.realpath(round_dir) == os.path.realpath(results_dir):
            return True
        try:
            for root, _dirs, names in os.walk(round_dir):
                rel = os.path.relpath(root, round_dir)
                dest_dir = results_dir if rel == "." else os.path.join(results_dir, rel)
                os.makedirs(dest_dir, exist_ok=True)
                for name in names:
                    shutil.copy2(os.path.join(root, name), os.path.join(dest_dir, name))
            # 保证复制后的模型是 results/train 下 mtime 最新的（下游按最新 pkl 推断）
            now = time.time()
            for pkl in glob.glob(os.path.join(results_dir, "train", "*.pkl")):
                os.utime(pkl, (now, now))
        except Exception as e:
            logger.warning(f"[train] 复制最佳轮产物失败（已忽略）: {e}")
            return False

        self._cleanup_and_record(ctx, best)
        return True

    def _cleanup_and_record(self, ctx: Any, best: dict) -> None:
        """接受最终结果后显式清理中间产物，并按被采纳的一轮重写阶段清单。"""
        project_dir = getattr(ctx, "project_dir", "") or ""
        if not project_dir:
            return
        try:
            from ... import manifest as run_manifest
            from ...intermediate_cleanup import cleanup_stage

            raw = best.get("raw") or {}
            run_manifest.record_stage(
                project_dir, "rf_model", run_manifest.STATUS_COMPLETED,
                stats={"train_metrics": raw.get("train_metrics", {}),
                       "test_metrics": raw.get("test_metrics", {}),
                       "params": raw.get("params", {}),
                       "tuning_rounds": len(self.rounds),
                       "accepted_round": best.get("round")},
            )
            cleanup_stage(project_dir, "rf_model")
        except Exception as e:
            logger.warning(f"[train] 收尾清理失败（已忽略）: {e}")

    # ── 内部工具 ──────────────────────────────────────────────────

    @staticmethod
    def _round_dir(ctx: Any, index: int) -> str:
        results_dir = getattr(ctx, "results_dir", "") or ""
        if not results_dir:
            return ""
        return f"{results_dir}/tuning/round_{index}"

    def _retry_params(self, ctx: Any, new_params: Dict[str, Any],
                      round_index: Optional[int] = None) -> Dict[str, Any]:
        """构造下一轮的参数：新超参 + 新的分轮目录 + 继续延迟清理。"""
        index = len(self.rounds) if round_index is None else round_index
        params = dict(new_params or {})
        target = self._round_dir(ctx, index)
        if target:
            params["output_dir"] = target
        params["defer_cleanup"] = True
        return params

    def _round_record(self, index: int, data: dict, ctx: Any) -> Dict[str, Any]:
        test = data.get("test_metrics") or {}
        train = (data.get("train_metrics") or {}).get("train") or data.get("train_metrics") or {}
        return {
            "round": index,
            "params": dict(data.get("params") or {}),
            "train_r2": train.get("R2"),
            "test_r2": test.get("R2"),
            "rmse": test.get("RMSE"),
            "mb": test.get("MB"),
            "output_dir": self._round_dir(ctx, index),
            "raw": dict(data),
        }

    def _best(self) -> Optional[dict]:
        return train_rules.best_round(self.rounds)

    def _emit_round_summary(self, ctx: Any, record: dict) -> None:
        from .. import presentation

        ctx.emit(f"第 {record['round'] + 1} 轮训练完成：测试集决定系数 "
                 f"{presentation.fmt_num(record.get('test_r2'))}，均方根误差 "
                 f"{presentation.fmt_num(record.get('rmse'))} 开尔文\n")

    def _decision_summary(self, record: dict) -> str:
        from .. import presentation

        parts = [f"测试集决定系数 {presentation.fmt_num(record.get('test_r2'))}"
                 f"（{train_rules.grade(record.get('test_r2'))}）",
                 f"均方根误差 {presentation.fmt_num(record.get('rmse'))} 开尔文"]
        top = self._top_features(record.get("raw") or {})
        if top:
            parts.append(f"贡献最大的特征是 {top}")
        best = self._history_best()
        if best:
            parts.append(best)
        return "，".join(parts) + "。"

    def _round_summary_text(self, record: dict, final: dict) -> str:
        from .. import presentation

        text = (f"第 {record['round'] + 1} 轮：测试集决定系数 "
                f"{presentation.fmt_num(record.get('test_r2'))}"
                f"（{train_rules.grade(record.get('test_r2'))}），均方根误差 "
                f"{presentation.fmt_num(record.get('rmse'))} 开尔文。")
        if final.get("note"):
            text += f"规则判定：{final['note']}。"
        return text

    def _history_best(self) -> str:
        if self.memory is None or not self.project_id:
            return ""
        try:
            best = self.memory.experiment_log(self.project_id).get_best()
        except Exception:
            return ""
        if not best:
            return ""
        r2 = ((best.get("metrics") or {}).get("test") or {}).get("R2")
        if r2 is None:
            return ""
        from .. import presentation

        return f"该区域上一次的结果是 {presentation.fmt_num(r2)}"

    @staticmethod
    def _top_features(raw: dict, limit: int = 3) -> str:
        items = [i for i in (raw.get("feature_importance") or []) if isinstance(i, dict)]
        items.sort(key=lambda i: i.get("importance", 0), reverse=True)
        labels = {"NDVI": "植被指数", "NDBI": "建筑指数", "NDWI": "水体指数",
                  "DEM": "高程", "TTRI": "地形热响应指数", "NIR": "近红外",
                  "SWIR1": "短波红外", "R": "红光", "G": "绿光", "B": "蓝光"}
        return "、".join(labels.get(i.get("feature"), str(i.get("feature")))
                        for i in items[:limit])

    def _manual_fields(self) -> List[dict]:
        skill = self.registry.get("rf_model") if self.registry else None
        return approval_proto.hyperparameter_fields(skill) if skill else []

    @staticmethod
    def _ask(hooks, payload: dict) -> Optional[Dict[str, Any]]:
        if hooks is None:
            return None
        return hooks._ask(payload)

    @staticmethod
    def _record_choice(ctx: Any, run_state, node: str, option_id: str) -> None:
        if run_state is not None:
            run_state.record_approval(node, option_id)
        try:
            ctx.exp_state.setdefault("approval_choices", {})[node] = option_id
        except Exception:
            pass

    @staticmethod
    def _replan(hooks, reason: str, payload: dict) -> StepDecision:
        if hooks is not None:
            return hooks._request_replan(reason, payload)
        return StepDecision.replan(reason=reason, payload=payload)

    # ── LLM 决策 ──────────────────────────────────────────────────

    def _llm_decision(self, ctx: Any, record: dict) -> Optional[Dict[str, Any]]:
        features = getattr(ctx, "data_features", None) or {}
        raw = record.get("raw") or {}
        advisories = train_rules.advisory_notes(features, raw)
        trace = "\n".join(
            f"- 第 {r['round'] + 1} 轮：决定系数 {r.get('test_r2')}，误差 {r.get('rmse')}"
            for r in self.rounds)
        parsed = self.call_json(
            train_prompts.decision_prompt(
                model_name="随机森林",
                terrain=_band(features.get("dem_std"), 30, 100, "平坦", "中等", "复杂"),
                vegetation=_band(features.get("ndvi_mean"), 0.2, 0.5, "低", "中", "高"),
                temperature=_band(features.get("lst_std"), 2, 5, "小", "中", "大"),
                current_params=json.dumps(record.get("params") or {}, ensure_ascii=False),
                train_r2=str(record.get("train_r2")),
                test_r2=str(record.get("test_r2")),
                rmse=str(record.get("rmse")),
                top_features=self._top_features(raw, limit=5) or "未知",
                trace=trace,
                bounds=json.dumps(train_rules.PARAM_BOUNDS, ensure_ascii=False),
                memory_block=self.memory_block("调参 指标 解读"),
                advisories="\n".join(f"- {n}" for n in advisories),
            ),
            # 实现期修订 v1.2：retry_once=False 只有一次机会，预算给足避免被截断
            "请给出本轮的调优决策。", temperature=0.1, max_tokens=1024, retry_once=False,
        )
        if not isinstance(parsed, dict):
            return None
        return {"action": parsed.get("action"), "reason": str(parsed.get("reason") or ""),
                "new_params": parsed.get("new_params") or {}}


def _band(value: Any, low: float, high: float, low_label: str, mid_label: str,
          high_label: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    if number >= high:
        return high_label
    if number >= low:
        return mid_label
    return low_label
