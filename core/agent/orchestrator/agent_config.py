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
    # ── I2：阈值可配置（默认值保持现状，只多一层配置读取） ──
    # 训练调优阈值
    "r2_force_tune_below": 0.60,       # R1: 低于此值强制调优
    "r2_forbid_tune_above": 0.88,      # R2: 达到此值禁止调优
    "overfit_gap": 0.20,               # R3: train-test gap 超过此值判过拟合
    "converge_delta": 0.01,            # R5: 轮间提升低于此值判收敛
    # 数据质量阈值
    "min_train_rows": 10000,           # D3: 最小训练样本数
    "min_valid_ratio": 0.15,           # D7: 云掩膜后有效像元占比下限
    "min_split_share": 0.05,           # D5: 划分集合占比下限
    # advisory_notes 阈值
    "large_sample_threshold": 50000,   # 样本量大
    "small_sample_threshold": 10000,   # 样本量小
    "complex_terrain_threshold": 100,  # 地形复杂（DEM 标准差）
    "flat_terrain_threshold": 30,      # 地形平坦
    "high_lst_std_threshold": 5,       # 温度变异大
    "high_ndvi_threshold": 0.5,        # 植被覆盖高
    "low_ttri_threshold": 0.01,        # TTRI 贡献极低
}


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
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
        # I2：阈值可配置（默认值保持现状）
        "r2_force_tune_below": _as_float(raw.get("r2_force_tune_below"),
                                          AGENT_DEFAULTS["r2_force_tune_below"]),
        "r2_forbid_tune_above": _as_float(raw.get("r2_forbid_tune_above"),
                                           AGENT_DEFAULTS["r2_forbid_tune_above"]),
        "overfit_gap": _as_float(raw.get("overfit_gap"), AGENT_DEFAULTS["overfit_gap"]),
        "converge_delta": _as_float(raw.get("converge_delta"), AGENT_DEFAULTS["converge_delta"]),
        "min_train_rows": _as_int(raw.get("min_train_rows"),
                                   AGENT_DEFAULTS["min_train_rows"], 100, 10000000),
        "min_valid_ratio": _as_float(raw.get("min_valid_ratio"),
                                      AGENT_DEFAULTS["min_valid_ratio"]),
        "min_split_share": _as_float(raw.get("min_split_share"),
                                      AGENT_DEFAULTS["min_split_share"]),
        "large_sample_threshold": _as_int(raw.get("large_sample_threshold"),
                                           AGENT_DEFAULTS["large_sample_threshold"], 100, 10000000),
        "small_sample_threshold": _as_int(raw.get("small_sample_threshold"),
                                           AGENT_DEFAULTS["small_sample_threshold"], 100, 10000000),
        "complex_terrain_threshold": _as_float(raw.get("complex_terrain_threshold"),
                                                 AGENT_DEFAULTS["complex_terrain_threshold"]),
        "flat_terrain_threshold": _as_float(raw.get("flat_terrain_threshold"),
                                              AGENT_DEFAULTS["flat_terrain_threshold"]),
        "high_lst_std_threshold": _as_float(raw.get("high_lst_std_threshold"),
                                              AGENT_DEFAULTS["high_lst_std_threshold"]),
        "high_ndvi_threshold": _as_float(raw.get("high_ndvi_threshold"),
                                          AGENT_DEFAULTS["high_ndvi_threshold"]),
        "low_ttri_threshold": _as_float(raw.get("low_ttri_threshold"),
                                         AGENT_DEFAULTS["low_ttri_threshold"]),
    }


def roles_enabled(settings: Optional[dict]) -> bool:
    return resolve(settings)["roles_enabled"]
