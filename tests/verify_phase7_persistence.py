# -*- coding: utf-8 -*-
"""在一个全新进程/容器中验证阶段七武汉台账和记忆仍可恢复。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from core.memory import MemoryManager
    from core.state_kernel import StateStore

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    store = StateStore(root / "data" / "state_kernel" / "ledger.sqlite3")
    try:
        run_id = result["run_id"]
        row = store.read(lambda c: c.execute(
            "SELECT r.status,t.summary_status,t.user_id,t.project_id FROM runs r"
            " JOIN tasks t ON t.id=r.task_id WHERE r.id=?", (run_id,)).fetchone())
        if row is None or tuple(row[:2]) != ("completed", "completed"):
            raise RuntimeError(f"持久台账状态不正确：{row}")
        jobs = store.read(lambda c: c.execute(
            "SELECT target,status,attempts_count FROM projection_jobs WHERE run_id=?"
            " AND target IN ('experiment_json','experiment_vector',"
            " 'workflow_json','workflow_vector') ORDER BY target", (run_id,)).fetchall())
        if len(jobs) != 4 or any(job[1] != "succeeded" for job in jobs):
            raise RuntimeError(f"持久写回状态不正确：{jobs}")
        user_id, project_id = row[2], row[3]
        manager = MemoryManager(
            str(root / "data" / "users" / user_id / "memory"),
            str(ROOT / "models" / "bge-small-zh-v1.5"))
        experiments = [item for item in manager.experiment_log(project_id).all()
                       if item.get("run_id") == run_id]
        workflows = [item for item in manager.workflows(project_id).all()
                     if item.get("run_id") == run_id]
        vectors = manager._rag.project_collection(project_id).get(
            include=[]).get("ids", [])
        expected = {f"exp_run_{run_id}", f"wf_run_{run_id}"}
        if len(experiments) != 1 or len(workflows) != 1 or not expected.issubset(vectors):
            raise RuntimeError(
                f"持久记忆不完整：experiments={len(experiments)} "
                f"workflows={len(workflows)} vectors={vectors}")
        artifacts = store.read(lambda c: c.execute(
            "SELECT path FROM artifacts a JOIN attempts x ON x.id=a.attempt_id"
            " JOIN nodes n ON n.id=x.node_id WHERE n.run_id=?"
            " AND a.availability='available' AND a.retention_class='keep_forever'",
            (run_id,)).fetchall())
        missing = [path for (path,) in artifacts if not Path(path).exists()]
        if not artifacts or missing:
            raise RuntimeError(f"持久正式产物缺失：{missing}")
        print(json.dumps({
            "ok": True, "run_id": run_id, "task_status": row[1],
            "projection_jobs": jobs, "experiment_count": len(experiments),
            "workflow_count": len(workflows), "vector_ids": sorted(vectors),
            "keep_forever_artifacts": len(artifacts),
        }, ensure_ascii=False))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
