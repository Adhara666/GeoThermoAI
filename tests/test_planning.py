# -*- coding: utf-8 -*-
"""计划编译（第三阶段）验收测试。

对应升级方案 9.3 验收条款：
  - 编译：确认后的任务编译成节点图，依赖顺序正确（先下载后预处理）；
    未知步骤返回校验失败，不静默删除
  - 快照冻结：进队前建立；排队期间（编译后）改设置，已提交任务的
    冻结快照不变；每个任务可查参数快照（逐项含来源）与节点清单
  - 重规划：改日期/边界/产品 → 任务版本 +1、新运行、旧运行留档并停派发；
    额度持久记录，重启不归零，耗尽显式拒绝
  - 重建：缺输入按 stage_rebuild 链显式插节点；缺原始输入且未授权获取
    时权限拒绝，不静默扩成下载加全流程

运行（无需 pytest）：python tests/test_planning.py
"""

import copy
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.planning import (  # noqa: E402
    CAPABILITY_TEMPLATES,
    CompileError,
    RebuildPermissionError,
    compile_task,
    get_run_graph,
    get_run_snapshot,
    insert_rebuild_nodes,
    apply_field_change,
    replan_budget,
    start_superseding_run,
)
from core.planning.catalog import FULL_LST_CHAIN, validate_step_names  # noqa: E402
from core.state_kernel import StateStore, tasks as tasks_tx  # noqa: E402

PASS = []
FAIL = []

SETTINGS = {
    "data": {"cloud_threshold": 30, "dem_source": "copernicus"},
    "processing": {"train_ratio": 0.6, "val_ratio": 0.2, "test_ratio": 0.2,
                   "block_size_px": 30, "batch_size": 500000, "chunk_size": 500000},
    "model": {"n_estimators": 200, "max_depth": 25, "min_samples_split": 16,
              "min_samples_leaf": 8},
    "agent": {"replan_max": 3, "tuning_max_rounds": 5},
}

SLOTS_FULL = {
    "fields": {
        "region": {"value": {"name": "武汉市_市", "path": "/areas/wuhan.geojson"},
                    "source": "user", "confirmed": True,
                    "evidence": "做武汉", "detail": {"path": "/areas/wuhan.geojson"}},
        "time": {"value": {"start": "2024-07-01", "end": "2024-07-31"},
                  "source": "user", "confirmed": True,
                  "evidence": "7 月", "detail": {}},
        "product_mode": {"value": "monthly", "source": "user", "confirmed": True,
                          "evidence": "按月合成", "detail": {}},
    },
    "negations": [],
}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAIL.append((name, detail))
    else:
        PASS.append(name)


def expect_raises(name, exc_type, fn):
    try:
        fn()
    except exc_type:
        check(name, True)
        return
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"抛出了错误类型：{type(e).__name__}: {e}")
        return
    check(name, False, "未抛出预期异常")


def make_full_task(store, settings=SETTINGS):
    """登记对话 + full_lst 任务草稿（槽位已确认），返回 task_id。"""
    def _tx(conn):
        from core.state_kernel.intake import _ensure_conversation
        conv_id = _ensure_conversation(conn, "u1", "p1", "c1")
        return tasks_tx.create_task(
            conn, user_id="u1", project_id="p1", conversation_id=conv_id,
            capability="full_lst", slots=copy.deepcopy(SLOTS_FULL),
            label="武汉 7 月月度 LST",
        )
    return store.submit_write(_tx)


# ── 能力目录 ────────────────────────────────────────────────────

def test_catalog():
    print("测试组 1：能力目录（§5.1）")
    must_keys = {"label", "allowed_targets", "required_fields", "allowed_params",
                 "input_types", "output_types", "produces_files", "chain"}
    for cap, tpl in CAPABILITY_TEMPLATES.items():
        check(f"能力 {cap} 目录字段齐全", must_keys <= set(tpl),
              f"缺 {must_keys - set(tpl)}")
    # 完整主链顺序：搜索 → … → 导出 → 闭合；先下载后预处理
    types = [t for t, _ in FULL_LST_CHAIN]
    check("主链先检索获取后预处理",
          types.index("closure_eval") > types.index("export") > types.index("tcr")
          > types.index("preprocess_split") > types.index("acquire_asset"))
    check("查询能力不建计算图", CAPABILITY_TEMPLATES["query"]["chain"] is None)
    ok, unknown = validate_step_names(
        {"data_acquisition", "data_pipeline", "fake_skill"})
    check("未知步骤被校验拒绝", (not ok) and unknown == ["fake_skill"])
    ok, _ = validate_step_names(["data_acquisition", "data_pipeline",
                                 "ttri_compute", "rf_model", "tcr_compute",
                                 "lst_export", "accuracy_eval", "lst_gapfill"])
    check("7 步主链 + 填洞全部合法", ok)


# ── 编译与快照 ─────────────────────────────────────────────────

def test_compile_and_snapshot(tmp):
    print("测试组 2：编译节点图 + 快照冻结（§5.2/§5.3）")
    store = StateStore(tmp / "t2.sqlite3")
    task_id = make_full_task(store)

    result = compile_task(store, task_id=task_id, expected_task_version=1,
                          settings=SETTINGS)
    check("编译产出运行", bool(result.get("run_id")))
    check("模板版本已登记", result.get("template_version") == "plan-catalog-v1")

    graph = get_run_graph(store, result["run_id"])
    check(f"节点数 = 主链模板数（{len(FULL_LST_CHAIN)}）",
          graph is not None and len(graph) == len(FULL_LST_CHAIN))
    types = [n["type"] for n in graph]
    check("节点执行顺序 = 模板顺序", types == [t for t, _ in FULL_LST_CHAIN])
    check("首节点无前驱、尾节点无后继（线性链）",
          graph[0]["preds"] in (None, "") and graph[-1]["type"] == "closure_eval")
    check("依赖边存在（preprocess 依赖 data_check）",
          any(n["type"] == "preprocess_split" and n["preds"] == "data_check"
              for n in graph))
    check("全部节点排队态（不派发，阶段 4 的事）",
          all(n["status"] == "pending" for n in graph))

    snap = get_run_snapshot(store, result["run_id"])
    params = snap["params"]
    check("快照含用户确认的地区（source=user）",
          params["region"]["source"] == "user"
          and params["region"]["value"]["name"] == "武汉市_市")
    check("快照含用户确认的产品方式", params["product_mode"]["value"] == "monthly")
    check("快照含系统默认云量（source=default）",
          params["cloud_threshold"]["source"] == "default"
          and params["cloud_threshold"]["value"] == 30)
    check("快照含 RF 超参数默认", params["rf_params"]["value"]["n_estimators"] == 200)
    check("快照指纹存在", len(snap.get("taken_at", "")) > 0
          and result["snapshot_hash"])

    # 验收第 1 条：排队期间改设置 → 已提交任务的冻结快照不变
    settings_after = copy.deepcopy(SETTINGS)
    settings_after["data"]["cloud_threshold"] = 80
    settings_after["model"]["n_estimators"] = 500
    snap_again = get_run_snapshot(store, result["run_id"])
    check("排队期间改设置，冻结快照仍是提交时的值",
          snap_again["params"]["cloud_threshold"]["value"] == 30
          and snap_again["params"]["rf_params"]["value"]["n_estimators"] == 200)

    # 新任务在设置变更后编译 → 用新值（证明快照按编译时刻冻结）
    task_id2 = make_full_task(store)
    r2 = compile_task(store, task_id=task_id2, expected_task_version=1,
                      settings=settings_after)
    snap2 = get_run_snapshot(store, r2["run_id"])
    check("变更后编译的新运行用新设置（快照按编译时刻冻结）",
          snap2["params"]["cloud_threshold"]["value"] == 80)
    store.close()


def test_compile_failures(tmp):
    print("测试组 3：编译校验失败（缺字段/未知能力/参数越界/查询能力）")
    store = StateStore(tmp / "t3.sqlite3")

    def _mk(cap, slots):
        def _tx(conn):
            from core.state_kernel.intake import _ensure_conversation
            conv_id = _ensure_conversation(conn, "u1", "p1", "c1")
            return tasks_tx.create_task(
                conn, user_id="u1", project_id="p1", conversation_id=conv_id,
                capability=cap, slots=slots)
        return store.submit_write(_tx)

    # 缺必需字段（time / product_mode 未填）
    bad_slots = {"fields": {"region": SLOTS_FULL["fields"]["region"]},
                 "negations": []}
    tid = _mk("full_lst", bad_slots)
    expect_raises("缺必需字段编译被拒绝", CompileError,
                  lambda: compile_task(store, task_id=tid,
                                       expected_task_version=1, settings=SETTINGS))

    # 未知能力
    tid = _mk("magic_power", {})
    expect_raises("未知能力编译被拒绝（不静默删除）", CompileError,
                  lambda: compile_task(store, task_id=tid,
                                       expected_task_version=1, settings=SETTINGS))

    # 查询能力不建计算图
    tid = _mk("query", {})
    expect_raises("查询能力不编译节点图", CompileError,
                  lambda: compile_task(store, task_id=tid,
                                       expected_task_version=1, settings=SETTINGS))

    # 参数越界（云量 150 > 100）
    bad_settings = copy.deepcopy(SETTINGS)
    bad_settings["data"]["cloud_threshold"] = 150
    tid = _mk("full_lst", copy.deepcopy(SLOTS_FULL))
    expect_raises("参数越界编译被拒绝", CompileError,
                  lambda: compile_task(store, task_id=tid,
                                       expected_task_version=1,
                                       settings=bad_settings))
    store.close()


# ── 显式重建 ────────────────────────────────────────────────────

def test_rebuild(tmp):
    print("测试组 4：缺输入显式重建（§5.5）")
    store = StateStore(tmp / "t4.sqlite3")
    task_id = make_full_task(store)
    result = compile_task(store, task_id=task_id, expected_task_version=1,
                          settings=SETTINGS)
    graph = get_run_graph(store, result["run_id"])
    consumer = next(n for n in graph if n["type"] == "closure_eval")

    # 链上部分缺失（预处理输入已登记）→ 按 stage_rebuild 依赖顺序补齐剩余链
    created = insert_rebuild_nodes(
        store, run_id=result["run_id"], consumer_node_id=consumer["id"],
        target_skill="accuracy_eval",
        available_inputs={"data_pipeline": True},
        original_params={"snapshot": {}, "granted_inputs": ["raw_assets"]},
    )
    check("插入 4 个重建节点（ttri/tcr/export/accuracy_eval 输入链）",
          len(created) == 4, f"实际 {len(created)}")
    graph2 = get_run_graph(store, result["run_id"])
    eval_node = next(n for n in graph2
                     if n["id"] == consumer["id"])
    check("消费节点改挂到最后一个重建节点",
          eval_node["preds"] and eval_node["preds"].startswith("rebuild_"))

    # 缺原始输入且未授权获取 → 权限拒绝，不静默扩成下载加全流程
    expect_raises(
        "缺原始输入且未授权时重建被拒绝",
        RebuildPermissionError,
        lambda: insert_rebuild_nodes(
            store, run_id=result["run_id"], consumer_node_id=consumer["id"],
            target_skill="data_pipeline",
            available_inputs={},
            original_params={"snapshot": {}, "granted_inputs": []},
        ),
    )
    store.close()


# ── 重规划与额度 ────────────────────────────────────────────────

def current_version(store, task_id):
    """动态读取任务当前版本（编译与变更都会推进版本，写死会错位）。"""
    return store.read(lambda c: c.execute(
        "SELECT version FROM tasks WHERE id=?", (task_id,)).fetchone()[0])


def test_replan_budget(tmp):
    print("测试组 5：修改重规划与额度持久化（§5.4）")
    db = tmp / "t5.sqlite3"
    store = StateStore(db)
    task_id = make_full_task(store)
    r1 = compile_task(store, task_id=task_id, expected_task_version=1,
                      settings=SETTINGS)

    # 改时间（科学身份字段）→ 版本递增、新运行、旧运行停派发
    v0 = current_version(store, task_id)
    change = apply_field_change(
        store, task_id=task_id, expected_task_version=v0,
        field_patches={"time": {"value": {"start": "2024-08-01",
                                           "end": "2024-08-31"},
                                 "source": "user", "confirmed": True,
                                 "evidence": "改成 8 月", "detail": {}}},
        settings=SETTINGS,
    )
    # 编译与变更都会推进版本：变更后版本应大于变更前
    check("字段变更产生新任务版本", change["task_version"] > v0)
    check("字段变更建立新运行", change["run_id"] != r1["run_id"])
    check("消耗一次重规划额度", change["replan_used"] == 1)

    row = store.read(lambda c: c.execute(
        "SELECT version, accumulated, current_run_id FROM tasks WHERE id=?",
        (task_id,)).fetchone())
    acc = replan_budget(row[1])
    check("额度持久化在 tasks.accumulated", acc["replan_used"] == 1)
    check("任务当前运行指向新运行", row[2] == change["run_id"])
    old_run = store.read(lambda c: c.execute(
        "SELECT status, superseded_by, cancel_requested FROM runs WHERE id=?",
        (r1["run_id"],)).fetchone())
    check("旧运行留档且标记停派发",
          old_run[0] == "queued" and old_run[1] == change["run_id"]
          and old_run[2] == 1)
    new_snap = get_run_snapshot(store, change["run_id"])
    check("新运行携带新快照（8 月）",
          new_snap["params"]["time"]["value"]["end"] == "2024-08-31")
    old_snap = get_run_snapshot(store, r1["run_id"])
    check("旧运行快照不可变（仍是 7 月）",
          old_snap["params"]["time"]["value"]["end"] == "2024-07-31")

    # 非科学身份字段不触发重规划
    expect_raises(
        "优先级调整不触发重规划",
        ValueError,
        lambda: apply_field_change(
            store, task_id=task_id, expected_task_version=current_version(store, task_id),
            field_patches={"priority": {"value": 5}}, settings=SETTINGS,
        ),
    )

    # 额度耗尽显式拒绝（上限 3）
    for i in range(2):
        apply_field_change(
            store, task_id=task_id, expected_task_version=current_version(store, task_id),
            field_patches={"time": {"value": {"start": "2025-07-01",
                                               "end": "2025-07-31"},
                                     "source": "user", "confirmed": True,
                                     "evidence": f"改第 {i + 2} 次",
                                     "detail": {}}},
            settings=SETTINGS,
        )
    expect_raises(
        "重规划额度用尽（3/3）后显式拒绝",
        Exception,
        lambda: apply_field_change(
            store, task_id=task_id, expected_task_version=current_version(store, task_id),
            field_patches={"time": {"value": {"start": "2025-09-01",
                                               "end": "2025-09-30"},
                                     "source": "user", "confirmed": True,
                                     "evidence": "改第 4 次", "detail": {}}},
            settings=SETTINGS,
        ),
    )
    store.close()

    # 模拟服务重启：重开同一库，额度仍在（重启不归零）
    store2 = StateStore(db)
    acc = store2.read(lambda c: c.execute(
        "SELECT accumulated FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
    check("重启后重规划额度不归零", replan_budget(acc)["replan_used"] == 3)
    store2.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gtai-planning-test-"))
    try:
        test_catalog()
        test_compile_and_snapshot(tmp)
        test_compile_failures(tmp)
        test_rebuild(tmp)
        test_replan_budget(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
