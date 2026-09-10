# -*- coding: utf-8 -*-
"""第五阶段（产物与恢复）调度级端到端验收。

对应 docs/升级方案_通俗解读版.md 9.5 验收条款（真实 spawn 子进程 + SQLite，
不模拟调度器；复用第四阶段探针注入机制）：

  1. 两步提交全链：节点成功 → 产物发布到唯一目录 + 数据库登记血缘
     （来自哪次运行/尝试），正式结果可溯源。
  2. 计算中途杀进程（crash）：不产生任何正式结果文件，节点如实失败。
  3. 调度器重启对账幂等：重启后补齐/清理，不重复登记、不重跑成功节点。

运行：python tests/e2e_stage5_artifacts.py
"""

import json
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.state_kernel import StateStore  # noqa: E402
from core.state_kernel.intake import _ensure_conversation  # noqa: E402
from core.state_kernel.tasks import create_task  # noqa: E402
from core.planning import compile_task  # noqa: E402
from core.scheduling.resources import Budget, Claim, MIB, GIB  # noqa: E402
from core.scheduling.scheduler import Scheduler, rows  # noqa: E402


def task(store, name, nodes):
    def create(c):
        conv = _ensure_conversation(c, "u", "p", "c")
        return create_task(c, user_id="u", project_id="p", conversation_id=conv,
                           capability="full_lst", label=name,
                           slots={"fields": {k: {"value": v, "source": "user", "confirmed": True}
                                             for k, v in {"region": name,
                                                          "time": {"start": "2024-07-01",
                                                                   "end": "2024-07-31"},
                                                          "product_mode": "pair"}.items()},
                                  "negations": []})
    tid = store.submit_write(create)
    run = compile_task(store, task_id=tid, expected_task_version=1, settings={})

    def graph(c):
        rid = run["run_id"]
        c.execute("DELETE FROM node_edges WHERE run_id=?", (rid,))
        c.execute("DELETE FROM nodes WHERE run_id=?", (rid,))
        prior = None
        ids = []
        for index, (kind, extra) in enumerate(nodes):
            nid = f"{rid}_{index}"
            resource = "download" if kind == "acquire_asset" else \
                "model" if kind == "select_scene" else "compute"
            claim = Claim(resource, 1 if resource == "compute" else 0, 1,
                          160 * MIB, 16 * MIB, 1 if resource == "download" else 0)
            p = {"test_claim": asdict(claim), **extra}
            c.execute("INSERT INTO nodes(id,run_id,node_key,node_type,status,params,exec_order)"
                      " VALUES(?,?,?,?,'pending',?,?)",
                      (nid, rid, f"{kind}_{index}", kind, json.dumps(p), index + 1))
            if prior:
                c.execute("INSERT INTO node_edges VALUES(?,?,?)", (rid, prior, nid))
            prior = nid
            ids.append(nid)
        return ids
    return tid, run["run_id"], store.submit_write(graph)


class Stage5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "ledger.db")
        self.budget = Budget(cpu=2, memory=GIB, disk_margin=16 * MIB, timeout=30)
        self.scheduler = Scheduler(self.store, self.root / "runs", self.budget,
                                   handler="scheduling_probe:execute")

    def tearDown(self):
        self.scheduler.close()
        self.store.close()
        self.temp.cleanup()

    def status(self, nid):
        return self.store.read(lambda c: c.execute(
            "SELECT status FROM nodes WHERE id=?", (nid,)).fetchone()[0])

    def wait(self, predicate, timeout=30):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if predicate():
                return
            if self.scheduler.last_error:
                self.fail(self.scheduler.last_error)
            time.sleep(.1)
        self.fail("等待超时 " + json.dumps(
            self.store.read(lambda c: rows(
                c, "SELECT node_type,status,wait_reason FROM nodes")),
            ensure_ascii=False))

    def test_two_step_publish_and_traceability(self):
        """9.5 验收 2：正式结果可溯源（产物编号 + 来自哪次运行/尝试）。"""
        _, run_id, ids = task(self.store, "溯源探针", [
            ("preprocess_split", {"emit_files": {"for_train/train.parquet": "T1"}}),
            ("export", {"emit_files": {"results/predict_10m/final.tif": "LST"}}),
        ])
        export_id = ids[-1]
        self.scheduler.start()
        self.wait(lambda: self.status(export_id) == "succeeded")

        arts = self.store.read(lambda c: rows(
            c, "SELECT a.type, a.path, a.retention_class, a.input_sources,"
               " a.availability, n.node_key FROM artifacts a"
               " JOIN attempts at ON at.id = a.attempt_id"
               " JOIN nodes n ON n.id = at.node_id WHERE n.run_id = ?", (run_id,)))
        self.assertEqual(2, len(arts), f"产物登记 {arts}")
        by_key = {a["node_key"]: a for a in arts}
        self.assertEqual("rebuildable", by_key["preprocess_split_0"]["retention_class"])
        self.assertEqual("keep_forever", by_key["export_1"]["retention_class"])
        # 血缘：产物 input_sources 记录运行与任务版本
        self.assertEqual(run_id, json.loads(by_key["export_1"]["input_sources"])["run_id"])
        # 发布目录：位于运行专属 committed 目录，非暂存区
        self.assertIn(str(Path("committed")), by_key["export_1"]["path"].replace("\\", "/"))
        self.assertTrue(Path(by_key["export_1"]["path"]).is_file(),
                        "确认后正式产物文件真实存在")
        # 引用：正式产品带 run 级持久依赖
        pinned = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM artifact_refs r JOIN artifacts a ON a.id = r.artifact_id"
            " WHERE r.pinned = 1 AND a.retention_class = 'keep_forever'").fetchone()[0])
        self.assertGreaterEqual(pinned, 1)
        # 提交意向全部 confirmed + 写回待办登记
        intents = self.store.read(lambda c: rows(
            c, "SELECT status FROM commit_intents"))
        self.assertTrue(all(i["status"] == "confirmed" for i in intents))
        jobs = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM projection_jobs WHERE target='run_manifest_view'"
        ).fetchone()[0])
        self.assertGreaterEqual(jobs, 2)
        print("  [PASS] 两步提交全链 + 溯源 + 血缘登记")

    def test_crash_leaves_no_formal_artifacts(self):
        """9.5 验收 3：计算中途杀进程 → 无正式结果，如实失败。"""
        _, run_id, ids = task(self.store, "崩溃探针", [
            ("preprocess_split", {"crash": True}),
            ("export", {"emit_files": {"results/final.tif": "LST"}}),
        ])
        boom_id, export_id = ids
        self.scheduler.start()
        self.wait(lambda: self.status(boom_id) == "failed")
        # 上游崩溃后下游被阻塞（§8.2：依赖失败不进就绪队列），不等待其终态
        arts = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM artifacts a JOIN attempts at ON at.id = a.attempt_id"
            " JOIN nodes n ON n.id = at.node_id WHERE n.run_id = ?",
            (run_id,)).fetchone()[0])
        self.assertEqual(0, arts, "崩溃运行不得产生任何正式产物")
        committed = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM commit_intents WHERE status='confirmed'"
        ).fetchone()[0])
        self.assertEqual(0, committed)
        downstream = self.status(export_id)
        self.assertNotEqual("succeeded", downstream, "上游崩溃时下游不得成功")
        print("  [PASS] 中途杀进程：无正式结果，节点如实失败，下游被阻塞")

    def test_restart_reconcile_idempotent(self):
        """9.5 验收 1（对账面）：调度器重启后对账幂等，不重复登记。"""
        _, run_id, ids = task(self.store, "重启探针", [
            ("export", {"emit_files": {"results/final.tif": "LST"}}),
        ])
        export_id = ids[-1]
        self.scheduler.start()
        self.wait(lambda: self.status(export_id) == "succeeded")
        before = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM artifacts").fetchone()[0])
        self.scheduler.close()
        # 重新启动同一任务库上的调度器：对账不应重复登记或报错
        self.scheduler = Scheduler(self.store, self.root / "runs", self.budget,
                                   handler="scheduling_probe:execute")
        self.scheduler.start()
        self.wait(lambda: self.scheduler.thread is not None)
        time.sleep(1.5)
        after = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) FROM artifacts").fetchone()[0])
        self.assertEqual(before, after, "重启对账不得重复登记产物")
        node = self.status(export_id)
        self.assertEqual("succeeded", node, "已成功节点不被重启破坏")
        print("  [PASS] 调度器重启对账幂等")


if __name__ == "__main__":
    unittest.main(verbosity=2)
