"""
角色编排的特性开关与可配置项（「特性开关」）

配置位置：`settings.agent`（每用户 `settings.json` 优先，缺失时回落本文件默认值）。

```json
{"agent": {"roles_enabled": true, "replan_max": 3, "tuning_max_rounds": 5,
           "approval_wait_seconds": 1800, "default_exec_mode": "approval"}}
```

`roles_enabled=false` 时 `process_command` 完全走现有旧路径，是随时可回退的安全阀。
"""

from typing import Any, Dict, Optional

from .exec_mode import DEFAULT_EXEC_MODE, normalize as normalize_exec_mode

# 自动 replan 上限（规则 4）
REPLAN_MAX = 3

# 审批等待超时（秒）
APPROVAL_WAIT_TIMEOUT = 1800

# 调优轮数：默认 5，硬上限 8。
# 这里是**单一来源**，`reflection/train_rules.py` 的规则 R7 从本文件导入，
# 保证「配置值 / 默认值 / 硬上限」三者不会在两处漂移。
MAX_TUNING_ROUNDS = 8
DEFAULT_TUNING_ROUNDS = 5

AGENT_DEFAULTS: Dict[str, Any] = {
    # 安全阀：P0–P6 全部阶段测试与回归验收通过，已置为 True。
    # 出现问题时把 config/settings.json 的 agent.roles_enabled 改回 false 即可整体回退到旧路径。
    "roles_enabled": True,
    "replan_max": REPLAN_MAX,
    "tuning_max_rounds": DEFAULT_TUNING_ROUNDS,
    "approval_wait_seconds": APPROVAL_WAIT_TIMEOUT,
    "default_exec_mode": DEFAULT_EXEC_MODE,
}


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def resolve(settings: Optional[dict]) -> Dict[str, Any]:
    """从完整 settings 字典解析出角色编排配置（缺项补默认，越界截断）。

    返回新字典，不修改入参。
    """
    raw = {}
    if isinstance(settings, dict) and isinstance(settings.get("agent"), dict):
        raw = settings["agent"]

    return {
        "roles_enabled": bool(raw.get("roles_enabled", AGENT_DEFAULTS["roles_enabled"])),
        "replan_max": _as_int(raw.get("replan_max"), AGENT_DEFAULTS["replan_max"], 0, 10),
        "tuning_max_rounds": _as_int(raw.get("tuning_max_rounds"),
                                     AGENT_DEFAULTS["tuning_max_rounds"],
                                     1, MAX_TUNING_ROUNDS),
        "approval_wait_seconds": _as_int(raw.get("approval_wait_seconds"),
                                        AGENT_DEFAULTS["approval_wait_seconds"], 30, 86400),
        "default_exec_mode": normalize_exec_mode(raw.get("default_exec_mode"),
                                                 AGENT_DEFAULTS["default_exec_mode"]),
    }


def roles_enabled(settings: Optional[dict]) -> bool:
    return resolve(settings)["roles_enabled"]
