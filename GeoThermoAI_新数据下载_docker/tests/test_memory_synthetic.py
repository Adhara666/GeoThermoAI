# -*- coding: utf-8 -*-
"""
记忆系统合成测试（无真实运行产物，验证 写入→检索→删除 闭环）

运行：python tests/test_memory_synthetic.py
覆盖：
- MemoryManager 初始化 + 领域知识幂等播种
- auto_save_experiment：experiments.json + ChromaDB 双写
- paused 覆盖（暂停后续跑完只留 success）
- enrich_prompt：检索注入
- delete_conversation / delete_project 级联
- Preferences 键值读写
"""

import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.memory import MemoryManager, SEED_ITEMS

SUCCESS_RECORD = {
    "schema_version": 1,
    "experiment_id": "exp_9f2c3a4b_001",
    "conv_id": "ab12cd34",
    "project_id": "9f2c3a4b0001",
    "region": "武汉市.geojson",
    "date_range": ["2024-07-01", "2024-07-31"],
    "pair": {"landsat_date": "2024-07-21", "sentinel2_date": "2024-07-22",
             "time_diff_days": 1, "landsat_cloud_cover": 12.5},
    "status": "success",
    "timestamp": "2026-08-05 10:00:00",
    "data_features": {"train_samples": 45678, "val_samples": 15226, "test_samples": 15226,
                      "dem_std": 120.0, "dem_range": 620.0, "ndvi_mean": 0.35,
                      "ndvi_std": 0.18, "lst_range": 17.0, "lst_std": 4.2},
    "model": "rf",
    "params": {"n_estimators": 300, "max_depth": 35, "min_samples_split": 16},
    "metrics": {"train": {"R2": 0.90, "RMSE": 1.50, "MAE": 1.10, "MB": 0.00},
                "test": {"R2": 0.87, "RMSE": 1.23, "MAE": 0.91, "MB": 0.12}},
    "feature_importance": [
        {"feature": "NDVI", "importance": 0.28},
        {"feature": "DEM", "importance": 0.23},
        {"feature": "NIR", "importance": 0.10},
    ],
    "closure": {"n_matched_cells": 373240,
                "metrics": {"MB_K": 0.05, "MAE_K": 0.40, "RMSE_K": 0.50, "R2": 0.995},
                "value_range": {"low_end_difference_K": -0.45, "high_end_difference_K": -0.58}},
    "train_time_seconds": 183.2,
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def test_memory_roundtrip():
    print("[1] 写入→检索→删除 闭环")
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))

        # 播种（幂等）
        mm.ensure_seeded()
        mm.ensure_seeded()  # 第二次应跳过
        seed_path = os.path.join(tmp, "memory", "knowledge_seed.json")
        _assert(os.path.exists(seed_path), "knowledge_seed.json 已落盘")
        _assert(mm._rag.count(knowledge=True) == len(SEED_ITEMS),
                f"global_knowledge 播种 {len(SEED_ITEMS)} 条")

        # 实验写入
        mm.auto_save_experiment("9f2c3a4b0001", dict(SUCCESS_RECORD))
        log = mm.experiment_log("9f2c3a4b0001")
        recent = log.get_recent(5)
        _assert(len(recent) == 1 and recent[0]["status"] == "success",
                "experiments.json 写入成功记录")
        _assert(mm._rag.count(project_id="9f2c3a4b0001") == 1,
                "ChromaDB project 写入实验段落")

        # 检索注入
        block = mm.enrich_prompt("9f2c3a4b0001", "武汉 RF 模型效果怎么样")
        _assert("领域知识参考" in block, "注入包含领域知识")
        _assert("历史最佳实验" in block and "R²=0.87" in block, "注入包含历史最佳实验")

        # 删除对话级联
        mm.delete_conversation("9f2c3a4b0001", "ab12cd34")
        _assert(log.count_by_conv("ab12cd34") == 0, "删除对话后 experiments.json 无残留")
        _assert(mm._rag.count(project_id="9f2c3a4b0001") == 0,
                "删除对话后 ChromaDB 无残留")

        # 删除项目级联
        mm.auto_save_experiment("9f2c3a4b0001", dict(SUCCESS_RECORD))
        mm.delete_project("9f2c3a4b0001")
        _assert(not os.path.exists(mm.project_memory_dir("9f2c3a4b0001")),
                "删除项目后 memory/projects 目录已清")
        _assert(mm._rag.count(project_id="9f2c3a4b0001") == 0,
                "删除项目后 Collection 已清")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_paused_overwrite():
    print("[2] paused 覆盖为 success")
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        paused = dict(SUCCESS_RECORD)
        paused["status"] = "paused"
        paused["conv_id"] = "conv9"
        mm.auto_save_experiment("p1", paused)
        log = mm.experiment_log("p1")
        _assert(log.all()[0]["status"] == "paused", "先写 paused")

        mm.auto_save_experiment("p1", dict(SUCCESS_RECORD, conv_id="conv9"))
        recs = log.all()
        _assert(len(recs) == 1 and recs[0]["status"] == "success",
                "恢复跑完后覆盖为 success（不重复）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preferences_and_failed():
    print("[3] 偏好读写 + 失败记录")
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        mm.set_preference("p2", "cloud_threshold", 40)
        _assert(mm.get_preference("p2", "cloud_threshold") == 40, "偏好写入/读取")

        failed = dict(SUCCESS_RECORD)
        failed.update(status="failed", conv_id="cf1",
                      failure_stage="rf_model", failure_message="模型训练失败: 内存不足")
        mm.auto_save_experiment("p2", failed)
        best = mm.experiment_log("p2").get_best()
        _assert(best is None, "失败记录不计入历史最佳")
        rec = mm.experiment_log("p2").all()[0]
        _assert(rec["failure_stage"] == "rf_model" and rec["status"] == "failed",
                "失败记录含 failure_stage/failure_message")
        # 失败段落组装含失败信息
        paragraph = mm._record_to_paragraph(rec)
        _assert("失败阶段" in paragraph and "模型训练失败" in paragraph,
                "RAG 段落包含失败原因")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_build_record():
    print("[4] _execute_plan 聚合判定（经合成 Skill 流程）")
    # 验证 exp_state 聚合逻辑：直接构造闭包数据来源不可行，
    # 这里只验证 memory_manager.auto_save_experiment 对缺字段记录不抛错
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        minimal = {"conv_id": "c", "project_id": "p", "status": "failed",
                   "failure_stage": "data_acquisition", "failure_message": "未找到影像配对"}
        mm.auto_save_experiment("p", minimal)
        _assert(mm.experiment_log("p").all()[0]["status"] == "failed",
                "最小失败记录可入库（缺字段不报错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_execute_plan_memory():
    """经 _execute_plan 验证：成功→success 记录；data_acquisition 失败→failed 记录并提前返回。"""
    print("[5] _execute_plan 聚合判定与记忆写入")
    from core.agent.geo_thermo_agent import GeoThermoAgent
    from core.ai_assistant import GeoThermoAI_Assistant
    from core.skills.skill_registry import SkillRegistry
    from core.skills.base_skill import BaseSkill, SkillParameter, SkillResult

    class _FakeSkill(BaseSkill):
        """合成 Skill：可配置返回成功/失败。"""
        def __init__(self, name, ok=True, message="ok"):
            self._name = name
            self._ok = ok
            self._msg = message
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
            data = {}
            if self._name == "data_acquisition":
                data = {
                    "image_pairs": [{"landsat_date": "2024-07-21", "sentinel2_date": "2024-07-22",
                                     "time_diff_days": 1}],
                    "landsat_count": 1, "sentinel_count": 1,
                }
            if self._name == "rf_model":
                data = {
                    "train_metrics": {"R2": 0.90, "RMSE": 1.5, "MAE": 1.1, "MB": 0.0},
                    "test_metrics": {"R2": 0.87, "RMSE": 1.23, "MAE": 0.91, "MB": 0.12},
                    "feature_importance": [{"feature": "NDVI", "importance": 0.28}],
                    "params": {"n_estimators": 300, "max_depth": 35},
                    "train_time_seconds": 3.2,
                }
            return SkillResult(success=self._ok, message=self._msg, data=data)

    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        assistant = GeoThermoAI_Assistant("", "", "", "", "openai")
        registry = SkillRegistry()
        registry.register(_FakeSkill("fake_step1", ok=True, message="第一步完成"))
        registry.register(_FakeSkill("fake_step2", ok=True, message="第二步完成"))
        registry.register(_FakeSkill("data_acquisition", ok=True, message="数据获取完成"))
        registry.register(_FakeSkill("rf_model", ok=True, message="模型训练完成"))
        registry.register(_FakeSkill("data_acquisition_fail", ok=False,
                                     message="数据获取失败: 网络错误"))
        agent = GeoThermoAgent(assistant, registry)
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        mm.ensure_seeded()

        # 场景 A：全流程成功
        plan_ok = {"steps": [
            {"skill": "fake_step1", "params": {}},
            {"skill": "fake_step2", "params": {}},
        ]}
        agent._execute_plan(plan_ok, conv_id="c1", project_id="p1", memory_manager=mm)
        recs = mm.experiment_log("p1").all()
        _assert(len(recs) == 1 and recs[0]["status"] == "success",
                "全流程成功 → 写入 success 记录")
        _assert(mm._rag.count(project_id="p1") == 1, "成功实验段落入 ChromaDB")

        # 场景 B：data_acquisition 失败 → failed 记录 + 提前返回
        plan_fail = {"steps": [
            {"skill": "data_acquisition_fail", "params": {"region": "x", "start_date": "2024-07-01",
                                                          "end_date": "2024-07-31"}},
            {"skill": "fake_step1", "params": {}},
        ]}
        out = agent._execute_plan(plan_fail, conv_id="c2", project_id="p1", memory_manager=mm)
        _assert("数据获取失败" in out, "data_acquisition 失败信息透出")
        recs2 = mm.experiment_log("p1").all()
        failed = [r for r in recs2 if r["conv_id"] == "c2"]
        _assert(len(failed) == 1 and failed[0]["status"] == "failed"
                and failed[0]["failure_stage"] == "data_acquisition_fail",
                "data_acquisition 失败 → 写入 failed 记录（failure_stage 定位）")

        # 场景 C：rf_model 成功 → 记录含模型指标（研究区目录指向空目录，避免被全局研究区替换）
        empty_study = os.path.join(tmp, "empty_study")
        os.makedirs(empty_study, exist_ok=True)
        plan_rf = {"steps": [
            {"skill": "data_acquisition", "params": {"region": "r.geojson",
                                                     "start_date": "2024-07-01", "end_date": "2024-07-31"}},
            {"skill": "rf_model", "params": {"n_estimators": 300}},
        ]}
        agent._execute_plan(plan_rf, conv_id="c3", project_id="p2", memory_manager=mm,
                            study_areas_dir=empty_study)
        rec3 = mm.experiment_log("p2").all()[0]
        _assert(rec3["status"] == "success" and rec3["metrics"]["test"]["R2"] == 0.87,
                "rf_model 结果（test_metrics/feature_importance）写入记录")
        _assert("特征重要性" in mm._record_to_paragraph(rec3), "段落含特征重要性")
        _assert(rec3["region"] == "r.geojson", "region 原样记录")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_memory_roundtrip()
    test_paused_overwrite()
    test_preferences_and_failed()
    test_agent_build_record()
    test_agent_execute_plan_memory()
    print("\n✅ 记忆系统合成测试全部通过")
