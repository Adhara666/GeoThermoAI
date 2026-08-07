"""
对话级会话状态（技术方案 8.2）

`memory/sessions/{conv_id}.json`：本对话已确认的槽位、待补问题、plan_id、replan 计数。
这是「多轮补全」的落盘依据——修复 1.5(1)「Agent 路径看不到上文」的另一半。

约定（与记忆系统现有约定一致）：写失败仅告警，绝不抛给 Agent 主流程。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from ..atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_lock = threading.Lock()

SESSION_SCHEMA_VERSION = 1

# 槽位来源。`user`（用户明确下达）在多轮中优先级最高；
# `mentioned` 表示「用户提到过但当轮不是任务指令」（如先问「你认识九江镇吗」），
# 后续轮次可以据此延续上下文，但不视为已确认的任务参数。
SLOT_SOURCES = ("user", "mentioned", "default", "preference", "memory", "inferred")


class SessionState:
    """单个对话的槽位状态读写封装。"""

    def __init__(self, path: str, conv_id: str = "", project_id: str = ""):
        self.path = path
        self.conv_id = conv_id
        self.project_id = project_id

    # ── 读写 ───────────────────────────────────────────────────────

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "conv_id": self.conv_id,
            "project_id": self.project_id,
            "updated_at": "",
            "intent": "",
            "slots": {},
            "missing": [],
            "pending_question": "",
            "plan_id": "",
            "replan_count": 0,
            "last_approval_node": "",
        }

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._empty()
            merged = self._empty()
            merged.update(data)
            if not isinstance(merged.get("slots"), dict):
                merged["slots"] = {}
            return merged
        except FileNotFoundError:
            return self._empty()
        except Exception as e:
            logger.warning(f"[memory] 会话状态读取失败（按空状态处理）: {e}")
            return self._empty()

    def save(self, data: Dict[str, Any]) -> None:
        """整体覆盖写入；失败仅告警。"""
        try:
            payload = {**data, "schema_version": SESSION_SCHEMA_VERSION,
                       "conv_id": self.conv_id or data.get("conv_id", ""),
                       "project_id": self.project_id or data.get("project_id", ""),
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            with _lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                atomic_write_json(self.path, payload)
        except Exception as e:
            logger.warning(f"[memory] 会话状态写入失败（已忽略）: {e}")

    def delete(self) -> None:
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
        except Exception as e:
            logger.warning(f"[memory] 会话状态删除失败（已忽略）: {e}")

    # ── 便捷更新（全部返回新字典，不就地改传入对象） ───────────────

    def update(self, **fields: Any) -> Dict[str, Any]:
        data = self.load()
        merged = {**data, **fields}
        self.save(merged)
        return merged

    def set_slots(self, slots: Dict[str, Any], intent: str = "",
                  missing: Optional[List[str]] = None,
                  pending_question: str = "", plan_id: str = "") -> Dict[str, Any]:
        """写入本轮确认的槽位。已有槽位中 source=user 的不会被非 user 来源覆盖。"""
        data = self.load()
        existing = data.get("slots") or {}
        merged_slots: Dict[str, Any] = dict(existing)
        for key, item in (slots or {}).items():
            incoming = item if isinstance(item, dict) else {"value": item, "source": "inferred"}
            old = existing.get(key)
            if (isinstance(old, dict) and old.get("source") == "user"
                    and incoming.get("source") != "user"):
                continue
            merged_slots[key] = incoming
        fields: Dict[str, Any] = {"slots": merged_slots,
                                  "missing": list(missing or []),
                                  "pending_question": pending_question}
        if intent:
            fields["intent"] = intent
        if plan_id:
            fields["plan_id"] = plan_id
        return self.update(**fields)

    def get_slot(self, name: str) -> Dict[str, Any]:
        item = (self.load().get("slots") or {}).get(name)
        return item if isinstance(item, dict) else {}

    def clear_pending_question(self) -> Dict[str, Any]:
        return self.update(pending_question="")

    def note_replan(self, reason: str = "") -> int:
        data = self.load()
        count = int(data.get("replan_count") or 0) + 1
        self.update(replan_count=count,
                    last_replan_reason=reason or data.get("last_replan_reason", ""))
        return count

    def reset_replan(self) -> None:
        self.update(replan_count=0)

    def record_approval(self, node: str) -> None:
        if node:
            self.update(last_approval_node=node)
