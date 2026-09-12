# -*- coding: utf-8 -*-
"""理解层 — 候选理解器（升级第二阶段：理解层）。

依据总体技术方案 §4.1 与 §4.5、升级方案 9.2 第 1、7 条：

  - 模型只输出**受限候选操作**；支持结构约束输出的走 JSON 模式，
    不支持时沿用兼容层 + 本地类型校验 + **最多一次格式修复**。
  - 模型超时、格式无效、能力不支持时：保留已确认信息、明确说明原因，
    **不用默认城市/整月/下载兜底**。

本模块只负责「拿到候选」，不做绑定与决定（那是 resolution.py）。
"""

import datetime
import logging
from typing import List, Optional, Sequence

from core.agent.roles.base_role import RoleAgent, extract_json, is_api_failure
from core.agent.understanding import operations as ops
from core.agent.understanding import prompts

logger = logging.getLogger(__name__)

# 模型不可用时的候选来源标记
SOURCE_LLM = "llm"
SOURCE_UNAVAILABLE = "unavailable"

# 候选理解是一次结构化输出：把推理关掉，避免推理内容吃掉 JSON 的输出预算
_THINKING_OFF = {"type": "disabled"}
_MAX_TOKENS = 1600


class CandidateUnderstander(RoleAgent):
    """把用户消息翻译成候选操作的角色（Planner 的前半部分，§4.1）。"""

    role = "understanding"
    role_name = "理解"

    def understand(
        self,
        message: str,
        *,
        anchor_date: datetime.date,
        tz_label: str,
        chat_mode: str,
        study_areas: Sequence[str],
        open_tasks: Sequence[str],
        open_questions: Sequence[str],
        history: Optional[List[dict]] = None,
        recent_summary: str = "",
    ) -> ops.CandidateBatch:
        """返回候选批次。模型不可用时返回 `source=unavailable` 的空批次。"""
        system = prompts.understand_prompt(
            anchor_date=anchor_date.isoformat(),
            tz_label=tz_label,
            chat_mode=chat_mode,
            study_areas=study_areas,
            open_tasks=open_tasks,
            open_questions=open_questions,
            recent_summary=recent_summary,
        )
        raw = self.call_text(system, message, temperature=0.0,
                             max_tokens=_MAX_TOKENS, history=history,
                             thinking=_THINKING_OFF, json_mode=True)
        if is_api_failure(raw):
            self.log(f"模型不可用：{raw[:120]}")
            batch = ops.CandidateBatch(raw_output=raw, source=SOURCE_UNAVAILABLE)
            batch.errors.append("模型不可用")
            return batch

        batch = self._to_batch(raw)
        if batch.valid:
            return ops.apply_shared_modifiers(batch)

        # 一次格式修复（§4.1「最多一次格式修复」）
        self.log(f"候选输出无效，进行一次格式修复：{'；'.join(batch.errors[:3])}")
        repaired = self.call_text(system + prompts.repair_hint(), message,
                                  temperature=0.0, max_tokens=_MAX_TOKENS,
                                  history=history, thinking=_THINKING_OFF,
                                  json_mode=True)
        if is_api_failure(repaired):
            batch.source = SOURCE_UNAVAILABLE
            return batch
        repaired_batch = self._to_batch(repaired)
        if repaired_batch.valid:
            return ops.apply_shared_modifiers(repaired_batch)
        # 两次都无效：保留第二次的原始输出与错误，交由上层如实说明
        repaired_batch.errors = list(dict.fromkeys(
            batch.errors + repaired_batch.errors))
        return repaired_batch

    def _to_batch(self, raw: str) -> ops.CandidateBatch:
        parsed = extract_json(raw)
        if parsed is None:
            batch = ops.CandidateBatch(raw_output=raw, source=SOURCE_LLM)
            batch.errors.append("模型输出无法解析为 JSON")
            return batch
        return ops.parse_batch(parsed, raw_output=raw)


def enforce_chat_mode(batch: ops.CandidateBatch) -> ops.CandidateBatch:
    """Chat 模式硬闸：从入口剔除一切会创建/修改任务的候选（§4.5）。

    这不是一句提示词，而是**代码层面的权限边界**：即便模型返回了 create，
    在 Chat 模式下也会被丢掉，只保留 reply_only。
    """
    kept = [op for op in batch.operations if op.op not in ops.PRODUCTION_OPS]
    dropped = len(batch.operations) - len(kept)
    batch.operations = kept or [ops.CandidateOperation(op=ops.OP_REPLY_ONLY)]
    if dropped:
        batch.note = (batch.note + " " if batch.note else "") + \
            f"Chat 只读模式已拦下 {dropped} 个生产类候选操作"
    return batch


def summarize_for_log(batch: ops.CandidateBatch) -> str:
    """给日志面板的一行摘要（技术细节只进日志，不进气泡）。"""
    if not batch.operations:
        return f"候选为空（{'；'.join(batch.errors) or '无原因'}）"
    parts = []
    for op in batch.operations:
        fields = ",".join(f"{p.field}={p.action}" for p in op.patches)
        parts.append(f"{op.op}/{op.capability or '-'}[{op.label or '-'}]"
                     f"({fields or '无补丁'})")
    return " | ".join(parts)
