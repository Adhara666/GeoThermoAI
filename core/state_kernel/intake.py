# -*- coding: utf-8 -*-
"""状态内核 — 命令接收服务（升级第一阶段：状态内核）。

依据总体技术方案 §3.4「接收消息」原子操作与升级方案 9.1 第 2 条：

  同一短事务内完成：命令去重、消息编号、命令载荷、接收事件；
  提交后才回「已接收」——即消息先持久化再处理，接收后立刻返回回执，
  不等任务完成。

去重语义（§3.2 commands 表约束）：
  - 同一用户 + 同一去重键 + 相同载荷指纹 → 幂等返回原命令（只记一次）；
  - 同一用户 + 同一去重键 + 不同载荷指纹 → 冲突（CommandConflictError）。

处理闭环：接收事务提交后任务异步执行；无论成败，最终由
mark_command_processed / mark_command_failed 在新事务里回写终态，
命令不会永久停留在 received 状态。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.state_kernel.store import (
    StateStore,
    append_event,
    new_id,
    utcnow_iso,
)

# 命令状态机：received → processing → processed / failed / rejected
COMMAND_STATUS_RECEIVED = "received"
COMMAND_STATUS_PROCESSING = "processing"
COMMAND_STATUS_PROCESSED = "processed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_REJECTED = "rejected"


class CommandConflictError(Exception):
    """同一去重键携带不同载荷（§3.2：同键不同载荷报冲突）。"""


@dataclass(frozen=True)
class CommandReceipt:
    """接收回执：提交后立即返回，不含任何任务执行结果。"""

    command_id: str
    message_id: str
    conversation_key: str
    accepted: bool          # True=新接收；False=重复请求（幂等命中）
    duplicate: bool
    message_seq: int
    # 台账内部对话主键（阶段 2 起，理解层登记任务/问题需要它作外键）
    conversation_id: str = ""


def payload_fingerprint(payload: Dict[str, Any]) -> str:
    """载荷指纹：规范化 JSON 的 SHA-256，用于去重等价性判断。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conversation_pk(conn, user_id: str, project_id: str,
                    conversation_id: str) -> Optional[str]:
    """按 (用户, 项目, 旧对话编号) 查台账内部对话主键；未登记返回 None。"""
    row = conn.execute(
        "SELECT id FROM conversations WHERE user_id = ? AND project_id = ?"
        " AND legacy_conv_id = ?",
        (user_id, project_id, conversation_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _ensure_conversation(conn, user_id: str, project_id: str,
                         conversation_id: str) -> str:
    """确保台账里有该对话的控制行；旧对话以 (user, project, legacy_conv_id) 映射。"""
    existing = conversation_pk(conn, user_id, project_id, conversation_id)
    if existing is not None:
        return existing
    conv_id = new_id()
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO conversations (id, user_id, project_id, legacy_conv_id,"
        " semantic_version, next_message_seq, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, 1, ?, ?)",
        (conv_id, user_id, project_id, conversation_id, now, now),
    )
    return conv_id


def _receive_command_tx(
    conn,
    *,
    user_id: str,
    project_id: str,
    conversation_id: str,
    message: str,
    dedup_key: str,
    operation_type: str,
    payload: Dict[str, Any],
) -> CommandReceipt:
    """「接收消息」原子操作本体（§3.4 第 1 行）。在 BEGIN IMMEDIATE 事务内执行。"""
    fingerprint = payload_fingerprint(payload)

    # 1) 命令去重：同键同载荷幂等；同键异载荷冲突
    existing = conn.execute(
        "SELECT id, message_id, payload_fingerprint, status FROM commands"
        " WHERE user_id = ? AND dedup_key = ?",
        (user_id, dedup_key),
    ).fetchone()
    if existing is not None:
        cmd_id, msg_id, old_fp, _status = existing
        if old_fp != fingerprint:
            raise CommandConflictError(
                f"去重键 {dedup_key} 已被不同载荷占用（同键不同载荷报冲突）"
            )
        seq_row = conn.execute(
            "SELECT seq, conversation_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        return CommandReceipt(
            command_id=str(cmd_id),
            message_id=str(msg_id),
            conversation_key=conversation_id,
            accepted=False,
            duplicate=True,
            message_seq=int(seq_row[0]) if seq_row else -1,
            conversation_id=str(seq_row[1]) if seq_row else "",
        )

    # 2) 对话控制行 + 消息编号
    conv_id = _ensure_conversation(conn, user_id, project_id, conversation_id)
    seq_row = conn.execute(
        "SELECT next_message_seq FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    seq = int(seq_row[0])

    # 3) 消息先落库（用户原文），命令载荷随之登记
    msg_id = new_id()
    cmd_id = new_id()
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO messages (id, conversation_id, seq, role, content, command_id,"
        " status, created_at) VALUES (?, ?, ?, 'user', ?, ?, 'final', ?)",
        (msg_id, conv_id, seq, message, cmd_id, now),
    )
    conn.execute(
        "INSERT INTO commands (id, user_id, dedup_key, payload_fingerprint,"
        " message_id, operation_type, payload, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cmd_id,
            user_id,
            dedup_key,
            fingerprint,
            msg_id,
            operation_type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            COMMAND_STATUS_RECEIVED,
            now,
        ),
    )

    # 4) 推进对话消息序号，写接收事件，随后由调用方统一 COMMIT
    conn.execute(
        "UPDATE conversations SET next_message_seq = ?, updated_at = ? WHERE id = ?",
        (seq + 1, now, conv_id),
    )
    append_event(
        conn,
        type="command.received",
        user_id=user_id,
        conversation_id=conv_id,
        object_type="command",
        object_id=cmd_id,
        payload={
            "operation_type": operation_type,
            "dedup_key": dedup_key,
            "message_seq": seq,
        },
    )

    return CommandReceipt(
        command_id=cmd_id,
        message_id=msg_id,
        conversation_key=conversation_id,
        accepted=True,
        duplicate=False,
        message_seq=seq,
        conversation_id=conv_id,
    )


def receive_command(
    store: StateStore,
    *,
    user_id: str,
    project_id: str,
    conversation_id: str,
    message: str,
    dedup_key: str,
    operation_type: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> CommandReceipt:
    """接收一条用户命令：先持久化（原子事务），提交后立即返回回执。

    dedup_key 必须由调用方提供（前端请求编号）；为空时本函数生成一次性
    编号——此时不具备重试去重保护，调用方需自行知晓这一限制。
    """
    payload = dict(payload or {})
    payload.setdefault("message", message)
    payload.setdefault("project", project_id)
    payload.setdefault("conversation", conversation_id)
    if not dedup_key:
        dedup_key = new_id()
    receipt: CommandReceipt = store.submit_write(
        _receive_command_tx,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        message=message,
        dedup_key=dedup_key,
        operation_type=operation_type,
        payload=payload,
        timeout=timeout,
    )
    return receipt


def mark_command_processing(store: StateStore, command_id: str,
                            timeout: float = 30.0) -> None:
    """命令进入处理中（接收事务已提交，处理由任务线程异步进行）。"""
    now = utcnow_iso()

    def _tx(conn):
        conn.execute(
            "UPDATE commands SET status = ?, processed_at = NULL WHERE id = ?"
            " AND status = ?",
            (COMMAND_STATUS_PROCESSING, command_id, COMMAND_STATUS_RECEIVED),
        )

    store.submit_write(_tx, timeout=timeout)


def mark_command_processed(store: StateStore, command_id: str,
                           result: Optional[Dict[str, Any]] = None,
                           timeout: float = 30.0) -> None:
    """回写命令处理结果（终态 processed）并记录事件。"""
    now = utcnow_iso()

    def _tx(conn):
        row = conn.execute(
            "SELECT user_id, message_id FROM commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"命令不存在：{command_id}")
        user_id, msg_id = row
        conn.execute(
            "UPDATE commands SET status = ?, result = ?, processed_at = ?"
            " WHERE id = ?",
            (
                COMMAND_STATUS_PROCESSED,
                json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                now,
                command_id,
            ),
        )
        conv_row = conn.execute(
            "SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        append_event(
            conn,
            type="command.processed",
            user_id=user_id,
            conversation_id=conv_row[0] if conv_row else None,
            object_type="command",
            object_id=command_id,
        )

    store.submit_write(_tx, timeout=timeout)


def mark_command_rejected(store: StateStore, command_id: str, reason: str,
                          timeout: float = 30.0) -> None:
    """回写命令被拒绝的终态（如对话已有任务在执行中）。

    拒绝也是一种处理结果：命令已经落库，就必须有终态，
    不能停在 received（§3.4 的处理闭环要求）。
    """
    now = utcnow_iso()

    def _tx(conn):
        row = conn.execute(
            "SELECT user_id, message_id, status FROM commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"命令不存在：{command_id}")
        user_id, msg_id, status = row
        if status != COMMAND_STATUS_RECEIVED:
            return          # 已经有终态的命令不被拒绝态覆盖
        conn.execute(
            "UPDATE commands SET status = ?, result = ?, processed_at = ?"
            " WHERE id = ?",
            (COMMAND_STATUS_REJECTED,
             json.dumps({"rejected": reason}, ensure_ascii=False), now,
             command_id),
        )
        conv_row = conn.execute(
            "SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        append_event(
            conn,
            type="command.rejected",
            user_id=user_id,
            conversation_id=conv_row[0] if conv_row else None,
            object_type="command",
            object_id=command_id,
            payload={"reason": reason[:500]},
        )

    store.submit_write(_tx, timeout=timeout)


def mark_command_failed(store: StateStore, command_id: str, error: str,
                        timeout: float = 30.0) -> None:
    """回写命令失败终态（不吞异常：失败也要留痕并记录事件）。"""
    now = utcnow_iso()

    def _tx(conn):
        row = conn.execute(
            "SELECT user_id, message_id FROM commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"命令不存在：{command_id}")
        user_id, msg_id = row
        conn.execute(
            "UPDATE commands SET status = ?, result = ?, processed_at = ?"
            " WHERE id = ?",
            (COMMAND_STATUS_FAILED, json.dumps({"error": error}, ensure_ascii=False),
             now, command_id),
        )
        conv_row = conn.execute(
            "SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        append_event(
            conn,
            type="command.failed",
            user_id=user_id,
            conversation_id=conv_row[0] if conv_row else None,
            object_type="command",
            object_id=command_id,
            payload={"error": error[:500]},
        )

    store.submit_write(_tx, timeout=timeout)
