"""阶段四调度验收，真实 spawn 子进程与 SQLite，不模拟调度器。"""
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.state_kernel import StateStore
from core.state_kernel.intake import _ensure_conversation
from core.state_kernel.tasks import create_task
from core.planning import compile_task
from core.scheduling.resources import Budget, Claim, ResourceLedger, MIB, GIB
from core.scheduling.scheduler import Scheduler, rows


def task(store, name, nodes, priority=0):
    def create(c):
        conv = _ensure_conversation(c, "u", "p", "c")
        return create_task(c, user_id="u", project_id="p", conversation_id=conv, capability="full_lst", label=name,
                           priority=priority, slots={"fields": {k: {"value": v, "source": "user", "confirmed": True} for k, v in {"region": name, "time": {"start": "2024-07-01", "end": "2024-07-31"}, "product_mode": "pair"}.items()}, "negations": []})
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
            resource = "download" if kind == "acquire_asset" else "model" if kind == "select_scene" else "compute"
            claim = Claim(resource, 1 if resource == "compute" else 0, 1, 160*MIB, 16*MIB, 1 if resource == "download" else 0)
            p = {"test_claim": asdict(claim), **extra}
            c.execute("INSERT INTO nodes(id,run_id,node_key,node_type,status,params,exec_order) VALUES(?,?,?,?,'pending',?,?)", (nid, rid, f"{kind}_{index}", kind, json.dumps(p), index+1))
            if prior:
                c.execute("INSERT INTO node_edges VALUES(?,?,?)", (rid, prior, nid))
            prior = nid
            ids.append(nid)
        return ids
    return tid, run["run_id"], store.submit_write(graph)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "ledger.db")
        self.budget = Budget(cpu=2, memory=GIB, disk_margin=16*MIB, timeout=30)
        self.scheduler = Scheduler(self.store, self.root / "runs", self.budget, handler="scheduling_probe:execute")

    def tearDown(self):
        self.scheduler.close()
        self.store.close()
        self.temp.cleanup()

    def wait(self, predicate, timeout=25):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if predicate():
                return
            if self.scheduler.last_error:
                self.fail(self.scheduler.last_error)
            time.sleep(.1)
        self.fail("等待超时 " + json.dumps(self.store.read(lambda c: rows(c, "SELECT node_type,status,wait_reason FROM nodes")), ensure_ascii=False))

    def status(self, nid):
        return self.store.read(lambda c: c.execute("SELECT status FROM nodes WHERE id=?", (nid,)).fetchone()[0])

    def test_overlap_retry_and_no_failure_spread(self):
        _, a, na = task(self.store, "武汉探针", [("preprocess_split", {"duration": 5}), ("export", {})])
        _, b, nb = task(self.store, "南京探针", [("acquire_asset", {"duration": .3, "fail_once": True}), ("export", {})])
        self.scheduler.start()
        self.wait(lambda: self.status(na[-1]) == self.status(nb[-1]) == "succeeded")
        attempts = self.store.read(lambda c: rows(c, "SELECT * FROM attempts ORDER BY started_at"))
        aa = next(x for x in attempts if x["node_id"] == na[0])
        bb = [x for x in attempts if x["node_id"] == nb[0]]
        self.assertEqual(len(bb), 2)
        self.assertEqual(len([x for x in attempts if x["node_id"] == na[0]]), 1)
        self.assertLess(bb[0]["started_at"], aa["finished_at"])
        self.assertNotEqual(aa["process_pid"], os.getpid())
        self.assertTrue(all(x["process_exited"] and x["authorization"] is None for x in attempts))
        self.assertFalse(self.scheduler.ledger.claims)

    def test_crash_blocks_only_own_descendants_and_manual_retry(self):
        _, _, ns = task(self.store, "crash", [("preprocess_split", {"crash": True}), ("export", {})])
        _, _, other = task(self.store, "independent", [("export", {})])
        self.scheduler.start()
        self.wait(lambda: self.status(ns[1]) == "blocked" and self.status(other[0]) == "succeeded")
        self.store.submit_write(lambda c: c.execute("UPDATE nodes SET params=json_remove(params,'$.crash') WHERE id=?", (ns[0],)))
        self.scheduler.retry(ns[0], "u")
        self.wait(lambda: self.status(ns[1]) == "succeeded")
        self.assertEqual(self.store.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE node_id=?", (ns[0],)).fetchone()[0]), 2)
        with self.assertRaises(KeyError):
            self.scheduler.retry(ns[0], "other_user")

    def test_cancel_keeps_reservation_until_exit_and_no_late_success(self):
        tid, _, ns = task(self.store, "cancel", [("preprocess_split", {"duration": 15}), ("export", {})])
        self.scheduler.start()
        self.wait(lambda: self.status(ns[0]) == "running")
        self.store.submit_write(lambda c: c.execute("UPDATE tasks SET summary_status='cancelled',version=version+1 WHERE id=?", (tid,)))
        self.scheduler.notify()
        self.wait(lambda: self.status(ns[0]) == "cancelled")
        self.assertEqual(self.status(ns[1]), "cancelled")
        self.assertFalse(self.scheduler.ledger.claims)

    def test_persistent_question_releases_worker_and_answer_once(self):
        _, _, ns = task(self.store, "approval", [("select_scene", {"question": True}), ("export", {})])
        self.scheduler.start()
        self.wait(lambda: self.status(ns[0]) == "waiting_input")
        self.assertFalse(self.scheduler.ledger.claims)
        q = self.store.read(lambda c: rows(c, "SELECT * FROM questions"))[0]
        self.scheduler.answer(q["id"], "accept", "u", q["conversation_id"])
        self.wait(lambda: self.status(ns[1]) == "succeeded")
        with self.assertRaises(Exception):
            self.scheduler.answer(q["id"], "accept", "u", q["conversation_id"])

    def test_revoke_authorization_rejects_late_completion(self):
        _, _, ns = task(self.store, "late", [("export", {"duration": 2})])
        self.scheduler.start()
        self.wait(lambda: self.status(ns[0]) == "running")
        self.store.submit_write(lambda c: c.execute("UPDATE attempts SET authorization=NULL WHERE node_id=?", (ns[0],)))
        self.wait(lambda: self.status(ns[0]) == "cancelled")

    def test_only_one_scheduler(self):
        self.scheduler.start()
        second = Scheduler(self.store, self.root / "runs", self.budget)
        try:
            with self.assertRaises(RuntimeError):
                second.start()
        finally:
            # start 拒绝的实例没有持有控制锁。
            second._lock_context = None
            second.close()

    def test_budget_fractional_cpu_and_measured_memory(self):
        budget = Budget(cpu=.5, memory=GIB, disk_margin=MIB)
        ledger = ResourceLedger(budget, self.root)
        first = Claim("compute", .5, 1, 160*MIB, MIB)
        ledger.reserve("a", first)
        ok, _ = ledger.can_fit(Claim("compute", .5, 1, 160*MIB, MIB))
        self.assertFalse(ok)
        ledger.measured["a"] = 200*MIB
        self.assertFalse(ledger.can_fit(Claim("download", 0, 1, 160*MIB, MIB, 1))[0])
        self.assertEqual(ledger.pressure()["reserved_active"], 200*MIB)

    def test_invalid_configuration_rejected(self):
        for value in ("0", "-1", "inf", "nan", "1.5", "invalid"):
            with self.assertRaises(ValueError):
                Budget.detect(self.root, {"GTAI_COMPUTE_JOBS": value})


if __name__ == "__main__":
    unittest.main(verbosity=2)
