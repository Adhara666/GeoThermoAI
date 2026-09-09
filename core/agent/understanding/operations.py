# -*- coding: utf-8 -*-
"""理解层 — 候选操作契约（升级第二阶段：理解层）。

依据总体技术方案 §4.1「模型只输出候选操作」与升级方案 9.2 第 1 条：

  模型**只提建议**，不再直接生成可执行计划。它能输出的东西被限制成
  下面这张枚举表；任何枚举外的内容（自造技能、正式文件路径、Python
  代码）都在本模块被判为无效——不做「删掉后继续说计划有效」的宽松
  解析（§5.1 最后一段）。

  程序拿到候选后自己校验、绑定、决定（见 resolution.py），
  模型猜错最多白猜一次，不会放行一个不存在的地区或编造的步骤。

本模块只做**格式与枚举**层面的事：解析、归一化、类型校验。
不碰真实边界文件、不查台账、不调用模型。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── 操作类型（§4.1「操作类型」行：新建、设值、清除、更正、回答、
#    继续、重试、取消、调整优先级、只回答） ──────────────────────
OP_CREATE = "create"          # 新建一个任务目标
OP_SET = "set"                # 给已有目标设值
OP_CLEAR = "clear"            # 清除某个字段（否定）
OP_CORRECT = "correct"        # 更正：清旧值 + 设新值
OP_ANSWER = "answer"          # 回答一个待答问题
OP_CONTINUE = "continue"      # 继续某个任务
OP_RETRY = "retry"            # 重试某个任务
OP_CANCEL = "cancel"          # 取消某个任务
OP_PRIORITY = "priority"      # 调整优先级
OP_REPLY_ONLY = "reply_only"  # 只回答，不动任何任务

ALL_OPS: Tuple[str, ...] = (
    OP_CREATE, OP_SET, OP_CLEAR, OP_CORRECT, OP_ANSWER,
    OP_CONTINUE, OP_RETRY, OP_CANCEL, OP_PRIORITY, OP_REPLY_ONLY,
)

# 会新建或修改任务草稿的操作（Chat 模式一律禁止，§4.5）
PRODUCTION_OPS: Tuple[str, ...] = (
    OP_CREATE, OP_SET, OP_CLEAR, OP_CORRECT, OP_CONTINUE, OP_RETRY,
    OP_CANCEL, OP_PRIORITY,
)

# ── 能力（§4.1「能力」行：搜索、下载子集、预处理、训练、完整 LST、
#    填洞、查询） ────────────────────────────────────────────────
CAP_QUERY = "query"                      # 解释与查询，只回答
CAP_SEARCH = "search"                    # 只搜索影像，不下载
CAP_DOWNLOAD_SUBSET = "download_subset"  # 只下载指定数据集合
CAP_PREPROCESS = "preprocess"            # 只预处理
CAP_TRAIN = "train"                      # 只训练
CAP_FULL_LST = "full_lst"                # 完整 10 m 地表温度生产
CAP_GAPFILL = "gapfill"                  # 对已有主图填洞

ALL_CAPABILITIES: Tuple[str, ...] = (
    CAP_QUERY, CAP_SEARCH, CAP_DOWNLOAD_SUBSET, CAP_PREPROCESS,
    CAP_TRAIN, CAP_FULL_LST, CAP_GAPFILL,
)

# ── 槽位字段（§4.3「槽位就是执行目标需要的信息项」） ─────────────
F_REGION = "region"              # 研究区
F_TIME = "time"                  # 时间范围
F_PRODUCT_MODE = "product_mode"  # 产品方式：配对 / 月度合成
F_DATASETS = "datasets"          # 数据集合（landsat / sentinel2 / dem）
F_PRODUCT = "product"            # 产品类型（当前只有 lst_10m）
F_MODEL = "model"                # 模型（rf）

ALL_FIELDS: Tuple[str, ...] = (
    F_REGION, F_TIME, F_PRODUCT_MODE, F_DATASETS, F_PRODUCT, F_MODEL,
)

# 字段补丁动作（§4.1「字段补丁」行：每字段区分设置、清除、本轮未提及）
PATCH_SET = "set"
PATCH_CLEAR = "clear"
PATCH_KEEP = "keep"
ALL_PATCH_ACTIONS: Tuple[str, ...] = (PATCH_SET, PATCH_CLEAR, PATCH_KEEP)

# 产品方式取值
MODE_PAIR = "pair"
MODE_MONTHLY = "monthly"
ALL_PRODUCT_MODES: Tuple[str, ...] = (MODE_PAIR, MODE_MONTHLY)

# 数据集合取值
DATASETS = ("landsat", "sentinel2", "dem")

# 单条消息内允许的候选操作上限：模型偶发的「笛卡尔积式」爆量输出直接判无效，
# 不让它变成几十个任务（§4.1「共享修饰范围…不由程序随意做笛卡尔积」）
MAX_OPERATIONS = 8


@dataclass(frozen=True)
class FieldPatch:
    """一个字段的补丁：设置什么值、依据原句哪一段。"""

    field: str
    action: str
    value: Any = None
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "action": self.action,
                "value": self.value, "evidence": self.evidence}


@dataclass(frozen=True)
class CandidateOperation:
    """一条候选操作。`label` 只在本条消息内用于绑定，正式编号由后端生成。"""

    op: str
    label: str = ""
    target_ref: str = ""            # 原句里的指称：「南京」「第二个」「这张图」
    capability: str = ""
    patches: Tuple[FieldPatch, ...] = ()
    question_id: str = ""           # op=answer 时指向的问题
    answer_text: str = ""           # op=answer 时的原文答复
    missing: Tuple[str, ...] = ()   # 模型认为还缺的字段（只作提示，程序自己判）
    ambiguity: Tuple[str, ...] = ()
    evidence: str = ""

    def patch_for(self, name: str) -> Optional[FieldPatch]:
        for patch in self.patches:
            if patch.field == name:
                return patch
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op, "label": self.label, "target_ref": self.target_ref,
            "capability": self.capability,
            "patches": [p.to_dict() for p in self.patches],
            "question_id": self.question_id, "answer_text": self.answer_text,
            "missing": list(self.missing), "ambiguity": list(self.ambiguity),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SharedModifier:
    """共享修饰：「都是 2025 年」作用于哪几个临时标签（§4.1）。

    `applies_to` 由模型显式给出；为空时**不做**笛卡尔积，也不广播到全部目标。
    """

    patches: Tuple[FieldPatch, ...]
    applies_to: Tuple[str, ...]
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"patches": [p.to_dict() for p in self.patches],
                "applies_to": list(self.applies_to), "evidence": self.evidence}


@dataclass
class CandidateBatch:
    """一条消息的全部候选：操作列表 + 共享修饰 + 解析问题。

    `errors` 是**致命**问题（操作/能力/字段不在枚举内），整批判为无效；
    `warnings` 是「已安全丢弃」的部分（如共享修饰没写作用范围），
    剩下的候选仍可使用——丢掉一条修饰只会让程序多问一次，
    而广播它可能凭空造出用户没要的任务。
    """

    operations: List[CandidateOperation] = field(default_factory=list)
    shared: List[SharedModifier] = field(default_factory=list)
    note: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_output: str = ""
    source: str = "llm"     # llm | unavailable

    @property
    def valid(self) -> bool:
        return not self.errors and bool(self.operations)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operations": [o.to_dict() for o in self.operations],
            "shared": [s.to_dict() for s in self.shared],
            "note": self.note, "errors": list(self.errors),
            "warnings": list(self.warnings), "source": self.source,
        }


# ── 解析与校验 ───────────────────────────────────────────────────


def _text(value: Any) -> str:
    if value is None:
        return ""
    out = str(value).strip()
    return "" if out.lower() in ("null", "none", "undefined") else out


def _parse_patches(raw: Any, errors: List[str], where: str
                   ) -> Tuple[FieldPatch, ...]:
    """字段补丁支持两种写法：字典 {字段: {action,value}} 或列表 [{field,...}]。"""
    items: List[Tuple[str, Any]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                items.append((_text(entry.get("field")), entry))
    elif raw:
        errors.append(f"{where}：字段补丁必须是对象或数组")
        return ()

    patches: List[FieldPatch] = []
    for name, body in items:
        name = _text(name)
        if name not in ALL_FIELDS:
            errors.append(f"{where}：未知槽位字段「{name}」")
            continue
        if not isinstance(body, dict):
            body = {"action": PATCH_SET, "value": body}
        action = _text(body.get("action")) or PATCH_SET
        if action not in ALL_PATCH_ACTIONS:
            errors.append(f"{where}：字段「{name}」的动作「{action}」不受支持")
            continue
        if action == PATCH_KEEP:
            continue
        value = body.get("value")
        if action == PATCH_SET and (value is None or _text(value) == ""):
            errors.append(f"{where}：字段「{name}」标为设置却没有值")
            continue
        patches.append(FieldPatch(field=name, action=action, value=value,
                                  evidence=_text(body.get("evidence"))))
    return tuple(patches)


def _parse_operation(raw: Any, index: int,
                     errors: List[str]) -> Optional[CandidateOperation]:
    where = f"第 {index + 1} 条候选操作"
    if not isinstance(raw, dict):
        errors.append(f"{where}：不是对象")
        return None
    op = _text(raw.get("op") or raw.get("operation"))
    if op not in ALL_OPS:
        errors.append(f"{where}：不支持的操作类型「{op}」")
        return None

    capability = _text(raw.get("capability"))
    if capability and capability not in ALL_CAPABILITIES:
        errors.append(f"{where}：不支持的能力「{capability}」")
        return None
    if op == OP_CREATE and not capability:
        errors.append(f"{where}：新建操作必须给出能力")
        return None

    question_id = _text(raw.get("question_id"))
    if op == OP_ANSWER and not (question_id or _text(raw.get("answer_text"))):
        errors.append(f"{where}：回答操作必须给出问题编号或答复原文")
        return None

    missing = tuple(f for f in (_text(x) for x in (raw.get("missing") or []))
                    if f in ALL_FIELDS)
    ambiguity = tuple(_text(x) for x in (raw.get("ambiguity") or []) if _text(x))
    return CandidateOperation(
        op=op,
        label=_text(raw.get("label") or raw.get("target_label")),
        target_ref=_text(raw.get("target_ref") or raw.get("target")),
        capability=capability,
        patches=_parse_patches(raw.get("patches") or raw.get("field_patches"),
                               errors, where),
        question_id=question_id,
        answer_text=_text(raw.get("answer_text") or raw.get("answer")),
        missing=missing,
        ambiguity=ambiguity,
        evidence=_text(raw.get("evidence")),
    )


def _parse_shared(raw: Any, errors: List[str],
                  warnings: List[str]) -> List[SharedModifier]:
    if not raw:
        return []
    if not isinstance(raw, list):
        warnings.append("共享修饰不是数组，已整体忽略")
        return []
    out: List[SharedModifier] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            warnings.append(f"第 {i + 1} 条共享修饰不是对象，已忽略")
            continue
        applies = tuple(_text(x) for x in (entry.get("applies_to") or [])
                        if _text(x))
        patches = _parse_patches(entry.get("patches") or entry.get("field_patches"),
                                 errors, f"第 {i + 1} 条共享修饰")
        if not patches:
            continue
        if not applies:
            # 作用范围缺失时整条丢弃：宁可少合并一次让程序多问一句，
            # 也不擅自广播到全部目标（§4.1「不由程序随意做笛卡尔积」）
            warnings.append(f"第 {i + 1} 条共享修饰没有说明作用于哪些目标，已丢弃")
            continue
        out.append(SharedModifier(patches=patches, applies_to=applies,
                                  evidence=_text(entry.get("evidence"))))
    return out


def parse_batch(raw: Any, *, raw_output: str = "") -> CandidateBatch:
    """把模型 JSON 归一化为候选批次；任何枚举外内容都记入 errors。

    调用方按 `batch.valid` 判断是否可用；无效时**保留已确认信息并说明原因**，
    不用默认城市/整月/下载兜底（§4.5）。
    """
    batch = CandidateBatch(raw_output=raw_output)
    if not isinstance(raw, dict):
        batch.errors.append("模型输出不是 JSON 对象")
        return batch

    ops_raw = raw.get("operations")
    if not isinstance(ops_raw, list):
        batch.errors.append("缺少 operations 数组")
        return batch
    if len(ops_raw) > MAX_OPERATIONS:
        batch.errors.append(
            f"一条消息给出了 {len(ops_raw)} 个候选操作，超过上限 {MAX_OPERATIONS}"
        )
        return batch

    for i, item in enumerate(ops_raw):
        parsed = _parse_operation(item, i, batch.errors)
        if parsed is not None:
            batch.operations.append(parsed)

    batch.shared = _parse_shared(raw.get("shared_modifiers") or raw.get("shared"),
                                 batch.errors, batch.warnings)
    batch.note = _text(raw.get("note"))
    if not batch.operations and not batch.errors:
        batch.errors.append("模型没有给出任何候选操作")
    return batch


def apply_shared_modifiers(batch: CandidateBatch) -> CandidateBatch:
    """把共享修饰合并进对应标签的操作；已有同字段补丁的目标不被覆盖。

    「都是 2025 年」只作用于模型显式列出的标签；本轮已明确给了该字段的目标
    保留自己的值（§4.2 槽位优先级：本轮明确值最高）。
    """
    if not batch.shared:
        return batch
    merged: List[CandidateOperation] = []
    for op in batch.operations:
        extra: List[FieldPatch] = []
        for shared in batch.shared:
            if op.label and op.label in shared.applies_to:
                for patch in shared.patches:
                    if op.patch_for(patch.field) is None and \
                            all(p.field != patch.field for p in extra):
                        extra.append(patch)
        merged.append(op if not extra
                      else CandidateOperation(
                          op=op.op, label=op.label, target_ref=op.target_ref,
                          capability=op.capability,
                          patches=tuple(list(op.patches) + extra),
                          question_id=op.question_id,
                          answer_text=op.answer_text, missing=op.missing,
                          ambiguity=op.ambiguity, evidence=op.evidence))
    batch.operations = merged
    return batch
