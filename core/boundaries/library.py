# -*- coding: utf-8 -*-
"""边界库：本地索引 + 名称检索。

目录布局（数据目录下，统一 WGS84）：
  boundary_library/
    index.json                      # 本文件维护的全量索引
    cn/武汉市/武汉市.geojson          # level=city
    cn/武汉市/洪山区.geojson          # level=district
    cn/武汉市/洪山区/关山街道.geojson  # level=street（OSM 尽力抓取）
    global/USA/ADM2/xxx.geojson     # 海外整文件（懒加载按 feature 查）

检索约定（供理解层绑定使用）：
  - 名字规范化：全角转半角、去空白、自动去行政后缀（市/区/县/街道/镇/乡…）；
  - 返回按匹配分排序的候选列表；上层按「唯一命中才绑定，多命中必追问」处理。
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_VERSION = 1

# 行政后缀（长后缀在前，避免「街道办事处」被「街道」截断）
_SUFFIXES = ("街道办事处", "自治区", "特别行政区", "自治县", "自治州",
             "街道", "省", "市", "区", "县", "镇", "乡", "盟", "旗")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    """全角转半角 + 去空白。"""
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if code == 0x3000:
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if not ch.isspace():
            out.append(ch)
    return "".join(out)


def _strip_suffix(text: str) -> str:
    for suf in _SUFFIXES:
        if len(text) > len(suf) and text.endswith(suf):
            return text[:-len(suf)]
    return text


def _match_score(cand_name: str, aliases: Sequence[str], query: str) -> int:
    """名称匹配分：精确 > 去后缀相等 > 别名 > 包含。0 表示不匹配。"""
    if not query:
        return 0
    if query == cand_name:
        return 100
    if query == _strip_suffix(cand_name):
        return 90
    if query in aliases:
        return 85
    base = _strip_suffix(query)
    if base == _strip_suffix(cand_name):
        return 80
    if len(query) >= 2 and (query in cand_name):
        return 70
    if len(base) >= 2 and base in cand_name:
        return 60
    return 0


class BoundaryLibrary:
    """边界库读写。索引文件懒加载；写操作加锁。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._index: Optional[Dict[str, Any]] = None
        self._global_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ── 索引读写 ────────────────────────────────────────────────

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self._index is None:
                p = self._index_path()
                if p.is_file():
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except ValueError:
                        data = {}
                else:
                    data = {}
                data.setdefault("version", _VERSION)
                data.setdefault("entries", [])
                data.setdefault("global_files", [])
                self._index = data
            return self._index

    def save(self) -> None:
        with self._lock:
            if self._index is None:
                return
            self._index["updated_at"] = _utcnow()
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._index, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            tmp.replace(self._index_path())

    # ── 写入 ────────────────────────────────────────────────────

    def add_entry(self, *, name: str, level: str, path: str,
                  source: str, parent: str = "", adcode: str = "",
                  aliases: Sequence[str] = (), bbox: Optional[Sequence[float]] = None,
                  crs: str = "wgs84") -> Dict[str, Any]:
        """新增/覆盖一条索引（同名同层级同 parent 视为覆盖）。"""
        data = self.load()
        key = adcode or f"{level}:{parent}:{name}"
        entry = {
            "key": str(key), "name": name, "level": level,
            "parent": parent, "adcode": str(adcode or ""),
            "aliases": list(dict.fromkeys([*aliases, _strip_suffix(name)])),
            "path": str(path), "source": source, "crs": crs,
            "bbox": list(bbox) if bbox else None,
        }
        entries = data["entries"]
        for i, old in enumerate(entries):
            if old.get("key") == entry["key"] or (
                    old.get("name") == name and old.get("level") == level
                    and old.get("parent") == parent):
                entries[i] = entry
                return entry
        entries.append(entry)
        return entry

    def register_global_file(self, *, iso: str, adm: int, path: str,
                             source: str, name_field: str = "shapeName",
                             count: int = 0) -> None:
        data = self.load()
        for old in data["global_files"]:
            if (old.get("iso") == iso and int(old.get("adm", 0)) == adm):
                old.update({"path": path, "source": source,
                            "name_field": name_field, "count": count})
                return
        data["global_files"].append({
            "iso": iso.upper(), "adm": adm, "path": path, "source": source,
            "name_field": name_field, "count": count,
        })

    # ── 检索（国内小文件） ──────────────────────────────────────

    def search(self, name: str, *, levels: Sequence[str] = (),
               parent_contains: str = "") -> List[Dict[str, Any]]:
        """按名称检索国内条目；返回带 score 的候选（降序）。

        levels 为空表示不限层级；parent_contains 可限定上级名。
        """
        query = _normalize(name)
        if not query:
            return []
        scored = []
        for e in self.load()["entries"]:
            if levels and e.get("level") not in levels:
                continue
            if parent_contains and parent_contains not in str(e.get("parent", "")):
                continue
            score = _match_score(str(e.get("name", "")),
                                 [str(a) for a in e.get("aliases") or []],
                                 query)
            if score:
                scored.append({**e, "score": score})
        scored.sort(key=lambda x: (-x["score"], str(x.get("name"))))
        return scored

    def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        for e in self.load()["entries"]:
            if str(e.get("key")) == str(key):
                return e
        return None

    def resolve_path(self, entry: Dict[str, Any]) -> Path:
        return self.root / str(entry.get("path"))

    # ── 检索（海外大文件，懒加载） ──────────────────────────────

    def search_global(self, name: str, *, iso: str = "",
                      adm: Optional[int] = None,
                      limit: int = 20) -> List[Dict[str, Any]]:
        """在已下载的海外边界文件中按名称检索 feature。"""
        query = _normalize(name)
        results: List[Dict[str, Any]] = []
        for meta in self.load()["global_files"]:
            if iso and str(meta.get("iso", "")).upper() != iso.upper():
                continue
            if adm is not None and int(meta.get("adm", 0)) != int(adm):
                continue
            feats = self._load_global_features(meta)
            nf = str(meta.get("name_field") or "shapeName")
            for idx, feat in enumerate(feats):
                props = feat.get("properties") or {}
                cand_name = str(props.get(nf) or props.get("name") or "")
                if not cand_name:
                    continue
                score = _match_score(cand_name,
                                     [_strip_suffix(cand_name)], query)
                if score:
                    results.append({
                        "name": cand_name, "level": "town",
                        "iso": meta.get("iso"), "adm": meta.get("adm"),
                        "path": meta.get("path"), "feature_index": idx,
                        "source": meta.get("source", "geoboundaries"),
                        "score": score,
                    })
        results.sort(key=lambda x: -x["score"])
        return results[:limit]

    def _load_global_features(self, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
        path = str(meta.get("path") or "")
        if path in self._global_cache:
            return self._global_cache[path]
        p = self.root / path if not Path(path).is_absolute() else Path(path)
        feats: List[Dict[str, Any]] = []
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("type") == "FeatureCollection":
                    feats = data.get("features") or []
            except (ValueError, OSError):
                feats = []
        self._global_cache[path] = feats
        return feats

    def load_global_feature(self, meta_path: str, index: int) -> Optional[dict]:
        """取海外某文件的第 index 个 feature（返回 {feature, bbox}）。"""
        for meta in self.load()["global_files"]:
            if str(meta.get("path")) == str(meta_path):
                feats = self._load_global_features(meta)
                if 0 <= index < len(feats):
                    return feats[index]
                return None
        return None


_LIBRARY: Optional[BoundaryLibrary] = None
_LIBRARY_LOCK = threading.Lock()


def get_library(root: Optional[Path] = None) -> BoundaryLibrary:
    """进程内单例；root 缺省为数据目录下 boundary_library/。"""
    global _LIBRARY
    if root is not None:
        return BoundaryLibrary(root)
    with _LIBRARY_LOCK:
        if _LIBRARY is None:
            from core.deployment import DeploymentConfig
            data_root = Path(DeploymentConfig.load().data_root)
            _LIBRARY = BoundaryLibrary(data_root / "boundary_library")
        return _LIBRARY


def guess_level(name: str) -> str:
    """按名称后缀猜层级（供提问路由用；不命中返回空串）。"""
    n = _normalize(name)
    if re.search(r"街道|镇$|乡$", n):
        return "street"
    if n.endswith(("区", "县", "旗")):
        return "district"
    if n.endswith("市"):
        return "city"
    return ""
