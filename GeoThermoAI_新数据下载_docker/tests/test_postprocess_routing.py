# -*- coding: utf-8 -*-
"""
结果后处理（空洞填补）请求路由合成测试

运行：python tests/test_postprocess_routing.py
覆盖：
- 关键词识别：单独的后处理请求命中（无空洞/填洞/补洞/结果后处理）；
  疑问句与「全流程…包括结果后处理」不命中
- _latest_pair：从当前对话工作区 pairs/ 里按最近 mtime 找到影像对并解析日期
- _handle_postprocess_request：
  - 没找到 10m LST 初始结果 → 明确告知，不执行
  - 找到结果 + 用户确认 → 交给结果 Agent 的 lst_gapfill 执行
  - 找到结果 + 用户拒绝 → 保留原始结果，不执行
  - 用户挂起/超时 → 返回 PAUSE_MARKER 等待选择
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import plan_schema
from core.agent.executor import PAUSE_MARKER
from core.agent.geo_thermo_agent import GeoThermoAgent, _is_postprocess_request
from core.agent.orchestrator.approval import Option
from core.agent.roles.planner_agent import PlannerAgent, PlannerContext, PlannerOutcome
from core.skills.base_skill import BaseSkill, SkillResult
from core.skills.skill_registry import SkillRegistry


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _FakeAssistant:
    def _call_api(self, *args, **kwargs):
        return "API调用失败: 不应走到这里"


class _PlannerFakeAssistant:
    """按调用顺序返回预设响应；不够时返回最后一条（规划 Agent 合成测试用）。"""

    def __init__(self, responses):
        self.responses = list(responses)

    def _call_api(self, messages, **kwargs):
        if not self.responses:
            return "API调用失败: 没有更多预设响应"
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class _FakeGapFill(BaseSkill):
    """记录调用参数并返回成功的假 lst_gapfill。"""

    @property
    def name(self):
        return "lst_gapfill"

    @property
    def group(self):
        return "lst_gapfill"

    @property
    def description(self):
        return "结果后处理（测试占位）"

    @property
    def parameters(self):
        return []

    @property
    def input_schema(self):
        return {}

    @property
    def output_schema(self):
        return {}

    def __init__(self):
        self.calls = []

    def execute(self, params, progress_callback=None, log_callback=None):
        self.calls.append(dict(params or {}))
        filled = params.get("output_tif", "") + ".filled"
        return SkillResult(True, "空洞填补完成",
                           data={"filled_tif": filled,
                                 "mask_tif": params.get("output_mask", "")})


def _make_agent():
    skill = _FakeGapFill()
    registry = SkillRegistry()
    registry.register(skill)
    return GeoThermoAgent(_FakeAssistant(), registry), skill


def _make_project(tmp, conv="cv1", ldate="20240721", sdate="20240722"):
    """造一个带 10m LST 结果的对话工作区，返回 project_dir 与 lst_tif 路径。"""
    project_dir = os.path.join(tmp, "convs", conv)
    results = os.path.join(project_dir, "pairs", f"L{ldate}_S{sdate}", "results")
    os.makedirs(results, exist_ok=True)
    lst_tif = os.path.join(results, f"rf_10m_lst_final_{sdate}.tif")
    with open(lst_tif, "w") as f:
        f.write("x")
    return project_dir, lst_tif


def _pause_choice(choice):
    def cb(payload):
        return {"paused": False, "data": {"option_id": choice}}
    return cb


def _pause_waiting(payload):
    return {"paused": True}


def test_keyword_detection():
    print("[1] 单独后处理请求 vs 全流程请求的关键词识别")
    for hit in ("根据我现有的结果，生成无空洞的10m的地表温度",
                "帮我填补一下空洞",
                "把结果后处理做一下",
                "生成无空洞的10m地表温度产品",
                "补洞",
                "对已有结果做空洞填充",
                "对我当前的结果进行结果后处理"):
        _assert(_is_postprocess_request(hit), f"命中（单独请求）：{hit}")
    for miss in ("为什么会有空洞？", "空洞会影响精度吗", "什么是结果后处理",
                 "跑一下九江镇 2025 年 7 月的全流程",
                 "对武汉市2024年7月的数据做地表温度降尺度全流程处理，包括结果后处理",
                 "对武汉市2024年7月的数据做地表温度降尺度全流程处理，包括最终的无空洞结果生成"):
        _assert(not _is_postprocess_request(miss), f"不命中：{miss}")


def test_latest_pair():
    print("[2] _latest_pair 从当前对话 pairs/ 找最近影像对")
    tmp = tempfile.mkdtemp(prefix="pp_pair_")
    try:
        project_dir = os.path.join(tmp, "convs", "cv1")
        old = os.path.join(project_dir, "pairs", "L20240601_S20240602", "results")
        new = os.path.join(project_dir, "pairs", "L20240721_S20240722", "results")
        for d, name in ((old, "rf_10m_lst_final_20240602.tif"),
                        (new, "rf_10m_lst_final_20240722.tif")):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        agent = GeoThermoAgent(_FakeAssistant(), SkillRegistry())
        pair = agent._latest_pair(project_dir)
        _assert(pair is not None, "找到影像对")
        _assert(pair == {"landsat_date": "2024-07-21", "sentinel2_date": "2024-07-22"},
                f"取最近 mtime 的一对且日期解析正确：{pair}")
        _assert(agent._latest_pair(os.path.join(tmp, "nonexist")) is None,
                "无 pairs 目录返回 None")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_no_result():
    print("[3] 没找到 10m LST 初始结果 → 明确告知，不执行、不跨对话")
    tmp = tempfile.mkdtemp(prefix="pp_nores_")
    try:
        agent, skill = _make_agent()
        project_dir = os.path.join(tmp, "convs", "cv1")
        os.makedirs(project_dir, exist_ok=True)
        tokens = []
        out = agent._handle_postprocess_request(
            "根据我现有的结果，生成无空洞的10m的地表温度",
            on_token=lambda t: tokens.append(t), project_dir=project_dir)
        _assert("没有找到已生成的 10m 地表温度初始结果" in out, "明确告知未找到初始结果")
        _assert(not skill.calls, "不执行 lst_gapfill")
        _assert("别的对话" in out, "提示可能是别的对话生成过（避免张冠李戴）")
        _assert(PAUSE_MARKER not in out, "不弹确认、不挂起")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_confirm_runs():
    print("[4] 找到结果 + 用户确认 → 交给结果 Agent 执行 lst_gapfill")
    tmp = tempfile.mkdtemp(prefix="pp_confirm_")
    try:
        project_dir, lst_tif = _make_project(tmp)
        agent, skill = _make_agent()
        tokens = []
        out = agent._handle_postprocess_request(
            "对我当前的结果进行结果后处理",
            on_token=lambda t: tokens.append(t),
            pause_callback=_pause_choice(Option.RUN_POSTPROCESS),
            project_dir=project_dir)
        _assert(skill.calls, "确认后 lst_gapfill 被执行")
        _assert(skill.calls[0]["input_tif"] == lst_tif, "输入为当前对话的含空洞 LST")
        _assert("空洞填补" in out, "气泡给出结果说明")
        _assert(PAUSE_MARKER not in out, "确认后流程直接跑完")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_decline_skips():
    print("[5] 找到结果 + 用户拒绝 → 保留原始结果，不执行")
    tmp = tempfile.mkdtemp(prefix="pp_skip_")
    try:
        project_dir, _ = _make_project(tmp)
        agent, skill = _make_agent()
        tokens = []
        out = agent._handle_postprocess_request(
            "把结果后处理做一下",
            on_token=lambda t: tokens.append(t),
            pause_callback=_pause_choice(Option.SKIP_POSTPROCESS),
            project_dir=project_dir)
        _assert(not skill.calls, "拒绝后不执行 lst_gapfill")
        _assert("保留当前带空洞" in out, "明确保留原始结果")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_paused():
    print("[6] 用户挂起/超时 → 返回 PAUSE_MARKER 等待选择")
    tmp = tempfile.mkdtemp(prefix="pp_pause_")
    try:
        project_dir, _ = _make_project(tmp)
        agent, skill = _make_agent()
        tokens = []
        out = agent._handle_postprocess_request(
            "生成无空洞的10m地表温度产品",
            on_token=lambda t: tokens.append(t),
            pause_callback=_pause_waiting,
            project_dir=project_dir)
        _assert(PAUSE_MARKER in out, "挂起时返回 PAUSE_MARKER")
        _assert(not skill.calls, "挂起时不执行 lst_gapfill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_postprocess_only():
    print("[7] 后处理执行路径：输入/输出命名与气泡")
    tmp = tempfile.mkdtemp(prefix="pp_run_")
    try:
        project_dir, lst_tif = _make_project(tmp)
        agent, skill = _make_agent()
        pair = agent._latest_pair(project_dir)
        tokens = []
        out = agent._run_postprocess_only(
            "根据我现有的结果，生成无空洞的10m的地表温度", pair,
            on_token=lambda t: tokens.append(t), project_dir=project_dir)
        _assert(skill.calls, "lst_gapfill 技能被执行")
        _assert(skill.calls[0]["input_tif"] == lst_tif, "输入是当前对话已有的含空洞 LST")
        _assert(skill.calls[0]["output_tif"].endswith("rf_10m_lst_final_filled_20240722.tif"),
                "输出为无空洞命名（_filled 前缀，不与 lst_export 串扰）")
        _assert("空洞填补" in out, "气泡给出结果说明")
        _assert("filled" not in out and "tif" not in out,
                "产物路径只进日志，不进气泡（见结果后处理气泡排版需求）")
        _assert(tokens and "空洞填补" in tokens[-1], "流式输出透传完整")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _planner_ctx(tmp, user_input):
    """构造规划 Agent 上下文（带一个研究区文件）。"""
    d = os.path.join(tmp, "study_areas")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "九江镇.geojson"), "w", encoding="utf-8") as f:
        f.write('{"type":"FeatureCollection","features":[]}')
    registry = SkillRegistry()
    registry.register(_FakeGapFill())
    ctx = PlannerContext(
        user_input=user_input,
        study_areas=["九江镇.geojson"],
        study_areas_dir=d,
        project_dir=os.path.join(tmp, "project"),
        settings={"data": {"cloud_threshold": 30, "dem_source": "copernicus"}},
        skill_catalog=registry.get_tool_descriptions_for_llm(),
    )
    return d, registry, ctx


def test_planner_classifies_postprocess():
    print("[8] 规划 Agent 用 LLM 将后处理请求分类为 postprocess 并出单步计划")
    tmp = tempfile.mkdtemp(prefix="pp_plan_")
    try:
        assistant = _PlannerFakeAssistant([
            json.dumps({"intent": "postprocess", "intent_confidence": 0.9,
                        "reason": "用户要求对已有结果做空洞填补",
                        "slots": {"region_name": None, "time_expression": None,
                                  "product": "lst_10m", "model": None},
                        "missing": [], "question": None}, ensure_ascii=False),
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(tmp, "根据我现有的结果，生成无空洞的10m的地表温度")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.action == PlannerOutcome.PLAN, "规划 Agent 产出可执行计划")
        _assert(outcome.intent == "postprocess", f"意图分类为 postprocess：{outcome.intent}")
        _assert(plan_schema.skill_names(outcome.plan) == ["lst_gapfill"],
                "计划只有 lst_gapfill 一步（不重跑生产流程）")
        _assert(str((outcome.plan.get("region") or {}).get("study_area_file") or "")
                .endswith("九江镇.geojson"), "研究区 GeoJSON 已带上（填洞限定研究区范围）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planner_keyword_fallback_postprocess():
    print("[9] LLM 不可用时规划 Agent 关键词兜底仍判 postprocess")
    tmp = tempfile.mkdtemp(prefix="pp_fallback_")
    try:
        assistant = _PlannerFakeAssistant([
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(tmp, "帮我生成无空洞的10m地表温度")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.action == PlannerOutcome.PLAN, "兜底仍产出可执行计划")
        _assert(outcome.intent == "postprocess", "关键词兜底判定为 postprocess")
        _assert(plan_schema.skill_names(outcome.plan) == ["lst_gapfill"], "单步 lst_gapfill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planner_llm_task_then_confirm_postprocess():
    print("[10] LLM 首判 task、二次判定 postprocess → 改判并出单步填洞计划")
    tmp = tempfile.mkdtemp(prefix="pp_confirm_")
    try:
        assistant = _PlannerFakeAssistant([
            json.dumps({"intent": "task", "intent_confidence": 0.9,
                        "reason": "测试（LLM 首判误判为 task）",
                        "slots": {"region_name": None, "time_expression": None,
                                  "product": "lst_10m", "model": None},
                        "missing": ["time_range"], "question": None}, ensure_ascii=False),
            json.dumps({"intent": "postprocess", "confidence": 0.9,
                        "reason": "用户要求对已有结果做空洞填补"}, ensure_ascii=False),
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(tmp, "好的，继续帮我生成无空洞的结果")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.action == PlannerOutcome.PLAN, "二次判定后产出可执行计划")
        _assert(outcome.intent == "postprocess", f"意图改判为 postprocess：{outcome.intent}")
        _assert(plan_schema.skill_names(outcome.plan) == ["lst_gapfill"],
                "计划只有 lst_gapfill 一步（不重跑生产流程）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planner_llm_task_high_confidence_respected():
    print("[11] LLM 首判 task 且二次判定高置信 task → 尊重 LLM，走完整流程")
    tmp = tempfile.mkdtemp(prefix="pp_respect_")
    try:
        _steps = [{"skill": s, "params": {"region": "九江镇.geojson",
                                          "start_date": "2025-07-01",
                                          "end_date": "2025-07-31"},
                   "reason": "测试"} for s in plan_schema.WORKFLOW_STEPS]
        assistant = _PlannerFakeAssistant([
            json.dumps({"intent": "task", "intent_confidence": 0.95,
                        "reason": "测试",
                        "slots": {"region_name": "九江镇", "time_expression": "2025年7月",
                                  "product": "lst_10m", "model": None},
                        "missing": [], "question": None}, ensure_ascii=False),
            json.dumps({"intent": "task", "confidence": 0.95,
                        "reason": "用户要求重新生成产品"}, ensure_ascii=False),
            json.dumps({"goal": "生成九江镇 2025 年 7 月的十米地表温度产品",
                        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus",
                                        "model": "rf"},
                        "steps": _steps, "memory_refs": []}, ensure_ascii=False),
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(tmp, "帮我重新跑一遍流程并生成无空洞结果")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.intent == "task", f"尊重 LLM 判 task：{outcome.intent}")
        _assert(outcome.action == PlannerOutcome.PLAN, "完整流程计划可执行")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planner_full_workflow_with_gapfill():
    print("[12] 规划 Agent：从头执行并包含结果后处理 → task + 完整流程末尾带 lst_gapfill")
    tmp = tempfile.mkdtemp(prefix="pp_full_")
    try:
        _steps = [{"skill": s, "params": {},
                   "reason": "测试"} for s in plan_schema.WORKFLOW_STEPS]
        _steps.append({"skill": "lst_gapfill", "params": {},
                       "reason": "对结果做空洞填补生成无空洞产品"})
        assistant = _PlannerFakeAssistant([
            json.dumps({"intent": "task", "intent_confidence": 0.95,
                        "reason": "用户要求对九江镇跑完整流程并包含结果后处理",
                        "slots": {"region_name": "九江镇", "time_expression": "2025年7月",
                                  "product": "lst_10m", "model": None},
                        "missing": [], "question": None}, ensure_ascii=False),
            json.dumps({"goal": "生成九江镇 2025 年 7 月的10m地表温度产品（含无空洞结果）",
                        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus",
                                        "model": "rf"},
                        "steps": _steps, "memory_refs": []}, ensure_ascii=False),
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(
            tmp, "对九江镇2025年7月的数据做地表温度降尺度全流程处理，包括最终的无空洞结果")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.action == PlannerOutcome.PLAN, "产出可执行计划")
        _assert(outcome.intent == "task", f"从头执行判为 task：{outcome.intent}")
        names = plan_schema.skill_names(outcome.plan)
        _assert(names == list(plan_schema.WORKFLOW_STEPS) + ["lst_gapfill"],
                f"计划 = 7 步完整流程 + lst_gapfill 末尾：{names}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planner_full_workflow_without_gapfill():
    print("[13] 规划 Agent：普通全流程请求（未提后处理）→ 恰好 7 步，不含 lst_gapfill")
    tmp = tempfile.mkdtemp(prefix="pp_plain_")
    try:
        _steps = [{"skill": s, "params": {}, "reason": "测试"}
                  for s in plan_schema.WORKFLOW_STEPS]
        assistant = _PlannerFakeAssistant([
            json.dumps({"intent": "task", "intent_confidence": 0.95,
                        "reason": "用户要求跑完整流程",
                        "slots": {"region_name": "九江镇", "time_expression": "2025年7月",
                                  "product": "lst_10m", "model": None},
                        "missing": [], "question": None}, ensure_ascii=False),
            json.dumps({"goal": "生成九江镇 2025 年 7 月的10m地表温度产品",
                        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus",
                                        "model": "rf"},
                        "steps": _steps, "memory_refs": []}, ensure_ascii=False),
            json.dumps({"ok": True, "action": "proceed", "question": "",
                        "note": "测试"}, ensure_ascii=False),
        ])
        d, registry, ctx = _planner_ctx(
            tmp, "对武汉市2024年7月的数据做地表温度降尺度全流程处理")
        planner = PlannerAgent(assistant, registry)
        outcome = planner.run(ctx)
        _assert(outcome.action == PlannerOutcome.PLAN, "产出可执行计划")
        _assert(outcome.intent == "task", f"判为 task：{outcome.intent}")
        names = plan_schema.skill_names(outcome.plan)
        _assert(names == list(plan_schema.WORKFLOW_STEPS), f"计划恰好 7 步：{names}")
        _assert("lst_gapfill" not in names, "未提结果后处理 → 计划不含 lst_gapfill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_keyword_detection()
    test_latest_pair()
    test_handle_no_result()
    test_handle_confirm_runs()
    test_handle_decline_skips()
    test_handle_paused()
    test_run_postprocess_only()
    test_planner_classifies_postprocess()
    test_planner_keyword_fallback_postprocess()
    test_planner_llm_task_then_confirm_postprocess()
    test_planner_llm_task_high_confidence_respected()
    test_planner_full_workflow_with_gapfill()
    test_planner_full_workflow_without_gapfill()
    print("\n✅ 结果后处理路由测试全部通过")

