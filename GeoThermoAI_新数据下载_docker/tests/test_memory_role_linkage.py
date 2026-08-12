# -*- coding: utf-8 -*-
"""
记忆系统角色联动合成测试

运行：python tests/test_memory_role_linkage.py
覆盖：
- E 系列在**已有 collection** 上能被增量播种
- 种子文件按 schema_version 刷新
- where 过滤生效，过滤异常时安全退回
- enrich_for_role 四个角色各自的检索范围
- enrich_prompt 保持原样（现有测试断言的两个小节仍在）
- WorkflowExperience 三个写入条件
- 工作流双写与按区域检索
- 会话状态与工作流的级联删除
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.memory import MemoryManager, ROLE_RETRIEVAL, seed_data, workflow_experience
from core.memory.knowledge_eval import EVAL_IDS, EVAL_SEED_ITEMS

SUCCESS_RECORD = {
    "schema_version": 1,
    "experiment_id": "exp_p1_001",
    "conv_id": "cv1",
    "project_id": "p1",
    "region": "九江镇.geojson",
    "date_range": ["2025-07-01", "2025-07-31"],
    "status": "success",
    "timestamp": "2026-08-07 10:00:00",
    "model": "rf",
    "params": {"n_estimators": 400, "max_depth": 30},
    "metrics": {"train": {"R2": 0.90}, "test": {"R2": 0.87, "RMSE": 1.23}},
    "feature_importance": [{"feature": "NDVI", "importance": 0.28}],
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def _mm(tmp):
    return MemoryManager(memory_root=os.path.join(tmp, "memory"))


def test_incremental_seeding():
    print("[1] E 系列在已有 collection 上增量播种")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        # 先只播 K 系列，模拟「老环境已有 18 条」
        k_items = [i for i in seed_data.SEED_ITEMS if i["id"].startswith("K")]
        mm._rag.save_knowledge(k_items)
        _assert(mm._rag.count(knowledge=True) == len(k_items),
                f"老环境先有 {len(k_items)} 条 K 系列知识")

        # 再走正常播种：E 系列应被补进去（改造前会因 count>0 整体跳过）
        mm.ensure_seeded()
        total = mm._rag.count(knowledge=True)
        _assert(total == len(seed_data.SEED_ITEMS),
                f"增量补齐到 {len(seed_data.SEED_ITEMS)} 条（新增 {len(EVAL_SEED_ITEMS)} 条 E 系列）")

        # 重复播种不产生重复条目
        mm.ensure_seeded()
        _assert(mm._rag.count(knowledge=True) == total, "重复播种保持幂等，不重复写入")

        got = mm._rag.global_collection().get(ids=list(EVAL_IDS))
        _assert(len(got.get("ids", [])) == len(EVAL_IDS), "E01–E09 全部入库")
        metas = {m["kid"]: m for m in got.get("metadatas", [])}
        _assert(metas["E03"]["domain"] == "eval", "E 系列带 domain=eval 标量字段")
        _assert(metas["E03"]["kind"] == "knowledge", "知识条目带 kind=knowledge")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_seed_file_schema_refresh():
    print("[2] 种子文件按 schema_version 刷新")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        seed_path = os.path.join(tmp, "memory", "knowledge_seed.json")
        os.makedirs(os.path.dirname(seed_path), exist_ok=True)
        with open(seed_path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "items": []}, f)

        mm.ensure_seeded()
        with open(seed_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _assert(data["schema_version"] == seed_data.SEED_SCHEMA_VERSION,
                f"旧版本种子文件被刷新到 v{seed_data.SEED_SCHEMA_VERSION}")
        _assert(len(data["items"]) == len(seed_data.SEED_ITEMS), "条目数与代码内一致")
        _assert(seed_data.SEED_SCHEMA_VERSION == 3, "schema_version 已升到 3")

        # 已是当前版本时不重复改写
        before = os.path.getmtime(seed_path)
        mm.ensure_seeded()
        _assert(os.path.getmtime(seed_path) == before, "版本一致时不重复改写文件")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_where_filter():
    print("[3] where 过滤生效")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        mm.ensure_seeded()

        hits = mm._rag.search_knowledge("评估协议与闭合口径", n=9,
                                        where={"kid": {"$in": list(EVAL_IDS)}})
        _assert(hits, "按 kid 过滤能取到结果")
        kids = {h["metadata"].get("kid") for h in hits}
        _assert(kids.issubset(set(EVAL_IDS)), f"过滤后只剩 E 系列（实际 {sorted(kids)}）")

        hits = mm._rag.search_knowledge("数据源与影像配对", n=5, where={"domain": "data"})
        domains = {h["metadata"].get("domain") for h in hits}
        _assert(domains == {"data"}, f"按 domain 过滤只剩数据类（实际 {domains}）")

        unfiltered = mm._rag.search_knowledge("数据源与影像配对", n=5)
        _assert(len(unfiltered) == 5, "不带 where 时行为与改造前一致")

        # 过滤语法异常时安全退回无过滤（不因为过滤把结果清空）
        weird = mm._rag.search_knowledge("数据源", n=3,
                                        where={"不存在的字段": {"$bad": 1}})
        _assert(isinstance(weird, list), "非法过滤条件不抛异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_enrich_for_role():
    print("[4] enrich_for_role 四个角色的检索范围")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        mm.ensure_seeded()
        mm.auto_save_experiment("p1", dict(SUCCESS_RECORD))
        mm.set_preference("p1", "cloud_threshold", 40)

        _assert(set(ROLE_RETRIEVAL) == {"planner", "data", "train", "eval"},
                "四个角色都有检索配置")

        planner = mm.enrich_for_role("p1", "planner", "九江镇 2025 年 7 月 全流程")
        _assert("领域知识参考" in planner, "规划角色注入领域知识")
        _assert("历史最佳实验" in planner, "规划角色注入历史最佳实验")
        _assert("用户偏好" in planner and "cloud_threshold" in planner,
                "规划角色注入用户偏好")

        data_block = mm.enrich_for_role("p1", "data", "影像配对 云量 数据源")
        _assert("领域知识参考" in data_block, "数据角色注入领域知识")
        _assert("历史最佳实验" not in data_block, "数据角色不注入历史最佳实验")

        train_block = mm.enrich_for_role("p1", "train", "调参 决定系数")
        _assert("历史最佳实验" in train_block, "训练角色注入历史最佳参数")

        eval_block = mm.enrich_for_role("p1", "eval", "闭合 精度 解读")
        _assert("领域知识参考" in eval_block, "评估角色注入领域知识")
        _assert("闭合不是精度" in eval_block or "算术均值闭合" in eval_block,
                "评估角色注入到了 E 系列的口径条款")
        _assert("历史最佳实验" not in eval_block, "评估角色不注入历史最佳实验")

        unknown = mm.enrich_for_role("p1", "不存在的角色", "随便问问")
        _assert("领域知识参考" in unknown, "未知角色退化为 enrich_prompt")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_enrich_prompt_unchanged():
    print("[5] enrich_prompt 保持原样（现有测试的断言仍成立）")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        mm.ensure_seeded()
        mm.auto_save_experiment("p1", dict(SUCCESS_RECORD))
        block = mm.enrich_prompt("p1", "九江镇 RF 模型效果怎么样")
        _assert("领域知识参考" in block, "仍含「领域知识参考」小节")
        _assert("历史最佳实验" in block and "R²=0.87" in block, "仍含「历史最佳实验」小节")
        _assert("用户偏好" not in block, "enrich_prompt 不新增小节（保持原样）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_workflow_write_conditions():
    print("[6] 工作流写回的三个条件")
    ok = dict(status="success", eval_passed=True, test_r2=0.87)
    _assert(workflow_experience.should_write(**ok), "三个条件都满足 → 写入")
    _assert(not workflow_experience.should_write(**{**ok, "status": "failed"}),
            "状态不是 success → 不写")
    _assert(not workflow_experience.should_write(**{**ok, "eval_passed": False}),
            "评估反思降级过 → 不写")
    _assert(not workflow_experience.should_write(**{**ok, "test_r2": 0.74}),
            "决定系数 0.74 低于门槛 → 不写")
    _assert(workflow_experience.should_write(**{**ok, "test_r2": 0.75}),
            "决定系数刚好 0.75（K24 合格线）→ 写入")
    _assert(not workflow_experience.should_write(**{**ok, "test_r2": None}),
            "缺决定系数 → 不写")
    _assert(workflow_experience.WORKFLOW_MIN_R2 == 0.75, "门槛为 0.75")


def test_workflow_storage():
    print("[7] 工作流双写与按区域检索")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        mm.ensure_seeded()
        record = workflow_experience.build_record(
            project_id="p1", experiment_id="exp_p1_001", conv_id="cv1",
            region="九江镇.geojson", date_range=["2025-07-01", "2025-07-31"],
            exec_mode="approval",
            pair={"landsat_date": "2025-07-17", "sentinel2_date": "2025-07-18",
                  "time_diff_days": 1, "score": 0.91, "selected_by": "user"},
            final_params={"n_estimators": 400, "max_depth": 30},
            tuning_trace=[{"round": 0, "test_r2": 0.79, "rmse": 1.51},
                          {"round": 1, "test_r2": 0.87, "rmse": 1.23}],
            metrics={"test_r2": 0.87, "rmse": 1.23, "closure_mb": 0.05,
                     "closure_mae": 0.40},
            approval_choices={"pair_selection": "0", "tuning_decision": "ai_tune"},
        )
        _assert(record["workflow_id"].startswith("wf_"), "工作流 id 前缀正确")
        _assert(record["tuning_rounds"] == 2, "调优轮数按轨迹长度自动填写")

        mm.save_workflow("p1", record)
        stored = mm.workflows("p1").all()
        _assert(len(stored) == 1 and stored[0]["region"] == "九江镇.geojson",
                "workflows.json 写入成功")

        hits = mm.search_workflows("p1", "九江镇 成功流程")
        _assert(hits, "ChromaDB 段落可被检索")
        _assert(all(h["metadata"].get("kind") == "workflow" for h in hits),
                "按 kind=workflow 过滤只返回工作流段落")

        # 实验段落带 kind=experiment，与工作流段落区分开
        mm.auto_save_experiment("p1", dict(SUCCESS_RECORD))
        exp_hits = mm._rag.search_for_agent("p1", "九江镇 实验", n=5,
                                            where={"kind": "experiment"})
        _assert(exp_hits and all(h["metadata"].get("kind") == "experiment"
                                 for h in exp_hits),
                "实验段落带 kind=experiment，可与工作流分流检索")

        best = mm.best_workflow("p1", "九江镇")
        _assert(best and best["final_params"]["n_estimators"] == 400,
                "按区域取到最佳可复用流程的参数")
        _assert(mm.best_workflow("p1", "武汉") is None, "其它区域取不到")

        block = mm.enrich_for_role("p1", "planner", "九江镇 再跑一次")
        _assert("可复用的成功流程" in block, "规划角色能看到可复用流程")
        _assert("n_estimators" in block, "可复用流程带最终参数")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cascade_delete():
    print("[8] 会话状态与工作流的级联删除")
    tmp = tempfile.mkdtemp(prefix="mem_role_")
    try:
        mm = _mm(tmp)
        mm.session_state("cv1", "p1").set_slots(
            {"region_name": {"value": "九江镇", "source": "user"}})
        mm.auto_save_experiment("p1", dict(SUCCESS_RECORD))
        mm.save_workflow("p1", workflow_experience.build_record(
            project_id="p1", experiment_id="e1", conv_id="cv1", region="九江镇.geojson",
            metrics={"test_r2": 0.87}))
        mm.save_workflow("p1", workflow_experience.build_record(
            project_id="p1", experiment_id="e2", conv_id="cv2", region="南海区.geojson",
            metrics={"test_r2": 0.83}))
        _assert(len(mm.workflows("p1").all()) == 2, "两条工作流已写入")

        mm.delete_conversation("p1", "cv1")
        remaining = mm.workflows("p1").all()
        _assert(len(remaining) == 1 and remaining[0]["conv_id"] == "cv2",
                "删除对话时级联删除该对话的工作流经验")
        _assert(mm.experiment_log("p1").count_by_conv("cv1") == 0,
                "实验记录同步删除")
        _assert(mm.session_state("cv1", "p1").load()["slots"] == {},
                "会话状态同步删除")

        mm.delete_project("p1")
        _assert(mm.workflows("p1").all() == [], "删除项目后工作流经验清空")
        _assert(not os.path.exists(mm.project_memory_dir("p1")),
                "项目记忆目录已清")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_seed_domains():
    print("[9] 知识条目的 domain 字段完整")
    missing = [i["id"] for i in seed_data.SEED_ITEMS if not i.get("domain")]
    _assert(missing == [], f"所有条目都有 domain（缺失：{missing}）")
    invalid = [i["id"] for i in seed_data.SEED_ITEMS
               if i["domain"] not in seed_data.DOMAINS]
    _assert(invalid == [], f"domain 取值合法（非法：{invalid}）")
    _assert(len(seed_data.items_by_domain("eval")) >= len(EVAL_SEED_ITEMS),
            "评估类知识至少包含 E 系列")
    _assert(seed_data.item_by_id("E03")["topic"].startswith("10 米与 30 米"),
            "可按 id 精确取条目")
    _assert(seed_data.item_by_id("不存在") == {}, "取不到时返回空字典")


if __name__ == "__main__":
    test_incremental_seeding()
    test_seed_file_schema_refresh()
    test_where_filter()
    test_enrich_for_role()
    test_enrich_prompt_unchanged()
    test_workflow_write_conditions()
    test_workflow_storage()
    test_cascade_delete()
    test_seed_domains()
    print("\n✅ 记忆系统角色联动测试全部通过")
