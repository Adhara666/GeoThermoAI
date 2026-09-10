# -*- coding: utf-8 -*-
"""理解层 — 槽位账本：值 + 来源 + 是否确认 + 证据 + 否定记录。

依据总体技术方案 §4.2 与升级方案 9.2 第 4 条：

  - 槽位更新固定优先级：**本轮明确值或清除 > 当前任务已确认值 >
    用户明确沿用 > 项目偏好 > 界面建议 > 历史建议 > 默认**。
  - 「清除」保留一条失效记录：「不是武汉」之后地区为空且记录武汉被否定，
    **下一轮不能被历史合槽重新填回**。
  - 默认值（RF / 10 m LST）的来源**始终是默认**，不因为模型把它回显出来
    就标成用户原话。

本模块是纯数据操作：不读文件、不查库、不调用模型，便于确定性测试。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.agent.understanding import operations as ops

# 槽位来源，按优先级从高到低（数字越小优先级越高）
SRC_USER = "user"              # 本轮用户明确说的
SRC_ANSWER = "answer"          # 用户回答追问给的
SRC_CONFIRMED = "confirmed"    # 当前任务此前已确认的
SRC_CARRIED = "carried"        # 用户明确沿用历史
SRC_PREFERENCE = "preference"  # 项目偏好
SRC_SETTINGS = "settings"      # 界面设置建议
SRC_MEMORY = "memory"          # 历史建议
SRC_DEFAULT = "default"        # 内置默认

_PRIORITY: Dict[str, int] = {
    SRC_USER: 0, SRC_ANSWER: 0, SRC_CONFIRMED: 1, SRC_CARRIED: 2,
    SRC_PREFERENCE: 3, SRC_SETTINGS: 4, SRC_MEMORY: 5, SRC_DEFAULT: 6,
}

# 只有这些来源算「用户确认过」——默认与建议一律 confirmed=False
CONFIRMED_SOURCES = (SRC_USER, SRC_ANSWER, SRC_CONFIRMED, SRC_CARRIED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def priority_of(source: str) -> int:
    return _PRIORITY.get(source, len(_PRIORITY))


def make_slot(value: Any, source: str, *, evidence: str = "",
              detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一个槽位条目。confirmed 由来源决定，调用方不能自行拔高。"""
    return {
        "value": value,
        "source": source,
        "confirmed": source in CONFIRMED_SOURCES,
        "evidence": evidence,
        "detail": dict(detail or {}),
        "updated_at": _now(),
    }


def _norm_compare(value: Any) -> str:
    """用于和否定记录比对的归一化形式（大小写与空白无关）。"""
    if isinstance(value, dict):
        for key in ("display", "raw", "start"):
            if value.get(key):
                return str(value[key]).strip().lower()
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(sorted(str(v).strip().lower() for v in value))
    return str(value or "").strip().lower()


class SlotBook:
    """一个任务的槽位账本（不可变风格：每次操作返回新的账本快照）。"""

    def __init__(self, bundle: Optional[Dict[str, Any]] = None):
        bundle = bundle or {}
        fields = bundle.get("fields")
        negations = bundle.get("negations")
        self._fields: Dict[str, Dict[str, Any]] = dict(fields or {})
        self._negations: List[Dict[str, Any]] = list(negations or [])

    # ── 读 ──────────────────────────────────────────────────────

    def to_bundle(self) -> Dict[str, Any]:
        return {"fields": dict(self._fields), "negations": list(self._negations)}

    def get(self, name: str) -> Dict[str, Any]:
        item = self._fields.get(name)
        return dict(item) if isinstance(item, dict) else {}

    def value(self, name: str, fallback: Any = None) -> Any:
        item = self._fields.get(name)
        return item.get("value") if isinstance(item, dict) else fallback

    def has_value(self, name: str) -> bool:
        value = self.value(name)
        if value is None:
            return False
        if isinstance(value, (str, list, tuple, dict)):
            return bool(value)
        return True

    def source(self, name: str) -> str:
        return str(self.get(name).get("source") or "")

    def confirmed(self, name: str) -> bool:
        return bool(self.get(name).get("confirmed"))

    def negations(self, name: str = "") -> List[Dict[str, Any]]:
        if not name:
            return list(self._negations)
        return [n for n in self._negations if n.get("field") == name]

    def is_negated(self, name: str, value: Any) -> bool:
        """该字段的这个值是否被用户否定过（否定记录未被撤销）。"""
        target = _norm_compare(value)
        if not target:
            return False
        for record in self.negations(name):
            recorded = _norm_compare(record.get("value"))
            if recorded and (recorded == target
                             or recorded in target or target in recorded):
                return True
        return False

    # ── 写（返回新账本，不改自身） ───────────────────────────────

    def _clone(self) -> "SlotBook":
        return SlotBook(self.to_bundle())

    def set(self, name: str, value: Any, source: str, *, evidence: str = "",
            detail: Optional[Dict[str, Any]] = None,
            override: bool = False,
            allow_negated: bool = False) -> "SlotBook":
        """写入一个槽位。两道闸门互相独立：

        `override`      跳过来源优先级比较（本轮明确值优先级最高，§4.2）。
        `allow_negated` 允许写入一个曾被否定的值，并撤销那条否定记录。
                        **只有用户在本轮原话里重新提到它时才为真**——
                        否则历史合槽会把「不是武汉」里的武汉又填回来
                        （9.2 第 4 条）。
        """
        book = self._clone()
        if book.is_negated(name, value) and not allow_negated:
            return book
        current = book._fields.get(name)
        if isinstance(current, dict) and not override:
            if priority_of(source) > priority_of(str(current.get("source") or "")):
                return book
        book._fields[name] = make_slot(value, source, evidence=evidence,
                                       detail=detail)
        if allow_negated:
            book._negations = [n for n in book._negations
                               if not (n.get("field") == name
                                       and _norm_compare(n.get("value"))
                                       == _norm_compare(value))]
        return book

    def drop(self, name: str) -> "SlotBook":
        """丢掉一个字段的值，**不留否定记录**。

        用于「模型给的地名压根绑不到真实文件」这类情况：那不是用户否定，
        只是一串没落地的文字，不该变成「以后都不许用它」。
        """
        book = self._clone()
        book._fields.pop(name, None)
        return book

    def clear(self, name: str, *, evidence: str = "",
              negated_value: Any = None) -> "SlotBook":
        """清除一个字段并留下否定记录（「不是武汉」→ 地区空 + 武汉被否定）。"""
        book = self._clone()
        recorded = negated_value if negated_value is not None \
            else book.value(name)
        book._fields.pop(name, None)
        if recorded not in (None, "", [], {}):
            book._negations.append({
                "field": name, "value": recorded, "evidence": evidence,
                "at": _now(),
            })
        return book

    def fill_default(self, name: str, value: Any, source: str = SRC_DEFAULT,
                     *, evidence: str = "") -> "SlotBook":
        """只在字段完全缺失时补默认/建议值，来源如实记为默认（9.2 第 4 条）。"""
        if self.has_value(name):
            return self._clone()
        return self.set(name, value, source, evidence=evidence)


def grounded_in_message(patch: ops.FieldPatch, message: str) -> bool:
    """这条补丁的值是否真的出自用户本轮原话。

    模型经常把上文里的旧值一并回显。判断「用户本轮明确说了」不能只看
    模型标了 set，必须能在原句里找到对应的字：只有找得到，才允许它
    撤销一条否定记录（「不是武汉」之后又说「还是用武汉吧」）。
    """
    text = (message or "").strip().lower()
    if not text:
        return False
    evidence = str(patch.evidence or "").strip().lower()
    if evidence and evidence in text:
        return True
    value = _norm_compare(patch.value)
    if not value:
        return False
    if value in text:
        return True
    # 模型常把「武汉」规范成「武汉市_市」：用值里长度 ≥2 的连续片段回查原句
    return any(value[i:i + 2] in text for i in range(len(value) - 1)
               if value[i:i + 2].strip())


def apply_patch(book: SlotBook, patch: ops.FieldPatch, message: str, *,
                source: str = SRC_USER) -> Tuple[SlotBook, str]:
    """把一条字段补丁应用到账本；返回 (新账本, 变更说明)。

    补丁里的值是**模型给的原始表达**（如「武汉」「7 月」），
    真实绑定与时间解析由 resolution.py 在此之后做。
    """
    if patch.action == ops.PATCH_CLEAR:
        return (book.clear(patch.field, evidence=patch.evidence,
                           negated_value=patch.value),
                f"清除 {patch.field}")
    return (book.set(patch.field, patch.value, source, evidence=patch.evidence,
                     override=True,
                     allow_negated=grounded_in_message(patch, message)),
            f"设置 {patch.field}")
