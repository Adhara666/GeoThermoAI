# -*- coding: utf-8 -*-
"""理解层 — 目标绑定（升级第二阶段：理解层）。

依据总体技术方案 §4.2「目标绑定与字段补丁」与升级方案 9.2 第 2 条：

  绑定优先级：经过归属校验的明确编号 → 本轮地区/时间/序号等指称 →
  当前有效问题或明确选中产物 → 唯一合理未完成任务。
  **多个合理目标就追问，不按「最近修改文件」或不相关历史城市猜测。**

研究区绑定另外要求（§4.2 末段）：先得到**全部**候选再判断唯一，
并绑定内容指纹；不能取第一个模糊匹配就执行。
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 绑定结果种类
BIND_UNIQUE = "unique"        # 唯一命中，可以直接用
BIND_AMBIGUOUS = "ambiguous"  # 多个合理候选，必须追问
BIND_MISSING = "missing"      # 一个候选都没有
BIND_EMPTY = "empty"          # 根本没有可选对象（如一个研究区都没上传）

# 序号指称：「第二个」「第 2 个」「第二组」
_ORDINAL_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL_RE = re.compile(r"第\s*(\d+|[一二两三四五六七八九十])\s*(?:个|条|项|组|张|次)?")

# 指代已有产物/任务的说法（「这张图」「上一个」「刚才那个」）
_DEICTIC_WORDS = ("这张", "这个", "该", "上一个", "上个", "刚才", "刚刚",
                  "之前那个", "那张", "那个")


@dataclass
class RegionBinding:
    """研究区绑定结果。`kind=unique` 时 path/display/content_hash 有效。"""

    kind: str
    query: str = ""
    path: str = ""
    display: str = ""
    content_hash: str = ""
    options: List[Dict[str, str]] = field(default_factory=list)

    def to_slot_detail(self) -> Dict[str, Any]:
        return {"path": self.path, "display": self.display,
                "content_hash": self.content_hash}


@dataclass
class TaskBinding:
    """台账任务绑定结果。`kind=unique` 时 task 有效。"""

    kind: str
    query: str = ""
    task: Optional[Dict[str, Any]] = None
    options: List[Dict[str, str]] = field(default_factory=list)


# ── 研究区 ───────────────────────────────────────────────────────


def _study_area_names(path: Path) -> List[str]:
    """一个研究区文件可用于匹配的名字：文件名 + GeoJSON 属性里的名称。"""
    names = [path.stem]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        features = data.get("features") or []
        props = (features[0].get("properties") or {}) if features else {}
        for key in ("name", "fullname", "pname", "city", "adcode"):
            value = props.get(key)
            if value:
                names.append(str(value))
    except Exception:
        pass
    return [n for n in names if n]


def file_content_hash(path: Path, *, chunk: int = 1 << 20) -> str:
    """研究区文件内容指纹（§4.2「绑定内容指纹和副本」）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def bind_region(name: str, candidates: Sequence[Path]) -> RegionBinding:
    """把地名绑定到真实研究区文件；先收全部候选再判唯一。"""
    paths = [Path(p) for p in candidates]
    query = (name or "").strip()
    if not paths:
        return RegionBinding(kind=BIND_EMPTY, query=query)

    options = [{"id": str(i + 1), "label": p.stem, "value": str(p.resolve())}
               for i, p in enumerate(paths)]

    if not query:
        # 只有一个可用研究区且用户未另指地区：摘要说明采用它即可
        # （最终版 §4.3「只有一个可用研究区」行）
        if len(paths) == 1:
            return _unique(paths[0], query)
        return RegionBinding(kind=BIND_MISSING, query=query, options=options)

    exact = [p for p in paths if p.stem == query]
    if len(exact) == 1:
        return _unique(exact[0], query)
    if len(exact) > 1:
        return RegionBinding(kind=BIND_AMBIGUOUS, query=query,
                             options=_options_of(exact))

    partial = [p for p in paths if query in p.stem or p.stem in query]
    if len(partial) == 1:
        return _unique(partial[0], query)
    if len(partial) > 1:
        return RegionBinding(kind=BIND_AMBIGUOUS, query=query,
                             options=_options_of(partial))

    by_property = [p for p in paths
                   if any(query in n or n in query for n in _study_area_names(p))]
    if len(by_property) == 1:
        return _unique(by_property[0], query)
    if len(by_property) > 1:
        return RegionBinding(kind=BIND_AMBIGUOUS, query=query,
                             options=_options_of(by_property))
    return RegionBinding(kind=BIND_MISSING, query=query, options=options)


def _options_of(paths: Sequence[Path]) -> List[Dict[str, str]]:
    return [{"id": str(i + 1), "label": p.stem, "value": str(p.resolve())}
            for i, p in enumerate(paths)]


def _unique(path: Path, query: str) -> RegionBinding:
    resolved = path.resolve()
    return RegionBinding(kind=BIND_UNIQUE, query=query, path=str(resolved),
                         display=path.stem, content_hash=file_content_hash(resolved))


# ── 台账任务 ─────────────────────────────────────────────────────


def parse_ordinal(text: str) -> Optional[int]:
    """从「第二个」这类指称里取出 1 基序号；取不到返回 None。"""
    match = _ORDINAL_RE.search(text or "")
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        value = int(token)
    else:
        value = _ORDINAL_CN.get(token, 0)
    return value if value > 0 else None


def is_deictic(text: str) -> bool:
    """是否是「这张图」「上一个」这类指代（需要有唯一在手对象才能绑定）。"""
    return any(word in (text or "") for word in _DEICTIC_WORDS)


def _task_label(task: Dict[str, Any]) -> str:
    return str(task.get("label") or "") or str(task.get("capability") or "任务")


def bind_task(ref: str, tasks: Sequence[Dict[str, Any]]) -> TaskBinding:
    """把「南京」「第二个」「这张图」绑定到台账里的一个未完成任务。

    多个合理目标一律返回 ambiguous 交给追问；绝不按「最近修改」猜。
    """
    query = (ref or "").strip()
    items = list(tasks)
    options = [{"id": str(i + 1), "label": _task_label(t), "value": str(t.get("id"))}
               for i, t in enumerate(items)]
    if not items:
        return TaskBinding(kind=BIND_EMPTY, query=query)

    # 1) 明确编号（前端选中/卡片回传的任务编号）
    if query:
        exact_id = [t for t in items if str(t.get("id")) == query]
        if len(exact_id) == 1:
            return TaskBinding(kind=BIND_UNIQUE, query=query, task=exact_id[0])

    # 2) 序号指称
    ordinal = parse_ordinal(query)
    if ordinal is not None:
        if 1 <= ordinal <= len(items):
            return TaskBinding(kind=BIND_UNIQUE, query=query,
                               task=items[ordinal - 1])
        return TaskBinding(kind=BIND_AMBIGUOUS, query=query, options=options)

    # 3) 地区/标签指称
    if query:
        hits = [t for t in items if query and query in _task_label(t)]
        if len(hits) == 1:
            return TaskBinding(kind=BIND_UNIQUE, query=query, task=hits[0])
        if len(hits) > 1:
            return TaskBinding(kind=BIND_AMBIGUOUS, query=query,
                               options=_task_options(hits))
        if not is_deictic(query):
            return TaskBinding(kind=BIND_MISSING, query=query, options=options)

    # 4) 唯一合理未完成任务（含「这张图」这类指代）
    if len(items) == 1:
        return TaskBinding(kind=BIND_UNIQUE, query=query, task=items[0])
    return TaskBinding(kind=BIND_AMBIGUOUS, query=query, options=options)


def _task_options(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [{"id": str(i + 1), "label": _task_label(t), "value": str(t.get("id"))}
            for i, t in enumerate(tasks)]
