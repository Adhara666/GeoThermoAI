"""
训练主反思：七条规则兜底

决策顺序：LLM 先给 `{action, reason, new_params}`，再按 R1→R7 逐条修正，
**规则永远覆盖 LLM**；每次覆盖都记进 `rule_hits`，供气泡与报告标注「[规则] R3」。

`MAX_TUNING_ROUNDS`（硬上限 8）与 `DEFAULT_TUNING_ROUNDS`（默认 5）的单一来源在
`orchestrator/agent_config.py`，这里只做导入与再导出。
"""

from typing import Any, Dict, List, Optional, Tuple

from ..orchestrator.agent_config import DEFAULT_TUNING_ROUNDS, MAX_TUNING_ROUNDS

TUNING_RULES = {
    "R1": "测试集 R² < 0.60 → 强制调优（覆盖 LLM 给出的 accept）",
    "R2": "测试集 R² ≥ 0.88 → 禁止继续调优（覆盖 LLM 给出的 adjust）",
    "R3": "训练 R² − 测试 R² > 0.20 → 判定过拟合，强制干预调优方向："
          "max_depth 下调 10（下限 5），min_samples_leaf 上调至少 2",
    "R4": "参数越界 → 截断到安全区间",
    "R5": "最近两轮 R²、MAE、RMSE 的提升均 < 0.01 → 强制停止（已收敛；"
          "三指标中任何一个提升 ≥ 0.01 都继续调优）",
    "R6": "最近两轮 R² 连续下降 → 强制停止（走势恶化）",
    "R7": "调优轮数达到生效上限 → 强制停止，取均方根误差最低的一轮作为最终结果",
}

# R1 / R2 阈值
R2_FORCE_TUNE_BELOW = 0.60
R2_FORBID_TUNE_ABOVE = 0.88
# R3 过拟合判据
OVERFIT_GAP = 0.20
# R5 收敛判据
CONVERGE_DELTA = 0.01

# R4 安全区间
PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "n_estimators": (10, 2000),
    "max_depth": (1, 100),
    "min_samples_split": (2, 100),
    "min_samples_leaf": (1, 50),
    "max_features": (0.1, 1.0),
}
_INT_PARAMS = ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf")

# 指标解读分档（领域知识 K24 / E02）
GRADES = ((0.85, "优秀"), (0.80, "良好"), (0.75, "合格"))

# 硬停止规则：只有这四条能覆盖用户在 tuning_round 节点上「继续下一轮」的明确选择。
# R1/R3 是「强制继续调优」，R4 只截断参数，都不构成停止理由。
HARD_STOP_RULES = ("R2", "R5", "R6", "R7")


class Decision:
    ACCEPT = "accept"   # 接受当前结果，结束循环
    ADJUST = "adjust"   # 改超参再训一轮
    STOP = "stop"       # 停止循环，取均方根误差最低的一轮

    ALL = (ACCEPT, ADJUST, STOP)


def resolve_max_rounds(configured: Optional[Any] = None) -> int:
    """生效上限 = clamp(配置值 或 默认 5, 1, MAX_TUNING_ROUNDS)。"""
    try:
        value = int(configured) if configured is not None else DEFAULT_TUNING_ROUNDS
    except (TypeError, ValueError):
        value = DEFAULT_TUNING_ROUNDS
    return max(1, min(MAX_TUNING_ROUNDS, value))


def grade(test_r2: Optional[float]) -> str:
    """按 K24 / E02 给出评级词。"""
    if test_r2 is None:
        return "未知"
    try:
        value = float(test_r2)
    except (TypeError, ValueError):
        return "未知"
    for threshold, label in GRADES:
        if value >= threshold:
            return label
    return "偏低"


def clamp_params(params: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """R4：把参数截断到安全区间。返回 (新参数字典, 是否发生截断)。"""
    out: Dict[str, Any] = {}
    clamped = False
    for key, value in (params or {}).items():
        bounds = PARAM_BOUNDS.get(key)
        if bounds is None:
            out[key] = value
            continue
        low, high = bounds
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        fixed = max(low, min(high, number))
        if abs(fixed - number) > 1e-9:
            clamped = True
        out[key] = int(round(fixed)) if key in _INT_PARAMS else round(fixed, 3)
    return out, clamped


def low_precision_direction(current: Dict[str, Any]) -> Dict[str, Any]:
    """R1 强制调优但 LLM 没给方向时的确定性兜底方向。

    精度过低通常是欠拟合：加大模型容量（更多的树、更深的树），
    截断由 R4 统一负责。这是规则层的兜底，保证「强制调优」不会变成空转。
    """
    try:
        trees = int(current.get("n_estimators", 200))
    except (TypeError, ValueError):
        trees = 200
    try:
        depth = int(current.get("max_depth", 25))
    except (TypeError, ValueError):
        depth = 25
    return {"n_estimators": int(trees * 1.5), "max_depth": depth + 5}


def fallback_direction(current: Dict[str, Any]) -> Dict[str, Any]:
    """必须再训一轮但拿不到调优方向时的兜底方向（与 R1 同一策略：加大模型容量）。

    用于「用户明确要求继续下一轮，但大模型不可用」的场景——把用户的「继续」
    落实成一次真实的参数变更，而不是悄悄变成「停止」。
    """
    return low_precision_direction(current)


def hard_stopped(rule_hits: Any) -> bool:
    """命中的规则里是否有硬停止规则（可覆盖用户的「继续下一轮」）。"""
    return any(rule in HARD_STOP_RULES for rule in (rule_hits or ()))


def overfit_direction(current: Dict[str, Any]) -> Dict[str, Any]:
    """R3：过拟合时的强制调优方向。"""
    try:
        depth = int(current.get("max_depth", 25))
    except (TypeError, ValueError):
        depth = 25
    try:
        leaf = int(current.get("min_samples_leaf", 8))
    except (TypeError, ValueError):
        leaf = 8
    return {"max_depth": max(5, depth - 10), "min_samples_leaf": leaf + 2}


def _r2(round_data: Dict[str, Any]) -> Optional[float]:
    value = round_data.get("test_r2")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_values(rounds: List[Dict[str, Any]], key: str) -> Optional[List[float]]:
    """取最近几轮某指标的数值序列；任一轮缺失该指标返回 None（不判收敛）。"""
    values = []
    for r in rounds:
        try:
            values.append(float(r.get(key)))
        except (TypeError, ValueError):
            return None
    return values


def converged(rounds: List[Dict[str, Any]]) -> bool:
    """R5：最近两轮 R²、MAE、RMSE 的提升**都** < 0.01 才算收敛。

    三个指标方向不同：R² 越大越好（提升 = 新 − 旧），MAE/RMSE 越小越好
    （提升 = 旧 − 新）。三指标中**任何一个**的相邻提升 ≥ 0.01 → 未收敛，
    继续调优；任一指标缺失 → 保守不判收敛（靠 R7 轮数上限兜底停止）。
    """
    if len(rounds) < 3:
        return False
    last3 = rounds[-3:]
    r2 = _metric_values(last3, "test_r2")
    mae = _metric_values(last3, "mae")
    rmse = _metric_values(last3, "rmse")
    if r2 is None or mae is None or rmse is None:
        return False

    def any_improvement(values: List[float], higher_better: bool) -> bool:
        for i in range(1, len(values)):
            delta = values[i] - values[i - 1] if higher_better else values[i - 1] - values[i]
            if delta >= CONVERGE_DELTA:
                return True
        return False

    if any_improvement(r2, higher_better=True):
        return False
    if any_improvement(mae, higher_better=False):
        return False
    if any_improvement(rmse, higher_better=False):
        return False
    return True


def deteriorating(rounds: List[Dict[str, Any]]) -> bool:
    """R6：最近两轮 R² 连续下降。"""
    if len(rounds) < 3:
        return False
    values = [_r2(r) for r in rounds[-3:]]
    if any(v is None for v in values):
        return False
    return values[1] < values[0] and values[2] < values[1]


def best_round(rounds: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """取均方根误差最低的一轮（R7 的最终结果口径）。"""
    candidates = []
    for r in rounds or []:
        try:
            candidates.append((float(r.get("rmse")), r))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return rounds[-1] if rounds else None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def rule_safeguard(llm_decision: Optional[Dict[str, Any]],
                   context: Dict[str, Any]) -> Dict[str, Any]:
    """按 R1→R7 顺序逐条修正 LLM 决策，返回最终决策。

    context 需含：
      rounds        已完成各轮的指标列表 [{round, params, train_r2, test_r2, rmse}]
      current       本轮指标 {train_r2, test_r2, rmse, params}
      max_rounds    生效轮数上限（AI 连续调优轮数，不含初始训练轮）
      ai_rounds     连续 AI 调优轮数（手动调优轮不占额度且会重置）；缺省时回退旧语义
    """
    rounds = list(context.get("rounds") or [])
    current = dict(context.get("current") or {})
    max_rounds = resolve_max_rounds(context.get("max_rounds"))

    # 轮数上限是否已命中：按连续 AI 调优轮数判定（手动轮不占额度、会重置计数）；
    # 未传 ai_rounds 的旧调用方回退「已完成轮数 + 即将进行的一轮 达到上限」的原语义。
    ai_raw = context.get("ai_rounds")
    if ai_raw is None:
        _limit_hit = len(rounds) + 1 >= max_rounds
    else:
        try:
            _limit_hit = int(ai_raw) >= max_rounds
        except (TypeError, ValueError):
            _limit_hit = len(rounds) + 1 >= max_rounds

    decision = dict(llm_decision or {})
    action = decision.get("action")
    if action not in Decision.ALL:
        # LLM 不可用/未给出动作时默认继续调优，是否停止统一交给 R1–R7 判定
        action = Decision.ADJUST
    reason = str(decision.get("reason") or "")
    new_params = dict(decision.get("new_params") or {})
    rule_hits: List[str] = []
    notes: List[str] = []

    test_r2 = _r2(current)
    train_r2 = None
    try:
        train_r2 = float(current.get("train_r2"))
    except (TypeError, ValueError):
        train_r2 = None

    # R1：精度过低强制继续调优。无论 LLM 原本给的是 accept 还是 adjust，
    # 只要没给出调优方向就用确定性兜底方向，保证「强制调优」不会变成空转。
    if test_r2 is not None and test_r2 < R2_FORCE_TUNE_BELOW:
        if action != Decision.ADJUST:
            action = Decision.ADJUST
            rule_hits.append("R1")
            notes.append(f"测试集决定系数 {test_r2:.2f} 低于 {R2_FORCE_TUNE_BELOW}，"
                         f"强制继续调优")
        if not new_params:
            new_params = low_precision_direction(current.get("params") or {})
            if "R1" not in rule_hits:
                rule_hits.append("R1")
                notes.append(f"测试集决定系数 {test_r2:.2f} 偏低，按规则给出加大模型容量的"
                             f"调优方向")

    # R2：精度足够高禁止继续调优
    if test_r2 is not None and test_r2 >= R2_FORBID_TUNE_ABOVE and action == Decision.ADJUST:
        action = Decision.ACCEPT
        rule_hits.append("R2")
        notes.append(f"测试集决定系数 {test_r2:.2f} 已达 {R2_FORBID_TUNE_ABOVE}，不再继续调优")

    # R3：过拟合强制干预调优方向
    if (train_r2 is not None and test_r2 is not None
            and train_r2 - test_r2 > OVERFIT_GAP):
        action = Decision.ADJUST
        new_params = {**new_params, **overfit_direction(current.get("params") or {})}
        rule_hits.append("R3")
        notes.append(f"训练与测试决定系数相差 {train_r2 - test_r2:.2f}，判定过拟合，"
                     f"下调树深并增大叶节点样本数")

    # R4：参数越界截断
    if new_params:
        new_params, clamped = clamp_params(new_params)
        if clamped:
            rule_hits.append("R4")
            notes.append("部分参数超出安全区间，已截断")

    # R5 / R6：收敛或走势恶化提前停。
    # 连续下降同时满足「提升 < 0.01」，因此两条可能同时命中，此时都记录以便追溯。
    sequence = rounds + [current]
    stop_rules: List[str] = []
    if converged(sequence):
        stop_rules.append("R5")
        notes.append("最近两轮决定系数、平均绝对误差与均方根误差均无明显改善，"
                     "判定已收敛，停止调优")
    if deteriorating(sequence):
        stop_rules.append("R6")
        notes.append("最近两轮精度连续下降，停止调优")
    if action == Decision.ADJUST and stop_rules:
        action = Decision.STOP
        rule_hits.extend(stop_rules)

    # R7：AI 连续调优轮数达到生效上限 → 强制停止（_limit_hit 已在函数开头计算，
    # 语义：上限只统计连续 AI 调优轮，初始训练轮不算，手动调优轮不占额度且会重置）
    if action == Decision.ADJUST and _limit_hit:
        action = Decision.STOP
        rule_hits.append("R7")
        notes.append(f"AI 调优轮数已达上限 {max_rounds} 轮，取误差最小的一轮作为最终结果")

    if action == Decision.ADJUST and not new_params:
        # LLM 没给新参数又要继续 → 视为无从调整，按接受处理，避免空转
        action = Decision.ACCEPT
        notes.append("没有给出可用的新参数，按当前结果继续")

    return {
        "action": action,
        "reason": reason,
        "note": "；".join(notes),
        "new_params": new_params,
        "rule_hits": rule_hits,
    }


# ── 主反思的额外检查项（只影响上下文与报告） ─────────

def advisory_notes(data_features: Optional[dict],
                   rf_data: Optional[dict]) -> List[str]:
    """样本量/地形/温度变异/植被/特征重要性的提示。"""
    features = data_features or {}
    rf = rf_data or {}
    notes: List[str] = []

    samples = features.get("train_samples")
    if isinstance(samples, (int, float)) and samples:
        if samples > 50000:
            notes.append("样本量较大，可适当增加决策树数量（领域知识 K20）")
        elif samples < 10000:
            notes.append("样本量偏小，应减少决策树数量以防过拟合（领域知识 K20）")

    dem_std = features.get("dem_std")
    if isinstance(dem_std, (int, float)):
        if dem_std > 100:
            notes.append("地形复杂，最大深度可放到 30 到 40（领域知识 K21）")
        elif 0 < dem_std < 30:
            notes.append("地形平坦，最大深度收到 15 到 20 更合适（领域知识 K21）")

    lst_std = features.get("lst_std")
    if isinstance(lst_std, (int, float)) and lst_std > 5:
        notes.append("温度变异较大，叶节点最小样本数可减到 5（领域知识 K22）")

    ndvi_mean = features.get("ndvi_mean")
    if isinstance(ndvi_mean, (int, float)) and ndvi_mean > 0.5:
        notes.append("植被覆盖高，最大特征比例可增到 0.7（领域知识 K23）")

    importance = {item.get("feature"): item.get("importance")
                  for item in (rf.get("feature_importance") or [])
                  if isinstance(item, dict)}
    ttri = importance.get("TTRI")
    if isinstance(ttri, (int, float)) and ttri < 0.01:
        notes.append("地形热响应指数贡献极低，需检查 DEM 数据是否正常")

    return notes
