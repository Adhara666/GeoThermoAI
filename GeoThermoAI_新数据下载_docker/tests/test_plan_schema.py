# -*- coding: utf-8 -*-
"""
结构化 plan schema 合成测试（技术方案 11.2）

运行：python tests/test_plan_schema.py
覆盖：
- 新格式完整解析
- 旧格式 {"steps":[...]} 自动补齐 stage / id / plan_id
- 非法 skill 剔除（registry 存在时）
- 7 步全流程顺序校验与重排
- to_legacy 降级形状与执行引擎期望一致
- parse 不修改入参（不可变约定）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import plan_schema


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


FULL_PLAN = {
    "plan_version": 1,
    "plan_id": "plan_7f3a2b",
    "intent": "task",
    "goal": "生成九江镇 2025 年 7 月的 10 米地表温度产品",
    "region": {"name": "九江镇", "study_area_file": "/app/data/users/u1/study_areas/九江镇.geojson"},
    "time_range": {"start": "2025-07-01", "end": "2025-07-31"},
    "constraints": {"cloud_threshold": 30, "dem_source": "copernicus", "model": "rf"},
    "steps": [
        {"id": "s1", "stage": "data", "skill": "data_acquisition", "params": {"region": "x"}, "reason": "下载遥感数据"},
        {"id": "s2", "stage": "data", "skill": "data_pipeline", "params": {}, "reason": "数据预处理与划分"},
        {"id": "s3", "stage": "data", "skill": "ttri_compute", "params": {}, "reason": "拟合地形热响应指数"},
        {"id": "s4", "stage": "train", "skill": "rf_model", "params": {}, "reason": "训练降尺度模型"},
        {"id": "s5", "stage": "eval", "skill": "tcr_compute", "params": {}, "reason": "计算热约束残差"},
        {"id": "s6", "stage": "eval", "skill": "lst_export", "params": {}, "reason": "导出最终产品"},
        {"id": "s7", "stage": "eval", "skill": "accuracy_eval", "params": {}, "reason": "精度评估"},
    ],
    "approval_nodes": ["pair_selection", "tuning_decision", "final_report"],
    "memory_refs": ["K13", "K24"],
    "reflection": {"info_complete": True, "risks": [], "note": ""},
}


def test_parse_full():
    print("[1] 新格式完整解析")
    plan = plan_schema.parse(FULL_PLAN)
    _assert(plan["plan_id"] == "plan_7f3a2b", "plan_id 原样保留")
    _assert(plan["region"]["name"] == "九江镇", "region.name 原样保留")
    _assert(plan["time_range"] == {"start": "2025-07-01", "end": "2025-07-31"}, "time_range 原样保留")
    _assert(plan_schema.is_full_workflow(plan), "7 步顺序被识别为全流程")
    _assert(plan["constraints"]["cloud_threshold"] == 30, "constraints 透传")
    _assert(plan["approval_nodes"] == ["pair_selection", "tuning_decision", "final_report"],
            "审批节点透传")


def test_parse_legacy():
    print("[2] 旧格式自动补全")
    legacy = {"steps": [
        {"skill": "data_acquisition", "params": {"region": "武汉"}, "reason": "下载"},
        {"skill": "rf_model", "params": {}, "reason": "训练"},
    ]}
    plan = plan_schema.parse(legacy)
    _assert(plan["plan_version"] == plan_schema.PLAN_VERSION, "plan_version 默认补齐")
    _assert(plan["plan_id"].startswith("plan_"), "plan_id 自动生成")
    _assert(plan["intent"] == "task", "intent 默认 task")
    _assert([s["stage"] for s in plan["steps"]] == ["data", "train"],
            "stage 按 STAGE_OF_SKILL 推断")
    _assert([s["id"] for s in plan["steps"]] == ["s1", "s2"], "step id 自动编号")
    _assert(plan["steps"][0]["params"] == {"region": "武汉"}, "params 原样保留")
    _assert(plan["steps"][0]["reason"] == "下载", "reason 原样保留")
    _assert(plan["region"]["study_area_file"] == "", "旧格式无研究区文件（执行引擎按现状兜底）")


def test_parse_robustness():
    print("[3] 异常输入不抛异常")
    for bad in [None, [], "x", 42, {"steps": "x"}, {"steps": [None, 1, {}, {"skill": ""}]}]:
        plan = plan_schema.parse(bad)
        _assert(isinstance(plan, dict) and plan["steps"] == [],
                f"非法输入 {type(bad).__name__} 返回空步骤的合法 plan")
    plan = plan_schema.parse({"intent": "不存在的意图", "steps": []})
    _assert(plan["intent"] == "task", "非法 intent 回落 task")


def test_drop_unknown_skill():
    print("[4] 非法 skill 剔除（规则 P4）")
    registry = _FakeRegistry(["data_acquisition", "rf_model"])
    plan = plan_schema.parse({"steps": [
        {"skill": "data_acquisition", "params": {}},
        {"skill": "none", "params": {}},
        {"skill": "rf_model", "params": {}},
    ]}, registry=registry)
    _assert(plan_schema.skill_names(plan) == ["data_acquisition", "rf_model"],
            "未注册的 skill 被剔除")
    _assert(plan["dropped_skills"] == ["none"], "被剔除的 skill 有记录（供反问用）")
    empty = plan_schema.parse({"steps": [{"skill": "none"}]}, registry=registry)
    _assert(empty["steps"] == [], "全部非法时步骤为空（调用方转 need_more_info）")


def test_workflow_helpers():
    print("[5] 全流程顺序校验与重排（规则 P5）")
    shuffled = plan_schema.parse({"steps": [
        {"skill": "rf_model"}, {"skill": "data_acquisition"}, {"skill": "data_pipeline"},
        {"skill": "accuracy_eval"}, {"skill": "ttri_compute"}, {"skill": "lst_export"},
        {"skill": "tcr_compute"},
    ]})
    _assert(not plan_schema.is_full_workflow(shuffled), "乱序 7 步不算合法全流程")
    fixed = plan_schema.reorder_to_workflow(shuffled)
    _assert(plan_schema.is_full_workflow(fixed), "重排后顺序正确")
    _assert(plan_schema.skill_names(shuffled)[0] == "rf_model", "重排不修改原 plan（不可变）")
    _assert([s["id"] for s in fixed["steps"]] == [f"s{i}" for i in range(1, 8)],
            "重排后 id 重新编号")

    partial = plan_schema.parse({"steps": [{"skill": "data_acquisition"}]})
    _assert(plan_schema.missing_workflow_skills(partial) == [
        "data_pipeline", "ttri_compute", "rf_model", "tcr_compute", "lst_export", "accuracy_eval",
    ], "缺失步骤清单正确")
    _assert(plan_schema.has_empty_params(partial), "空 params 被识别（沿用现有安全网判据）")
    _assert(not plan_schema.has_empty_params(
        plan_schema.parse({"steps": [{"skill": "rf_model", "params": {"n_estimators": 200}}]})),
        "非空 params 不误判")


def test_to_legacy_and_immutability():
    print("[6] 降级形状与不可变约定")
    plan = plan_schema.parse(FULL_PLAN)
    legacy = plan_schema.to_legacy(plan)
    _assert(set(legacy.keys()) == {"steps"}, "降级后只剩 steps")
    _assert(set(legacy["steps"][0].keys()) == {"skill", "params", "reason"},
            "步骤形状与执行引擎期望一致（skill/params/reason）")

    src = {"steps": [{"skill": "rf_model", "params": {"a": 1}}]}
    plan2 = plan_schema.parse(src)
    plan2["steps"][0]["params"]["a"] = 999
    _assert(src["steps"][0]["params"]["a"] == 1, "parse 深拷贝 params，不污染入参")

    with_new = plan_schema.with_steps(plan, [{"skill": "ai_assistant", "params": {}}])
    _assert(plan_schema.skill_names(plan)[0] == "data_acquisition", "with_steps 不修改原 plan")
    _assert(plan_schema.skill_names(with_new) == ["ai_assistant"], "with_steps 返回新 plan")


def test_validate():
    print("[7] 结构校验")
    registry = _FakeRegistry(list(plan_schema.WORKFLOW_STEPS))
    _assert(plan_schema.validate(plan_schema.parse(FULL_PLAN), registry) == [],
            "合法 plan 无问题")
    broken = plan_schema.parse(FULL_PLAN)
    broken = {**broken, "plan_version": 99}
    issues = plan_schema.validate(broken, registry)
    _assert(any("plan_version" in s for s in issues), "plan_version 非法被检出")
    dup = {**plan_schema.parse(FULL_PLAN)}
    dup["steps"] = [dict(dup["steps"][0]), dict(dup["steps"][0])]
    _assert(any("重复" in s for s in plan_schema.validate(dup, registry)),
            "重复 step id 被检出")
    _assert(any("未注册" in s for s in plan_schema.validate(
        {**plan_schema.parse(FULL_PLAN),
         "steps": [{"id": "s1", "stage": "data", "skill": "ghost", "params": {}, "reason": ""}]},
        registry)), "未注册 skill 被检出")


if __name__ == "__main__":
    test_parse_full()
    test_parse_legacy()
    test_parse_robustness()
    test_drop_unknown_skill()
    test_workflow_helpers()
    test_to_legacy_and_immutability()
    test_validate()
    print("\n✅ plan schema 合成测试全部通过")
