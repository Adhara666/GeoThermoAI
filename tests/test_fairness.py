"""阶段四保护队列的真实进程时序检查。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_scheduling import task
from core.state_kernel import StateStore
from core.scheduling.resources import Budget, GIB
from core.scheduling.scheduler import Scheduler, rows


class FairnessTests(unittest.TestCase):
    def test_gapfill_is_protected_after_two_feasible_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / "ledger.db")
            scheduler = Scheduler(store, root / "runs", Budget(cpu=1, memory=GIB, disk_margin=16 * 1024 ** 2,
                                                               timeout=30), handler="scheduling_probe:execute")
            try:
                _, _, gap = task(store, "填洞", [("gapfill", {"duration": .2})])
                _, _, first = task(store, "主结果一", [("export", {"duration": .2})])
                _, _, second = task(store, "主结果二", [("export", {"duration": .2})])
                scheduler.start()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    statuses = store.read(lambda c: rows(c, "SELECT status FROM nodes"))
                    if statuses and all(r["status"] == "succeeded" for r in statuses):
                        break
                    time.sleep(.1)
                else:
                    self.fail("保护队列探针超时")
                order = store.read(lambda c: rows(c,
                    "SELECT n.node_type,a.started_at FROM attempts a JOIN nodes n ON n.id=a.node_id ORDER BY a.started_at"))
                kinds = [r["node_type"] for r in order]
                self.assertEqual(kinds[:3], ["export", "export", "gapfill"])
            finally:
                scheduler.close()
                store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
