"""
结构化执行计划 schema

向后兼容硬性要求：`steps[].skill / params / reason` 三个字段的形状与旧格式完全一致，
因此 `_normalize_plan_paths` 与执行引擎无需改动即可消费本模块产出的 plan；
`parse()` 遇到只有 `{"steps": [...]}` 的旧格式时自动补齐默认值。

不可变约定：所有函数都返回新对象，绝不就地修改传入的 plan/steps。
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

PLAN_VERSION = 1

# 全流程固定 7 步及其顺序（与 server.WORKFLOW_STEPS 一致）
WORKFLOW_STEPS: Tuple[str, ...] = (
    "data_acquisition",
    "data_pipeline",
    "ttri_compute",
    "rf_model",
    "tcr_compute",
    "lst_export",
    "accuracy_eval",
)

STAGE_OF_SKILL: Dict[str, str] = {
    "data_acquisition": "data",
    "data_pipeline": "data",
    "ttri_compute": "data",
    "rf_model": "train",
    "tcr_compute": "eval",
    "lst_export": "eval",
    "lst_gapfill": "eval",
    "accuracy_eval": "eval",
    "ai_assistant": "chat",
}

# 产品类型枚举（城市热岛等新功能只在此预留位置，本次不实现）
PRODUCTS = ("lst_10m",)

# 意图取值
# - postprocess 为结果后处理，如对已有 10m LST 做空洞填补
# - partial 为部分流程，用户只要求执行某些步骤（如只下载数据、只做预处理），不跑完整7步
INTENTS = ("chat", "qa", "task", "modify", "unclear", "postprocess", "partial")

DEFAULT_APPROVAL_NODES = ("pair_selection", "tuning_decision", "final_report")


def new_plan_id() -> str:
    return "plan_" + uuid.uuid4().hex[:6]


def stage_of(skill_name: str) -> str:
    return STAGE_OF_SKILL.get(skill_name, "data")


def _clean_step(step: Any, index: int) -> Optional[Dict[str, Any]]:
    """把任意形态的 step 归一化为 {id, stage, skill, params, reason}。"""
    if not isinstance(step, dict):
        return None
    skill = str(step.get("skill", "") or "").strip()
    if not skill:
        return None
    params = step.get("params")
    if not isinstance(params, dict):
        params = {}
    return {
        "id": str(step.get("id") or f"s{index + 1}"),
        "stage": str(step.get("stage") or stage_of(skill)),
        "skill": skill,
        "params": dict(params),
        "reason": str(step.get("reason", "") or ""),
    }


def parse(obj: Any, registry=None) -> Dict[str, Any]:
    """解析并补全 plan；旧格式 `{"steps": [...]}` 自动获得默认字段。

    registry 非空时剔除注册表里不存在的 skill（规则 P4）。
    非法输入返回空步骤的合法 plan，绝不抛异常。
    """
    src = obj if isinstance(obj, dict) else {}
    raw_steps = src.get("steps")
    raw_steps = raw_steps if isinstance(raw_steps, list) else []

    steps: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for i, s in enumerate(raw_steps):
        cleaned = _clean_step(s, i)
        if cleaned is None:
            continue
        if registry is not None and registry.get(cleaned["skill"]) is None:
            dropped.append(cleaned["skill"])
            continue
        steps.append(cleaned)

    region = src.get("region") if isinstance(src.get("region"), dict) else {}
    time_range = src.get("time_range") if isinstance(src.get("time_range"), dict) else {}
    constraints = src.get("constraints") if isinstance(src.get("constraints"), dict) else {}
    reflection = src.get("reflection") if isinstance(src.get("reflection"), dict) else {}

    intent = str(src.get("intent") or "task")
    if intent not in INTENTS:
        intent = "task"

    approval_nodes = src.get("approval_nodes")
    if not isinstance(approval_nodes, list) or not approval_nodes:
        approval_nodes = list(DEFAULT_APPROVAL_NODES)

    memory_refs = src.get("memory_refs")
    memory_refs = list(memory_refs) if isinstance(memory_refs, list) else []

    plan = {
        "plan_version": int(src.get("plan_version") or PLAN_VERSION),
        "plan_id": str(src.get("plan_id") or new_plan_id()),
        "intent": intent,
        "goal": str(src.get("goal", "") or ""),
        "region": {
            "name": str(region.get("name", "") or ""),
            "study_area_file": str(region.get("study_area_file", "") or ""),
        },
        "time_range": {
            "start": str(time_range.get("start", "") or ""),
            "end": str(time_range.get("end", "") or ""),
        },
        "constraints": dict(constraints),
        "steps": steps,
        "approval_nodes": [str(n) for n in approval_nodes],
        "memory_refs": [str(r) for r in memory_refs],
        "reflection": {
            "info_complete": bool(reflection.get("info_complete", True)),
            "risks": list(reflection.get("risks") or []),
            "note": str(reflection.get("note", "") or ""),
        },
        "dropped_skills": dropped,
        "created_at": str(src.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
    }
    return plan


def skill_names(plan: Dict[str, Any]) -> List[str]:
    return [s.get("skill", "") for s in (plan.get("steps") or [])]


def is_full_workflow(plan: Dict[str, Any]) -> bool:
    """steps 是否正好是 7 步全流程且顺序正确（规则 P5）。"""
    return tuple(skill_names(plan)) == WORKFLOW_STEPS


def missing_workflow_skills(plan: Dict[str, Any]) -> List[str]:
    present = set(skill_names(plan))
    return [s for s in WORKFLOW_STEPS if s not in present]


def has_empty_params(plan: Dict[str, Any]) -> bool:
    """任一步骤的 params 为空或全空值（沿用现有安全网判据）。"""
    for step in plan.get("steps") or []:
        params = step.get("params") or {}
        if not params or all(v == "" or v is None for v in params.values()):
            return True
    return False


def reorder_to_workflow(plan: Dict[str, Any]) -> Dict[str, Any]:
    """按 WORKFLOW_STEPS 顺序重排已有步骤（缺失的不补，多余的丢弃）。

    返回新 plan，不修改入参。
    """
    by_skill = {s.get("skill"): s for s in (plan.get("steps") or [])}
    ordered = [dict(by_skill[name]) for name in WORKFLOW_STEPS if name in by_skill]
    for i, step in enumerate(ordered):
        step["id"] = f"s{i + 1}"
        step["stage"] = stage_of(step["skill"])
    return {**plan, "steps": ordered}


def with_steps(plan: Dict[str, Any], steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """替换步骤（不可变）。"""
    normalized = [c for c in (_clean_step(s, i) for i, s in enumerate(steps)) if c]
    return {**plan, "steps": normalized}


def to_legacy(plan: Dict[str, Any]) -> Dict[str, Any]:
    """降级为旧格式 `{"steps": [{skill, params, reason}]}`。

    供只认旧格式的路径（如现有测试与兜底逻辑）消费。
    """
    return {
        "steps": [
            {"skill": s["skill"], "params": dict(s.get("params") or {}),
             "reason": s.get("reason", "")}
            for s in (plan.get("steps") or [])
        ]
    }


def validate(plan: Dict[str, Any], registry=None) -> List[str]:
    """返回问题清单（空列表表示合法）。只做结构校验，不做业务判定。"""
    issues: List[str] = []
    if plan.get("plan_version") != PLAN_VERSION:
        issues.append(f"plan_version 不受支持：{plan.get('plan_version')}")
    if plan.get("intent") not in INTENTS:
        issues.append(f"intent 非法：{plan.get('intent')}")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        issues.append("steps 必须是数组")
        return issues
    seen_ids = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            issues.append(f"第 {i + 1} 步不是对象")
            continue
        if not step.get("skill"):
            issues.append(f"第 {i + 1} 步缺少 skill")
        elif registry is not None and registry.get(step["skill"]) is None:
            issues.append(f"第 {i + 1} 步的技能未注册：{step['skill']}")
        if step.get("id") in seen_ids:
            issues.append(f"步骤 id 重复：{step.get('id')}")
        seen_ids.add(step.get("id"))
        if not isinstance(step.get("params"), dict):
            issues.append(f"第 {i + 1} 步的 params 必须是对象")
    return issues
