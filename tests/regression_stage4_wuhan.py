"""武汉冻结输入经阶段四真实调度器逐节点复跑。禁止覆盖已有验证目录。"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from core.state_kernel import StateStore
    from core.state_kernel.intake import _ensure_conversation
    from core.state_kernel.tasks import create_task
    from core.planning import compile_task
    from core.scheduling.scheduler import Scheduler, rows
    from core.scheduling.transfer import file_hash
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--region", default=str(ROOT / "武汉市_市.geojson"))
    args = parser.parse_args()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=False)
    raw = Path(args.raw_dir)
    paths = {"landsat_path": str(raw / "landsat_lst_20240721.tif"), "qa_path": str(raw / "landsat_qa_pixel_20240721.tif"),
             "sentinel2_path": str(raw / "sentinel2_bands_20240722.tif"), "scl_path": str(raw / "sentinel2_scl_20240722.tif"), "dem_path": str(raw / "dem.tif")}
    hashes = {k: file_hash(p) for k, p in paths.items()}
    settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))
    settings["agent"]["default_exec_mode"] = "auto"
    settings["processing"].update(seed=42, guard_buffer_m=100., tcr_mode="block_constant")
    settings["model"].update(max_features=.5, random_state=42)
    settings["_execution"] = {"raw_paths": paths, "raw_hashes": hashes,
                               "scene_ids": {"landsat": ["LC09_L2SP_123038_20240721_02_T1", "LC09_L2SP_123039_20240721_02_T1"],
                                             "sentinel2": ["T49RGQ", "T49RGP", "T50RKU", "T50RKV"]}}
    store = StateStore(root / "ledger.sqlite3")
    def create(conn):
        conv = _ensure_conversation(conn, "wuhan_regression", "phase4", "wuhan")
        fields = {k: {"value": v, "source": "user", "confirmed": True, "evidence": "武汉冻结输入回归"} for k, v in {
            "region": "武汉市", "time": {"start": "2024-07-01", "end": "2024-07-31"}, "product_mode": "pair"}.items()}
        fields["region"]["detail"] = {"path": args.region, "content_hash": file_hash(args.region)}
        return create_task(conn, user_id="wuhan_regression", project_id="phase4", conversation_id=conv,
                           capability="full_lst", slots={"fields": fields, "negations": []}, label="武汉冻结输入回归", summary_status="ready")
    tid = store.submit_write(create)
    run = compile_task(store, task_id=tid, expected_task_version=1, settings=settings)
    scheduler = Scheduler(store, root / "executions")
    logging.basicConfig(level=logging.INFO)
    print(json.dumps({"run_id": run["run_id"], "input_hashes": hashes}, ensure_ascii=False), flush=True)
    scheduler.start()
    last = None
    start = time.monotonic()
    try:
        while time.monotonic() - start < 14400:
            graph = store.read(lambda c: rows(c, "SELECT node_type,status,wait_reason,result FROM nodes WHERE run_id=? ORDER BY exec_order", (run["run_id"],)))
            summary = [(n["node_type"], n["status"], n["wait_reason"]) for n in graph]
            if summary != last:
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                last = summary
            if any(n["status"] == "failed" for n in graph):
                raise RuntimeError("武汉调度复跑有失败节点")
            if all(n["status"] == "succeeded" for n in graph):
                final = json.loads(graph[-1]["result"])["context"]
                target = root / "result.json"
                target.write_text(json.dumps({"workspace": final["workspace"], "run_id": run["run_id"], "elapsed": time.monotonic() - start,
                                               "input_hashes": hashes, "resources": scheduler.ledger.snapshot()}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"武汉全部节点完成，结果目录 {final['workspace']}", flush=True)
                return 0
            if scheduler.last_error:
                raise RuntimeError(scheduler.last_error)
            time.sleep(1)
        raise TimeoutError("武汉回归超过四小时")
    finally:
        scheduler.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
