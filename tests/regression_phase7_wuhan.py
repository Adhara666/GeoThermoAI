# -*- coding: utf-8 -*-
"""武汉冻结输入经当前调度器全链复跑，并验收阶段七记忆写回。

输出目录必须不存在，脚本绝不覆盖历史验收结果。科学等价性随后使用
``tests/compare_runs.py`` 与阶段四的同输入输出逐项比较。
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from core.deployment import DeploymentConfig
    from core.memory import MemoryManager, ProjectionExecutor
    from core.planning import compile_task
    from core.scheduling.resources import Budget
    from core.scheduling.scheduler import Scheduler, rows
    from core.scheduling.transfer import file_hash
    from core.state_kernel import StateStore
    from core.state_kernel.intake import _ensure_conversation
    from core.state_kernel.tasks import create_task

    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--region", default=str(ROOT / "config" / "study_areas" /
                                                 "武汉市_市.geojson"))
    args = parser.parse_args()
    acceptance_root = Path(args.out).resolve()
    acceptance_root.mkdir(parents=True, exist_ok=False)
    data_root = acceptance_root / "data"
    projects_root = acceptance_root / "projects"

    # Use the same public deployment parser as server startup. This makes the
    # Wuhan run cover path writability and budget configuration as well.
    process_env = dict(os.environ)
    process_env.update({
        "GTAI_DATA_ROOT": str(data_root),
        "WORKSPACE_ROOT": str(projects_root),
        "GTAI_TEMP_ROOT": str(data_root / "tmp"),
        "GTAI_STATE_DB": str(data_root / "state_kernel" / "ledger.sqlite3"),
    })
    deployment = DeploymentConfig.load(project_root=ROOT, env=process_env)
    deployment.prepare(adopt_legacy=False)
    deployment.apply_process_defaults()

    raw = Path(args.raw_dir)
    paths = {
        "landsat_path": str(raw / "landsat_lst_20240721.tif"),
        "qa_path": str(raw / "landsat_qa_pixel_20240721.tif"),
        "sentinel2_path": str(raw / "sentinel2_bands_20240722.tif"),
        "scl_path": str(raw / "sentinel2_scl_20240722.tif"),
        "dem_path": str(raw / "dem.tif"),
    }
    for name, path in paths.items():
        if not Path(path).is_file():
            raise FileNotFoundError(f"冻结输入缺失 {name}: {path}")
    hashes = {key: file_hash(path) for key, path in paths.items()}
    settings = json.loads((ROOT / "config" / "settings.json").read_text(
        encoding="utf-8"))
    settings.setdefault("agent", {})["default_exec_mode"] = "auto"
    settings.setdefault("processing", {}).update(
        seed=42, guard_buffer_m=100.0, tcr_mode="block_constant")
    settings.setdefault("model", {}).update(max_features=.5, random_state=42)
    settings["_execution"] = {
        "raw_paths": paths,
        "raw_hashes": hashes,
        "scene_ids": {
            "landsat": ["LC09_L2SP_123038_20240721_02_T1",
                         "LC09_L2SP_123039_20240721_02_T1"],
            "sentinel2": ["T49RGQ", "T49RGP", "T50RKU", "T50RKV"],
        },
    }

    store = StateStore(deployment.state_db)
    user_id, project_id, legacy_conv = "wuhan_regression", "武汉阶段七", "wuhan"

    def create(conn):
        conv = _ensure_conversation(conn, user_id, project_id, legacy_conv)
        fields = {key: {"value": value, "source": "user", "confirmed": True,
                        "evidence": "武汉阶段七冻结输入回归"}
                  for key, value in {
                      "region": "武汉市",
                      "time": {"start": "2024-07-01", "end": "2024-07-31"},
                      "product_mode": "pair",
                  }.items()}
        fields["region"]["detail"] = {
            "path": args.region, "content_hash": file_hash(args.region)}
        return create_task(
            conn, user_id=user_id, project_id=project_id,
            conversation_id=conv, capability="full_lst",
            slots={"fields": fields, "negations": []},
            label="武汉阶段七冻结输入回归", summary_status="ready")

    task_id = store.submit_write(create)
    project_view = projects_root / user_id / "workspace" / "武汉阶段七"
    project_view.mkdir(parents=True, exist_ok=True)
    run = compile_task(
        store, task_id=task_id, expected_task_version=1, settings=settings,
        project_dir=str(project_view), run_label="武汉阶段七冻结输入回归")
    execution_root = deployment.state_db.parent / "executions"
    execution_root.mkdir(parents=True, exist_ok=True)
    budget = Budget.detect(execution_root, deployment.budget_environment(process_env))
    scheduler = Scheduler(store, execution_root, budget=budget,
                          users_root=deployment.users_root)
    managers = {}

    def memory_for(uid: str):
        manager = managers.get(uid)
        if manager is None:
            manager = MemoryManager(
                str(deployment.users_root / uid / "memory"),
                str(ROOT / "models" / "bge-small-zh-v1.5"))
            manager.ensure_seeded()
            managers[uid] = manager
        return manager

    projection = ProjectionExecutor(
        store, memory_for,
        max_attempts=deployment.memory_writeback_attempts,
        base_seconds=deployment.memory_writeback_base_seconds,
        poll_seconds=deployment.memory_writeback_poll_seconds)
    logging.basicConfig(level=logging.INFO)
    print(json.dumps({"run_id": run["run_id"], "input_hashes": hashes,
                      "deployment": deployment.public_summary()},
                     ensure_ascii=False), flush=True)
    projection.start()
    scheduler.start()
    last = None
    start = time.monotonic()
    try:
        while time.monotonic() - start < 14400:
            graph = store.read(lambda c: rows(
                c, "SELECT node_type,status,wait_reason,result FROM nodes"
                   " WHERE run_id=? ORDER BY exec_order", (run["run_id"],)))
            summary = [(node["node_type"], node["status"], node["wait_reason"])
                       for node in graph]
            if summary != last:
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                last = summary
            if any(node["status"] == "failed" for node in graph):
                raise RuntimeError("武汉调度复跑有失败节点")
            task_status = store.read(lambda c: c.execute(
                "SELECT summary_status FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
            if graph and all(node["status"] == "succeeded" for node in graph) \
                    and task_status == "completed":
                break
            if scheduler.last_error:
                raise RuntimeError(scheduler.last_error)
            time.sleep(1)
        else:
            raise TimeoutError("武汉回归超过四小时")

        # Scientific completion is already authoritative. Wait separately for
        # memory projection and fail this acceptance run if a target exhausts retries.
        deadline = time.monotonic() + 900
        memory_targets = {"experiment_json", "experiment_vector",
                          "workflow_json", "workflow_vector"}
        jobs = []
        while time.monotonic() < deadline:
            jobs = store.read(lambda c: rows(
                c, "SELECT target,status,attempts_count,last_error,result"
                   " FROM projection_jobs WHERE run_id=? ORDER BY target",
                (run["run_id"],)))
            memory_jobs = [job for job in jobs if job["target"] in memory_targets]
            if len(memory_jobs) == 4 and all(
                    job["status"] in ("succeeded", "skipped", "failed")
                    for job in memory_jobs):
                break
            time.sleep(1)
        memory_jobs = [job for job in jobs if job["target"] in memory_targets]
        if len(memory_jobs) != 4 or any(job["status"] != "succeeded"
                                        for job in memory_jobs):
            raise RuntimeError("武汉记忆写回未全部成功：" + json.dumps(
                memory_jobs, ensure_ascii=False))

        graph = store.read(lambda c: rows(
            c, "SELECT node_type,status,result FROM nodes WHERE run_id=?"
               " ORDER BY exec_order", (run["run_id"],)))
        final = json.loads(graph[-1]["result"])["context"]
        manager = managers[user_id]
        experiments = manager.experiment_log(project_id).all()
        workflows = manager.workflows(project_id).all()
        vector_ids = manager._rag.project_collection(project_id).get(
            include=[]).get("ids", [])
        result = {
            "schema_version": 1,
            "workspace": final["workspace"],
            "comparison_root": str(execution_root / "runs" / run["run_id"] /
                                   "committed"),
            "task_id": task_id,
            "run_id": run["run_id"],
            "elapsed_seconds": time.monotonic() - start,
            "input_hashes": hashes,
            "resources": scheduler.ledger.snapshot(),
            "deployment": deployment.public_summary(),
            "projection_jobs": jobs,
            "memory": {
                "experiments": len(experiments),
                "workflows": len(workflows),
                "vector_ids": sorted(vector_ids),
                "experiment_id": experiments[-1].get("experiment_id"),
                "workflow_id": workflows[-1].get("workflow_id"),
                "test_r2": (workflows[-1].get("metrics") or {}).get("test_r2"),
            },
        }
        (acceptance_root / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("武汉阶段七全链与记忆写回完成", flush=True)
        print(json.dumps(result["memory"], ensure_ascii=False), flush=True)
        return 0
    finally:
        projection.close()
        scheduler.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
