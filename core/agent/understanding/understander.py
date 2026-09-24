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
        recent_completed: Optional[Sequence[str]] = None,
        selected_point: str = "",
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
            recent_completed=list(recent_completed or []),
            selected_point=selected_point,
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
            batch = ops.apply_shared_modifiers(batch)
            return self._supplement_uncovered(message, batch, system, history)

        # 一次格式修复（§4.1「最多一次格式修复」）：把不合格的原始输出
        # 回传给模型做结构化修复（空手重问等于原地再错一次）
        self.log(f"候选输出无效，进行一次格式修复：{'；'.join(batch.errors[:3])}")
        repaired = self.call_text(system + prompts.repair_hint(),
                                  prompts.repair_request(message, batch.raw_output),
                                  temperature=0.0, max_tokens=_MAX_TOKENS,
                                  history=history, thinking=_THINKING_OFF,
                                  json_mode=True)
        if is_api_failure(repaired):
            batch.source = SOURCE_UNAVAILABLE
            return batch
        repaired_batch = self._to_batch(repaired)
        if repaired_batch.valid:
            repaired_batch = ops.apply_shared_modifiers(repaired_batch)
            return self._supplement_uncovered(message, repaired_batch,
                                              system, history)
        # 两次都无效：留痕（日志面板可见 + 由上层归档），保留第二次的
        # 原始输出与错误，交由上层决定（现为“降级为问答”而非报错）
        snippet = (repaired_batch.raw_output or batch.raw_output or "").strip()
        self.log(f"两次输出均无法解析为操作 JSON；原始输出前300字："
                 f"{snippet[:300] or '（空）'}")
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

    def _supplement_uncovered(self, message, batch, system, history):
        """覆盖审计回路：有子句未被任何操作自报覆盖时，交回模型补充解析一次。

        设计边界（不做关键词规则）：程序只做结构比对（子句 vs covers），
        不包含业务词表、不推断意图；漏掉的子句由模型重新理解并合并。
        最多触发一次；审计异常/模型不可用/无补充时保持原批次，
        不改变主流程结果。
        """
        try:
            missing = ops.uncovered_clauses(message, batch.operations)
        except Exception:  # noqa: BLE001 — 审计失败不影响主流程
            return batch
        if not missing:
            return batch
        self.log("覆盖审计：%d 个子句未被操作覆盖，触发补充解析：%s"
                 % (len(missing), "；".join(missing)[:100]))
        try:
            raw = self.call_text(
                system + prompts.supplement_hint(),
                prompts.supplement_request(message, batch.operations, missing),
                temperature=0.0, max_tokens=_MAX_TOKENS, history=history,
                thinking=_THINKING_OFF, json_mode=True)
        except Exception:  # noqa: BLE001
            return batch
        if is_api_failure(raw):
            return batch
        extra = self._to_batch(raw)
        if not extra.valid or not extra.operations:
            return batch
        existing = list(batch.operations)
        added = []
        for op in extra.operations:
            if op.op == ops.OP_REPLY_ONLY:
                continue  # 模型明确表示“无补充操作”
            dup = any(op.op == o.op
                      and (op.target_ref or "") == (o.target_ref or "")
                      and (getattr(op, "intent", "") or "")
                      == (getattr(o, "intent", "") or "")
                      for o in existing)
            if not dup:
                added.append(op)
        if not added:
            return batch
        batch.operations = tuple(existing + added)
        batch.note = ((batch.note + " " if batch.note else "")
                      + "覆盖审计补充 %d 条操作" % len(added))
        self.log("覆盖审计补充 %d 条操作（%s）"
                 % (len(added), "、".join(op.op for op in added)))
        return batch


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
