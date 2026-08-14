"""
项目用户偏好 JSON 存储（键值对）

`memory/projects/{project_id}/preferences.json`：如
`{"cloud_threshold": 40, "preferred_model": "rf"}`。
来源为用户显式告知或 Agent 询问确认后写入；写入原子替换。
"""

import json
import os
import threading
from typing import Any, Dict

from ..atomic_io import atomic_write_json

_lock = threading.Lock()


class Preferences:
    """preferences.json 键值读写封装（每项目一份）。"""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with _lock:
            data = self._load()
            data[key] = value
            self._save(data)

    def all(self) -> Dict[str, Any]:
        return self._load()
