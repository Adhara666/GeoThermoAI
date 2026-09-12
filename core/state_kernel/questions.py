# -*- coding: utf-8 -*-
"""状态内核 — 持久问题的原子操作（升级第二阶段：理解层）。

依据总体技术方案 §3.4「消费答案」与 §4.4「澄清、审批与自动选择」：

  同一短事务内完成：校验整个问题目标列表 → 保存有效答复 → 按问题类型
  更新草稿 → 完成原问题，并原子生成仍缺信息的新问题。

关键约束：

  - **问题落库**，不靠一条线程 `threading.Event` 等半小时；服务重启后
    未回答的问题仍在（升级方案 9.2 第 5、6 条）。
  - **答案一次消费**：已回答/已失效的问题再次提交只返回既有结果，
    不会重复生效（§4.4「旧卡片因此不能重复回答」）。
  - **问题绑定目标版本**：目标任务版本变化后旧问题自动失效，
    过期卡片点提交时得到「已失效」而不是错误执行（§4.4）。
  - 候选按**保存时的编号**解释：文字「第二组」映射到这份已保存候选，
    不按重新排序后的数组下标选择（§4.4）。
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.state_kernel.store import (
    append_event,
    insert_versioned,
    new_id,
    update_versioned,
    utcnow_iso,
)

# 问题状态（§3.5「问题记录有待答、已回答、被新问题接替、失效状态」）
Q_OPEN = "open"
Q_ANSWERED = "answered"
Q_SUPERSEDED = "superseded"
Q_EXPIRED = "expired"

# 问题类型（§4.4 三类）
QT_SEMANTIC = "semantic_clarify"     # 语义澄清：地区/时间/产品方式/目标不唯一
QT_APPROVAL = "execution_approval"   # 执行审批：计划、候选、调优
QT_DATA_CHOICE = "data_choice"       # 数据异常选择


class QuestionClosedError(Exception):
    """问题已被回答、接替或失效（答案一次消费；过期卡片不得重复执行）。"""


class QuestionTargetStaleError(Exception):
    """问题目标的任务版本已变化，本问题不再适用于当前草稿。"""


_Q_COLUMNS = (
    "id, user_id, conversation_id, qtype, candidates, answer_constraint,"
    " status, version, created_at, updated_at, prompt, answer, answered_at,"
    " superseded_by, origin_command_id"
)
_Q_FIELDS = [c.strip() for c in _Q_COLUMNS.split(",")]


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _row_to_question(row: Any) -> Dict[str, Any]:
    q = dict(zip(_Q_FIELDS, row))
    q["candidates"] = _loads(q.get("candidates"), [])
    q["answer_constraint"] = _loads(q.get("answer_constraint"), {})
    q["answer"] = _loads(q.get("answer"), None)
    return q


def _load_targets(conn, question_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT task_id, task_version, run_id, field FROM question_targets"
        " WHERE question_id = ? ORDER BY rowid ASC",
        (question_id,),
    ).fetchall()
    return [{"task_id": r[0], "task_version": r[1], "run_id": r[2], "field": r[3]}
            for r in rows]


# ── 写操作（必须在写事务内调用） ─────────────────────────────────


def renumber_candidates(conn, conversation_id: str,
                        candidates: Optional[Sequence[Dict[str, Any]]]
                        ) -> List[Dict[str, Any]]:
    """把候选编号续编为对话内全局唯一（§4.4：编号保存后可稳定引用）。

    问题创建时统一调用：新问题候选编号从“本对话已出现过的最大编号 + 1”
    开始连排——提示语“如 1、3”与实际显示一致，用户按编号回复也不会在
    多个任务之间产生歧义（旧对话数据不受影响）。仅对**纯数字编号**的
    候选生效（配对/产品方式等选择题）；语义编号（accept/ai_tune 等
    审批选项）保持原样，保证答案回传可与审批载荷匹配。
    """
    items = [dict(c) for c in (candidates or [])]
    if not items:
        return items
    # 仅对“数字编号型”候选续编（如配对/产品方式选择，用户按编号回复）；
    # 带语义编号的审批选项（accept / ai_tune / start 等）保持原样——
    # 否则答案按编号回传时与审批载荷无法匹配（scheduler.answer 校验失败）
    for c in items:
        try:
            int(str(c.get("id")))
        except (TypeError, ValueError):
            return items
    max_id = 0
    for (raw,) in conn.execute(
            "SELECT candidates FROM questions WHERE conversation_id = ?",
            (conversation_id,)):
        try:
            for c in json.loads(raw or "[]"):
                max_id = max(max_id, int(str(c.get("id"))))
        except (TypeError, ValueError):
            continue
    for c in items:
        max_id += 1
        c["id"] = str(max_id)
    return items


def create_question(
    conn,
    *,
    user_id: str,
    conversation_id: str,
    qtype: str,
    prompt: str,
    targets: Sequence[Dict[str, Any]],
    candidates: Optional[Sequence[Dict[str, Any]]] = None,
    answer_constraint: Optional[Dict[str, Any]] = None,
    origin_command_id: str = "",
) -> str:
    """登记一个持久问题及其覆盖目标列表（目标列表创建后固定，§3.2）。

    `targets` 每项：`{"task_id", "task_version", "field", "run_id"(可选)}`。
    `candidates` 每项：`{"id", "label", "value"(可选)}`——编号在此固定，
    答案按编号解释；编号自动续编为对话内全局唯一（跨问题连排）。
    """
    question_id = new_id()
    candidates = renumber_candidates(conn, conversation_id, candidates)
    insert_versioned(
        conn,
        "questions",
        object_id=question_id,
        fields={
            "user_id": user_id,
            "conversation_id": conversation_id,
            "qtype": qtype,
            "candidates": list(candidates or []),
            "answer_constraint": dict(answer_constraint or {}),
            "status": Q_OPEN,
            "prompt": prompt,
            "origin_command_id": origin_command_id,
        },
    )
    for target in targets:
        conn.execute(
            "INSERT INTO question_targets (question_id, task_id, task_version,"
            " run_id, field) VALUES (?, ?, ?, ?, ?)",
            (
                question_id,
                str(target.get("task_id") or ""),
                int(target.get("task_version") or 0),
                target.get("run_id"),
                str(target.get("field") or ""),
            ),
        )
    append_event(
        conn,
        type="question.opened",
        user_id=user_id,
        conversation_id=conversation_id,
        task_id=(targets[0].get("task_id") if targets else None),
        object_type="question",
        object_id=question_id,
        object_version=1,
        payload={"qtype": qtype, "fields": sorted(
            {str(t.get("field") or "") for t in targets})},
    )
    return question_id


def close_question(
    conn,
    question_id: str,
    expected_version: int,
    *,
    status: str,
    answer: Optional[Dict[str, Any]] = None,
    superseded_by: str = "",
    reason: str = "",
) -> int:
    """把问题置为终态（已回答 / 被接替 / 失效），返回新版本号。"""
    patch: Dict[str, Any] = {"status": status}
    if answer is not None:
        patch["answer"] = answer
        patch["answered_at"] = utcnow_iso()
    if superseded_by:
        patch["superseded_by"] = superseded_by
    new_version = update_versioned(conn, "questions", question_id,
                                   expected_version, patch)
    row = conn.execute(
        "SELECT user_id, conversation_id FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    append_event(
        conn,
        type=f"question.{status}",
        user_id=row[0] if row else None,
        conversation_id=row[1] if row else None,
        object_type="question",
        object_id=question_id,
        object_version=new_version,
        payload={"reason": reason} if reason else {},
    )
    return new_version


def expire_questions_for_task(conn, task_id: str, *, reason: str,
                              keep_ids: Sequence[str] = ()) -> List[str]:
    """任务草稿版本变化后，让指向它的开放问题失效（不再能启动修改后的任务）。

    返回被失效的问题编号列表。`keep_ids` 用于保留本轮刚创建的新问题。
    """
    keep = set(keep_ids)
    rows = conn.execute(
        "SELECT DISTINCT q.id, q.version FROM questions q"
        " JOIN question_targets t ON t.question_id = q.id"
        " WHERE t.task_id = ? AND q.status = ?",
        (task_id, Q_OPEN),
    ).fetchall()
    expired: List[str] = []
    for qid, version in rows:
        if qid in keep:
            continue
        close_question(conn, qid, int(version), status=Q_EXPIRED, reason=reason)
        expired.append(str(qid))
    return expired


def validate_answerable(conn, question_id: str) -> Tuple[Dict[str, Any],
                                                         List[Dict[str, Any]]]:
    """校验问题当前是否可回答；返回 (问题, 目标列表)。

    §3.4「消费答案」要求**校验整个问题目标列表**后才保存答复：
    任一目标任务版本已变化或任务已消失，整份答案都不部分生效。
    """
    row = conn.execute(
        f"SELECT {_Q_COLUMNS} FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"问题不存在：{question_id}")
    question = _row_to_question(row)
    if question["status"] != Q_OPEN:
        raise QuestionClosedError(
            f"问题已处于 {question['status']} 状态，答案一次消费，不再重复生效"
        )
    targets = _load_targets(conn, question_id)
    for target in targets:
        task_id = target.get("task_id")
        if not task_id:
            continue
        trow = conn.execute(
            "SELECT version, summary_status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if trow is None:
            raise QuestionTargetStaleError(
                f"问题指向的任务已不存在：{task_id}（问题范围需要更新）"
            )
        if int(trow[0]) != int(target.get("task_version") or 0):
            raise QuestionTargetStaleError(
                f"任务 {task_id} 已从版本 {target.get('task_version')} 变为 "
                f"{trow[0]}，这张问题卡片已失效"
            )
    return question, targets


# ── 读操作（只读连接，短事务） ───────────────────────────────────


def load_question(conn, question_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT {_Q_COLUMNS} FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return None
    question = _row_to_question(row)
    question["targets"] = _load_targets(conn, question_id)
    return question


def list_open_questions(conn, *, user_id: str, conversation_id: str = "",
                        limit: int = 50) -> List[Dict[str, Any]]:
    sql = (f"SELECT {_Q_COLUMNS} FROM questions WHERE user_id = ? AND status = ?")
    args: List[Any] = [user_id, Q_OPEN]
    if conversation_id:
        sql += " AND conversation_id = ?"
        args.append(conversation_id)
    sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?"
    args.append(int(limit))
    out = []
    for row in conn.execute(sql, args).fetchall():
        question = _row_to_question(row)
        question["targets"] = _load_targets(conn, question["id"])
        out.append(question)
    return out
