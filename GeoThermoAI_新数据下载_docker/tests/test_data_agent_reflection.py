# -*- coding: utf-8 -*-
"""
数据轻反思 D1–D7 合成测试（技术方案 11.2）

运行：python tests/test_data_agent_reflection.py
覆盖：
- D1–D7 逐条判定
- 全部通过时放行
- 任一不通过时执行引擎收到 REPLAN/PAUSE/ABORT 而不是 CONTINUE
- LLM 只做翻译，不会把「不通过」改成「通过」
- 数据阶段任一步失败时立刻拦截，不进入训练
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent.orchestrator.approval import Node, Option
from core.agent.orchestrator.exec_mode import ExecMode
from core.agent.orchestrator.hooks import StepDecision
from core.agent.orchestrator.role_hooks import RoleHooks
from core.agent.orchestrator.run_state import RunState
from core.agent.reflection import data_rules
from core.agent.reflection.result import Action
from core.agent.roles.data_agent import DataAgent
from core.skills.base_skill import SkillResult


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _NoLLM:
    def _call_api(self, messages, **kwargs):
        return "API调用失败: 测试不接 LLM"


class _FakeLLM:
    """返回一份想把结论改成「通过」的恶意响应，用于验证规则不可被 LLM 覆盖。"""

    def __init__(self, payload):
        self.payload = payload

    def _call_api(self, messages, **kwargs):
        return self.payload


GOOD_PIPELINE = {
    # train_rows 是 30m_features_step2.csv 的 step=2 抽样行数，D3 用它检查样本量；
    # constraint_rows 是完整 30 米约束层（step=1）的有效像元数，D4 与 D7 用它检查
    # 约束层是否有效、有效像元占比是否达标——两者抽样粒度不同，不能混用（v1.2 修复）。
    "train_rows": 45678,
    "constraint_rows": 45678,
    "predict_valid_pixels": 400000,
    "split_stats": {"train": {"count": 27406}, "validate": {"count": 9136},
                    "test": {"count": 9136}},
}


def _ok_raster(path):
    return True, ""


def _ok_csv(path):
    return {"exists": True, "has_ttri": True, "ttri_std": 1.5, "rows": 1000}


def _ok_meta(path):
    return {"height": 300, "width": 400}     # 120000 格，constraint_rows/格数 ≈ 38%


def _check(**overrides):
    kwargs = dict(raw_dir="raw", processed_dir="processed", pipeline_data=GOOD_PIPELINE,
                  manifest={"stages": {s: {"status": "completed"}
                                       for s in data_rules.REQUIRED_STAGES}},
                  raster_probe=_ok_raster, csv_probe=_ok_csv, meta_probe=_ok_meta)
    kwargs.update(overrides)
    return data_rules.check(**kwargs)


def test_all_pass():
    print("[1] 全部通过时放行")
    res = _check()
    _assert(res.ok and res.action == Action.PROCEED, "D1–D7 全过 → 放行")
    _assert(res.rule_hits == [] and res.violations == [], "无规则命中、无违规项")


def test_d1_rasters():
    print("[2] D1 五个栅格文件")
    def missing_dem(path):
        return (False, "文件不存在") if path.endswith("dem.tif") else (True, "")

    res = _check(raster_probe=missing_dem)
    _assert(not res.ok and "D1" in res.rule_hits, "DEM 缺失 → D1 不通过")
    _assert(any("DEM" in v for v in res.violations), "违规项用中文点名是哪个文件")
    _assert("换一组影像组合重新下载" in res.suggestions, "给出换配对的建议")

    def empty_all(path):
        return False, "文件为空"

    res = _check(raster_probe=empty_all)
    _assert(len(res.violations) == 5, "五个栅格都为空时逐个列出")

    # 默认探针：真实文件系统行为
    tmp = tempfile.mkdtemp(prefix="data_rules_")
    try:
        p = os.path.join(tmp, "landsat_lst.tif")
        ok, reason = data_rules.default_raster_probe(p)
        _assert(not ok and reason == "文件不存在", "默认探针识别缺失文件")
        open(p, "wb").close()
        ok, reason = data_rules.default_raster_probe(p)
        _assert(not ok and reason == "文件为空", "默认探针识别零字节文件")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_d2_manifest():
    print("[3] D2 阶段清单")
    res = _check(manifest={"stages": {"data_acquisition": {"status": "completed"},
                                      "data_pipeline": {"status": "failed"},
                                      "ttri_compute": {"status": "skipped_upstream"}}})
    _assert(not res.ok and "D2" in res.rule_hits, "阶段失败 → D2 不通过")
    _assert(any("数据预处理阶段状态为失败" in v for v in res.violations),
            "违规项中文说明失败阶段")
    _assert(any("因上游失败被跳过" in v for v in res.violations),
            "下游被跳过的状态也中文化")

    res = _check(manifest=None)
    _assert(res.ok, "没有阶段清单时不做 D2 判定（旧项目目录与合成测试）")


def test_d3_train_rows():
    print("[4] D3 训练样本数")
    res = _check(pipeline_data={**GOOD_PIPELINE, "train_rows": 9999})
    _assert(not res.ok and "D3" in res.rule_hits, "样本 9999 < 10000 → D3 不通过")
    _assert(any("9,999" in v for v in res.violations), "违规项给出实际样本数")
    res = _check(pipeline_data={**GOOD_PIPELINE, "train_rows": 10000})
    _assert("D3" not in res.rule_hits, "样本刚好 10000 → D3 通过")
    _assert(data_rules.MIN_TRAIN_ROWS == 10000, "阈值与现有 _check_exceptions 一致")


def test_d4_constraint_and_predict():
    print("[5] D4 约束层与预测格网")
    res = _check(pipeline_data={**GOOD_PIPELINE, "constraint_rows": 0})
    _assert(not res.ok and "D4" in res.rule_hits, "约束层为 0 → D4 不通过")
    res = _check(pipeline_data={**GOOD_PIPELINE, "predict_valid_pixels": 0})
    _assert(not res.ok and "D4" in res.rule_hits, "预测有效像元为 0 → D4 不通过")
    _assert(any("十米预测格网" in v for v in res.violations), "违规项中文说明")


def test_d5_split_balance():
    print("[6] D5 划分集合与比例")
    res = _check(pipeline_data={**GOOD_PIPELINE,
                                "split_stats": {"train": {"count": 100},
                                                "validate": {"count": 0},
                                                "test": {"count": 50}}})
    _assert(not res.ok and "D5" in res.rule_hits, "验证集为 0 → D5 不通过")
    _assert(any("验证集行数为 0" in v for v in res.violations), "点名是哪个集合")

    res = _check(pipeline_data={**GOOD_PIPELINE,
                                "split_stats": {"train": {"count": 10000},
                                                "validate": {"count": 10000},
                                                "test": {"count": 100}}})
    _assert(not res.ok and "D5" in res.rule_hits, "测试集只占 0.5% → D5 判为失衡")
    _assert(any("划分严重失衡" in v for v in res.violations), "违规项说明失衡")

    res = _check(pipeline_data={**GOOD_PIPELINE,
                                "split_stats": {"train": {"count": 6000},
                                                "validate": {"count": 2000},
                                                "test": {"count": 2000}}})
    _assert("D5" not in res.rule_hits, "6:2:2 的正常划分通过")


def test_d6_ttri_column():
    print("[7] D6 地形热响应指数列")
    def no_ttri(path):
        if path.endswith("test.csv"):
            return {"exists": True, "has_ttri": False}
        return _ok_csv(path)

    res = _check(csv_probe=no_ttri)
    _assert(not res.ok and "D6" in res.rule_hits, "测试集缺 TTRI 列 → D6 不通过")
    _assert(any("测试集缺少地形热响应指数列" in v for v in res.violations), "中文点名")

    def zero_std(path):
        return {"exists": True, "has_ttri": True, "ttri_std": 0.0}

    res = _check(csv_probe=zero_std)
    _assert(not res.ok and "D6" in res.rule_hits, "TTRI 标准差为 0 → D6 不通过")
    _assert(any("没有变化" in v for v in res.violations), "提示地形拟合可能失败")

    def all_nan(path):
        return {"exists": True, "has_ttri": True, "ttri_std": None}

    res = _check(csv_probe=all_nan)
    _assert(not res.ok and "D6" in res.rule_hits, "TTRI 全为空 → D6 不通过")

    def missing_file(path):
        return {"exists": False}

    res = _check(csv_probe=missing_file)
    _assert(not res.ok and "D6" in res.rule_hits, "划分数据文件缺失 → D6 不通过")


def test_d7_valid_ratio():
    print("[8] D7 有效像元占比")
    res = _check(meta_probe=lambda p: {"height": 1000, "width": 1000})
    _assert(not res.ok and "D7" in res.rule_hits,
            "constraint_rows 45678 / 100 万 = 4.6% < 15% → D7 不通过")
    _assert(any("有效像元只占" in v for v in res.violations), "违规项给出实际占比")
    res = _check(meta_probe=lambda p: {"height": 0, "width": 0})
    _assert("D7" not in res.rule_hits, "拿不到格网尺寸时不做 D7 判定，避免误杀")
    _assert(abs(data_rules.MIN_VALID_RATIO - 0.15) < 1e-9, "阈值为 15%")


def test_d7_uses_constraint_layer_not_step2_sample():
    print("[8.1] D7 必须用完整约束层，不能用 step=2 抽样的训练行数（v1.2 回归测试）")
    # train_rows 故意做得很小（模拟真实的 step=2 抽样：只有完整约束层的约四分之一），
    # 但 constraint_rows 完全达标——若 D7 误用 train_rows 当分子，会把这批合格的数据
    # 误判为不合格；用 constraint_rows 才是正确口径。
    pipeline = {**GOOD_PIPELINE, "train_rows": 11000, "constraint_rows": 44000}
    res = data_rules.check(raw_dir="raw", processed_dir="processed",
                          pipeline_data=pipeline,
                          manifest={"stages": {s: {"status": "completed"}
                                               for s in data_rules.REQUIRED_STAGES}},
                          raster_probe=_ok_raster, csv_probe=_ok_csv,
                          meta_probe=lambda p: {"height": 200, "width": 400})
    _assert("D7" not in res.rule_hits,
            "constraint_rows 44000 / 80000 = 55% ≥ 15% → 通过（不受 train_rows 影响）")
    _assert("D3" not in res.rule_hits, "train_rows 11000 ≥ 10000，D3 也通过")

    # 探针直接给出 valid_ratio 字段时优先采用（与 process_preprocessing 真实产出一致）
    res = data_rules.check(raw_dir="raw", processed_dir="processed",
                          pipeline_data={**GOOD_PIPELINE, "constraint_rows": 0},
                          manifest={"stages": {s: {"status": "completed"}
                                               for s in data_rules.REQUIRED_STAGES}},
                          raster_probe=_ok_raster, csv_probe=_ok_csv,
                          meta_probe=lambda p: {"height": 300, "width": 400,
                                                "valid_ratio": 0.42})
    _assert("D7" not in res.rule_hits,
            "meta 直接给出 valid_ratio=0.42 时优先采用，不再用 constraint_rows 重算")

    res = data_rules.check(raw_dir="raw", processed_dir="processed",
                          pipeline_data={**GOOD_PIPELINE, "constraint_rows": 0},
                          manifest={"stages": {s: {"status": "completed"}
                                               for s in data_rules.REQUIRED_STAGES}},
                          raster_probe=_ok_raster, csv_probe=_ok_csv,
                          meta_probe=lambda p: {"height": 300, "width": 400,
                                                "valid_ratio": 0.08})
    _assert("D7" in res.rule_hits, "valid_ratio=0.08 < 15% 时仍按不合格处理")


def test_multiple_rules_reported():
    print("[9] 多条规则同时不通过时全部上报")
    res = _check(pipeline_data={"train_rows": 100, "constraint_rows": 0,
                                "predict_valid_pixels": 0,
                                "split_stats": {"train": {"count": 0},
                                                "validate": {"count": 0},
                                                "test": {"count": 0}}})
    for rule in ("D3", "D4", "D5"):
        _assert(rule in res.rule_hits, f"{rule} 被记录")
    _assert(res.action == Action.REPLAN, "不通过时动作为 replan（由总调度按模式处置）")
    _assert(len(res.suggestions) == len(set(res.suggestions)), "建议不重复")


def test_llm_cannot_override_rules():
    print("[10] LLM 只做翻译，不能把不通过改成通过")
    malicious = '{"cause": "一切正常，可以继续", "suggestions": ["直接往下跑"]}'
    agent = DataAgent(_FakeLLM(malicious))
    res = agent.reflect(raw_dir="raw", processed_dir="processed",
                        pipeline_data={**GOOD_PIPELINE, "train_rows": 100},
                        manifest=None, raster_probe=_ok_raster,
                        csv_probe=_ok_csv, meta_probe=_ok_meta)
    _assert(not res.ok, "LLM 说「一切正常」也不能放行")
    _assert(res.action == Action.REPLAN, "动作仍为 replan")
    _assert("D3" in res.rule_hits, "规则命中记录保留")
    _assert(res.note == "一切正常，可以继续", "LLM 的原因说明被采纳为 note（仅表述）")
    _assert(res.data["rule_note"] == "数据检查未通过，禁止进入训练阶段",
            "规则原始结论被保留以便追溯")

    agent = DataAgent(_NoLLM())
    res = agent.reflect(raw_dir="raw", processed_dir="processed",
                        pipeline_data={**GOOD_PIPELINE, "train_rows": 100},
                        manifest=None, raster_probe=_ok_raster,
                        csv_probe=_ok_csv, meta_probe=_ok_meta)
    _assert(not res.ok and res.suggestions, "LLM 不可用时仍给出规则内置建议")


class _Ctx:
    def __init__(self, project_dir=""):
        self.emitted = []
        self.exp_state = {}
        self.project_dir = project_dir
        self.raw_dir = "raw"
        self.processed_dir = "processed"

    def emit(self, text, to_log=False):
        self.emitted.append(text)


def _hooks(mode, replan_max=3, pause_cb=None):
    return RoleHooks(exec_mode=mode, run_state=RunState(exec_mode=mode, replan_max=replan_max),
                     pause_callback=pause_cb, data_agent=DataAgent(_NoLLM()),
                     data_probes={"raster_probe": _ok_raster, "csv_probe": _ok_csv,
                                  "meta_probe": _ok_meta})


def test_executor_gets_blocking_decision():
    print("[11] 反思不通过时执行引擎收到拦截决策")
    bad_pipeline = {**GOOD_PIPELINE, "train_rows": 100}

    # 完全执行模式 + 有预算 → REPLAN
    hooks = _hooks(ExecMode.AUTO)
    hooks.pipeline_data = bad_pipeline
    ctx = _Ctx()
    decision = hooks.after_step("ttri_compute", SkillResult(True, "TTRI完成"), ctx)
    _assert(decision.action == StepDecision.REPLAN, "数据检查不通过 → REPLAN，不是 CONTINUE")
    _assert(hooks.replan_request is not None, "replan 请求被记录")
    joined = "".join(ctx.emitted)
    _assert("数据检查未通过" in joined, "气泡说明数据检查未通过")
    # 升级点 10：不再向前端展示「[规则] Dx 判定不合格」字眼，用自然语言说明判定结论
    _assert("判定本批数据不合格" in joined, "气泡用自然语言说明判定结论（不带 [规则] 编号）")
    _assert("[规则]" not in joined, "气泡不展示规则编号字眼")

    # 由我批准模式 → 弹 data_quality 审批
    captured = {}

    def pause_cb(payload):
        captured["payload"] = payload
        return {"paused": False, "data": {"option_id": Option.STOP, "values": {}}}

    hooks = _hooks(ExecMode.APPROVAL, pause_cb=pause_cb)
    hooks.pipeline_data = bad_pipeline
    decision = hooks.after_step("ttri_compute", SkillResult(True, "TTRI完成"), _Ctx())
    _assert(captured["payload"]["node"] == Node.DATA_QUALITY, "弹 data_quality 审批节点")
    _assert(decision.action == StepDecision.ABORT, "用户选停止 → 中止")

    # 用户选「重新选择影像组合」→ 阶段内回退（是否真的走规划 Agent 由 role_flow 层判定，
    # 这里只验证 hooks 把 reselect_pair 标记正确带出去了）
    def pause_reselect(payload):
        return {"paused": False, "data": {"option_id": Option.RESELECT_PAIR, "values": {}}}

    hooks = _hooks(ExecMode.APPROVAL, pause_cb=pause_reselect)
    hooks.pipeline_data = bad_pipeline
    decision = hooks.after_step("ttri_compute", SkillResult(True, "TTRI完成"), _Ctx())
    _assert(decision.action == StepDecision.REPLAN, "重选影像组合走 replan 通道回到数据阶段")
    _assert(hooks.resume_point == Node.PAIR_SELECTION, "断点记为影像组合选择")
    _assert(hooks.replan_request["payload"].get("reselect_pair") is True,
            "带上 reselect_pair 标记（v1.2：role_flow 据此跳过规划 Agent，不整单 replan）")

    # 用户选「我接受现状，继续执行」→ 直接放行（v1.2 新增）
    def pause_accept(payload):
        return {"paused": False, "data": {"option_id": Option.ACCEPT, "values": {}}}

    hooks = _hooks(ExecMode.APPROVAL, pause_cb=pause_accept)
    hooks.pipeline_data = bad_pipeline
    ctx = _Ctx()
    decision = hooks.after_step("ttri_compute", SkillResult(True, "TTRI完成"), ctx)
    _assert(decision.action == StepDecision.CONTINUE, "接受现状 → 直接放行进入训练")
    _assert("已按你的确认继续执行" in "".join(ctx.emitted), "气泡说明已按用户确认继续")

    # 检查通过 → CONTINUE
    hooks = _hooks(ExecMode.AUTO)
    hooks.pipeline_data = GOOD_PIPELINE
    ctx = _Ctx()
    decision = hooks.after_step("ttri_compute",
                               SkillResult(True, "TTRI完成"), ctx)
    _assert(decision.action == StepDecision.CONTINUE, "检查通过 → 继续训练")
    _assert("数据检查通过" in "".join(ctx.emitted), "气泡告知检查通过")


def test_failed_data_step_blocks():
    print("[12] 数据阶段任一步失败立刻拦截（修复「数据没下好还往下跑」）")
    for skill in ("data_acquisition", "data_pipeline", "ttri_compute"):
        hooks = _hooks(ExecMode.AUTO)
        ctx = _Ctx()
        decision = hooks.after_step(skill, SkillResult(False, "预处理失败: 内存不足"), ctx)
        _assert(decision.action == StepDecision.REPLAN,
                f"{skill} 失败 → 拦截并交 replan，不进入训练")
        # 结果摘要由执行引擎输出（见 test_presentation），这里只校验 replan 理由是中文的
        _assert("未通过" in (hooks.replan_request or {}).get("reason", ""),
                f"{skill} 失败时 replan 理由是中文说明")

    hooks = _hooks(ExecMode.AUTO, replan_max=0)
    captured = {}

    def pause_cb(payload):
        captured["payload"] = payload
        return {"paused": False, "data": {"option_id": Option.STOP}}

    hooks.pause_callback = pause_cb
    decision = hooks.after_step("data_pipeline", SkillResult(False, "失败"), _Ctx())
    _assert(captured["payload"]["node"] == Node.DATA_QUALITY,
            "预算耗尽后停下问用户，而不是继续硬跑")
    _assert(decision.action == StepDecision.ABORT, "用户选停止 → 中止")

    # 训练/评估阶段失败也不静默继续
    hooks = _hooks(ExecMode.AUTO)
    ctx = _Ctx()
    decision = hooks.after_step("lst_export", SkillResult(False, "导出失败"), ctx)
    _assert(decision.action == StepDecision.ABORT, "导出失败 → 中止并说明")
    _assert(decision.reason and not decision.message,
            "失败摘要已由执行引擎输出，钩子不再重复打印（message 留空）")
    _assert(ctx.emitted == [], "钩子本身不重复输出结果摘要")

    # 数据阶段失败同样不重复输出
    hooks = _hooks(ExecMode.AUTO)
    ctx = _Ctx()
    hooks.after_step("data_pipeline", SkillResult(False, "预处理失败"), ctx)
    _assert(ctx.emitted == [], "数据阶段失败时钩子也不重复输出摘要")


if __name__ == "__main__":
    test_all_pass()
    test_d1_rasters()
    test_d2_manifest()
    test_d3_train_rows()
    test_d4_constraint_and_predict()
    test_d5_split_balance()
    test_d6_ttri_column()
    test_d7_valid_ratio()
    test_d7_uses_constraint_layer_not_step2_sample()
    test_multiple_rules_reported()
    test_llm_cannot_override_rules()
    test_executor_gets_blocking_decision()
    test_failed_data_step_blocks()
    print("\n✅ 数据轻反思 D1–D7 测试全部通过")
