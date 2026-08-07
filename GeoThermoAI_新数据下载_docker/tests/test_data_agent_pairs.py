# -*- coding: utf-8 -*-
"""
数据 Agent 的影像配对评分与选择合成测试（技术方案 11.2）

运行：python tests/test_data_agent_pairs.py
覆盖：
- score_pair 排序正确性（构造 5 组配对，人工核对期望顺序）
- 评分是确定性的（同输入同输出，不调用 LLM）
- 只标记推荐不代选（由我批准模式）
- 完全执行模式自动选最高分
- 缺字段/脏字段不崩
- 无合格配对时返回 no_pair 决策而不是继续
- rank_pairs 不修改入参（不可变约定）
- 推荐字段能透传到前端载荷
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent.orchestrator.approval import Node, Option
from core.agent.orchestrator.exec_mode import ExecMode
from core.agent.orchestrator.hooks import StepDecision
from core.agent.orchestrator.role_hooks import RoleHooks
from core.agent.orchestrator.run_state import RunState
from core.agent.roles.data_agent import (PAIR_WEIGHTS, DataAgent, best_pair,
                                         rank_pairs, score_pair)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _NoLLM:
    """任何 LLM 调用都失败，用于证明评分与选择不依赖 LLM。"""

    def __init__(self):
        self.calls = 0

    def _call_api(self, messages, **kwargs):
        self.calls += 1
        return "API调用失败: 测试禁止调用 LLM"


def _pair(name, l_cloud, s_cloud, l_cov, s_cov, dt, l_cnt=1, s_cnt=1):
    return {
        "landsat_date": name, "landsat_satellite": "L9",
        "landsat_cloud_cover": l_cloud, "landsat_coverage": l_cov, "landsat_count": l_cnt,
        "sentinel2_date": name, "sentinel2_cloud_cover": s_cloud,
        "sentinel2_coverage": s_cov, "sentinel2_count": s_cnt,
        "time_diff_days": dt,
    }


# 5 组配对，人工核对的期望顺序（A 最好 → E 最差）
PAIRS = [
    _pair("C-中等", 25, 20, 90, 92, 1),                    # 云量中等
    _pair("A-最优", 3, 5, 99, 98, 0),                      # 云低、覆盖满、同日
    _pair("E-最差", 70, 65, 55, 60, 2, l_cnt=3, s_cnt=4),  # 云高、覆盖差、时间差最大、拼接多
    _pair("B-次优", 8, 6, 96, 97, 1),                      # 云低、覆盖好、差 1 天
    _pair("D-较差", 45, 40, 75, 78, 2),                    # 云偏高、覆盖一般
]


def test_weights():
    print("[1] 评分权重与技术方案一致")
    _assert(PAIR_WEIGHTS == {"cloud": 0.45, "coverage": 0.30,
                             "time_diff": 0.15, "scene_count": 0.10},
            "权重为 云 0.45 / 覆盖 0.30 / 时间差 0.15 / 景数 0.10")
    _assert(abs(sum(PAIR_WEIGHTS.values()) - 1.0) < 1e-9, "权重之和为 1")


def test_score_ordering():
    print("[2] 排序正确性（人工核对期望顺序）")
    ranked = rank_pairs(PAIRS)
    order = [p["landsat_date"] for p in ranked]
    _assert(order == ["A-最优", "B-次优", "C-中等", "D-较差", "E-最差"],
            f"排序结果为 {' > '.join(order)}")
    scores = [p["quality_score"] for p in ranked]
    _assert(all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
            "得分严格降序")
    _assert(0.0 <= scores[-1] and scores[0] <= 1.0, "得分落在 0 到 1 之间")

    perfect, _ = score_pair(_pair("完美", 0, 0, 100, 100, 0))
    _assert(abs(perfect - 1.0) < 1e-6, "云 0、覆盖 100、同日、单景 → 满分 1.0")
    worst, _ = score_pair(_pair("最烂", 100, 100, 0, 0, 2, l_cnt=20, s_cnt=20))
    _assert(abs(worst) < 1e-6, "各项均最差 → 0 分")


def test_deterministic_no_llm():
    print("[3] 评分确定性且不调用 LLM")
    a = [p["quality_score"] for p in rank_pairs(PAIRS)]
    b = [p["quality_score"] for p in rank_pairs(PAIRS)]
    _assert(a == b, "同输入两次评分结果完全一致（可复现）")

    llm = _NoLLM()
    agent = DataAgent(llm)
    ranked = agent.rank(PAIRS)
    _assert(llm.calls == 0, "打分与排序全程不调用 LLM")
    _assert(ranked[0]["landsat_date"] == "A-最优", "Agent 排序结果与函数一致")


def test_recommend_marking():
    print("[4] 只标记推荐，不改变其它字段")
    ranked = rank_pairs(PAIRS)
    _assert(ranked[0]["recommended"] is True, "最优的一组带推荐标记")
    _assert(all(p["recommended"] is False for p in ranked[1:]), "其余组不带推荐标记")
    _assert(ranked[0]["recommend_reason"], "推荐项带中文理由")
    _assert("云量很低" in ranked[0]["recommend_reason"], "理由里说明云量情况")
    _assert("覆盖完整" in ranked[0]["recommend_reason"], "理由里说明覆盖情况")
    for key in ("landsat_date", "landsat_satellite", "sentinel2_date", "time_diff_days"):
        _assert(key in ranked[0], f"原有字段 {key} 保留（前端旧组件继续可用）")

    from core.agent import presentation
    _assert(presentation.strip_emoji(ranked[0]["recommend_reason"])
            == ranked[0]["recommend_reason"], "推荐理由不含表情符号")


def test_immutability():
    print("[5] 不修改入参（不可变约定）")
    src = [dict(p) for p in PAIRS]
    rank_pairs(src)
    _assert(all("recommended" not in p and "quality_score" not in p for p in src),
            "rank_pairs 返回新对象，不污染入参")
    _assert(len(rank_pairs([])) == 0, "空列表返回空列表")
    _assert(best_pair([]) is None, "空列表没有最优组")


def test_dirty_fields():
    print("[6] 缺字段与脏字段不崩")
    dirty = [
        {},
        {"landsat_cloud_cover": "?", "sentinel2_coverage": None, "time_diff_days": "x"},
        {"landsat_count": "3", "sentinel2_count": 2.0, "landsat_coverage": "96.1"},
    ]
    ranked = rank_pairs(dirty)
    _assert(len(ranked) == 3, "脏数据全部完成评分")
    _assert(all(isinstance(p["quality_score"], float) for p in ranked), "得分均为浮点数")
    _assert(ranked[0]["quality_score"] >= ranked[-1]["quality_score"], "仍能排序")


class _Ctx:
    """最小 ExecContext 替身。"""

    def __init__(self):
        self.emitted = []
        self.exp_state = {}
        self.project_dir = ""
        self.raw_dir = ""
        self.processed_dir = ""

    def emit(self, text, to_log=False):
        self.emitted.append(text)


def test_approval_mode_does_not_autoselect():
    print("[7] 由我批准模式：只标记推荐，不代选")
    hooks = RoleHooks(exec_mode=ExecMode.APPROVAL, run_state=RunState(),
                      data_agent=DataAgent(_NoLLM()))
    ctx = _Ctx()
    ranked = hooks.rank_pairs(PAIRS, ctx)
    _assert(ranked[0]["recommended"] is True, "最优组带推荐标记")
    _assert(hooks.select_pair(ranked, ctx) is None,
            "由我批准模式不代选，交回配对卡片让用户决定")
    _assert(ctx.exp_state["pair_candidates"][0]["recommended"] is True,
            "候选配对与得分并入实验记录草稿")
    _assert(len(ctx.exp_state["pair_candidates"]) == 5, "记录全部候选（上限内）")


def test_auto_mode_selects_best():
    print("[8] 完全执行模式：自动选最高分并说明理由")
    hooks = RoleHooks(exec_mode=ExecMode.AUTO, run_state=RunState(exec_mode="auto"),
                      data_agent=DataAgent(_NoLLM()))
    ctx = _Ctx()
    ranked = hooks.rank_pairs(PAIRS, ctx)
    chosen = hooks.select_pair(ranked, ctx)
    _assert(chosen is not None and chosen["landsat_date"] == "A-最优",
            "自动选择质量得分最高的一组")
    joined = "".join(ctx.emitted)
    _assert("已自动选择第 1 组" in joined, "气泡里说明自动选了第几组")
    _assert("云量很低" in joined, "气泡里给出选择理由")


def test_no_pair_never_hard_runs():
    print("[9] 无合格配对时两种模式都不硬跑")
    detail = {"landsat_count": 4, "sentinel_count": 6, "cloud_threshold": 30}

    # 完全执行 + 还有 replan 预算 → 交规划 Agent replan
    state = RunState(exec_mode="auto", replan_max=3)
    hooks = RoleHooks(exec_mode=ExecMode.AUTO, run_state=state,
                      data_agent=DataAgent(_NoLLM()))
    ctx = _Ctx()
    decision = hooks.on_no_pair(detail, ctx)
    _assert(decision.action == StepDecision.REPLAN, "完全执行模式带原因交 replan")
    _assert(hooks.replan_request is not None, "replan 请求被记录，由总调度发起")
    _assert(state.replan_count == 1, "replan 计数 +1")
    joined = "".join(ctx.emitted)
    _assert("陆地卫星 4 景" in joined and "哨兵二号 6 景" in joined,
            "气泡说清搜到了什么，不只说「未找到影像配对」")
    _assert("云量阈值" in joined, "气泡说明当前云量阈值")

    # 完全执行 + replan 预算耗尽 → 弹窗问用户（不硬跑）
    state = RunState(exec_mode="auto", replan_max=0)
    asked = {}

    def pause_cb(payload):
        asked["payload"] = payload
        return {"paused": False, "data": {"option_id": Option.STOP, "values": {}}}

    hooks = RoleHooks(exec_mode=ExecMode.AUTO, run_state=state, pause_callback=pause_cb,
                      data_agent=DataAgent(_NoLLM()))
    decision = hooks.on_no_pair(detail, _Ctx())
    _assert(asked["payload"]["node"] == Node.NO_PAIR, "预算耗尽后弹 no_pair 审批节点")
    _assert(decision.action == StepDecision.ABORT, "用户选停止 → 中止而不是硬跑")

    # 由我批准 → 直接弹窗，用户选放宽云量
    def pause_relax(payload):
        asked["payload"] = payload
        return {"paused": False, "data": {"option_id": Option.RELAX_CLOUD, "values": {}}}

    state = RunState(replan_max=3)
    hooks = RoleHooks(exec_mode=ExecMode.APPROVAL, run_state=state,
                      pause_callback=pause_relax, data_agent=DataAgent(_NoLLM()))
    decision = hooks.on_no_pair(detail, _Ctx())
    _assert(decision.action == StepDecision.REPLAN, "用户选放宽云量 → 交 replan")
    _assert(hooks.replan_request["payload"]["relax_cloud"] is True,
            "replan 请求带上「放宽云量」的针对性调整")
    options = [o["id"] for o in asked["payload"]["options"]]
    _assert(Option.RELAX_CLOUD in options and Option.WIDEN_TIME in options
            and Option.CHANGE_SOURCE in options and Option.STOP in options,
            "no_pair 节点给出放宽云量/扩大时间/换数据源/停止等选项")

    # 无 pause_callback（无法询问）→ 挂起，绝不硬跑
    hooks = RoleHooks(exec_mode=ExecMode.APPROVAL, run_state=RunState(replan_max=0),
                      data_agent=DataAgent(_NoLLM()))
    decision = hooks.on_no_pair(detail, _Ctx())
    _assert(decision.action == StepDecision.PAUSE, "无法询问用户时挂起，不替用户决定")


def test_pair_payload_carries_recommendation():
    print("[10] 推荐字段透传到前端载荷")
    from core.agent.geo_thermo_agent import GeoThermoAgent
    from core.ai_assistant import GeoThermoAI_Assistant
    from core.skills.skill_registry import SkillRegistry

    agent = GeoThermoAgent(GeoThermoAI_Assistant("", "", "", "", "openai"), SkillRegistry())
    ranked = rank_pairs(PAIRS)
    captured = {}

    def pause_cb(payload):
        captured["payload"] = payload
        return {"paused": False, "data": {"landsat_date": "A-最优", "sentinel_date": "A-最优"}}

    selected = agent._ask_user_to_select_pair(ranked, pause_cb, lambda *a, **k: None,
                                              return_selected=True)
    pairs_info = captured["payload"]["pairs"]
    _assert(captured["payload"]["type"] == "select_pair", "沿用旧的 select_pair 载荷类型")
    _assert(pairs_info[0]["recommended"] is True, "推荐标记透传到前端")
    _assert(pairs_info[0]["recommend_reason"], "推荐理由透传到前端")
    _assert("quality_score" in pairs_info[0], "质量得分透传到前端")
    _assert("recommended" not in pairs_info[1], "非推荐项不带该字段（前端旧逻辑不受影响）")
    _assert(selected["landsat_date"] == "A-最优", "按用户选择回填配对")

    # 未打分的配对（旧路径）不应出现新字段
    captured.clear()
    agent._ask_user_to_select_pair(PAIRS, pause_cb, lambda *a, **k: None,
                                  return_selected=True)
    _assert(all("recommended" not in p for p in captured["payload"]["pairs"]),
            "旧路径未打分时载荷形状与改造前一致")


if __name__ == "__main__":
    test_weights()
    test_score_ordering()
    test_deterministic_no_llm()
    test_recommend_marking()
    test_immutability()
    test_dirty_fields()
    test_approval_mode_does_not_autoselect()
    test_auto_mode_selects_best()
    test_no_pair_never_hard_runs()
    test_pair_payload_carries_recommendation()
    print("\n✅ 数据 Agent 配对评分与选择测试全部通过")
