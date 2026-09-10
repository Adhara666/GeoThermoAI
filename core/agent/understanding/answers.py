# -*- coding: utf-8 -*-
"""理解层 — 把用户答复变成草稿修改（升级第二阶段：理解层）。

依据总体技术方案 §4.4：

  - 文字回答和卡片点击**走同一处理入口**：「第二组」「都用七月」与点按钮
    行为完全一致。
  - 候选按**保存问题时固定的编号**解释，不按重新排序后的数组下标选择。
  - 答案落到问题绑定的那个任务与字段上；仍缺的信息由后续校验继续追问。

本模块同样是纯函数：不写库、不调模型。
"""

from typing import Any, Dict, List, Optional

from core.agent.understanding import binding, operations as ops
from core.agent.understanding import resolution as res

# 「都用七月」这类整体回答里出现的字段无关词，不参与候选匹配
_STOP_WORDS = ("都", "全部", "两个", "这个", "那个", "用", "要", "就")


def match_candidate(candidates: List[Dict[str, Any]],
                    text: str) -> Optional[Dict[str, Any]]:
    """按保存时的编号/标签把文字答复映射到一个候选。

    支持：「第二组」「2」「配对模式」「武汉市_市」。匹配不到返回 None，
    由调用方按自由文本处理。
    """
    raw = (text or "").strip()
    if not raw or not candidates:
        return None

    ordinal = binding.parse_ordinal(raw)
    if ordinal is None and raw.isdigit():
        ordinal = int(raw)
    if ordinal is not None:
        for item in candidates:
            if str(item.get("id")) == str(ordinal):
                return item

    cleaned = raw
    for word in _STOP_WORDS:
        cleaned = cleaned.replace(word, "")
    cleaned = cleaned.strip()
    for item in candidates:
        label = str(item.get("label") or "")
        if label and (label == raw or label == cleaned):
            return item
    hits = [item for item in candidates
            if str(item.get("label") or "")
            and (str(item["label"]) in raw or raw in str(item["label"]))]
    return hits[0] if len(hits) == 1 else None


def resolve_answer(question: Dict[str, Any], text: str,
                   ctx: res.ResolveContext) -> Optional[res.DraftChange]:
    """把一条答复变成对目标任务的草稿修改；无法应用时返回 None。"""
    constraint = question.get("answer_constraint") or {}
    field_name = str(constraint.get("field") or "")
    candidates = list(question.get("candidates") or [])
    targets = list(question.get("targets") or [])
    chosen = match_candidate(candidates, text)

    if field_name == "target":
        return _resolve_target_answer(constraint, chosen, ctx)

    if field_name not in ops.ALL_FIELDS:
        return None

    value: Any = chosen.get("value") if chosen else (text or "").strip()
    if field_name == ops.F_REGION and chosen:
        # 候选里存的是绝对路径，绑定时用显示名走同一套匹配逻辑
        value = chosen.get("label") or value
    if field_name == ops.F_DATASETS and not chosen:
        value = _parse_datasets(text)
    if value in (None, "", []):
        return None

    patch = ops.FieldPatch(field=field_name, action=ops.PATCH_SET,
                           value=value, evidence=(text or "").strip())
    op = ops.CandidateOperation(op=ops.OP_ANSWER, patches=(patch,),
                                answer_text=text)

    for target in targets:
        task = _find_task(ctx, str(target.get("task_id") or ""))
        if task is None:
            continue
        change = res.DraftChange(
            action=res.ACT_UPDATE,
            capability=str(task.get("capability") or ""),
            label=str(task.get("label") or ""),
            slots=task.get("slots") or {},
            task_id=str(task.get("id")),
            expected_version=int(task.get("version") or 1),
        )
        return res._apply_and_validate(change, op, ctx)
    return None


def _resolve_target_answer(constraint: Dict[str, Any],
                           chosen: Optional[Dict[str, Any]],
                           ctx: res.ResolveContext) -> Optional[res.DraftChange]:
    """「你想取消哪一个」这类目标选择：选中后执行当初挂起的那个动作。"""
    if not chosen:
        return None
    task = _find_task(ctx, str(chosen.get("value") or ""))
    if task is None:
        return None
    action = str(constraint.get("pending_action") or res.ACT_UPDATE)
    return res.DraftChange(
        action=action,
        capability=str(task.get("capability") or ""),
        label=str(task.get("label") or ""),
        slots=task.get("slots") or {},
        task_id=str(task.get("id")),
        expected_version=int(task.get("version") or 1),
        priority=1 if action == res.ACT_PRIORITY else None,
    )


def _find_task(ctx: res.ResolveContext, task_id: str) -> Optional[Dict[str, Any]]:
    for task in ctx.open_tasks:
        if str(task.get("id")) == task_id:
            return task
    return None


def _parse_datasets(text: str) -> List[str]:
    """从自由文本里认出数据集合（只认注册过的三种，不扩展）。"""
    lowered = (text or "").lower()
    hits = []
    if "landsat" in lowered or "陆地卫星" in lowered:
        hits.append("landsat")
    if "sentinel" in lowered or "哨兵" in lowered:
        hits.append("sentinel2")
    if "dem" in lowered or "高程" in lowered:
        hits.append("dem")
    return sorted(set(hits))
