# -*- coding: utf-8 -*-
"""
训练主反思七规则 R1–R7 合成测试（技术方案 11.2）

运行：python tests/test_train_tuning_rules.py
覆盖：
- R1–R7 逐条单测
- 规则永远覆盖 LLM 决策
- 用脚本化指标序列驱动完整循环：收敛提前停、恶化提前停、轮数上限、取 RMSE 最低轮
- 参数越界被截断；过拟合触发 max_depth 下调
- 轮数默认 5、硬上限 8
- 6.3 的额外检查项产出提示
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent.reflection import train_rules
from core.agent.reflection.train_rules import Decision


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def _round(i, train_r2, test_r2, rmse, params=None, mae=None):
    return {"round": i, "train_r2": train_r2, "test_r2": test_r2, "rmse": rmse,
            "mae": mae,
            "params": params or {"n_estimators": 200, "max_depth": 25,
                                 "min_samples_leaf": 8}}


def _guard(llm, current, rounds=None, max_rounds=5):
    return train_rules.rule_safeguard(llm, {"rounds": rounds or [], "current": current,
                                            "max_rounds": max_rounds})


def test_rounds_config():
    print("[1] 轮数默认值与硬上限")
    _assert(train_rules.MAX_TUNING_ROUNDS == 8, "硬上限为 8")
    _assert(train_rules.DEFAULT_TUNING_ROUNDS == 5, "默认值为 5（拍板结论 4）")
    _assert(train_rules.resolve_max_rounds(None) == 5, "未配置时取 5")
    _assert(train_rules.resolve_max_rounds(3) == 3, "配置 3 生效")
    _assert(train_rules.resolve_max_rounds(100) == 8, "配置 100 截断为 8")
    _assert(train_rules.resolve_max_rounds(0) == 1, "配置 0 截断为 1")
    _assert(train_rules.resolve_max_rounds("abc") == 5, "非法配置回落 5")


def test_r1_force_tune():
    print("[2] R1 精度过低强制调优（覆盖 LLM 的 accept）")
    out = _guard({"action": Decision.ACCEPT, "new_params": {}},
                 _round(0, 0.62, 0.55, 2.10))
    _assert(out["action"] == Decision.ADJUST, "LLM 说接受，规则改为继续调优")
    _assert("R1" in out["rule_hits"], "命中 R1")
    _assert("0.55" in out["note"], "说明里给出实际决定系数")
    out = _guard({"action": Decision.ACCEPT}, _round(0, 0.68, 0.60, 1.9))
    _assert("R1" not in out["rule_hits"], "刚好 0.60 不触发 R1")


def test_r2_forbid_tune():
    print("[3] R2 精度足够高禁止继续调优（覆盖 LLM 的 adjust）")
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 500}},
                 _round(0, 0.92, 0.89, 1.05))
    _assert(out["action"] == Decision.ACCEPT, "LLM 想继续调，规则改为接受")
    _assert("R2" in out["rule_hits"], "命中 R2")
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 500}},
                 _round(0, 0.90, 0.87, 1.2))
    _assert("R2" not in out["rule_hits"], "0.87 未达 0.88，不触发 R2")


def test_r3_overfit():
    print("[4] R3 过拟合强制干预调优方向")
    out = _guard({"action": Decision.ACCEPT, "new_params": {}},
                 _round(0, 0.95, 0.70, 1.60,
                        params={"max_depth": 40, "min_samples_leaf": 4}))
    _assert(out["action"] == Decision.ADJUST, "过拟合时强制继续调优")
    _assert("R3" in out["rule_hits"], "命中 R3")
    _assert(out["new_params"]["max_depth"] == 30, "max_depth 由 40 下调 10 到 30")
    _assert(out["new_params"]["min_samples_leaf"] == 6, "min_samples_leaf 由 4 上调到 6")

    out = _guard({"action": Decision.ADJUST, "new_params": {}},
                 _round(0, 0.95, 0.70, 1.6, params={"max_depth": 8}))
    _assert(out["new_params"]["max_depth"] == 5, "max_depth 下调不低于 5")

    out = _guard({"action": Decision.ACCEPT}, _round(0, 0.90, 0.75, 1.4))
    _assert("R3" not in out["rule_hits"], "差距 0.15 未超 0.20，不判过拟合")

    direction = train_rules.overfit_direction({})
    _assert(direction["max_depth"] == 15 and direction["min_samples_leaf"] == 10,
            "缺参数时按默认值给出调优方向")


def test_r4_clamp():
    print("[5] R4 参数越界截断")
    params, clamped = train_rules.clamp_params({
        "n_estimators": 99999, "max_depth": 0, "min_samples_split": 1,
        "min_samples_leaf": 999, "max_features": 5.0,
    })
    _assert(clamped, "检测到越界")
    _assert(params["n_estimators"] == 2000, "决策树数量截断到 2000")
    _assert(params["max_depth"] == 1, "最大深度截断到 1")
    _assert(params["min_samples_split"] == 2, "最小分裂样本数截断到 2")
    _assert(params["min_samples_leaf"] == 50, "叶节点最小样本数截断到 50")
    _assert(abs(params["max_features"] - 1.0) < 1e-9, "最大特征比例截断到 1.0")
    _assert(all(isinstance(params[k], int) for k in
                ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf")),
            "整数型参数保持整数")

    params, clamped = train_rules.clamp_params({"n_estimators": 300, "unknown": "x"})
    _assert(not clamped and params["n_estimators"] == 300, "区间内的参数不变")
    _assert(params["unknown"] == "x", "未声明的参数原样保留（由 Skill 白名单再过滤）")

    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 99999}},
                 _round(0, 0.82, 0.80, 1.3))
    _assert("R4" in out["rule_hits"] and out["new_params"]["n_estimators"] == 2000,
            "safeguard 中同样触发 R4 截断")


def test_r5_converged():
    print("[6] R5 收敛提前停（R²/MAE/RMSE 三指标都无提升才停）")
    # 三指标最近两轮提升都 < 0.01 → 收敛停止
    rounds = [_round(0, 0.85, 0.800, 1.40, mae=2.00),
              _round(1, 0.86, 0.805, 1.392, mae=1.992)]
    current = _round(2, 0.86, 0.808, 1.385, mae=1.985)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.STOP, "三指标提升均 < 0.01 → 强制停止")
    _assert("R5" in out["rule_hits"], "命中 R5")

    # RMSE 提升 0.02 ≥ 0.01 → 不收敛，继续调优
    rounds = [_round(0, 0.85, 0.800, 1.40, mae=2.00),
              _round(1, 0.86, 0.805, 1.38, mae=1.992)]
    current = _round(2, 0.86, 0.808, 1.37, mae=1.985)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.ADJUST, "均方根误差有明显提升 → 继续调优")

    # MAE 提升 0.02 ≥ 0.01 → 不收敛
    rounds = [_round(0, 0.85, 0.800, 1.40, mae=2.00),
              _round(1, 0.86, 0.805, 1.392, mae=1.98)]
    current = _round(2, 0.86, 0.808, 1.385, mae=1.97)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.ADJUST, "平均绝对误差有明显提升 → 继续调优")

    # R² 提升 0.015 ≥ 0.01 → 不收敛
    rounds = [_round(0, 0.85, 0.800, 1.40, mae=2.00),
              _round(1, 0.86, 0.800, 1.392, mae=1.992)]
    current = _round(2, 0.86, 0.815, 1.385, mae=1.985)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.ADJUST, "决定系数有明显提升 → 继续调优")

    # 提升明显（大跨度）→ 继续
    rounds = [_round(0, 0.85, 0.70, 1.8, mae=2.6), _round(1, 0.86, 0.78, 1.5, mae=2.2)]
    current = _round(2, 0.87, 0.84, 1.3, mae=1.9)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.ADJUST, "提升明显时继续调优")
    _assert(not train_rules.converged([_round(0, 0.8, 0.8, 1.0)]), "轮数不足不判收敛")
    _assert(not train_rules.converged(
        [_round(0, 0.8, 0.80, 1.0, mae=1.5), _round(1, 0.8, 0.80, 1.0, mae=1.5)]),
        "两轮数据不足三指标比较，不判收敛")


def test_r6_deteriorating():
    print("[7] R6 走势恶化提前停")
    rounds = [_round(0, 0.88, 0.84, 1.30), _round(1, 0.87, 0.81, 1.45)]
    current = _round(2, 0.86, 0.78, 1.55)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 600}},
                 current, rounds=rounds)
    _assert(out["action"] == Decision.STOP, "两轮连续下降 → 强制停止")
    _assert("R6" in out["rule_hits"], "命中 R6")
    _assert(not train_rules.deteriorating(
        [_round(0, 0.8, 0.80, 1.0), _round(1, 0.8, 0.82, 0.9), _round(2, 0.8, 0.81, 0.95)]),
        "只下降一轮不判恶化")


def test_r7_round_limit():
    print("[8] R7 轮数上限")
    rounds = [_round(i, 0.85, 0.70 + i * 0.03, 1.6 - i * 0.05) for i in range(4)]
    current = _round(4, 0.86, 0.83, 1.35)
    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 current, rounds=rounds, max_rounds=5)
    _assert(out["action"] == Decision.STOP, "第 5 轮达上限 → 强制停止")
    _assert("R7" in out["rule_hits"], "命中 R7")
    _assert("5 轮" in out["note"], "说明里给出生效上限")

    out = _guard({"action": Decision.ADJUST, "new_params": {"n_estimators": 400}},
                 _round(3, 0.86, 0.83, 1.35), rounds=rounds[:3], max_rounds=5)
    _assert(out["action"] == Decision.ADJUST, "未达上限继续调优")


def test_best_round():
    print("[9] 取均方根误差最低的一轮")
    rounds = [_round(0, 0.85, 0.79, 1.51), _round(1, 0.86, 0.84, 1.33),
              _round(2, 0.90, 0.87, 1.23), _round(3, 0.91, 0.86, 1.28)]
    best = train_rules.best_round(rounds)
    _assert(best["round"] == 2, "选中误差最小的第 3 轮（不是决定系数最高之外的轮次）")
    rounds_bad = [{"round": 0, "rmse": None}, {"round": 1, "rmse": "x"}]
    _assert(train_rules.best_round(rounds_bad)["round"] == 1,
            "误差不可比较时退回最后一轮")
    _assert(train_rules.best_round([]) is None, "空轨迹没有最佳轮")


def test_no_new_params_falls_back():
    print("[10] 想继续但没给新参数 → 按接受处理，避免空转")
    out = _guard({"action": Decision.ADJUST, "new_params": {}}, _round(0, 0.85, 0.82, 1.3))
    _assert(out["action"] == Decision.ACCEPT, "无新参数时不空转")
    _assert("没有给出可用的新参数" in out["note"], "说明原因")


def test_llm_missing_or_invalid():
    print("[11] LLM 不可用或给非法动作时的兜底")
    out = _guard(None, _round(0, 0.85, 0.82, 1.3), max_rounds=5)
    _assert(out["action"] == Decision.ACCEPT,
            "LLM 不可用且无新参数 → 接受当前结果，不空转")
    out = _guard({"action": "乱写", "new_params": {"n_estimators": 400}},
                 _round(0, 0.85, 0.82, 1.3))
    _assert(out["action"] == Decision.ADJUST, "非法动作按未达上限时的默认继续处理")
    out = _guard({"action": "乱写"}, _round(4, 0.85, 0.82, 1.3),
                 rounds=[_round(i, 0.85, 0.8, 1.4) for i in range(4)], max_rounds=5)
    _assert(out["action"] == Decision.STOP, "已达上限时非法动作按停止处理")


def test_scripted_loop():
    print("[12] 脚本化指标序列驱动完整循环")

    def run_loop(sequence, max_rounds=5, llm_action=Decision.ADJUST):
        rounds = []
        for i, item in enumerate(sequence):
            train_r2, test_r2, rmse = item[0], item[1], item[2]
            mae = item[3] if len(item) > 3 else None
            current = _round(i, train_r2, test_r2, rmse, mae=mae)
            out = train_rules.rule_safeguard(
                {"action": llm_action, "new_params": {"n_estimators": 200 + i * 50}},
                {"rounds": list(rounds), "current": current, "max_rounds": max_rounds})
            rounds.append(current)
            if out["action"] != Decision.ADJUST:
                return i, out, rounds
        return len(sequence) - 1, out, rounds

    # 收敛：R²/MAE/RMSE 三轮提升都很小 → 第 3 轮停
    stop_at, out, rounds = run_loop([(0.85, 0.800, 1.40, 2.00), (0.86, 0.805, 1.395, 1.995),
                                     (0.86, 0.808, 1.392, 1.992), (0.86, 0.809, 1.390, 1.990)])
    _assert(stop_at == 2 and "R5" in out["rule_hits"], "三指标收敛时在第 3 轮提前停")

    # 恶化：连续下降 → 提前停
    stop_at, out, rounds = run_loop([(0.88, 0.84, 1.30, 2.0), (0.87, 0.81, 1.45, 2.2),
                                     (0.86, 0.78, 1.55, 2.4), (0.85, 0.75, 1.70, 2.6)])
    _assert(stop_at == 2 and "R6" in out["rule_hits"], "恶化时在第 3 轮提前停")
    _assert(train_rules.best_round(rounds)["round"] == 0,
            "最终取误差最低的第 1 轮")

    # 一路上升到轮数上限
    stop_at, out, rounds = run_loop([(0.80, 0.70, 1.80), (0.83, 0.75, 1.65),
                                     (0.86, 0.80, 1.48), (0.88, 0.84, 1.33),
                                     (0.90, 0.87, 1.23)], max_rounds=5)
    _assert(stop_at == 4 and "R7" in out["rule_hits"], "一路上升时跑满 5 轮后停")
    _assert(train_rules.best_round(rounds)["round"] == 4, "最终取误差最低的第 5 轮")

    # 硬上限 8：即使配置 20 也只跑 8 轮
    seq = [(0.80 + i * 0.01, 0.70 + i * 0.02, 1.80 - i * 0.05) for i in range(12)]
    stop_at, out, rounds = run_loop(seq, max_rounds=20)
    _assert(stop_at == 7 and "R7" in out["rule_hits"], "配置 20 轮仍在第 8 轮被硬上限截停")

    # 高精度立刻停（R2）
    stop_at, out, rounds = run_loop([(0.92, 0.89, 1.05)])
    _assert(stop_at == 0 and "R2" in out["rule_hits"], "首轮就达 0.89 时立刻停止调优")


def test_grade():
    print("[13] 指标解读分档（K24 / E02）")
    _assert(train_rules.grade(0.90) == "优秀", "0.90 优秀")
    _assert(train_rules.grade(0.85) == "优秀", "0.85 优秀（含边界）")
    _assert(train_rules.grade(0.82) == "良好", "0.82 良好")
    _assert(train_rules.grade(0.77) == "合格", "0.77 合格")
    _assert(train_rules.grade(0.70) == "偏低", "0.70 偏低")
    _assert(train_rules.grade(None) == "未知", "缺指标时不编造评级")


def test_advisory_notes():
    print("[14] 6.3 额外检查项产出提示")
    notes = train_rules.advisory_notes(
        {"train_samples": 80000, "dem_std": 150.0, "lst_std": 6.0, "ndvi_mean": 0.6},
        {"feature_importance": [{"feature": "TTRI", "importance": 0.005}],
         "test_metrics": {"R2": 0.87},
         "independent_prediction": {"R2": 0.70}})
    joined = "；".join(notes)
    _assert("K20" in joined, "样本量与模型容量匹配（K20）")
    _assert("K21" in joined, "地形复杂度与深度匹配（K21）")
    _assert("K22" in joined, "温度变异与叶节点匹配（K22）")
    _assert("K23" in joined, "植被覆盖与特征比例匹配（K23）")
    _assert("地形热响应指数贡献极低" in joined, "特征重要性异常被提示")
    _assert("空间泛化不够稳定" in joined, "独立预测一致性差被提示")

    notes = train_rules.advisory_notes({"train_samples": 5000, "dem_std": 10.0}, {})
    joined = "；".join(notes)
    _assert("减少决策树数量" in joined, "小样本给出减少树数量的提示")
    _assert("15 到 20" in joined, "平坦地形给出收深度的提示")
    _assert(train_rules.advisory_notes(None, None) == [], "无数据时不编造提示")


if __name__ == "__main__":
    test_rounds_config()
    test_r1_force_tune()
    test_r2_forbid_tune()
    test_r3_overfit()
    test_r4_clamp()
    test_r5_converged()
    test_r6_deteriorating()
    test_r7_round_limit()
    test_best_round()
    test_no_new_params_falls_back()
    test_llm_missing_or_invalid()
    test_scripted_loop()
    test_grade()
    test_advisory_notes()
    print("\n✅ 训练七规则测试全部通过")
