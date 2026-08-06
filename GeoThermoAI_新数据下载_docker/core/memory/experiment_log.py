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

    def all(self) -> List[Dict[str, Any]]:
        return self._load()
