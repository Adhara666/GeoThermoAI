# -*- coding: utf-8 -*-
"""状态内核（升级第一/第二阶段）— SQLite 台账 + 命令接收 + 任务/问题原子操作。

公开入口：
  - StateStore        单一写入者存储层（BEGIN IMMEDIATE 短事务）
  - ensure_schema     结构版本与带备份迁移
  - receive_command   命令接收（先持久化、去重幂等、冲突检测、立即回执）
  - tasks / questions 任务草稿与持久问题的原子操作（阶段 2 理解层写入）
  - CommandConflictError / StaleVersionError
  - append_event / update_versioned / insert_versioned
"""

from core.state_kernel import questions, tasks
from core.state_kernel.schema import SCHEMA_VERSION, ensure_schema, open_connection
from core.state_kernel.store import (
    StaleVersionError,
    StateStore,
    append_event,
    db_status,
    insert_versioned,
    new_id,
    update_versioned,
    utcnow_iso,
)
from core.state_kernel.intake import (
    CommandConflictError,
    CommandReceipt,
    conversation_pk,
    mark_command_failed,
    mark_command_processed,
    mark_command_processing,
    payload_fingerprint,
    receive_command,
)

__all__ = [
    "SCHEMA_VERSION",
    "StateStore",
    "StaleVersionError",
    "CommandConflictError",
    "CommandReceipt",
    "append_event",
    "conversation_pk",
    "db_status",
    "ensure_schema",
    "insert_versioned",
    "mark_command_failed",
    "mark_command_processed",
    "mark_command_processing",
    "new_id",
    "open_connection",
    "payload_fingerprint",
    "questions",
    "receive_command",
    "tasks",
    "update_versioned",
    "utcnow_iso",
]
