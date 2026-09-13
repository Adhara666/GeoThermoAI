# -*- coding: utf-8 -*-
"""第七阶段记忆与部署专项验收（无需网络和大模型）。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.compatibility import import_legacy_conversations, import_legacy_results
from core.deployment import DeploymentConfig
from core.memory.experiment_log import ExperimentLog
from core.memory.projection import ProjectionExecutor, build_memory_records
from core.memory.rag_store import project_collection_name
from core.memory.workflow_experience import WorkflowExperience
from core.planning import compile_task
from core.runtime_cache import BoundedTTLCache
from core.scheduling.resources import Budget, GIB, MIB
from core.scheduling.scheduler import Scheduler
from core.state_kernel import SCHEMA_VERSION, StateStore
from core.state_kernel.intake import _ensure_conversation
from core.state_kernel.tasks import create_task


class FakeMemory:
    def __init__(self):
        self.calls = []

    def _save(self, target, project, record):
        self.calls.append((target, project, record))
        return {"id": record.get("experiment_id") or record.get("workflow_id")}

    def write_experiment_json(self, p, r):
        return self._save("experiment_json", p, r)

    def write_experiment_vector(self, p, r):
        return self._save("experiment_vector", p, r)

    def write_workflow_json(self, p, r):
        return self._save("workflow_json", p, r)

    def write_workflow_vector(self, p, r):
        return self._save("workflow_vector", p, r)


def seed_completed_science_run(store: StateStore, root: Path):
    def create(conn):
        conv = _ensure_conversation(conn, "user7", "武汉项目", "conv7")
        task_id = create_task(
            conn, user_id="user7", project_id="武汉项目", conversation_id=conv,
            capability="full_lst", label="武汉七月地表温度",
            slots={"fields": {
                "region": {"value": "武汉市", "source": "user", "confirmed": True},
                "time": {"value": {"start": "2024-07-01", "end": "2024-07-31"},
                         "source": "user", "confirmed": True},
                "product_mode": {"value": "pair", "source": "user", "confirmed": True},
            }, "negations": []})
        return task_id

    task_id = store.submit_write(create)
    compiled = compile_task(store, task_id=task_id, expected_task_version=1,
                            settings={"model": {"n_estimators": 200}})
    run_id = compiled["run_id"]
    closure_path = root / "coarse_constraint_closure.json"
    closure_path.write_text(json.dumps({
        "schema_version": 2,
        "protocol": "coarse_constraint_closure",
        "tcr_mode": "block_constant",
        "closure": {"n_matched_cells": 1234,
                    "metrics": {"MB_K": 0.0, "MAE_K": 0.001}},
        "value_range": {"min_30m_K": 290.0, "min_10m_K": 289.5},
    }), encoding="utf-8")
    final_path = root / "rf_10m_lst_final.tif"
    final_path.write_bytes(b"scientific-output")
    context = {"context": {"rf_data": {
        "train_metrics": {"R2": 0.98, "RMSE": 0.8},
        "test_metrics": {"R2": 0.91, "RMSE": 1.2, "MAE": 0.9, "MB": 0.02},
        "params": {"n_estimators": 200, "max_depth": 25},
        "feature_importance": [{"feature": "NDVI", "importance": 0.4}],
    }, "data_features": {"train_samples": 8000}}}

    def finish(conn):
        nodes = conn.execute(
            "SELECT id FROM nodes WHERE run_id=? ORDER BY exec_order", (run_id,)).fetchall()
        for index, (node_id,) in enumerate(nodes):
            conn.execute("UPDATE nodes SET status='succeeded',result=? WHERE id=?",
                         (json.dumps(context if index == 0 else {"context": {}},
                                     ensure_ascii=False), node_id))
        node_id = nodes[-1][0]
        attempt_id = "attempt_phase7"
        conn.execute(
            "INSERT INTO attempts"
            " (id,node_id,attempt_no,status,process_exited,started_at,finished_at,task_version)"
            " VALUES (?,?,1,'succeeded',1,'now','now',1)",
            (attempt_id, node_id))
        conn.execute(
            "INSERT INTO artifacts"
            " (id,attempt_id,type,path,content_hash,input_sources,availability,retention_class,created_at)"
            " VALUES ('closure7',?,'json',?,NULL,'{}','available','keep_forever','now')",
            (attempt_id, str(closure_path)))
        conn.execute(
            "INSERT INTO artifacts"
            " (id,attempt_id,type,path,content_hash,input_sources,availability,retention_class,created_at)"
            " VALUES ('final7',?,'geotiff',?,NULL,'{}','available','keep_forever','now')",
            (attempt_id, str(final_path)))

    store.submit_write(finish)
    budget = Budget(cpu=1, memory=GIB, disk_margin=MIB)
    scheduler = Scheduler(store, root / "executions", budget=budget)
    scheduler._refresh()
    return task_id, run_id


class Phase7Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "ledger.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_completion_transaction_registers_four_independent_jobs(self):
        task_id, run_id = seed_completed_science_run(self.store, self.root)
        status = self.store.read(lambda c: c.execute(
            "SELECT summary_status FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
        jobs = self.store.read(lambda c: c.execute(
            "SELECT target,status FROM projection_jobs WHERE run_id=? ORDER BY target",
            (run_id,)).fetchall())
        self.assertEqual("completed", status)
        self.assertEqual(4, len(jobs))
        self.assertEqual({"experiment_json", "experiment_vector", "workflow_json",
                          "workflow_vector"}, {row[0] for row in jobs})
        self.assertTrue(all(row[1] == "pending" for row in jobs))
        # A second reconciliation must not create duplicate writeback jobs.
        Scheduler(self.store, self.root / "second",
                  Budget(cpu=1, memory=GIB, disk_margin=MIB))._refresh()
        self.assertEqual(4, self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM projection_jobs WHERE run_id=?", (run_id,)).fetchone()[0]))

    def test_truthful_records_and_successful_independent_projections(self):
        _task_id, run_id = seed_completed_science_run(self.store, self.root)
        records = build_memory_records(self.store, run_id)
        exp, workflow = records["experiment"], records["workflow"]
        self.assertEqual(run_id, exp["run_id"])
        self.assertEqual(0.91, exp["metrics"]["test"]["R2"])
        self.assertEqual("coarse_constraint_closure", exp["closure"]["protocol"])
        self.assertEqual(200, exp["final_params"]["n_estimators"])
        self.assertEqual(2, len(exp["artifacts"]))
        self.assertTrue(records["workflow_eligible"])
        self.assertEqual(0.91, workflow["metrics"]["test_r2"])

        memory = FakeMemory()
        executor = ProjectionExecutor(self.store, lambda _uid: memory,
                                      max_attempts=2, base_seconds=.001)
        while executor.drain_once():
            pass
        states = self.store.read(lambda c: c.execute(
            "SELECT target,status FROM projection_jobs WHERE run_id=? ORDER BY target",
            (run_id,)).fetchall())
        self.assertTrue(all(status == "succeeded" for _target, status in states))
        self.assertEqual(4, len(memory.calls))

    def test_projection_failure_retries_finitely_without_reverting_result(self):
        task_id, run_id = seed_completed_science_run(self.store, self.root)

        def fail(_job):
            raise RuntimeError("vector store unavailable")

        executor = ProjectionExecutor(self.store, lambda _uid: FakeMemory(),
                                      max_attempts=2, base_seconds=.001, handler=fail)
        self.assertTrue(executor.drain_once())
        time.sleep(.01)
        self.assertTrue(executor.drain_once())
        failed = self.store.read(lambda c: c.execute(
            "SELECT status,attempts_count,last_error FROM projection_jobs"
            " WHERE run_id=? AND status='failed'", (run_id,)).fetchone())
        self.assertEqual(("failed", 2, "vector store unavailable"), failed)
        self.assertEqual("completed", self.store.read(lambda c: c.execute(
            "SELECT summary_status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]))
        warning = self.store.read(lambda c: c.execute(
            "SELECT text FROM task_logs WHERE run_id=? AND text LIKE '结果已生成%'"
            " ORDER BY id DESC LIMIT 1", (run_id,)).fetchone())
        self.assertIsNotNone(warning)

    def test_running_projection_recovers_after_restart(self):
        _task_id, run_id = seed_completed_science_run(self.store, self.root)
        self.store.submit_write(lambda c: c.execute(
            "UPDATE projection_jobs SET status='running',attempts_count=1"
            " WHERE run_id=?", (run_id,)))
        executor = ProjectionExecutor(self.store, lambda _uid: FakeMemory(),
                                      max_attempts=3, poll_seconds=.01,
                                      handler=lambda job: {"recovered": job["id"]})
        executor.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            pending = self.store.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM projection_jobs WHERE run_id=?"
                " AND status NOT IN ('succeeded','skipped','failed')",
                (run_id,)).fetchone()[0])
            if not pending:
                break
            time.sleep(.02)
        executor.close()
        rows = self.store.read(lambda c: c.execute(
            "SELECT status,attempts_count FROM projection_jobs WHERE run_id=?",
            (run_id,)).fetchall())
        self.assertTrue(all(status == "succeeded" for status, _attempts in rows))
        self.assertTrue(all(attempts == 2 for _status, attempts in rows))

    def test_exhausted_running_projection_becomes_terminal_on_restart(self):
        _task_id, run_id = seed_completed_science_run(self.store, self.root)
        self.store.submit_write(lambda c: c.execute(
            "UPDATE projection_jobs SET status='running',attempts_count=2"
            " WHERE run_id=?", (run_id,)))
        executor = ProjectionExecutor(self.store, lambda _uid: FakeMemory(),
                                      max_attempts=2, poll_seconds=.01,
                                      handler=lambda _job: self.fail(
                                          "达到上限的写回不得再次执行"))
        executor.start()
        time.sleep(.05)
        executor.close()
        rows = self.store.read(lambda c: c.execute(
            "SELECT status,attempts_count,last_error FROM projection_jobs"
            " WHERE run_id=?", (run_id,)).fetchall())
        self.assertTrue(all(status == "failed" for status, _attempts, _error in rows))
        self.assertTrue(all(attempts == 2 for _status, attempts, _error in rows))
        self.assertTrue(all("重试上限" in error for _status, _attempts, error in rows))

    def test_low_quality_run_skips_workflow_but_keeps_experiment(self):
        _task_id, run_id = seed_completed_science_run(self.store, self.root)
        self.store.submit_write(lambda c: c.execute(
            "UPDATE nodes SET result=replace(result,'0.91','0.70') WHERE run_id=?",
            (run_id,)))
        records = build_memory_records(self.store, run_id)
        self.assertFalse(records["workflow_eligible"])
        memory = FakeMemory()
        executor = ProjectionExecutor(self.store, lambda _uid: memory)
        while executor.drain_once():
            pass
        by_target = dict(self.store.read(lambda c: c.execute(
            "SELECT target,status FROM projection_jobs WHERE run_id=?", (run_id,)).fetchall()))
        self.assertEqual("succeeded", by_target["experiment_json"])
        self.assertEqual("succeeded", by_target["experiment_vector"])
        self.assertEqual("skipped", by_target["workflow_json"])
        self.assertEqual("skipped", by_target["workflow_vector"])

    def test_json_memory_upsert_is_idempotent(self):
        exp_log = ExperimentLog(str(self.root / "experiments.json"))
        exp_log.add({"experiment_id": "e1", "status": "success", "value": 1})
        exp_log.add({"experiment_id": "e1", "status": "success", "value": 2})
        self.assertEqual([2], [r["value"] for r in exp_log.all()])
        workflows = WorkflowExperience(str(self.root / "workflows.json"))
        workflows.add({"workflow_id": "w1", "metrics": {"test_r2": .8}})
        workflows.add({"workflow_id": "w1", "metrics": {"test_r2": .9}})
        self.assertEqual(1, len(workflows.all()))
        self.assertEqual(.9, workflows.all()[0]["metrics"]["test_r2"])
        self.assertEqual("project_ascii-1", project_collection_name("ascii-1"))
        self.assertRegex(project_collection_name("武汉项目"), r"^project_[0-9a-f]{64}$")
        self.assertEqual(project_collection_name("武汉项目"),
                         project_collection_name("武汉项目"))

    def test_bounded_idle_cache_does_not_invalidate_active_reference(self):
        clock = [0.0]
        cache = BoundedTTLCache(2, 5, clock=lambda: clock[0])
        first = {"user": 1}
        cache["u1"] = first
        cache["u2"] = {"user": 2}
        active = cache["u1"]
        cache["u3"] = {"user": 3}
        self.assertNotIn("u2", cache.keys_snapshot())
        clock[0] = 6
        self.assertEqual(0, len(cache))
        self.assertIs(active, first)

    def test_deployment_config_validation_and_path_separation(self):
        project = self.root / "repo"
        cfg = project / "config" / "deployment.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({
            "paths": {"data_root": str(self.root / "durable"),
                      "workspace_root": str(self.root / "projects")},
            "budget": {"compute_jobs": 2, "download_jobs": 1,
                       "download_connections": 4},
        }), encoding="utf-8")
        deployment = DeploymentConfig.load(project_root=project, env={}, config_path=cfg)
        result = deployment.prepare(adopt_legacy=False)
        self.assertTrue(result["ok"])
        self.assertNotEqual(deployment.data_root, deployment.workspace_root)
        self.assertEqual(deployment.data_root / "users", deployment.users_root)
        with patch.dict(os.environ, {"GTAI_DATA_ROOT": "stale-relative-path"}):
            deployment.apply_process_defaults()
            self.assertEqual(str(deployment.data_root), os.environ["GTAI_DATA_ROOT"])
            self.assertEqual(str(deployment.workspace_root), os.environ["WORKSPACE_ROOT"])
        bad = project / "config" / "bad.json"
        bad.write_text(json.dumps({"budget": {"compute_jobs": 0}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            DeploymentConfig.load(project_root=project, env={}, config_path=bad)

    def test_legacy_import_is_idempotent_and_never_fabricates_success(self):
        users = self.root / "users"
        convs = users / "olduser" / "conversations"
        results = self.root / "old-project" / "results"
        convs.mkdir(parents=True)
        results.mkdir(parents=True)
        (convs / "_projects.json").write_text(json.dumps({"projects": [
            {"name": "武汉旧项目", "dir": str(results.parent)}]}), encoding="utf-8")
        (convs / "oldconv.json").write_text(json.dumps({
            "id": "oldconv", "project": "武汉旧项目",
            "messages": [{"role": "user", "content": "原始问题"},
                         {"role": "assistant", "content": "原始回答"}],
        }, ensure_ascii=False), encoding="utf-8")
        (results / "accuracy_metrics.json").write_text(
            json.dumps({"R2": .88}), encoding="utf-8")

        first_conv = import_legacy_conversations(self.store, users)
        first_result = import_legacy_results(self.store, users)
        before = self.store.read(lambda c: (
            c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]))
        second_conv = import_legacy_conversations(self.store, users)
        second_result = import_legacy_results(self.store, users)
        after = self.store.read(lambda c: (
            c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]))
        self.assertEqual(2, first_conv["messages"])
        self.assertEqual(1, first_result["imported"])
        self.assertEqual(0, second_conv["imported"])
        self.assertEqual(0, second_result["imported"])
        self.assertEqual(before, after)
        legacy = self.store.read(lambda c: c.execute(
            "SELECT legacy_conv_id FROM conversations WHERE project_id=?",
            ("武汉旧项目",)).fetchone())
        self.assertEqual(("oldconv",), tuple(legacy))
        messages = self.store.read(lambda c: c.execute(
            "SELECT role,content FROM messages ORDER BY seq").fetchall())
        self.assertIn(("user", "原始问题"), messages)
        self.assertIn(("assistant", "原始回答"), messages)
        statuses = self.store.read(lambda c: c.execute(
            "SELECT summary_status FROM tasks WHERE capability='legacy_results'"
        ).fetchall())
        self.assertEqual([("legacy_unverified",)], statuses)
        provenance = self.store.read(lambda c: c.execute(
            "SELECT input_sources FROM artifacts").fetchone()[0])
        self.assertIn("scientific_completion", json.loads(provenance)["unknown_fields"])
        self.assertEqual(0, self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM projection_jobs").fetchone()[0]))

    def test_schema_and_state_survive_reopen(self):
        self.assertEqual(SCHEMA_VERSION, self.store.read(
            lambda c: c.execute("PRAGMA user_version").fetchone()[0]))
        _task_id, run_id = seed_completed_science_run(self.store, self.root)
        db = self.store.db_path
        self.store.close()
        self.store = StateStore(db)
        jobs = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM projection_jobs WHERE run_id=?", (run_id,)).fetchone()[0])
        self.assertEqual(4, jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
