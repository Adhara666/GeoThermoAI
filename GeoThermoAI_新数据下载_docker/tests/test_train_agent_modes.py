# -*- coding: utf-8 -*-
"""
训练 Agent 两种模式合成测试

运行：python tests/test_train_agent_modes.py
覆盖：
- 由我批准模式下高精度也弹窗（用户明确要求）
- tuning_decision 五个选项各自映射到正确的 StepDecision
- 每轮调优都弹 tuning_round
- 完全执行模式不弹窗，按七规则自动跑
- defer_cleanup 在非最终轮为 True；接受结果后显式清理一次
- 每轮输出到 results/tuning/round_{i}
- 最终 results/train 下最新的模型文件就是被接受的那一轮
- project_root 能从 results/tuning/round_N 正确反推
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core import manifest as run_manifest
from core.agent.orchestrator.approval import Node, Option
from core.agent.orchestrator.exec_mode import ExecMode
from core.agent.orchestrator.hooks import StepDecision
from core.agent.orchestrator.run_state import RunState
from core.agent.roles.train_agent import TrainAgent
from core.skills.base_skill import SkillResult
from core.skills.skill_registry import SkillRegistry
from core.skills.builtin.rf_model import RFModelSkill


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _NoLLM:
    """LLM 不可用，全部走规则兜底（证明规则可独立工作）。"""

    def _call_api(self, messages, **kwargs):
        return "API调用失败: 测试不接 LLM"


class _Ctx:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.results_dir = (project_dir + "/results").replace("\\", "/")
        self.raw_dir = project_dir + "/raw"
        self.processed_dir = project_dir + "/processed"
        self.emitted = []
        self.exp_state = {}
        self.data_features = {"train_samples": 45678, "dem_std": 120.0,
                              "ndvi_mean": 0.35, "lst_std": 4.2}

    def emit(self, text, to_log=False):
        self.emitted.append(text)

    def text(self):
        return "".join(self.emitted)


class _Hooks:
    """最小 RoleHooks 替身：记录审批载荷、按脚本返回用户选择。"""

    def __init__(self, exec_mode, choices=None, run_state=None):
        self.exec_mode = exec_mode
        self.run_state = run_state or RunState(exec_mode=exec_mode)
        self.choices = list(choices or [])
        self.payloads = []
        self.replan_request = None

    def _ask(self, payload):
        self.payloads.append(payload)
        if not self.choices:
            return None
        choice = self.choices.pop(0)
        return choice

    def _request_replan(self, reason, payload=None):
        self.replan_request = {"reason": reason, "payload": dict(payload or {})}
        return StepDecision.replan(reason=reason, payload=payload)


def _result(train_r2, test_r2, rmse, params=None):
    return SkillResult(True, "模型训练完成", data={
        "train_metrics": {"train": {"R2": train_r2}},
        "test_metrics": {"R2": test_r2, "RMSE": rmse, "MB": 0.1},
        "feature_importance": [{"feature": "NDVI", "importance": 0.28},
                               {"feature": "TTRI", "importance": 0.20}],
        "params": params or {"n_estimators": 200, "max_depth": 25, "min_samples_leaf": 8},
        "model_path": "",
    })


def _registry():
    reg = SkillRegistry()
    reg.register(RFModelSkill())
    return reg


def _agent(max_rounds=5):
    return TrainAgent(_NoLLM(), _registry(), max_rounds=max_rounds)


def test_before_train_sets_round_dir():
    print("[1] 首轮输出目录与延迟清理")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        step = {"skill": "rf_model", "params": {"output_dir": ctx.results_dir,
                                               "n_estimators": 200}}
        _agent().before_train(step, ctx)
        _assert(step["params"]["output_dir"].endswith("/results/tuning/round_0"),
                "第 1 轮输出到 results/tuning/round_0")
        _assert(step["params"]["defer_cleanup"] is True,
                "调优期传 defer_cleanup=True，避免每轮重建划分数据")
        _assert(step["params"]["n_estimators"] == 200, "原有超参不被覆盖")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approval_asks_even_when_accurate():
    print("[2] 由我批准模式：高精度也弹窗")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.ACCEPT, "values": {}}])
        agent = _agent()
        decision = agent.on_trained(_result(0.95, 0.93, 0.90), ctx, hooks=hooks)
        _assert(len(hooks.payloads) == 1, "即使决定系数 0.93 也弹了审批窗")
        _assert(hooks.payloads[0]["node"] == Node.TUNING_DECISION, "弹的是 tuning_decision")
        _assert("0.93" in hooks.payloads[0]["summary"], "报告本轮精度")
        _assert("优秀" in hooks.payloads[0]["summary"], "报告评级")
        _assert(decision.action == StepDecision.CONTINUE, "用户选接受 → 继续下一步")
        options = [o["id"] for o in hooks.payloads[0]["options"]]
        _assert(options == [Option.AI_TUNE, Option.MANUAL_TUNE, Option.ACCEPT,
                            Option.RESELECT_PAIR, Option.REPLAN],
                "五个选项齐全且顺序一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_five_options_mapping():
    print("[3] tuning_decision 五个选项映射到正确的决策")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        # ① AI 调优 → RETRY（规则给出方向）
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.AI_TUNE},
                                {"option_id": Option.NEXT_ROUND}])
        decision = _agent().on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.RETRY, "AI 调优 → 重跑本步")
        _assert(decision.new_params.get("output_dir", "").endswith("round_1"),
                "下一轮输出到 round_1")
        _assert(decision.new_params["defer_cleanup"] is True, "非最终轮仍延迟清理")

        # ② 我自己设置参数 → RETRY 用用户值
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.MANUAL_TUNE,
                                 "values": {"n_estimators": 400, "max_depth": 30}}])
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.RETRY, "手动调参 → 重跑本步")
        _assert(decision.new_params["n_estimators"] == 400
                and decision.new_params["max_depth"] == 30, "用用户设定的参数")
        _assert("按你设置的参数" in ctx.text(), "气泡说明用了用户参数")

        # 手动调参的越界值在后端被截断
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.MANUAL_TUNE,
                                 "values": {"n_estimators": 99999}}])
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.new_params["n_estimators"] == 2000, "手动参数越界被截断")

        # ③ 不调优 → CONTINUE
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[{"option_id": Option.ACCEPT}])
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.CONTINUE, "不调优 → 直接进入评估阶段")

        # ④ 重新选择影像组合 → REPLAN（阶段内回退）
        ctx = _Ctx(tmp)
        state = RunState(exec_mode=ExecMode.APPROVAL)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[{"option_id": Option.RESELECT_PAIR}],
                       run_state=state)
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.REPLAN, "重选影像组合 → 交回总调度")
        _assert(state.resume_point == Node.PAIR_SELECTION, "断点记为影像组合选择")

        # ⑤ 换时间或地区 → REPLAN
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[{"option_id": Option.REPLAN}])
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.REPLAN, "换时间地区 → replan")
        _assert("更换时间或地区" in hooks.replan_request["reason"], "replan 原因明确")

        # 挂起：用户没响应
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[])
        decision = _agent().on_trained(_result(0.90, 0.85, 1.20), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.PAUSE, "用户未响应 → 挂起，不替用户决定")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tuning_round_asks_every_round():
    print("[4] 由我批准模式：每轮调优都询问")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.AI_TUNE},
                                {"option_id": Option.NEXT_ROUND},
                                {"option_id": Option.NEXT_ROUND}])
        agent = _agent()
        agent.on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)   # 第 1 轮
        decision = agent.on_trained(_result(0.70, 0.64, 1.80), ctx, hooks=hooks)  # 第 2 轮
        nodes = [p["node"] for p in hooks.payloads]
        _assert(nodes == [Node.TUNING_DECISION, Node.TUNING_ROUND],
                "第 1 轮弹 tuning_decision，第 2 轮弹 tuning_round")
        _assert("0.64" in hooks.payloads[1]["summary"], "每轮都报告本轮指标")
        _assert(decision.action == StepDecision.RETRY, "用户选继续 → 再训一轮")

        # 用户选停止调优 → 取最好的一轮
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.AI_TUNE},
                                {"option_id": Option.STOP_TUNING}])
        agent = _agent()
        agent.on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)
        decision = agent.on_trained(_result(0.70, 0.64, 1.80), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.CONTINUE, "停止调优 → 继续下一步")
        _assert("采用调优第 1 轮（总第 2 轮）的结果" in ctx.text(),
                "取误差最小的一轮（调优第 1 轮，总第 2 轮）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tuning_round_manual_option():
    print("[4.1] tuning_round 弹窗也支持「我自己设置参数」且生效")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL,
                       choices=[{"option_id": Option.AI_TUNE},
                                {"option_id": Option.MANUAL_TUNE,
                                 "values": {"max_depth": 12, "min_samples_leaf": 20}}])
        agent = _agent()
        agent.on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)   # 第 1 轮
        decision = agent.on_trained(_result(0.70, 0.64, 1.80), ctx, hooks=hooks)  # 第 2 轮
        _assert(Option.MANUAL_TUNE in [o["id"] for o in hooks.payloads[1]["options"]],
                "tuning_round 弹窗包含「我自己设置参数」")
        _assert(decision.action == StepDecision.RETRY, "tuning_round 选手动调参 → 重跑")
        _assert(decision.new_params["max_depth"] == 12
                and decision.new_params["min_samples_leaf"] == 20,
                "使用用户在弹窗表单里填的参数")
        _assert("按你设置的参数" in ctx.text(), "气泡说明用了用户参数")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ai_rounds_manual_reset():
    print("[4.2] 手动调优打断 AI 连续计数，AI 上限从重新介入后重算")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[
            {"option_id": Option.AI_TUNE},                          # 第 1 轮后：AI 调优
            {"option_id": Option.MANUAL_TUNE, "values": {"max_depth": 12}},  # 第 2 轮后：手动
            {"option_id": Option.NEXT_ROUND},                       # 第 3 轮后：继续（AI）
            {"option_id": Option.NEXT_ROUND},                       # 第 4 轮后：继续（AI）
            {"option_id": Option.ACCEPT},                           # 第 5 轮后：接受
        ])
        agent = _agent(max_rounds=2)
        agent.on_trained(_result(0.60, 0.55, 2.10), ctx, hooks=hooks)  # round0 初始
        agent.on_trained(_result(0.65, 0.60, 1.90), ctx, hooks=hooks)  # round1 AI#1
        agent.on_trained(_result(0.68, 0.63, 1.80), ctx, hooks=hooks)  # round2 手动
        agent.on_trained(_result(0.72, 0.66, 1.70), ctx, hooks=hooks)  # round3 AI#1（重算后）
        decision = agent.on_trained(_result(0.75, 0.70, 1.60), ctx, hooks=hooks)  # round4 AI#2
        _assert(decision.action == StepDecision.CONTINUE,
                "手动轮打断后 AI 重新计数，连续 2 轮 AI（round3/round4）后停止")
        _assert("采用调优第 4 轮（总第 5 轮）的结果" in ctx.text(),
                "取误差最小的调优第 4 轮（总第 5 轮）")
        _assert("已达上限" in ctx.text(), "R7 提示 AI 连续调优已达上限")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_six_rounds_popup_note():
    print("[4.3] 总训练达 6 轮（初始 1 + 调优 5）后，弹窗提示继续调优收益可能不显著")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.APPROVAL, choices=[
            {"option_id": Option.AI_TUNE},                            # 第 1 轮后：AI 调优
            {"option_id": Option.NEXT_ROUND},                         # 第 2 轮后
            {"option_id": Option.NEXT_ROUND},                         # 第 3 轮后
            {"option_id": Option.NEXT_ROUND},                         # 第 4 轮后
            {"option_id": Option.NEXT_ROUND},                         # 第 5 轮后
            {"option_id": Option.ACCEPT},                             # 第 6 轮后：接受
        ])
        agent = TrainAgent(_ScriptedLLM(), _registry(), max_rounds=5)
        for metrics in ((0.70, 0.60, 2.00), (0.72, 0.63, 1.90), (0.74, 0.66, 1.80),
                        (0.76, 0.69, 1.70), (0.78, 0.72, 1.60), (0.80, 0.75, 1.50)):
            agent.on_trained(_result(*metrics), ctx, hooks=hooks)
        _assert(len(hooks.payloads) == 6, "tuning_decision ×1 + tuning_round ×5")
        round6 = hooks.payloads[5]
        _assert(round6["node"] == Node.TUNING_ROUND, "第 6 轮仍弹 tuning_round")
        _assert("当前训练轮数已达 6 轮（调优轮数已达 5 轮），继续调优的收益可能不显著"
                in round6["summary"], "第 6 轮弹窗带「已达 6 轮」提示")
        _assert("已达 6 轮" not in hooks.payloads[1]["summary"],
                "第 2 轮（总轮数 2）不提前出现该提示")
        _assert(Option.MANUAL_TUNE in [o["id"] for o in round6["options"]],
                "第 6 轮弹窗仍保留「我自己设置参数」")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _ScriptedLLM:
    """每次调用都返回一份「继续调优」的决策，模拟可用的大模型。"""

    def __init__(self):
        self.calls = 0

    def _call_api(self, messages, **kwargs):
        self.calls += 1
        return ('{"action": "adjust", "reason": "加大模型容量", '
                '"new_params": {"n_estimators": %d}}' % (200 + self.calls * 100))


def test_auto_mode_no_prompt():
    print("[5] 完全执行模式：不弹窗，按七规则自动跑")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        state = RunState(exec_mode="auto")
        hooks = _Hooks(ExecMode.AUTO, run_state=state)
        llm = _ScriptedLLM()
        agent = TrainAgent(llm, _registry(), max_rounds=2)

        d1 = agent.on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)
        _assert(hooks.payloads == [], "完全执行模式一次也不弹窗")
        _assert(d1.action == StepDecision.RETRY, "精度过低 → 自动继续调优")
        _assert(d1.new_params["n_estimators"] == 300, "采用大模型给出的新参数")
        _assert("开始调优训练（总第2轮，调优第1轮）" in ctx.text(),
                "调优公告写明总轮数与调优序号（初始轮不算调优轮）")
        _assert("  - 开始调优训练" in ctx.text(),
                "调优公告用项目符号，与「- 模型训练（第N轮）完成：…」摘要对齐")

        d2 = agent.on_trained(_result(0.70, 0.64, 1.80), ctx, hooks=hooks)
        _assert(d2.action == StepDecision.RETRY, "AI 连续 1 轮，仍在调优（上限 2 不含初始轮）")
        _assert("开始调优训练（总第3轮，调优第2轮）" in ctx.text(),
                "第二轮调优公告序号顺延（总第3轮，调优第2轮）")
        d3 = agent.on_trained(_result(0.88, 0.86, 1.25), ctx, hooks=hooks)
        _assert(d3.action == StepDecision.CONTINUE, "AI 连续调优达上限（R7）→ 停止并继续下一步")
        _assert("已达上限" in ctx.text(), "气泡说明调优已达上限（不带 [规则] 编号）")
        _assert("采用调优第 2 轮（总第 3 轮）的结果" in ctx.text(),
                "取误差最小的调优第 2 轮（总第 3 轮）")
        _assert(len(ctx.exp_state["tuning_trace"]) == 3, "调优轨迹写入实验记录草稿")
        _assert(ctx.exp_state["final_params"], "最终生效参数写入实验记录草稿")
        _assert(state.tuning_rounds == 2, "流程状态记录了调优轮次")

        # 高精度首轮直接停（R2 覆盖大模型的 adjust）
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.AUTO)
        decision = TrainAgent(_ScriptedLLM(), _registry()).on_trained(
            _result(0.92, 0.90, 0.95), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.CONTINUE, "首轮已达 0.90（R2）→ 直接继续")
        _assert("不再继续调优" in ctx.text(), "气泡说明精度已达标（不带 [规则] 编号）")

        # 大模型不可用：给不出调优方向时不空转，说明原因后继续下一步
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.AUTO)
        decision = _agent().on_trained(_result(0.86, 0.82, 1.30), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.CONTINUE,
                "大模型不可用且无规则方向 → 按当前结果继续，不用同一组参数空转")
        _assert("没有给出可用的新参数" in ctx.text(), "气泡说清为什么没有继续调优")

        # 大模型不可用但精度过低：R1 仍给出确定性方向，继续调优
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.AUTO)
        decision = _agent().on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)
        _assert(decision.action == StepDecision.RETRY,
                "精度过低时 R1 的兜底方向让调优继续进行")
        _assert("偏低" in ctx.text() or "低于" in ctx.text(),
                "气泡说明精度偏低（不带 [规则] 编号）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_round_dirs_and_promotion():
    print("[6] 分轮目录与最佳轮产物提升")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        # 构造两轮的真实产物目录
        for i, name in ((0, "rf_ttri_model_r0.pkl"), (1, "rf_ttri_model_r1.pkl")):
            d = os.path.join(tmp, "results", "tuning", f"round_{i}", "train")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"model" + str(i).encode())
            time.sleep(0.01)

        state = RunState(exec_mode="auto")
        hooks = _Hooks(ExecMode.AUTO, run_state=state)
        agent = TrainAgent(_ScriptedLLM(), _registry(), max_rounds=1)
        d1 = agent.on_trained(_result(0.70, 0.62, 1.90), ctx, hooks=hooks)
        _assert(d1.new_params["output_dir"].endswith("round_1"), "第 2 轮写入 round_1")
        # 第 2 轮误差更小 → 应被采纳（AI 连续调优达上限 1 → 第 2 轮完成即采纳）
        agent.on_trained(_result(0.88, 0.86, 1.20), ctx, hooks=hooks)

        promoted = os.path.join(tmp, "results", "train")
        _assert(os.path.isdir(promoted), "最佳轮的 train 目录被复制到 results/train")
        pkls = sorted(os.listdir(promoted))
        _assert("rf_ttri_model_r1.pkl" in pkls, "被接受的第 2 轮模型进入 results/train")
        # 磁盘瘦身：非最佳轮目录整个删除；最佳轮目录保留轻量记录但 pkl 副本已删
        _assert(not os.path.isdir(os.path.join(tmp, "results", "tuning", "round_0")),
                "非最佳轮（round_0）目录已删除，不保留冗余 pkl")
        r1_dir = os.path.join(tmp, "results", "tuning", "round_1", "train")
        if os.path.isdir(r1_dir):
            _assert(not [f for f in os.listdir(r1_dir) if f.lower().endswith(".pkl")],
                    "最佳轮在 tuning 里的 pkl 副本已删除（模型只在 results/train 一份）")
        _assert(len(agent.rounds) == 2 and agent.rounds[0]["test_r2"] == 0.62
                and agent.rounds[1]["test_r2"] == 0.86,
                "每一轮调优的指标仍记录在内存（tuning_trace 照常写入记忆），不因删文件丢失")

        # 下游按「results/train 下最新 pkl」推断，必须取到被接受的那一轮
        newest = max((os.path.join(promoted, n) for n in os.listdir(promoted)),
                     key=os.path.getmtime)
        _assert(os.path.basename(newest) == "rf_ttri_model_r1.pkl",
                "results/train 下最新的模型就是被接受的那一轮（下游不会取错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_project_root_from_round_dir():
    print("[7] 项目根目录能从分轮目录反推")
    cases = {
        "/p/results": "/p",
        "/p/raw": "/p",
        "/p/processed": "/p",
        "/p/results/tuning/round_0": "/p",
        "/p/results/tuning/round_12": "/p",
    }
    for path, expected in cases.items():
        got = run_manifest.project_root_from_stage_output_dir(path).replace("\\", "/")
        _assert(got == expected, f"{path} → {expected}")
    _assert(run_manifest.project_root_from_stage_output_dir("") == "", "空路径原样返回")
    custom = run_manifest.project_root_from_stage_output_dir("/tmp/custom_out")
    _assert(custom.replace("\\", "/") == "/tmp/custom_out",
            "自定义目录（无固定子目录名）原样返回")


def test_defer_cleanup_respected():
    print("[8] defer_cleanup 参数被 rf_model 尊重")
    import inspect

    source = inspect.getsource(RFModelSkill.execute)
    _assert('params.get("defer_cleanup")' in source,
            "rf_model 读取 defer_cleanup 决定是否清理")
    _assert("cleanup_stage(project_root" in source, "未传该参数时仍执行原有清理")

    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        agent = _agent(max_rounds=3)
        hooks = _Hooks(ExecMode.AUTO)
        step = {"skill": "rf_model", "params": {"output_dir": ctx.results_dir}}
        agent.before_train(step, ctx, hooks=hooks)
        _assert(step["params"]["defer_cleanup"] is True, "第 1 轮 defer_cleanup 为 True")
        d = agent.on_trained(_result(0.62, 0.55, 2.10), ctx, hooks=hooks)
        _assert(d.new_params["defer_cleanup"] is True, "中间轮 defer_cleanup 仍为 True")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_replan_from_train_itself():
    print("[9] 训练 Agent 只做阶段内优化，不擅自 replan")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        hooks = _Hooks(ExecMode.AUTO)
        agent = _agent(max_rounds=3)
        for metrics in ((0.62, 0.55, 2.10), (0.70, 0.64, 1.80), (0.88, 0.86, 1.25)):
            agent.on_trained(_result(*metrics), ctx, hooks=hooks)
        _assert(hooks.replan_request is None,
                "自动调优全程不发起 replan（改超参属于阶段内优化）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approval_mode_keeps_only_best_pkl():
    print("[10] 由我批准模式：调优结束后也只保留最佳轮 pkl，其他轮次全部删除")
    tmp = tempfile.mkdtemp(prefix="train_agent_")
    try:
        ctx = _Ctx(tmp)
        # 构造两轮的真实产物目录
        for i, name in ((0, "rf_ttri_model_r0.pkl"), (1, "rf_ttri_model_r1.pkl")):
            d = os.path.join(tmp, "results", "tuning", f"round_{i}", "train")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"model" + str(i).encode())
            time.sleep(0.01)

        hooks = _Hooks(ExecMode.APPROVAL, choices=[
            {"option_id": Option.AI_TUNE},
            {"option_id": Option.ACCEPT},
        ])
        agent = TrainAgent(_ScriptedLLM(), _registry(), max_rounds=3)
        agent.on_trained(_result(0.70, 0.62, 1.90), ctx, hooks=hooks)   # 第 1 轮
        agent.on_trained(_result(0.88, 0.86, 1.20), ctx, hooks=hooks)   # 第 2 轮（最佳）

        promoted = os.path.join(tmp, "results", "train")
        _assert(os.path.isdir(promoted), "最佳轮模型提升到 results/train")
        _assert("rf_ttri_model_r1.pkl" in os.listdir(promoted),
                "被接受的第 2 轮模型进入 results/train")
        _assert(not os.path.isdir(os.path.join(tmp, "results", "tuning", "round_0")),
                "非最佳轮（round_0）目录已删除，不保留冗余 pkl")
        r1_dir = os.path.join(tmp, "results", "tuning", "round_1", "train")
        _assert(not [f for f in os.listdir(r1_dir) if f.lower().endswith(".pkl")],
                "最佳轮在 tuning 里的 pkl 副本已删除（模型只在 results/train 一份）")
        pkl_count = sum(len([f for f in files if f.lower().endswith(".pkl")])
                        for _r, _d, files in os.walk(os.path.join(tmp, "results")))
        _assert(pkl_count == 1, f"由我批准模式全项目只剩 1 份 pkl（实际 {pkl_count}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_before_train_sets_round_dir()
    test_approval_asks_even_when_accurate()
    test_five_options_mapping()
    test_tuning_round_asks_every_round()
    test_tuning_round_manual_option()
    test_ai_rounds_manual_reset()
    test_six_rounds_popup_note()
    test_auto_mode_no_prompt()
    test_round_dirs_and_promotion()
    test_project_root_from_round_dir()
    test_defer_cleanup_respected()
    test_no_replan_from_train_itself()
    test_approval_mode_keeps_only_best_pkl()
    print("\n✅ 训练 Agent 两种模式测试全部通过")
