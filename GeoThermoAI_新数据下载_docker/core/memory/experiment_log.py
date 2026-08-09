"""
实验记录 JSON 存储（精确查询层）

`memory/projects/{project_id}/experiments.json`：一次实验一条记录（数组 append）。
提供按 conv 级联删除、按 R² 取历史最佳、取最近 N 条等精确查询。
写入使用原子替换（core.atomic_io），避免半写文件。
"""

import json
import os
import threading
from typing import Any, Dict, List, Optional

from ..atomic_io import atomic_write_json

_lock = threading.Lock()


def _norm_date(s) -> str:
    """日期归一化为 YYYYMMDD 定长字符串（YYYY-MM-DD / YYYYMMDD / 空）。"""
    return str(s or "").replace("-", "").replace("/", "").strip()


def _ranges_overlap(rec_range, start: str, end: str) -> bool:
    """记录 date_range 与查询区间是否相交；空串边界视为无界。"""
    rec_range = rec_range or ["", ""]
    rs, re = _norm_date(rec_range[0]), _norm_date(rec_range[1])
    qs, qe = _norm_date(start), _norm_date(end)
    if not rs and not re:
        return True  # 记录无日期，仅靠 region/影像对过滤
    if re and qs and re < qs:
        return False  # 记录结束早于查询开始
    if rs and qe and rs > qe:
        return False  # 记录开始晚于查询结束
    return True


class ExperimentLog:
    """experiments.json 读写封装（每项目一份）。"""

    def __init__(self, path: str):
        self.path = path

    # ── 内部 ───────────────────────────────────────────────────────

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            return []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, records)

    # ── 写入 ───────────────────────────────────────────────────────

    def add(self, record: Dict[str, Any]) -> None:
        """追加一条实验记录。若同 conv 已存在 paused 记录（暂停后续跑完），先移除再追加。"""
        with _lock:
            records = self._load()
            conv_id = record.get("conv_id", "")
            if conv_id:
                records = [r for r in records if not (
                    r.get("conv_id") == conv_id and r.get("status") == "paused"
                )]
            records.append(record)
            self._save(records)

    def delete_by_conv(self, conv_id: str) -> int:
        """删除某对话产生的全部实验记录，返回删除条数。"""
        with _lock:
            records = self._load()
            kept = [r for r in records if r.get("conv_id") != conv_id]
            removed = len(records) - len(kept)
            if removed:
                self._save(kept)
            return removed

    def count_by_conv(self, conv_id: str) -> int:
        return sum(1 for r in self._load() if r.get("conv_id") == conv_id)

    # ── 查询 ───────────────────────────────────────────────────────

    def get_recent(self, n: int = 5) -> List[Dict[str, Any]]:
        records = [r for r in self._load() if r.get("status") == "success"]
        return sorted(records, key=lambda r: r.get("timestamp", ""), reverse=True)[:n]

    def get_best(self, region: str = "", model: str = "") -> Optional[Dict[str, Any]]:
        """取指定区域/模型下测试集 R² 最高的成功实验。"""
        best = None
        for r in self._load():
            if r.get("status") != "success":
                continue
            if region and region not in str(r.get("region", "")):
                continue
            if model and r.get("model") != model:
                continue
            r2 = ((r.get("metrics") or {}).get("test") or {}).get("R2")
            if r2 is None:
                continue
            if best is None or r2 > best.get("_r2", -1):
                r["_r2"] = r2
                best = r
        if best:
            best.pop("_r2", None)
        return best

    def query(self, region: str = "", start: str = "", end: str = "",
              landsat_date: str = "", sentinel2_date: str = "",
              model: str = "") -> List[Dict[str, Any]]:
        """精确查询历史实验（结构化过滤，结构化查询层）。

        支持按研究区（子串）、时间范围（date_range 区间相交）、
        影像对日期（pair.landsat_date / pair.sentinel2_date）、模型组合过滤，
        返回匹配的成功实验列表（按时间倒序）。全部条件可选，为空表示不限制。
        """
        results = []
        for r in self._load():
            if r.get("status") != "success":
                continue
            if region and region not in str(r.get("region", "")):
                continue
            if model and r.get("model") != model:
                continue
            if (start or end) and not _ranges_overlap(r.get("date_range"), start, end):
                continue
            pair = r.get("pair") or {}
            if landsat_date and _norm_date(pair.get("landsat_date")) != _norm_date(landsat_date):
                continue
            if sentinel2_date and _norm_date(pair.get("sentinel2_date")) != _norm_date(sentinel2_date):
                continue
            results.append(r)
        return sorted(results, key=lambda x: x.get("timestamp", ""), reverse=True)

    def all(self) -> List[Dict[str, Any]]:
        return self._load()
