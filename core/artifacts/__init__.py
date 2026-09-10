# -*- coding: utf-8 -*-
"""产物与恢复（升级第五阶段）— 两步提交、产物登记、引用保护、
启动对账与受控清理。

公开入口：
  - publisher.prepare_commit_tx / publish_outputs / confirm_commit_tx /
    abandon_commit_tx / collect_outputs     两步提交（§10.2）
  - refs.acquire_ref_tx / release_ref_tx / is_protected   引用保护（§10.4）
  - cleanup.request_cleanup_tx / mark_missing_tx /
    sweep_missing_tx / cleanup_stale_intents_tx           受控清理（§11.4）
  - lock.InstanceLock                                     实例锁（§11.1）
"""

from core.artifacts.lock import InstanceLock
from core.artifacts.refs import (
    acquire_ref_tx,
    is_protected,
    list_refs,
    release_consumer_tx,
    release_ref_tx,
)
from core.artifacts.publisher import (
    AV_AVAILABLE,
    AV_CLEANED,
    AV_MISSING,
    CommitError,
    abandon_commit_tx,
    collect_outputs,
    confirm_commit_tx,
    prepare_commit_tx,
    publish_outputs,
)
from core.artifacts.cleanup import (
    cleanup_stale_intents_tx,
    mark_missing_tx,
    request_cleanup_tx,
    sweep_missing_tx,
)

__all__ = [
    "InstanceLock",
    "CommitError",
    "AV_AVAILABLE", "AV_CLEANED", "AV_MISSING",
    "abandon_commit_tx",
    "acquire_ref_tx",
    "cleanup_stale_intents_tx",
    "collect_outputs",
    "confirm_commit_tx",
    "is_protected",
    "list_refs",
    "mark_missing_tx",
    "prepare_commit_tx",
    "publish_outputs",
    "release_consumer_tx",
    "release_ref_tx",
    "request_cleanup_tx",
    "sweep_missing_tx",
]
