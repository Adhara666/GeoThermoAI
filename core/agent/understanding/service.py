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
                          active_study_area_paths: Sequence[Path] = (),
                          ledger: Dict[str, List[Dict[str, Any]]],
                          default_product: str = "lst_10m",
                          default_model: str = "rf") -> res.ResolveContext:
    return res.ResolveContext(
        message=message,
        anchor_date=timeparse.anchor_from(received_at, tz_offset),
        tz_offset=tz_offset,
        chat_mode=chat_mode,
        study_area_paths=list(study_area_paths),
        active_study_area_paths=list(active_study_area_paths),
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
        if change.action == res.ACT_CREATE:
            dup = _find_duplicate_open_task(change, ctx.open_tasks)
            if dup is not None:
                # 重复需求抑制（用户实测：同一句消息重发/模型断连恢复后
                # 重发会导致任务与追问成倍累积）：本对话已有同指纹
                # （研究区+时间+能力）的开放任务时，不新建、不重复追问，
                # 直接展示既有任务及其问题。
                result.notes.append("该需求已存在，未重复创建")
                continue
        result.tasks.append(
            _commit_change(store, change, user_id=user_id,
                           project_id=project_id,
                           conversation_id=conversation_id,
                           command_id=command_id, message_id=message_id))
        result.notes.extend(change.notes)

    return _finalize(result, outcome, store, user_id=user_id,
                     conversation_id=conversation_id,
                     lang=res._lang_of(message))


def _task_fingerprint(task_or_slots: Dict[str, Any]) -> tuple:
    """任务指纹（研究区+时间范围）：判断两次表达是否同一个需求。"""
    slots = task_or_slots.get("slots") if "slots" in task_or_slots \
        else task_or_slots
    fields = ((slots or {}).get("fields") or {})
    region = fields.get(ops.F_REGION) or {}
    detail = (region.get("detail") or {}) if isinstance(region, dict) else {}
    tval = ((fields.get(ops.F_TIME) or {}).get("value") or {})
    return (
        str(detail.get("path") or (region.get("value") if isinstance(region, dict) else "") or ""),
        str(tval.get("start") or ""),
        str(tval.get("end") or ""),
    )


def _find_duplicate_open_task(change: res.DraftChange,
                              open_tasks) -> Optional[Dict[str, Any]]:
    """查找同指纹的开放任务；指纹全空（未绑定研究区且无时间）不判重。"""
    fp = _task_fingerprint(change.slots)
    if not any(fp):
        return None
    for task in open_tasks or []:
        if str(task.get("capability") or "") != str(change.capability or ""):
            continue
        if _task_fingerprint(task) == fp:
            return task
    return None


def _dedupe_questions_for_display(questions, tasks):
    """展示去重：同指纹任务只保留最新一轮的问题；同任务内同题去重。

    用户重复发送同一需求时，历史任务仍在台账（可在任务面板查看），
    但气泡不重复刷屏；模型断连恢复后的重发也不会堆出多套追问。
    """
    latest = {}
    for t in tasks or []:
        fp = _task_fingerprint(t)
        if not any(fp):
            continue
        prev = latest.get(fp)
        if prev is None or str(t.get("created_at") or "") >= \
                str(prev.get("created_at") or ""):
            latest[fp] = t
    keep = {str(t.get("task_id")) for t in latest.values()}
    by_id = {str(t.get("task_id")): t for t in tasks or []}
    out, seen = [], set()
    for q in questions or []:
        targets = q.get("targets") or []
        tid = str((targets[0] or {}).get("task_id") or "") if targets else ""
        tinfo = by_id.get(tid)
        fp = _task_fingerprint(tinfo) if tinfo else ()
        if any(fp) and tid not in keep:
            continue  # 旧的同指纹任务：已被最新一轮替代展示
        key = (tid, str(q.get("prompt") or ""),
               tuple(str(c.get("id")) for c in (q.get("candidates") or [])))
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _finalize(result: UnderstandingResult, outcome: res.ResolutionOutcome,
              store, *, user_id: str, conversation_id: str, lang: str = "zh"
              ) -> UnderstandingResult:
    """按落库后的真实状态决定这一轮给用户什么。"""
    pending = load_ledger_context(store, user_id=user_id,
                                  conversation_id=conversation_id)
    tasks_all = list(pending.get("tasks") or [])
    result.open_questions = _dedupe_questions_for_display(
        pending.get("questions") or [], tasks_all)

    ready = [t for t in result.tasks
             if t.get("summary_status") == t_store.TASK_READY]
    result.ready_tasks = ready

    if result.open_questions:
        result.kind = KIND_ASK
        message = _compose_ask(result.open_questions,
                               result.tasks + tasks_all, lang)
        if "该需求已存在，未重复创建" in result.notes:
            message = (("这句话和已有任务重复，这次没有重复创建；"
                        "下面是它还在等的信息：\n\n" + message)
                       if lang == "zh" else
                       ("This message duplicates an existing task, so no new "
                        "task was created. Here is what it is still waiting "
                        "for:\n\n" + message))
        result.message = message
        return result
    if "该需求已存在，未重复创建" in result.notes and not result.tasks:
        # 重复需求且既有任务不缺信息（如已提交执行）——给出明确提示，
        # 不落入 reply_only（否则会被当作普通闲聊交给模型）
        result.kind = KIND_NOOP
        result.message = (("这句话与已记录的任务重复（研究区和时间相同），"
                           "这次没有重复创建；可在「任务与进度」里查看它的状态。")
                          if lang == "zh" else
                          ("This message duplicates an existing task (same "
                           "study area and time range); no new task was "
                           "created. Its status is shown in Tasks & Progress."))
        return result
    if ready:
        result.kind = KIND_PROCEED
        return result
    if outcome.reply_only and not result.tasks:
        result.kind = KIND_REPLY_ONLY
        return result
    if result.tasks:
        result.kind = KIND_NOOP
        result.message = _compose_noop(result.tasks, lang)
        return result
    result.kind = KIND_REPLY_ONLY
    return result


def _compose_ask(open_questions: List[Dict[str, Any]],
                 changed: List[Dict[str, Any]],
                 lang: str = "zh") -> str:
    """把待答问题汇成清晰的分块文案；多任务时每问独立成块。

    排版约定（第六阶段修正）：每个问题独立成块（空行分隔），
    任务名前缀加粗并带序号，选项用有序列表行“n. 名称”（不缩进、
    前后留空行，避免 Markdown 吞并；编号在创建时已续编为对话内
    全局唯一，提示语与实际显示一致，用户可直接回复编号）。
    文案随用户对话语言（zh/en）。
    """
    labels = {str(t.get("task_id")): str(t.get("label") or "")
              for t in changed}
    single = len(open_questions) == 1
    task_word = "任务" if lang == "zh" else "Task"
    blocks: List[str] = []
    for index, question in enumerate(open_questions, 1):
        targets = question.get("targets") or []
        label = ""
        for target in targets:
            label = labels.get(str(target.get("task_id")), "")
            if label:
                break
        prompt = str(question.get("prompt") or "")
        lines: List[str] = []
        if label:
            # 单问题时任务名不重复占行（冒号只在多任务时用）
            lines.append((f"**{task_word} {index}｜{label}**" if not single
                          else f"**{label}**"))
            lines.append("")  # 段落与正文/列表之间空行，防 Markdown 吞并
        lines.append(prompt)
        candidates = question.get("candidates") or []
        if candidates:
            lines.append("")  # 列表块前后必须空行，否则下一块标题会被并入列表项缩进
        for item in candidates:
            lines.append(f"{item.get('id')}. {item.get('label')}")
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if any(q.get("candidates") for q in open_questions):
        if single:
            # 单问题也要给出“怎么选”的说明（用户反馈：缺了这类提示）
            text += (("\n\n可直接点击下方卡片选择，或回复选项编号、"
                      "直接告诉我选项名称。")
                     if lang == "zh" else
                     ("\n\nClick the card below, or reply with the option "
                      "number or the option name."))
        else:
            text += (("\n\n直接回复选项编号即可（编号在各任务间连续，"
                      "如\"1、3\"），或直接告诉我选项名称。")
                     if lang == "zh" else
                     ("\n\nReply with the option number (numbering continues "
                      "across tasks, e.g. \"1, 3\"), or just tell me the option "
                      "name."))
    return text


def _compose_noop(changed: List[Dict[str, Any]], lang: str = "zh") -> str:
    parts = []
    for task in changed:
        status = str(task.get("summary_status") or "")
        label = str(task.get("label") or ("该任务" if lang == "zh" else "the task"))
        if status == t_store.TASK_CANCELLED:
            parts.append(f"已取消「{label}」。" if lang == "zh"
                         else f'Cancelled "{label}".')
        elif status == t_store.TASK_AWAITING_INFO:
            parts.append(f"「{label}」还缺信息，先记在这里。" if lang == "zh"
                         else f'"{label}" is still missing information; noted for now.')
    return " ".join(parts) or ("已记录你的修改。" if lang == "zh"
                               else "Your changes have been noted.")


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
