# -*- coding: utf-8 -*-
"""理解层 — 候选 → 校验绑定 → 草稿修改或持久问题（升级第二阶段：理解层）。

依据总体技术方案 §4.2「目标绑定与字段补丁」流程图：

    候选操作 → 绑定已有对象或建立临时目标 → 逐字段设置/清除/保留
    → 解析日期与匹配真实边界 → 按能力检查必需信息
    → 缺项或歧义则生成持久问题；完整则标记就绪

本模块是**纯函数式**的：只读上下文（真实边界文件列表、台账里的任务与
问题快照），产出「打算怎么改」的描述，不写库、不调模型。
落库由 service.py 在一个短事务里完成（§3.4 草稿修改 / 消费答案）。
"""

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.agent.understanding import binding, capabilities, operations as ops
from core.agent.understanding import timeparse
from core.agent.understanding.slotbook import (
    SRC_ANSWER,
    SRC_DEFAULT,
    SRC_USER,
    SlotBook,
    apply_patch,
)

# 追问顺序：先问决定执行方向的关键信息（§4.4「优先问决定执行方向的关键问题」）
ASK_ORDER = (ops.F_REGION, ops.F_TIME, ops.F_PRODUCT_MODE, ops.F_DATASETS)

# 草稿动作
ACT_CREATE = "create"
ACT_UPDATE = "update"
ACT_CANCEL = "cancel"
ACT_CONTINUE = "continue"
ACT_RETRY = "retry"
ACT_PRIORITY = "priority"


@dataclass
class QuestionSpec:
    """一条待登记的持久问题（真实候选与回答约束在此固定）。"""

    field: str
    prompt: str
    candidates: List[Dict[str, str]] = field(default_factory=list)
    qtype: str = "semantic_clarify"
    missing_fields: List[str] = field(default_factory=list)
    pending_action: str = ""    # field=target 时：答案选定后要执行的动作

    def answer_constraint(self) -> Dict[str, Any]:
        return {"field": self.field,
                "kind": "choice" if self.candidates else "text",
                "missing_fields": list(self.missing_fields),
                "pending_action": self.pending_action}


@dataclass
class DraftChange:
    """对一个任务草稿的修改意图（尚未落库）。"""

    action: str
    capability: str
    label: str
    slots: Dict[str, Any]
    task_id: str = ""
    expected_version: int = 0
    missing: List[str] = field(default_factory=list)
    ambiguity: List[str] = field(default_factory=list)
    questions: List[QuestionSpec] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    priority: Optional[int] = None

    @property
    def ready(self) -> bool:
        return not self.missing and not self.questions and \
            self.action in (ACT_CREATE, ACT_UPDATE, ACT_CONTINUE, ACT_RETRY)


@dataclass
class AnswerAction:
    """用户回答了某个待答问题（答案随后走同一套绑定/校验）。"""

    question_id: str
    text: str


@dataclass
class ResolveContext:
    """理解上下文（§2.2「理解上下文」边界对象；不含凭据与栅格）。"""

    message: str
    anchor_date: datetime.date
    tz_offset: float = 0.0
    chat_mode: str = "work"
    study_area_paths: Sequence[Path] = ()
    open_tasks: Sequence[Dict[str, Any]] = ()
    open_questions: Sequence[Dict[str, Any]] = ()
    default_product: str = "lst_10m"
    default_model: str = "rf"


@dataclass
class ResolutionOutcome:
    changes: List[DraftChange] = field(default_factory=list)
    answers: List[AnswerAction] = field(default_factory=list)
    reply_only: bool = False
    failure: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def blocking_questions(self) -> List[QuestionSpec]:
        out: List[QuestionSpec] = []
        for change in self.changes:
            out.extend(change.questions)
        return out


# ── 入口 ─────────────────────────────────────────────────────────


def turn_negations(batch: ops.CandidateBatch) -> Dict[str, List[Any]]:
    """收集**本条消息**里被清除掉的字段值。

    模型经常一边照抄上一轮的内容、一边给出用户刚说的否定（真实模型在
    「不是武汉」那一轮就同时回显了一条武汉的新建）。本轮的清除优先级最高
    （§4.2「本轮明确值或清除 > 当前任务已确认值 > …」），所以要先把这些值
    收齐，再去应用任何 set，否则模型的回显会把用户刚否定的东西又立起来。
    """
    negated: Dict[str, List[Any]] = {}
    for op in batch.operations:
        for patch in op.patches:
            if patch.action == ops.PATCH_CLEAR and patch.value not in (None, ""):
                negated.setdefault(patch.field, []).append(patch.value)
    return negated


def _contradicts_turn(patch: ops.FieldPatch,
                      negated: Dict[str, List[Any]]) -> bool:
    if patch.action != ops.PATCH_SET:
        return False
    probe = SlotBook({"fields": {}, "negations": [
        {"field": field, "value": value}
        for field, values in negated.items() for value in values]})
    return probe.is_negated(patch.field, patch.value)


def resolve(batch: ops.CandidateBatch,
            ctx: ResolveContext) -> ResolutionOutcome:
    """把候选批次解析成草稿修改与追问。任何无法确定的地方一律追问，不猜。"""
    outcome = ResolutionOutcome()
    if not batch.valid:
        outcome.failure = _failure_message(batch, _lang_of(ctx.message))
        return outcome

    negated = turn_negations(batch)

    # 本轮内新建的临时目标：让同一条消息里的后续 set 能落到正确的草稿上
    fresh: Dict[str, DraftChange] = {}

    for op in batch.operations:
        if op.op == ops.OP_REPLY_ONLY:
            outcome.reply_only = True
            continue
        # 用户在这条消息里刚否定的东西，模型又拿它新建一个任务：整条丢弃。
        # 这不是「少做一件事」，而是不让被否定的地区凭空复活。
        if op.op == ops.OP_CREATE and negated and \
                any(_contradicts_turn(p, negated) for p in op.patches):
            outcome.notes.append("本轮已否定的内容不再新建任务")
            continue
        if op.op == ops.OP_ANSWER:
            question_id = _bind_question(op, ctx)
            if question_id:
                outcome.answers.append(
                    AnswerAction(question_id=question_id,
                                 text=op.answer_text or ctx.message))
            else:
                outcome.notes.append("没有找到这条回答对应的待答问题，按新指令处理")
                outcome.reply_only = True
            continue
        if op.op in (ops.OP_CANCEL, ops.OP_CONTINUE, ops.OP_RETRY,
                     ops.OP_PRIORITY):
            change = _resolve_task_command(op, ctx)
            if change is not None:
                outcome.changes.append(change)
            continue

        change = _resolve_draft_op(op, ctx, fresh, negated)
        if change is None:
            continue
        if op.label:
            fresh[op.label] = change
        outcome.changes.append(change)

    if not outcome.changes and not outcome.answers and not outcome.reply_only:
        outcome.failure = "没有得到可以执行的理解结果"
    return outcome


def _failure_message(batch: ops.CandidateBatch, lang: str = "zh") -> str:
    """模型失败时的说明：讲清楚原因，不默认城市/整月/下载（§4.5）。"""
    if batch.source == "unavailable":
        if lang == "en":
            return ("The language model is temporarily unreachable, so I could not "
                    "parse this message. Confirmed information is kept; you can "
                    "try again later or tell me the study area, time range and "
                    "target product directly.")
        return ("暂时联系不上语言模型，这一句我没法理解。"
                "已经确认过的信息都还在，你可以稍后再说一次，"
                "或者直接告诉我研究区、时间和要做的产品。")
    detail = "；".join(batch.errors[:3]) or ("输出不符合约定格式" if lang == "zh"
                                              else "output format mismatch")
    if lang == "en":
        return (f"I could not turn this into an executable operation ({detail}). "
                f"Please state the study area, time range and what to do separately.")
    return (f"我没能把这句话整理成可执行的操作（{detail}）。"
            f"请把研究区、时间范围和要做的事分开说一次。")


# ── 目标绑定 ─────────────────────────────────────────────────────


def _bind_question(op: ops.CandidateOperation, ctx: ResolveContext) -> str:
    """把 answer 操作绑定到一条真实的待答问题。"""
    open_questions = list(ctx.open_questions)
    if op.question_id:
        for question in open_questions:
            if str(question.get("id")) == op.question_id:
                return op.question_id
    if len(open_questions) == 1:
        return str(open_questions[0].get("id"))
    return ""


def _resolve_task_command(op: ops.CandidateOperation,
                          ctx: ResolveContext) -> Optional[DraftChange]:
    """继续 / 重试 / 取消 / 调整优先级：必须唯一绑定到一个已有任务。"""
    result = binding.bind_task(op.target_ref, list(ctx.open_tasks))
    action = {ops.OP_CANCEL: ACT_CANCEL, ops.OP_CONTINUE: ACT_CONTINUE,
              ops.OP_RETRY: ACT_RETRY, ops.OP_PRIORITY: ACT_PRIORITY}[op.op]

    if result.kind == binding.BIND_UNIQUE and result.task:
        task = result.task
        return DraftChange(
            action=action,
            capability=str(task.get("capability") or ""),
            label=str(task.get("label") or ""),
            slots=task.get("slots") or {},
            task_id=str(task.get("id")),
            expected_version=int(task.get("version") or 1),
            priority=1 if op.op == ops.OP_PRIORITY else None,
        )

    verb = {ACT_CANCEL: "取消", ACT_CONTINUE: "继续", ACT_RETRY: "重试",
            ACT_PRIORITY: "调整顺序"}[action]
    if result.kind == binding.BIND_EMPTY:
        return DraftChange(action=action, capability="", label="", slots={},
                           notes=[f"现在没有可以{verb}的任务"])
    change = DraftChange(action=action, capability="", label="", slots={})
    change.questions.append(QuestionSpec(
        field="target",
        prompt=f"你想{verb}哪一个？",
        candidates=result.options,
        pending_action=action,
    ))
    return change


def _resolve_draft_op(op: ops.CandidateOperation, ctx: ResolveContext,
                      fresh: Dict[str, DraftChange],
                      negated: Optional[Dict[str, List[Any]]] = None
                      ) -> Optional[DraftChange]:
    """新建 / 设值 / 清除 / 更正：确定落到哪个草稿，再逐字段处理。"""
    if op.op == ops.OP_CREATE:
        change = DraftChange(action=ACT_CREATE, capability=op.capability,
                             label="", slots=SlotBook().to_bundle())
        return _apply_and_validate(change, op, ctx, negated=negated)

    # set / clear / correct：先看本轮新建的临时目标，再看台账里的任务
    if op.label and op.label in fresh:
        target = fresh[op.label]
        return _apply_and_validate(target, op, ctx, in_place=True,
                                   negated=negated)

    result = binding.bind_task(op.target_ref, list(ctx.open_tasks))
    if result.kind == binding.BIND_UNIQUE and result.task:
        task = result.task
        change = DraftChange(
            action=ACT_UPDATE,
            capability=op.capability or str(task.get("capability") or ""),
            label=str(task.get("label") or ""),
            slots=task.get("slots") or {},
            task_id=str(task.get("id")),
            expected_version=int(task.get("version") or 1),
        )
        return _apply_and_validate(change, op, ctx, negated=negated)

    if result.kind == binding.BIND_AMBIGUOUS:
        change = DraftChange(action=ACT_UPDATE, capability="", label="", slots={})
        change.questions.append(QuestionSpec(
            field="target", prompt="这句话是对哪一个任务说的？",
            candidates=result.options))
        return change

    # 台账里没有可绑定的任务：本条消息自己开一个草稿承载这次修改
    # （「不是武汉」也要留下否定记录，下一轮才不会被历史填回来）
    change = DraftChange(action=ACT_CREATE, capability=op.capability, label="",
                         slots=SlotBook().to_bundle())
    return _apply_and_validate(change, op, ctx, negated=negated)


# ── 字段处理与校验 ───────────────────────────────────────────────


def _apply_and_validate(change: DraftChange, op: ops.CandidateOperation,
                        ctx: ResolveContext,
                        in_place: bool = False,
                        negated: Optional[Dict[str, List[Any]]] = None
                        ) -> DraftChange:
    book = SlotBook(change.slots)
    source = SRC_ANSWER if op.op == ops.OP_ANSWER else SRC_USER

    # 先落清除，再落设置：同一条消息里用户的否定优先于模型的回显
    ordered = sorted(op.patches, key=lambda p: p.action != ops.PATCH_CLEAR)
    for patch in ordered:
        if negated and _contradicts_turn(patch, negated):
            continue
        book, _ = apply_patch(book, patch, ctx.message, source=source)

    book, questions, notes = _validate(book, change.capability, ctx)
    change.slots = book.to_bundle()
    change.questions = (change.questions + questions) if in_place else questions
    change.notes = list(dict.fromkeys(change.notes + notes))
    change.missing = _missing_fields(book, change.capability)
    change.ambiguity = list(op.ambiguity)
    change.label = _label_for(book, change.capability, _lang_of(ctx.message))
    return change


def _validate(book: SlotBook, capability: str, ctx: ResolveContext):
    """把原始表达变成真实对象：边界文件、绝对日期、枚举值。"""
    questions: List[QuestionSpec] = []
    notes: List[str] = []
    lang = _lang_of(ctx.message)

    book, region_q, region_note = _resolve_region(book, ctx, lang)
    if region_q:
        questions.append(region_q)
    if region_note:
        notes.append(region_note)

    book, time_q = _resolve_time(book, ctx, lang)
    if time_q:
        questions.append(time_q)

    book, mode_q = _resolve_product_mode(book, capability, lang)
    if mode_q:
        questions.append(mode_q)

    book = _resolve_datasets(book)

    # 默认值只在字段缺失时补，且来源如实记为默认（9.2 第 4 条）
    book = book.fill_default(ops.F_PRODUCT, ctx.default_product, SRC_DEFAULT)
    book = book.fill_default(ops.F_MODEL, ctx.default_model, SRC_DEFAULT)

    # 只保留优先级最高的那条追问：一次只问一个决定方向的问题，
    # 其余缺项在这条问题的正文里一并列清（§4.4）
    if len(questions) > 1:
        questions.sort(key=lambda q: ASK_ORDER.index(q.field)
                       if q.field in ASK_ORDER else len(ASK_ORDER))
        head, rest = questions[0], questions[1:]
        head.missing_fields = [q.field for q in questions]
        if lang == "en":
            head.prompt = head.prompt + " (Still missing: " + \
                ", ".join(_field_label(q.field, lang) for q in rest) + ")"
        else:
            head.prompt = head.prompt + "（这个任务还差：" + \
                "、".join(_field_label(q.field, lang) for q in rest) + "）"
        questions = [head]
    elif questions:
        questions[0].missing_fields = [questions[0].field]
    return book, questions, notes


def _resolve_region(book: SlotBook, ctx: ResolveContext, lang: str = "zh"):
    raw = book.value(ops.F_REGION)
    detail = book.get(ops.F_REGION).get("detail") or {}
    if detail.get("path") and Path(str(detail["path"])).is_file():
        return book, None, ""          # 已经绑定过真实文件，不重复解析

    name = "" if raw is None else str(raw).strip()
    if isinstance(raw, dict):
        name = str(raw.get("display") or raw.get("value") or "").strip()

    result = binding.bind_region(name, ctx.study_area_paths)
    if result.kind == binding.BIND_UNIQUE:
        # 被否定过的地区不能靠「只剩一个候选」悄悄回来（9.2 第 4 条）
        if book.is_negated(ops.F_REGION, result.display):
            return book.drop(ops.F_REGION), QuestionSpec(
                field=ops.F_REGION,
                prompt=("这次要处理哪个研究区？" if lang == "zh"
                        else "Which study area should be processed this time?"),
                candidates=result.options), ""
        note = (("只有一个可用研究区，本次采用它" if lang == "zh"
                 else "Only one study area is available; it will be used this time.")
                if not name else "")
        return (book.set(ops.F_REGION, result.display,
                         book.source(ops.F_REGION) or SRC_USER,
                         evidence=book.get(ops.F_REGION).get("evidence", ""),
                         detail=result.to_slot_detail(), override=True),
                None, note)

    # 绑不到真实文件的地名不是研究区，只是一串没落地的文字：把槽位清空，
    # 别让它显示在任务卡上、也别让必需信息检查误以为地区已经有了
    unbound = book.drop(ops.F_REGION)

    if result.kind == binding.BIND_EMPTY:
        return unbound, QuestionSpec(
            field=ops.F_REGION,
            prompt=("还没有看到你上传的研究区文件，请先上传研究区"
                    "（GeoJSON 或 Shapefile），我再安排流程。"
                    if lang == "zh" else
                    "No uploaded study area file was found. Please upload a "
                    "study area (GeoJSON or Shapefile) first, then I will "
                    "arrange the workflow.")), ""
    if result.kind == binding.BIND_AMBIGUOUS:
        listed = ("、".join(o["label"] for o in result.options[:6]) if lang == "zh"
                  else ", ".join(o["label"] for o in result.options[:6]))
        prompt = (f"「{name}」匹配到多个研究区：{listed}，你要处理哪一个？"
                  if lang == "zh" else
                  f'"{name}" matches multiple study areas: {listed}. '
                  f"Which one do you want to process?")
        return unbound, QuestionSpec(
            field=ops.F_REGION, prompt=prompt,
            candidates=result.options), ""
    listed = ("、".join(o["label"] for o in result.options[:6]) if lang == "zh"
              else ", ".join(o["label"] for o in result.options[:6]))
    if lang == "zh":
        prompt = (f"没有找到名为「{name}」的研究区。已上传的有：{listed}，要用哪一个？"
                  if name else f"你已上传的研究区有：{listed}。这次要处理哪一个？")
    else:
        prompt = (f'No study area named "{name}" was found. Uploaded ones: '
                  f"{listed}. Which one should be used?" if name else
                  f"You have these study areas uploaded: {listed}. "
                  f"Which one should be processed?")
    return unbound, QuestionSpec(field=ops.F_REGION, prompt=prompt,
                                 candidates=result.options), ""


def _resolve_time(book: SlotBook, ctx: ResolveContext, lang: str = "zh"):
    raw = book.value(ops.F_TIME)
    if isinstance(raw, dict) and raw.get("start") and raw.get("end"):
        return book, None              # 已经是绝对区间，不重新解释

    expression = "" if raw is None else str(raw).strip()
    if not expression:
        return book, QuestionSpec(
            field=ops.F_TIME,
            prompt=("要处理哪个时间范围？可以说具体月份（如 2025 年 7 月）、"
                    "具体某天（如 2025-07-15），也可以说相对时间（如上个月）。"
                    if lang == "zh" else
                    "Which time range should be processed? You can give a "
                    "month (e.g., July 2025), a specific day (e.g., "
                    "2025-07-15), or a relative time (e.g., last month)."))

    resolved = timeparse.resolve(expression, anchor_date=ctx.anchor_date,
                                 tz_offset=ctx.tz_offset)
    if not resolved.ok:
        if lang == "en":
            return book.clear(ops.F_TIME), QuestionSpec(
                field=ops.F_TIME,
                prompt=f"{resolved.reason} Please provide an unambiguous time, "
                       f"e.g. July 2025, or 2025-07-01 to 2025-07-31.")
        return book.clear(ops.F_TIME), QuestionSpec(
            field=ops.F_TIME,
            prompt=f"{resolved.reason}。请给一个能唯一确定的时间，"
                   f"例如 2025 年 7 月，或 2025-07-01 到 2025-07-31。")
    return book.set(ops.F_TIME, resolved.to_slot_value(),
                    book.source(ops.F_TIME) or SRC_USER,
                    evidence=book.get(ops.F_TIME).get("evidence", ""),
                    override=True), None


def _resolve_product_mode(book: SlotBook, capability: str, lang: str = "zh"):
    """整月才问配对/月度合成；非整月按配对，来源记为默认（§4.4）。"""
    if capability not in capabilities.MODE_ONLY_FOR_WHOLE_MONTH:
        return book, None
    time_value = book.value(ops.F_TIME)
    if not isinstance(time_value, dict) or not time_value.get("start"):
        return book, None              # 时间还没定，先解决时间

    raw_mode = str(book.value(ops.F_PRODUCT_MODE) or "").strip().lower()
    if raw_mode in ops.ALL_PRODUCT_MODES:
        return book, None
    if raw_mode:
        book = book.clear(ops.F_PRODUCT_MODE)

    if not timeparse.is_whole_month(str(time_value.get("start")),
                                    str(time_value.get("end"))):
        return book.fill_default(ops.F_PRODUCT_MODE, ops.MODE_PAIR,
                                 SRC_DEFAULT), None

    if lang == "en":
        try:
            month = (f"{_MONTHS_EN[int(time_value['start'][5:7]) - 1]} "
                     f"{time_value['start'][:4]}")
        except (ValueError, IndexError):
            month = str(time_value["start"])[:7]
        return book, QuestionSpec(
            field=ops.F_PRODUCT_MODE,
            prompt=f"Your time range is {month} (a whole month). "
                   f"Which approach should be used?",
            candidates=[
                {"id": "1", "label": "Pair mode", "value": ops.MODE_PAIR},
                {"id": "2", "label": "Monthly composite mode",
                 "value": ops.MODE_MONTHLY},
            ])
    month = f"{time_value['start'][:4]} 年 {int(time_value['start'][5:7])} 月"
    return book, QuestionSpec(
        field=ops.F_PRODUCT_MODE,
        prompt=f"你的时间范围是{month}（整月），要按哪种方式做？",
        candidates=[
            {"id": "1", "label": "配对模式", "value": ops.MODE_PAIR},
            {"id": "2", "label": "月度合成模式", "value": ops.MODE_MONTHLY},
        ])


def _resolve_datasets(book: SlotBook) -> SlotBook:
    raw = book.value(ops.F_DATASETS)
    if raw is None:
        return book
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    cleaned = [str(v).strip().lower() for v in values]
    cleaned = [v for v in cleaned if v in ops.DATASETS]
    if not cleaned:
        return book.clear(ops.F_DATASETS)
    return book.set(ops.F_DATASETS, sorted(set(cleaned)),
                    book.source(ops.F_DATASETS) or SRC_USER, override=True)


def _missing_fields(book: SlotBook, capability: str) -> List[str]:
    return [f for f in capabilities.required_fields(capability)
            if not book.has_value(f)]


def _field_label(name: str, lang: str = "zh") -> str:
    if lang == "en":
        return {ops.F_REGION: "study area", ops.F_TIME: "time range",
                ops.F_PRODUCT_MODE: "product mode", ops.F_DATASETS: "datasets",
                ops.F_PRODUCT: "product type", ops.F_MODEL: "model",
                "target": "target task"}.get(name, name)
    return {ops.F_REGION: "研究区", ops.F_TIME: "时间范围",
            ops.F_PRODUCT_MODE: "产品方式", ops.F_DATASETS: "数据集合",
            ops.F_PRODUCT: "产品类型", ops.F_MODEL: "模型",
            "target": "目标任务"}.get(name, name)


def _lang_of(text: str) -> str:
    """按用户消息判定语言：含 CJK→zh，否则有拉丁字母→en。

    任务名等程序生成文案遵循“用户当时使用的语言”（英文对话→英文任务名）。"""
    s = str(text or "")
    if any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return "zh"
    return "en" if any(ch.isascii() and ch.isalpha() for ch in s) else "zh"


_MONTHS_EN = ("January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November",
              "December")


def _label_for(book: SlotBook, capability: str, lang: str = "zh") -> str:
    """任务卡片标签：研究区 + 时间 + 能力（随用户对话语言）。"""
    parts: List[str] = []
    region = book.value(ops.F_REGION)
    if region:
        parts.append(str(region))
    time_value = book.value(ops.F_TIME)
    if isinstance(time_value, dict) and time_value.get("start"):
        start, end = str(time_value["start"]), str(time_value.get("end") or "")
        if lang == "en":
            if start == end:
                parts.append(start)
            elif start[:7] == end[:7]:
                try:
                    parts.append(f"{_MONTHS_EN[int(start[5:7]) - 1]} {start[:4]}")
                except (ValueError, IndexError):
                    parts.append(start[:7])
            else:
                parts.append(f"{start} to {end}")
        else:
            parts.append(start if start == end
                         else (f"{start[:4]} 年 {int(start[5:7])} 月"
                               if start[:7] == end[:7] else f"{start} 至 {end}"))
    elif time_value:
        parts.append(str(time_value))
    if capability:
        parts.append(capabilities.label(capability, lang))
    else:
        parts.append("待确认目标" if lang == "zh" else "target pending")
    return " ".join(parts)
