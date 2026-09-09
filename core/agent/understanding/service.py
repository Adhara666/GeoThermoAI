# -*- coding: utf-8 -*-
"""理解层 — 服务编排：读上下文 → 要候选 → 校验决定 → 落台账。

依据总体技术方案 §3.4 的两类原子操作：

  草稿修改：比较对象版本 → 应用补丁 → 生成/失效问题 → 递增任务版本 → 记录事件
  消费答案：校验整个问题目标列表 → 保存有效答复 → 更新草稿
            → 完成原问题并原子生成仍缺信息的新问题

一条消息里的多个任务**各自**是一次草稿修改（缺南京边界只阻塞南京）；
同一个共享问题的回答是一次消费答案事务，全部目标校验通过才提交。
两种事务粒度不混用。

模型调用在事务外完成——事务里不调模型、不下载、不算校验和（§3.3）。
"""

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.state_kernel import questions as q_store
from core.state_kernel import tasks as t_store
from core.agent.understanding import answers as answer_lib
from core.agent.understanding import capabilities, operations as ops
from core.agent.understanding import resolution as res
from core.agent.understanding import timeparse
from core.agent.understanding.understander import (
    CandidateUnderstander,
    enforce_chat_mode,
    summarize_for_log,
)

logger = logging.getLogger(__name__)

# 结果类别
KIND_REPLY_ONLY = "reply_only"   # 只回答，不动任何任务
KIND_ASK = "ask"                 # 需要用户补信息，问题已落台账
KIND_PROCEED = "proceed"         # 至少一个任务信息齐全，可交执行链
KIND_FAILED = "failed"           # 模型不可用或输出无效，如实说明
KIND_NOOP = "noop"               # 有变更但没有可执行任务（如取消）


@dataclass
class UnderstandingResult:
    kind: str
    message: str = ""
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    ready_tasks: List[Dict[str, Any]] = field(default_factory=list)
    open_questions: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    candidate_log: str = ""
    raw_model_output: str = ""

    @property
    def should_execute(self) -> bool:
        return self.kind == KIND_PROCEED and bool(self.ready_tasks)


# ── 上下文装载 ───────────────────────────────────────────────────


def load_ledger_context(store, *, user_id: str,
                        conversation_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """读一份任务/问题快照（独立读连接 + 短事务，§3.3）。"""
    def _read(conn):
        return {
            "tasks": t_store.list_open_tasks(conn, user_id=user_id,
                                             conversation_id=conversation_id),
            "questions": q_store.list_open_questions(
                conn, user_id=user_id, conversation_id=conversation_id),
        }

    return store.read(_read)


def build_resolve_context(*, message: str, received_at: datetime.datetime,
                          tz_offset: float, chat_mode: str,
                          study_area_paths: Sequence[Path],
                          ledger: Dict[str, List[Dict[str, Any]]],
                          default_product: str = "lst_10m",
                          default_model: str = "rf") -> res.ResolveContext:
    return res.ResolveContext(
        message=message,
        anchor_date=timeparse.anchor_from(received_at, tz_offset),
        tz_offset=tz_offset,
        chat_mode=chat_mode,
        study_area_paths=list(study_area_paths),
        open_tasks=list(ledger.get("tasks") or []),
        open_questions=list(ledger.get("questions") or []),
        default_product=default_product,
        default_model=default_model,
    )


# ── 主入口：处理一条用户消息 ─────────────────────────────────────


def handle_message(
    store,
    understander: CandidateUnderstander,
    *,
    user_id: str,
    project_id: str,
    conversation_id: str,
    message: str,
    ctx: res.ResolveContext,
    command_id: str = "",
    message_id: str = "",
    history: Optional[List[dict]] = None,
) -> UnderstandingResult:
    """一次完整理解：候选 → 校验 → 落库 → 返回给编排层的决定。"""
    batch = understander.understand(
        message,
        anchor_date=ctx.anchor_date,
        tz_label=_tz_label(ctx.tz_offset),
        chat_mode=ctx.chat_mode,
        study_areas=[p.stem for p in ctx.study_area_paths],
        open_tasks=[_task_line(t) for t in ctx.open_tasks],
        open_questions=[_question_line(q) for q in ctx.open_questions],
        history=history,
    )
    if ctx.chat_mode == "chat":
        batch = enforce_chat_mode(batch)

    outcome = res.resolve(batch, ctx)
    result = UnderstandingResult(
        kind=KIND_NOOP,
        candidate_log=summarize_for_log(batch),
        raw_model_output=batch.raw_output,
        notes=list(outcome.notes),
    )

    if outcome.failure:
        result.kind = KIND_FAILED
        result.message = outcome.failure
        return result

    # 1) 先消费答案（§3.4 消费答案事务）
    for action in outcome.answers:
        applied = _consume_answer(store, action.question_id, action.text, ctx,
                                  user_id=user_id, project_id=project_id,
                                  conversation_id=conversation_id,
                                  command_id=command_id)
        if applied is not None:
            result.tasks.append(applied)
        else:
            result.notes.append("这条回答没能对上任何待答问题，已按新指令处理")

    # 2) 再提交草稿修改（每个任务各自一次事务）
    for change in outcome.changes:
        if not change.task_id and change.action != res.ACT_CREATE:
            # 绑定失败但带追问：登记一条不挂任务的问题
            result.open_questions.extend(
                _commit_unbound_questions(store, change, user_id=user_id,
                                          conversation_id=conversation_id,
                                          command_id=command_id))
            result.notes.extend(change.notes)
            continue
        result.tasks.append(
            _commit_change(store, change, user_id=user_id,
                           project_id=project_id,
                           conversation_id=conversation_id,
                           command_id=command_id, message_id=message_id))
        result.notes.extend(change.notes)

    return _finalize(result, outcome, store, user_id=user_id,
                     conversation_id=conversation_id)


def _finalize(result: UnderstandingResult, outcome: res.ResolutionOutcome,
              store, *, user_id: str, conversation_id: str
              ) -> UnderstandingResult:
    """按落库后的真实状态决定这一轮给用户什么。"""
    pending = load_ledger_context(store, user_id=user_id,
                                  conversation_id=conversation_id)
    result.open_questions = list(pending.get("questions") or [])

    ready = [t for t in result.tasks
             if t.get("summary_status") == t_store.TASK_READY]
    result.ready_tasks = ready

    if result.open_questions:
        result.kind = KIND_ASK
        result.message = _compose_ask(result.open_questions, result.tasks)
        return result
    if ready:
        result.kind = KIND_PROCEED
        return result
    if outcome.reply_only and not result.tasks:
        result.kind = KIND_REPLY_ONLY
        return result
    if result.tasks:
        result.kind = KIND_NOOP
        result.message = _compose_noop(result.tasks)
        return result
    result.kind = KIND_REPLY_ONLY
    return result


def _compose_ask(open_questions: List[Dict[str, Any]],
                 changed: List[Dict[str, Any]]) -> str:
    """把待答问题汇成一段话；多任务时点名是哪个任务在等信息。"""
    labels = {str(t.get("task_id")): str(t.get("label") or "")
              for t in changed}
    lines: List[str] = []
    for question in open_questions:
        targets = question.get("targets") or []
        label = ""
        for target in targets:
            label = labels.get(str(target.get("task_id")), "")
            if label:
                break
        prompt = str(question.get("prompt") or "")
        lines.append(f"{label}：{prompt}" if label and len(open_questions) > 1
                     else prompt)
        for item in question.get("candidates") or []:
            lines.append(f"  {item.get('id')}. {item.get('label')}")
    return "\n".join(lines)


def _compose_noop(changed: List[Dict[str, Any]]) -> str:
    parts = []
    for task in changed:
        status = str(task.get("summary_status") or "")
        label = str(task.get("label") or "该任务")
        if status == t_store.TASK_CANCELLED:
            parts.append(f"已取消「{label}」。")
        elif status == t_store.TASK_AWAITING_INFO:
            parts.append(f"「{label}」还缺信息，先记在这里。")
    return " ".join(parts) or "已记录你的修改。"


# ── 草稿修改事务 ─────────────────────────────────────────────────


def _status_for(change: res.DraftChange) -> str:
    if change.action == res.ACT_CANCEL:
        return t_store.TASK_CANCELLED
    if change.questions or change.missing:
        return t_store.TASK_AWAITING_INFO
    return t_store.TASK_READY


def _commit_change(store, change: res.DraftChange, *, user_id: str,
                   project_id: str, conversation_id: str,
                   command_id: str, message_id: str) -> Dict[str, Any]:
    status = _status_for(change)

    def _tx(conn):
        if change.action == res.ACT_CREATE:
            task_id = t_store.create_task(
                conn, user_id=user_id, project_id=project_id,
                conversation_id=conversation_id,
                capability=change.capability, slots=change.slots,
                label=change.label, summary_status=status,
                ambiguity=change.ambiguity, origin_message_id=message_id,
                origin_command_id=command_id)
            version = 1
        else:
            task_id = change.task_id
            version = t_store.patch_task(
                conn, task_id, change.expected_version,
                slots=change.slots or None,
                capability=change.capability or None,
                summary_status=status,
                ambiguity=change.ambiguity,
                label=change.label or None,
                priority=change.priority,
                event_payload={"action": change.action,
                               "missing": change.missing})
        q_store.expire_questions_for_task(
            conn, task_id, reason="任务草稿已更新，旧问题不再适用")
        question_ids = [
            q_store.create_question(
                conn, user_id=user_id, conversation_id=conversation_id,
                qtype=spec.qtype, prompt=spec.prompt,
                candidates=spec.candidates,
                answer_constraint=spec.answer_constraint(),
                targets=[{"task_id": task_id, "task_version": version,
                          "field": spec.field}],
                origin_command_id=command_id)
            for spec in change.questions
        ]
        return {"task_id": task_id, "version": version,
                "question_ids": question_ids}

    written = store.submit_write(_tx)
    return {
        "task_id": written["task_id"], "version": written["version"],
        "question_ids": written["question_ids"],
        "capability": change.capability, "label": change.label,
        "slots": change.slots, "summary_status": status,
        "missing": list(change.missing), "action": change.action,
    }


def _commit_unbound_questions(store, change: res.DraftChange, *, user_id: str,
                              conversation_id: str,
                              command_id: str) -> List[Dict[str, Any]]:
    """没有绑定到具体任务的问题（如「你想取消哪一个」）也要落库。"""
    if not change.questions:
        return []

    def _tx(conn):
        out = []
        for spec in change.questions:
            qid = q_store.create_question(
                conn, user_id=user_id, conversation_id=conversation_id,
                qtype=spec.qtype, prompt=spec.prompt,
                candidates=spec.candidates,
                answer_constraint=spec.answer_constraint(),
                targets=[], origin_command_id=command_id)
            out.append({"id": qid, "prompt": spec.prompt,
                        "candidates": spec.candidates, "targets": []})
        return out

    return store.submit_write(_tx)


# ── 消费答案事务 ─────────────────────────────────────────────────


def _consume_answer(store, question_id: str, text: str,
                    ctx: res.ResolveContext, *, user_id: str, project_id: str,
                    conversation_id: str,
                    command_id: str) -> Optional[Dict[str, Any]]:
    """在一个短事务内校验目标、保存答复、更新草稿、生成后续问题。"""
    question = next((q for q in ctx.open_questions
                     if str(q.get("id")) == question_id), None)
    if question is None:
        return None
    change = answer_lib.resolve_answer(question, text, ctx)
    if change is None:
        return None
    status = _status_for(change)

    def _tx(conn):
        stored, _targets = q_store.validate_answerable(conn, question_id)
        q_store.close_question(conn, question_id, int(stored["version"]),
                               status=q_store.Q_ANSWERED,
                               answer={"text": text,
                                       "field": (stored.get("answer_constraint")
                                                 or {}).get("field", "")})
        if change.action == res.ACT_CREATE or not change.task_id:
            task_id = t_store.create_task(
                conn, user_id=user_id, project_id=project_id,
                conversation_id=conversation_id,
                capability=change.capability, slots=change.slots,
                label=change.label, summary_status=status,
                ambiguity=change.ambiguity, origin_command_id=command_id)
            version = 1
        else:
            task_id = change.task_id
            version = t_store.patch_task(
                conn, task_id, change.expected_version,
                slots=change.slots or None,
                capability=change.capability or None,
                summary_status=status, ambiguity=change.ambiguity,
                label=change.label or None, priority=change.priority,
                event_type="task.answer_applied",
                event_payload={"question_id": question_id,
                               "missing": change.missing})
        q_store.expire_questions_for_task(
            conn, task_id, reason="答案已生效，旧问题不再适用")
        question_ids = [
            q_store.create_question(
                conn, user_id=user_id, conversation_id=conversation_id,
                qtype=spec.qtype, prompt=spec.prompt,
                candidates=spec.candidates,
                answer_constraint=spec.answer_constraint(),
                targets=[{"task_id": task_id, "task_version": version,
                          "field": spec.field}],
                origin_command_id=command_id)
            for spec in change.questions
        ]
        return {"task_id": task_id, "version": version,
                "question_ids": question_ids}

    try:
        written = store.submit_write(_tx)
    except (q_store.QuestionClosedError, q_store.QuestionTargetStaleError) as e:
        logger.info(f"[understanding] 答案未生效：{e}")
        return None
    return {
        "task_id": written["task_id"], "version": written["version"],
        "question_ids": written["question_ids"],
        "capability": change.capability, "label": change.label,
        "slots": change.slots, "summary_status": status,
        "missing": list(change.missing), "action": change.action,
    }


def answer_question(store, *, user_id: str, project_id: str,
                    conversation_id: str, question_id: str, text: str,
                    ctx: res.ResolveContext,
                    command_id: str = "") -> Optional[Dict[str, Any]]:
    """卡片点击的入口——和文字回答共用同一套消费逻辑（§4.4）。"""
    return _consume_answer(store, question_id, text, ctx, user_id=user_id,
                           project_id=project_id,
                           conversation_id=conversation_id,
                           command_id=command_id)


# ── 小工具 ───────────────────────────────────────────────────────


def _tz_label(tz_offset: float) -> str:
    sign = "+" if tz_offset >= 0 else "-"
    hours = abs(float(tz_offset))
    return f"UTC{sign}{hours:g}"


def _task_line(task: Dict[str, Any]) -> str:
    return (f"{task.get('id')}｜{task.get('label') or '未命名'}"
            f"｜{capabilities.label(str(task.get('capability') or ''))}"
            f"｜{task.get('summary_status')}")


def _question_line(question: Dict[str, Any]) -> str:
    return f"{question.get('id')}｜{question.get('prompt') or ''}"
