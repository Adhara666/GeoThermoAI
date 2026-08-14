"""
规划轻反思：确定性规则 P1–P7

先跑确定性规则，再跑 LLM 反思，**规则结论覆盖 LLM 结论**。
本模块只包含确定性规则；LLM 反思在 `roles/planner_agent.py` 里调用，
两者的合并顺序也在那里（规则在后，永远覆盖）。

所有函数都返回新 plan，不就地修改入参。
"""

import datetime
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import plan_schema
from ..orchestrator.agent_config import REPLAN_MAX
from .result import ReflectionResult

RULES = {
    "P1": "region.study_area_file 必须是当前用户 study_areas 目录下真实存在的文件",
    "P2": "time_range 必须精确到月、start<=end、且不晚于今天",
    "P3": "intent 为 chat/qa 时 steps 必须为空",
    "P4": "steps[].skill 必须全部在 SkillRegistry 中存在",
    "P5": "全流程任务的 steps 必须是 7 步且顺序与 WORKFLOW_STEPS 一致",
    "P6": "replan_count 不得超过上限",
    "P7": "replan 时新 plan 必须与上一版有实质差异",
}

# P7 判定「实质差异」时比较的字段
_REPLAN_DIFF_KEYS = ("time_range", "cloud_threshold", "region", "dem_source")


def _study_area_names(study_areas: List[str]) -> List[str]:
    return [os.path.splitext(os.path.basename(n))[0] for n in study_areas or []]


def _ask_region(study_areas: List[str]) -> str:
    names = _study_area_names(study_areas)
    if not names:
        return "还没有看到你上传的研究区文件，请先上传研究区（GeoJSON 或 Shapefile），我再安排流程。"
    if len(names) == 1:
        return f"这次要处理的是「{names[0]}」吗？确认后我就安排流程。"
    listed = "、".join(names[:6])
    return f"你已上传的研究区有：{listed}。这次要处理哪一个？"


def check_region(plan: Dict[str, Any], study_areas_dir: str,
                 study_areas: Optional[List[str]] = None) -> Optional[ReflectionResult]:
    """P1：研究区必须解析到当前用户目录下真实存在的文件。"""
    region = plan.get("region") or {}
    path = str(region.get("study_area_file") or "")
    if not path:
        return ReflectionResult.ask(_ask_region(study_areas or []),
                                    note="研究区未确定", rule_hits=["P1"],
                                    violations=[RULES["P1"]])
    if not os.path.isfile(path):
        return ReflectionResult.ask(_ask_region(study_areas or []),
                                    note="研究区文件不存在", rule_hits=["P1"],
                                    violations=[RULES["P1"]])
    if study_areas_dir:
        try:
            base = os.path.realpath(study_areas_dir)
            target = os.path.realpath(path)
            if not (target == base or target.startswith(base + os.sep)):
                return ReflectionResult.ask(
                    _ask_region(study_areas or []),
                    note="研究区文件不在当前用户的研究区目录内", rule_hits=["P1"],
                    violations=[RULES["P1"]])
        except Exception:
            pass
    return None


def check_time_range(plan: Dict[str, Any],
                     today: Optional[datetime.date] = None) -> Optional[ReflectionResult]:
    """P2：时间范围必须精确到月、start<=end、且不晚于今天。"""
    from ..roles.slots import time_range_valid

    tr = plan.get("time_range") or {}
    start, end = str(tr.get("start") or ""), str(tr.get("end") or "")
    if not start or not end:
        return ReflectionResult.ask(
            "还需要具体的时间范围，请给到月份，例如 2025 年 7 月。",
            note="时间范围未确定", rule_hits=["P2"], violations=[RULES["P2"]])
    reason = time_range_valid(start, end, today=today)
    if reason:
        return ReflectionResult.ask(
            f"{reason}，请重新确认时间范围，例如 2025 年 7 月。",
            note=reason, rule_hits=["P2"], violations=[RULES["P2"]])
    return None


def enforce_chat_intent(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """P3：intent 为 chat/qa 时丢弃 steps，改走聊天路径。"""
    if plan.get("intent") in ("chat", "qa") and plan.get("steps"):
        return plan_schema.with_steps(plan, []), ["P3"]
    return plan, []


def drop_unknown_skills(plan: Dict[str, Any], registry) -> Tuple[Dict[str, Any], List[str]]:
    """P4：剔除注册表里不存在的 skill。"""
    if registry is None:
        return plan, []
    kept, dropped = [], []
    for step in plan.get("steps") or []:
        if registry.get(step.get("skill")) is None:
            dropped.append(step.get("skill"))
        else:
            kept.append(step)
    if not dropped:
        return plan, []
    fixed = plan_schema.with_steps(plan, kept)
    fixed = {**fixed, "dropped_skills": list(plan.get("dropped_skills") or []) + dropped}
    return fixed, ["P4"]


def enforce_workflow_order(plan: Dict[str, Any],
                           wants_full_workflow: bool) -> Tuple[Dict[str, Any], List[str]]:
    """P5：全流程任务的步骤必须是 7 步且顺序正确，缺失则补齐、乱序则重排。

    用户明确要求「从头执行并包含结果后处理」时，LLM 会在 steps 里带 lst_gapfill
    步骤——把它保留并追加到 accuracy_eval 之后，成为完整流程的最后一步
    （此前 reorder_to_workflow 只保留固定 7 步技能，会把该步骤丢弃）。
    """
    if not wants_full_workflow:
        return plan, []
    if plan_schema.is_full_workflow(plan):
        return plan, []

    by_skill = {s.get("skill"): s for s in (plan.get("steps") or [])}
    acq = by_skill.get("data_acquisition") or {}
    acq_params = dict(acq.get("params") or {})
    time_range = plan.get("time_range") or {}
    region_file = (plan.get("region") or {}).get("study_area_file", "")

    steps = []
    for name in plan_schema.WORKFLOW_STEPS:
        existing = by_skill.get(name)
        if existing is not None:
            steps.append(dict(existing))
            continue
        params: Dict[str, Any] = {}
        if name == "data_acquisition":
            params = {
                "region": acq_params.get("region") or region_file,
                "start_date": acq_params.get("start_date") or time_range.get("start", ""),
                "end_date": acq_params.get("end_date") or time_range.get("end", ""),
            }
        steps.append({"skill": name, "params": params,
                      "reason": _DEFAULT_REASONS.get(name, "")})
    # 结果后处理步骤（LLM 因用户明确要求而输出）追加到完整流程末尾，
    # 由执行引擎交给结果 Agent（EvalAgent）执行
    extra = by_skill.get("lst_gapfill")
    if extra is not None:
        steps.append(dict(extra))
    # 直接 with_steps：steps 已按 WORKFLOW_STEPS 顺序构建 + lst_gapfill 在末尾，
    # 不再走 reorder_to_workflow（它只会保留固定 7 步技能，会丢掉 lst_gapfill）
    return plan_schema.with_steps(plan, steps), ["P5"]


_DEFAULT_REASONS = {
    "data_acquisition": "下载遥感数据",
    "data_pipeline": "数据预处理与划分",
    "ttri_compute": "拟合地形热响应指数",
    "rf_model": "训练降尺度模型",
    "tcr_compute": "计算热约束残差",
    "lst_export": "导出最终产品",
    "accuracy_eval": "精度评估",
}


def check_replan_budget(replan_count: int,
                        replan_max: int = REPLAN_MAX) -> Optional[ReflectionResult]:
    """P6：replan 次数达上限后停止自动 replan，转人工询问。"""
    if replan_count > replan_max:
        return ReflectionResult.ask(
            f"已经自动重新规划 {replan_max} 次仍未成功，请告诉我下一步怎么调整"
            f"（例如换时间段、换研究区，或放宽云量要求）。",
            note="自动重新规划次数已达上限", rule_hits=["P6"], violations=[RULES["P6"]])
    return None


def _replan_signature(plan: Dict[str, Any]) -> Tuple:
    tr = plan.get("time_range") or {}
    constraints = plan.get("constraints") or {}
    region = plan.get("region") or {}
    return (
        tr.get("start", ""), tr.get("end", ""),
        constraints.get("cloud_threshold"), constraints.get("dem_source"),
        region.get("study_area_file", ""),
    )


def check_replan_difference(plan: Dict[str, Any],
                            previous_plan: Optional[Dict[str, Any]]) -> Optional[ReflectionResult]:
    """P7：replan 产出的新 plan 必须与上一版有实质差异，否则判为无效 replan。"""
    if not previous_plan:
        return None
    if _replan_signature(plan) != _replan_signature(previous_plan):
        return None
    return ReflectionResult.ask(
        "重新规划后的方案和上一次一样，自动调整没有效果。"
        "请告诉我要改什么：换时间段、换研究区，还是放宽云量要求？",
        note="replan 未做出实质调整", rule_hits=["P7"], violations=[RULES["P7"]])


def _plan_has_data_acquisition(plan: Dict[str, Any]) -> bool:
    """plan 的 steps 是否包含 data_acquisition（下载步骤）。"""
    return any(str((s or {}).get("skill") or "") == "data_acquisition"
               for s in (plan.get("steps") or []))


def check(plan: Dict[str, Any], *, registry=None, study_areas_dir: str = "",
          study_areas: Optional[List[str]] = None, wants_full_workflow: bool = True,
          replan_count: int = 0, replan_max: int = REPLAN_MAX,
          previous_plan: Optional[Dict[str, Any]] = None,
          today: Optional[datetime.date] = None) -> Tuple[Dict[str, Any], ReflectionResult]:
    """按 P1→P7 顺序跑完确定性规则，返回 (可能被修正的 plan, 反思结论)。

    先跑「可自动修正」的规则（P3/P4/P5），再跑「必须反问」的规则（P1/P2/P6/P7），
    这样修正后的 plan 才能参与后续判定。
    """
    rule_hits: List[str] = []

    plan, hits = enforce_chat_intent(plan)
    rule_hits += hits
    if plan.get("intent") in ("chat", "qa"):
        return plan, ReflectionResult.chat_only(note="判定为聊天或领域问答，不进入生产流程",
                                                rule_hits=rule_hits)

    plan, hits = drop_unknown_skills(plan, registry)
    rule_hits += hits
    plan, hits = enforce_workflow_order(plan, wants_full_workflow)
    rule_hits += hits

    if not plan.get("steps"):
        return plan, ReflectionResult.ask(
            "我没能把你的需求拆成可执行的步骤，能再说一下要生成什么产品吗？",
            note="计划中没有可执行步骤", rule_hits=rule_hits + ["P4"],
            violations=[RULES["P4"]])

    checks = [check_replan_budget(replan_count, replan_max)]
    intent = plan.get("intent", "")
    if intent != "postprocess":
        # 后处理针对已有结果，不需要新的研究区文件与时间范围，跳过 P1/P2
        checks += [check_region(plan, study_areas_dir, study_areas)]
        # partial 意图：不含 data_acquisition（如只做预处理）时不需要时间范围
        if intent != "partial" or _plan_has_data_acquisition(plan):
            checks += [check_time_range(plan, today=today)]
    checks.append(check_replan_difference(plan, previous_plan))

    for result in checks:
        if result is not None:
            return plan, ReflectionResult(
                ok=result.ok, action=result.action, question=result.question,
                note=result.note, violations=result.violations,
                suggestions=result.suggestions,
                rule_hits=rule_hits + result.rule_hits, data=result.data)

    return plan, ReflectionResult.passed(note="信息齐全，可以开始执行", rule_hits=rule_hits)
