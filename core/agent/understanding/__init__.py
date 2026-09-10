# -*- coding: utf-8 -*-
"""理解层（升级第二阶段）：模型只提候选，程序做决定。

对应总体技术方案 §4「理解层的具体实现路线」与升级方案 9.2。

模块分工：

| 模块 | 责任 |
|---|---|
| `operations` | 候选操作契约：枚举、解析、类型校验 |
| `prompts` | 候选理解器提示词（把模型输出面收窄到枚举） |
| `understander` | 调模型拿候选，一次格式修复，失败如实上报 |
| `timeparse` | 以消息接收时间为锚的时间解析，单日不扩月 |
| `binding` | 地区/任务目标绑定，先收全部候选再判唯一 |
| `slotbook` | 槽位账本：值 + 来源 + 是否确认 + 否定记录 |
| `capabilities` | 各能力的编译前最低必要信息与旧执行链映射 |
| `resolution` | 候选 → 草稿修改或持久问题（纯函数） |
| `answers` | 文字答复与卡片点击的统一解释 |
| `service` | 落台账：草稿修改 / 消费答案两类原子事务 |
| `execplan` | 已确认任务 → 既有执行链的 plan 形状 |

只有 `understander` 会调用模型，只有 `service` 会写库；其余都是纯函数，
可以用确定性测试逐条验证。
"""

from core.agent.understanding import (
    answers,
    binding,
    capabilities,
    execplan,
    operations,
    prompts,
    resolution,
    service,
    slotbook,
    timeparse,
    understander,
)
from core.agent.understanding.execplan import ResolvedTask
from core.agent.understanding.service import (
    KIND_ASK,
    KIND_FAILED,
    KIND_NOOP,
    KIND_PROCEED,
    KIND_REPLY_ONLY,
    UnderstandingResult,
    answer_question,
    build_resolve_context,
    handle_message,
    load_ledger_context,
)
from core.agent.understanding.understander import CandidateUnderstander

__all__ = [
    "CandidateUnderstander",
    "KIND_ASK",
    "KIND_FAILED",
    "KIND_NOOP",
    "KIND_PROCEED",
    "KIND_REPLY_ONLY",
    "ResolvedTask",
    "UnderstandingResult",
    "answer_question",
    "answers",
    "binding",
    "build_resolve_context",
    "capabilities",
    "execplan",
    "handle_message",
    "load_ledger_context",
    "operations",
    "prompts",
    "resolution",
    "service",
    "slotbook",
    "timeparse",
    "understander",
]
