# -*- coding: utf-8 -*-
"""
多角色路径端到端合成测试（技术方案 11.3 的自动化版本）

运行：python tests/test_roles_end_to_end_synthetic.py
用合成 Skill + FakeAssistant 走完整的 `process_command_with_roles`，零网络依赖。
覆盖 11.3 表格里的全部场景：
- 纯聊天 / 领域问答：不触发任何 Skill、不写实验记录
- 多轮模糊（九江镇 → 生成啊 → 25 年 → 7 月）：四轮后出 plan，区域正确，绝不出现武汉
- 完全执行 · 全流程：一次跑完，自动选配对、自动调优、直接出报告
- 由我批准 · 全流程：在配对选择、调优决策、每轮调优、最终报告都停下来问
- 数据预处理失败：停下不进训练
- 未设项目目录 / 未上传研究区：对话方式引导（拍板结论 3）
- 特性开关关闭时完全走旧路径
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import plan_schema
from core.agent.executor import PAUSE_MARKER
from core.agent.geo_thermo_agent import GeoThermoAgent
from core.agent.orchestrator.approval import Node, Option
from core.memory import MemoryManager
from core.skills.base_skill import BaseSkill, SkillResult
from core.skills.skill_registry import SkillRegistry


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


# ── 合成产物：让数据轻反思 D1–D7 走真实探针 ─────────────────────────

def _write_rasters(raw_dir):
    """写 5 个 4×4 的真实小栅格，供 D1 的默认探针打开。"""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    os.makedirs(raw_dir, exist_ok=True)
    transform = from_origin(500000, 3400000, 30, 30)
    for name in ("landsat_lst.tif", "landsat_qa_pixel.tif", "sentinel2_bands.tif",
                 "sentinel2_scl.tif", "dem.tif"):
        with rasterio.open(os.path.join(raw_dir, name), "w", driver="GTiff",
                           height=4, width=4, count=1, dtype="float32",
                           crs="EPSG:32650", transform=transform) as ds:
            ds.write(np.arange(16, dtype="float32").reshape(4, 4), 1)


def _write_tables(processed_dir):
    """写 train/validate/test CSV（含取值有变化的 TTRI 列）与 30 米格网元数据。"""
    os.makedirs(processed_dir, exist_ok=True)
    for name in ("train.csv", "validate.csv", "test.csv"):
        with open(os.path.join(processed_dir, name), "w", encoding="utf-8") as f:
            f.write("row,col,NDVI,DEM,LST,TTRI\n")
            for i in range(20):
                f.write(f"{i},{i},0.3{i % 5},{100 + i},300.{i % 9},{1.0 + i * 0.1:.2f}\n")
    with open(os.path.join(processed_dir, "30m_features_step2_meta.json"), "w",
              encoding="utf-8") as f:
        json.dump({"height": 300, "width": 400}, f)


# ── 合成 Skill ─────────────────────────────────────────────────────

class _FakeSkill(BaseSkill):
    def __init__(self, name, ok=True, message="完成", data=None):
        self._name = name
        self._ok = ok
        self._msg = message
        self._data = data or {}
        self.calls = []

    @property
    def name(self):
        return self._name

    @property
    def group(self):
        return "test"

    @property
    def description(self):
        return "合成测试 Skill"

    @property
    def parameters(self):
        return []

    @property
    def input_schema(self):
        return {}

    @property
    def output_schema(self):
        return {}

    def execute(self, params, progress_callback=None, log_callback=None):
        self.calls.append(dict(params))
        data = dict(self._data)
        # 真实写出下游检查会读的产物，让 D1–D7 走默认探针而不是被注入替身
        if self._ok and params.get("output_dir"):
            if self._name == "data_acquisition" and params.get("selected_pair"):
                _write_rasters(params["output_dir"])
            elif self._name == "data_pipeline":
                _write_tables(params["output_dir"])
        if self._name == "data_acquisition" and not params.get("selected_pair"):
            data = {
                "image_pairs": [
                    {"landsat_date": "2025-07-25", "landsat_satellite": "L9",
                     "landsat_cloud_cover": 40, "landsat_coverage": 80,
                     "landsat_count": 1, "sentinel2_date": "2025-07-27",
                     "sentinel2_cloud_cover": 35, "sentinel2_coverage": 82,
                     "sentinel2_count": 1, "time_diff_days": 2},
                    {"landsat_date": "2025-07-17", "landsat_satellite": "L9",
                     "landsat_cloud_cover": 5, "landsat_coverage": 98,
                     "landsat_count": 1, "sentinel2_date": "2025-07-18",
                     "sentinel2_cloud_cover": 4, "sentinel2_coverage": 99,
                     "sentinel2_count": 1, "time_diff_days": 1},
                ],
                "landsat_count": 6, "sentinel_count": 8,
            }
        return SkillResult(success=self._ok, message=self._msg, data=data)


PIPELINE_DATA = {
    "train_rows": 45678, "constraint_rows": 5000, "predict_valid_pixels": 400000,
    "split_stats": {"train": {"count": 27406}, "validate": {"count": 9136},
                    "test": {"count": 9136}},
    "train_csv": "", "val_csv": "", "test_csv": "",
}

RF_DATA = {
    "train_metrics": {"train": {"R2": 0.90}},
    "test_metrics": {"R2": 0.87, "RMSE": 1.23, "MB": 0.12},
    "feature_importance": [{"feature": "NDVI", "importance": 0.28},
                           {"feature": "TTRI", "importance": 0.20}],
    "params": {"n_estimators": 200, "max_depth": 25},
    "independent_prediction": {"R2": 0.82, "RMSE_K": 1.41, "n_samples": 388869},
    "train_time_seconds": 12.3,
}

ACC_DATA = {
    "closure_metrics": {
        "closure": {"n_matched_cells": 373240, "coverage_ratio": 0.98,
                    "metrics": {"MB_K": 0.05, "MAE_K": 0.40, "RMSE_K": 0.50}},
        "value_range": {"low_end_difference_K": -0.45, "high_end_difference_K": -0.58},
    },
}


def _registry(pipeline_ok=True):
    reg = SkillRegistry()
    reg.register(_FakeSkill("data_acquisition", message="数据下载完成"))
    reg.register(_FakeSkill("data_pipeline", ok=pipeline_ok,
                            message="预处理完成" if pipeline_ok else "预处理失败: 内存不足",
                            data=PIPELINE_DATA if pipeline_ok else {}))
    reg.register(_FakeSkill("ttri_compute", message="TTRI计算完成",
                            data={"coefficients": {"r2": 0.62}, "total_valid": 4231905}))
    reg.register(_FakeSkill("rf_model", message="模型训练完成", data=RF_DATA))
    reg.register(_FakeSkill("tcr_compute", message="TCR计算完成",
                            data={"tcr_statistics": {"mean": 0.02, "std": 0.45,
                                                    "n_valid_blocks": 373240}}))
    reg.register(_FakeSkill("lst_export", message="导出完成",
                            data={"image_size": {"height": 2100, "width": 1800},
                                  "stats": {"valid_percent": 92.4,
                                            "total_valid": 4231905}}))
    reg.register(_FakeSkill("accuracy_eval", message="闭合评估完成", data=ACC_DATA))
    reg.register(_FakeSkill("ai_assistant", message="分析完成"))
    return reg


# ── 合成 LLM ───────────────────────────────────────────────────────

class _FakeAssistant:
    """按顺序返回预设响应；ask_stream 直接回一句话。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.api_key = "sk-test"
        self.api_base_url = ""
        self.ask_calls = 0

    def _call_api(self, messages, **kwargs):
        if not self.responses:
            return "API调用失败: 没有更多预设响应"
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def ask_stream(self, question, on_token, context=None, prior_messages=None,
                   on_thinking=None):
        self.ask_calls += 1
        text = "这是一次纯对话回答。"
        if on_token:
            on_token(text)
        return text


def _intent(intent, region=None, time_expr=None):
    return json.dumps({"intent": intent, "intent_confidence": 0.9, "reason": "测试",
                       "slots": {"region_name": region, "time_expression": time_expr,
                                 "product": "lst_10m", "model": None},
                       "missing": [], "question": None}, ensure_ascii=False)


def _reflect(action="proceed", question=""):
    return json.dumps({"ok": action == "proceed", "action": action,
                       "question": question, "note": "测试"}, ensure_ascii=False)


def _plan_json(region_file):
    return json.dumps({
        "goal": "生成九江镇 2025 年 7 月的十米地表温度产品",
        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus", "model": "rf"},
        "steps": [{"skill": s,
                   "params": ({"region": region_file, "start_date": "2025-07-01",
                               "end_date": "2025-07-31"} if s == "data_acquisition" else {}),
                   "reason": "测试"} for s in plan_schema.WORKFLOW_STEPS],
        "memory_refs": [],
    }, ensure_ascii=False)


def _train_accept():
    return json.dumps({"action": "accept", "reason": "精度已达标", "new_params": {}},
                      ensure_ascii=False)


def _eval_text():
    return ("产品概况：九江镇 2025 年 7 月十米地表温度产品，有效像元 4,231,905 个。\n"
            "模型精度：测试集决定系数 0.87，属于优秀；独立预测决定系数 0.82。\n"
            "闭合情况：平均偏差 0.05 开尔文，平均绝对误差 0.40 开尔文，"
            "共比对 373,240 个格网；这是算术均值闭合，不是十米精度。\n"
            "关键特征与局限：植被指数贡献最大；云掩膜区域没有结果。")


# ── 测试环境搭建 ───────────────────────────────────────────────────

class _Env:
    def __init__(self, tmp, agent_settings=None):
        self.tmp = tmp
        self.study_dir = os.path.join(tmp, "study_areas")
        os.makedirs(self.study_dir, exist_ok=True)
        for name in ("九江镇.geojson", "南海区.geojson"):
            with open(os.path.join(self.study_dir, name), "w", encoding="utf-8") as f:
                f.write('{"type":"FeatureCollection","features":[]}')
        self.region_file = os.path.join(self.study_dir, "九江镇.geojson")
        self.project_dir = os.path.join(tmp, "project")
        os.makedirs(self.project_dir, exist_ok=True)
        self.settings_path = os.path.join(tmp, "settings.json")
        settings = {
            "data": {"cloud_threshold": 30, "dem_source": "copernicus"},
            "model": {"n_estimators": 200, "max_depth": 25},
            "agent": agent_settings or {"roles_enabled": True, "replan_max": 3,
                                       "tuning_max_rounds": 3,
                                       "approval_wait_seconds": 60,
                                       "default_exec_mode": "approval"},
        }
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False)
        self.memory = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        self.tokens = []
        self.logs = []

    def on_token(self, text):
        self.tokens.append(text)

    def on_log(self, text):
        self.logs.append(text)

    def bubble(self):
        return self.tokens[-1] if self.tokens else ""

    def run(self, agent, message, exec_mode="approval", pause_callback=None,
            conv_id="cv1", project_id="p1", project_dir=None):
        return agent.process_command(
            message, on_token=self.on_token, on_log=self.on_log,
            pause_callback=pause_callback,
            project_dir=self.project_dir if project_dir is None else project_dir,
            settings_path=self.settings_path, study_areas_dir=self.study_dir,
            conv_id=conv_id, project_id=project_id, memory_manager=self.memory,
            exec_mode=exec_mode, prior_messages=[],
        )


def _scripted_pause(script):
    """按节点顺序返回用户选择；script 为 [(node, option_id, values)]。"""
    seen = []

    def cb(payload):
        seen.append(payload)
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if pairs:
            # 配对选择：选推荐项
            best = next((p for p in pairs if p.get("recommended")), pairs[0])
            return {"paused": False, "data": {"landsat_date": best["landsat_date"],
                                              "sentinel_date": best["sentinel_date"]}}
        if not script:
            return {"paused": True}
        node, option_id, values = script.pop(0)
        assert payload.get("node") == node, \
            f"期望节点 {node}，实际 {payload.get('node')}"
        return {"paused": False, "data": {"option_id": option_id, "values": values or {}}}

    cb.seen = seen
    return cb


# ── 场景 ───────────────────────────────────────────────────────────

def test_chat_does_not_run_skills():
    print("[1] 纯聊天与领域问答：不触发任何 Skill、不写实验记录")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        for intent, label in (("chat", "纯聊天"), ("qa", "领域问答")):
            env = _Env(tmp)
            registry = _registry()
            assistant = _FakeAssistant([_intent(intent)])
            agent = GeoThermoAgent(assistant, registry)
            out = env.run(agent, "你好" if intent == "chat" else "TTRI 是解决什么问题的")
            _assert(assistant.ask_calls == 1, f"{label} 走流式对话")
            _assert(all(not s.calls for s in registry._skills.values()),
                    f"{label} 没有执行任何 Skill")
            _assert(env.memory.experiment_log("p1").all() == [],
                    f"{label} 不写实验记录")
            _assert(out == "这是一次纯对话回答。", f"{label} 返回对话内容")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_multi_turn_never_wuhan():
    print("[2] 多轮模糊：九江镇 → 生成啊 → 25 年 → 7 月")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()

        turns = [
            ("你认识九江镇吗？", [_intent("qa", region="九江镇")]),
            ("生成啊", [_intent("task", region="九江镇")]),
            ("25 年", [_intent("task", time_expr="25年")]),
            ("7 月", [_intent("task", time_expr="7月"),
                     _plan_json(env.region_file), _reflect()]),
        ]
        outputs = []
        for i, (message, responses) in enumerate(turns, 1):
            agent = GeoThermoAgent(_FakeAssistant(responses), registry)
            pause = _scripted_pause([(Node.PLAN_CONFIRM, Option.EDIT_REQUEST, {})])
            out = env.run(agent, message, pause_callback=pause)
            outputs.append(out)
            if i == 2:
                _assert("时间" in out or "月份" in out, "第 2 轮反问时间范围")
            if i == 3:
                _assert("2025" in out and "月" in out, "第 3 轮要求确认月份")

        _assert(all("武汉" not in o for o in outputs), "任何一轮都不出现「默认武汉」")
        session = env.memory.session_state("cv1", "p1").load()
        _assert(session["slots"]["region_name"]["value"] == "九江镇",
                "会话槽位记住了九江镇")
        _assert(session["slots"]["time_range"]["value"] == ["2025-07-01", "2025-07-31"],
                "时间范围在第 4 轮补全为 2025 年 7 月")
        acq = registry.get("data_acquisition")
        _assert(not acq.calls, "第 4 轮在方案确认处被用户打断，未执行下载")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_full_workflow():
    print("[3] 完全执行 · 全流程：一次跑完")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
            _train_accept(), _eval_text(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        out = env.run(agent, "跑一下九江镇 2025 年 7 月的全流程", exec_mode="auto")

        for name in plan_schema.WORKFLOW_STEPS:
            _assert(registry.get(name).calls, f"{name} 已执行")
        _assert(PAUSE_MARKER not in out, "完全执行模式全程不暂停")
        bubble = env.bubble()
        _assert("已自动选择第 1 组" in bubble, "自动选了质量最高的一组影像")
        _assert("云量很低" in bubble, "说明自动选择的理由")
        acq_params = registry.get("data_acquisition").calls[-1]
        _assert(acq_params["selected_pair"]["landsat_date"] == "2025-07-17",
                "选中的是云量最低的那一组（不是数据源给的第一组）")
        _assert(bubble.count("找到 2 组可用的影像组合") == 1,
                "「找到 N 组可用的影像组合」只输出一次，不重复")
        _assert("数据检查通过" in bubble, "数据轻反思通过后才进训练")
        _assert("模型训练完成" in bubble, "训练阶段有中文摘要")
        _assert("闭合校核完成" in bubble, "评估阶段有中文摘要")
        _assert("不是十米精度" in bubble, "结果说明写明了闭合的口径")

        records = env.memory.experiment_log("p1").all()
        _assert(len(records) == 1 and records[0]["status"] == "success",
                "写入一条成功实验记录")
        _assert(records[0]["metrics"]["test"]["R2"] == 0.87, "实验记录含模型指标")
        _assert(records[0]["region"] == "九江镇.geojson", "实验记录的研究区正确")
        workflows = env.memory.workflows("p1").all()
        _assert(len(workflows) == 1, "满足三个条件 → 写入可复用工作流经验")
        _assert(workflows[0]["metrics"]["test_r2"] == 0.87, "工作流记录含最终指标")

        from core.agent import presentation
        _assert(presentation.strip_emoji(bubble) == bubble, "整条气泡不含表情符号")
        for name in plan_schema.WORKFLOW_STEPS:
            _assert(name not in bubble, f"气泡里不出现英文技能名 {name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approval_full_workflow():
    print("[4] 由我批准 · 全流程：在每个关键节点都停下来问")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
            _train_accept(), _eval_text(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        pause = _scripted_pause([
            (Node.PLAN_CONFIRM, Option.START, {}),
            (Node.TUNING_DECISION, Option.ACCEPT, {}),
            (Node.FINAL_REPORT, Option.DONE, {}),
        ])
        out = env.run(agent, "跑一下九江镇 2025 年 7 月的全流程",
                      exec_mode="approval", pause_callback=pause)

        nodes = [p.get("node") for p in pause.seen if isinstance(p, dict) and p.get("node")]
        _assert(nodes == [Node.PLAN_CONFIRM, Node.TUNING_DECISION, Node.FINAL_REPORT],
                f"依次弹出方案确认、调优决策、最终报告（实际 {nodes}）")
        pair_payloads = [p for p in pause.seen if p.get("pairs")]
        _assert(len(pair_payloads) == 1, "影像配对选择也停下来问了")
        pairs = pair_payloads[0]["pairs"]
        _assert(pairs[0]["recommended"] is True, "配对列表里最优的一组带推荐标记")
        _assert(pairs[0]["landsat_date"] == "2025-07-17", "推荐的是云量最低的一组")
        _assert(PAUSE_MARKER not in out, "所有节点都得到响应，流程跑完")
        _assert(len(env.memory.experiment_log("p1").all()) == 1, "写入实验记录")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approval_high_accuracy_still_asks():
    print("[5] 由我批准：精度很高也要问（用户明确要求）")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()
        high = {**RF_DATA, "test_metrics": {"R2": 0.95, "RMSE": 0.80, "MB": 0.02}}
        registry._skills["rf_model"] = _FakeSkill("rf_model", message="模型训练完成",
                                                 data=high)
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(), _train_accept(), _eval_text(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        pause = _scripted_pause([
            (Node.PLAN_CONFIRM, Option.START, {}),
            (Node.TUNING_DECISION, Option.ACCEPT, {}),
            (Node.FINAL_REPORT, Option.DONE, {}),
        ])
        env.run(agent, "跑全流程", exec_mode="approval", pause_callback=pause)
        tuning = [p for p in pause.seen if p.get("node") == Node.TUNING_DECISION]
        _assert(len(tuning) == 1, "决定系数 0.95 仍然弹了调优询问")
        _assert("0.95" in tuning[0]["summary"], "询问时报告了本轮精度")
        _assert("优秀" in tuning[0]["summary"], "询问时报告了评级")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pipeline_failure_blocks_training():
    print("[6] 数据预处理失败：停下不进训练")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry(pipeline_ok=False)
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        pause = _scripted_pause([
            (Node.PLAN_CONFIRM, Option.START, {}),
            (Node.DATA_QUALITY, Option.STOP, {}),
        ])
        out = env.run(agent, "跑全流程", exec_mode="approval", pause_callback=pause)

        _assert(not registry.get("rf_model").calls, "训练阶段没有被执行")
        _assert(not registry.get("accuracy_eval").calls, "评估阶段没有被执行")
        nodes = [p.get("node") for p in pause.seen if p.get("node")]
        _assert(Node.DATA_QUALITY in nodes, "弹出了数据检查未通过的审批节点")
        records = env.memory.experiment_log("p1").all()
        _assert(records and records[0]["status"] == "failed", "写入失败实验记录")
        _assert(records[0]["failure_stage"] == "data_pipeline", "失败阶段定位正确")
        _assert(env.memory.workflows("p1").all() == [], "失败流程不写工作流经验")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_project_dir_guided():
    print("[7] 未设项目目录：对话方式引导（拍板结论 3）")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        out = env.run(agent, "跑全流程", project_dir="")
        _assert("项目目录" in out and "选择或创建项目" in out,
                "以对话方式提示先创建项目，而不是生硬拦截")
        _assert(all(not s.calls for s in registry._skills.values()),
                "没有执行任何 Skill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_study_area_guided():
    print("[8] 未上传研究区：对话方式引导")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        empty = os.path.join(tmp, "empty_study")
        os.makedirs(empty, exist_ok=True)
        env.study_dir = empty
        registry = _registry()
        agent = GeoThermoAgent(_FakeAssistant([_intent("task")]), registry)
        out = env.run(agent, "跑全流程")
        _assert("上传" in out and "研究区" in out, "提示先上传研究区")
        _assert(all(not s.calls for s in registry._skills.values()), "没有执行任何 Skill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_feature_switch_off_uses_old_path():
    print("[9] 特性开关关闭时完全走旧路径")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp, agent_settings={"roles_enabled": False})
        registry = _registry()
        # 旧路径直接让 LLM 出 {"steps":[...]}，不经过意图分类
        assistant = _FakeAssistant([json.dumps(
            {"steps": [{"skill": "ai_assistant", "params": {"mode": "diagnose"},
                        "reason": "测试"}]}, ensure_ascii=False)])
        agent = GeoThermoAgent(assistant, registry)
        out = env.run(agent, "生成一下产品")
        _assert(registry.get("ai_assistant").calls, "旧路径直接按 LLM 计划执行")
        _assert(env.memory.session_state("cv1", "p1").load()["slots"] == {},
                "旧路径不写会话槽位（不启用角色能力）")
        _assert("执行方案已确定" in "".join(env.tokens), "旧路径也用中文气泡文案")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_tuning_rounds():
    print("[10] 完全执行 · 自动调优跑多轮并取最佳轮")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()

        class _ImprovingRF(_FakeSkill):
            def __init__(self):
                super().__init__("rf_model", message="模型训练完成")
                self.round = 0
                self.metrics = [(0.70, 0.58, 2.10), (0.82, 0.76, 1.55),
                                (0.90, 0.86, 1.22)]

            def execute(self, params, progress_callback=None, log_callback=None):
                self.calls.append(dict(params))
                train_r2, test_r2, rmse = self.metrics[min(self.round,
                                                           len(self.metrics) - 1)]
                self.round += 1
                return SkillResult(True, "模型训练完成", data={
                    **RF_DATA,
                    "train_metrics": {"train": {"R2": train_r2}},
                    "test_metrics": {"R2": test_r2, "RMSE": rmse, "MB": 0.1},
                })

        rf = _ImprovingRF()
        registry._skills["rf_model"] = rf
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
            json.dumps({"action": "adjust", "reason": "加大容量",
                        "new_params": {"n_estimators": 400}}, ensure_ascii=False),
        ])
        agent = GeoThermoAgent(assistant, registry)
        env.run(agent, "跑全流程", exec_mode="auto")

        _assert(len(rf.calls) == 3, f"按配置上限跑了 3 轮（实际 {len(rf.calls)} 轮）")
        _assert(rf.calls[0]["output_dir"].endswith("round_0"), "第 1 轮写入 round_0")
        _assert(rf.calls[1]["output_dir"].endswith("round_1"), "第 2 轮写入 round_1")
        _assert(all(c["defer_cleanup"] for c in rf.calls), "各轮都延迟清理中间产物")
        bubble = env.bubble()
        _assert("采用第 3 轮" in bubble, "取误差最小的第 3 轮")
        _assert("已达上限" in bubble, "气泡说明调优已达上限（升级点 10：不带 [规则] 编号）")
        records = env.memory.experiment_log("p1").all()
        _assert(records[0]["tuning_trace"] and len(records[0]["tuning_trace"]) == 3,
                "实验记录保留完整调优轨迹")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reselect_pair_skips_replan():
    print("[11] 「重新选择影像组合」直接复用原 plan 重跑，不经过规划 Agent 的整单 replan（v1.2）")
    tmp = tempfile.mkdtemp(prefix="roles_e2e_")
    try:
        env = _Env(tmp)
        registry = _registry()
        # 只给规划阶段与最终报告准备好响应；若「重新选择影像组合」误触发了 replan，
        # 会因为拿不到额外的计划 JSON 而在气泡里留下明显痕迹（下面会断言不出现）。
        assistant = _FakeAssistant([
            _intent("task", region="九江镇", time_expr="2025年7月"),
            _plan_json(env.region_file), _reflect(),
            _eval_text(),
        ])
        agent = GeoThermoAgent(assistant, registry)
        pause = _scripted_pause([
            (Node.PLAN_CONFIRM, Option.START, {}),
            (Node.TUNING_DECISION, Option.RESELECT_PAIR, {}),
            (Node.TUNING_DECISION, Option.ACCEPT, {}),
            (Node.FINAL_REPORT, Option.DONE, {}),
        ])
        out = env.run(agent, "跑一下九江镇 2025 年 7 月的全流程",
                      exec_mode="approval", pause_callback=pause)
        bubble = env.bubble()

        _assert(PAUSE_MARKER not in out, "重新选择影像组合后流程仍能跑完，没有卡住")
        _assert("回到影像组合选择，重新来一次" in bubble, "气泡说明这是阶段内回退")
        _assert("正在按新的条件重新规划" not in bubble,
                "没有走通用 replan 文案，证明没有调用规划 Agent 的 replan()")
        _assert("没能给出有效的新方案" not in out,
                "没有因为规划 Agent 被误触发、又拿不到新响应而报错")

        pair_payloads = [p for p in pause.seen if p.get("pairs")]
        _assert(len(pair_payloads) == 2,
                "配对选择被真正问了两次：第一次训练前、重选后再问一次（重新搜索了一次）")

        acq = registry.get("data_acquisition")
        _assert(len(acq.calls) == 4,
                f"data_acquisition 恰好被调用 4 次：搜索×2 + 下载×2（实际 {len(acq.calls)} 次）")
        _assert(acq.calls[0].get("start_date") == "2025-07-01"
                and acq.calls[2].get("start_date") == "2025-07-01",
                "两轮搜索用的是同一个时间范围，没有被规划 Agent 改写")

        nodes = [p.get("node") for p in pause.seen if isinstance(p, dict) and p.get("node")]
        _assert(nodes.count(Node.TUNING_DECISION) == 2,
                "调优决策节点被真正问了两次（重跑后训练完成又问了一次）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_chat_does_not_run_skills()
    test_multi_turn_never_wuhan()
    test_auto_full_workflow()
    test_approval_full_workflow()
    test_approval_high_accuracy_still_asks()
    test_pipeline_failure_blocks_training()
    test_missing_project_dir_guided()
    test_no_study_area_guided()
    test_feature_switch_off_uses_old_path()
    test_auto_tuning_rounds()
    test_reselect_pair_skips_replan()
    print("\n✅ 多角色路径端到端合成测试全部通过")
