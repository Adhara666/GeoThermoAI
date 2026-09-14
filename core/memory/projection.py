# -*- coding: utf-8 -*-
"""Reliable, idempotent memory projections for completed runs.

SQLite owns completion.  This executor only projects those facts to legacy JSON
and per-user Chroma stores; projection failure never changes scientific result
state and is retried with a finite exponential backoff.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.atomic_io import atomic_write_json
from core.state_kernel.store import append_event, new_id, utcnow_iso
from . import workflow_experience


log = logging.getLogger(__name__)

MEMORY_TARGETS = (
    "experiment_json", "experiment_vector", "workflow_json", "workflow_vector",
)
SUPPORTED_TARGETS = (*MEMORY_TARGETS, "run_manifest_view")


class ProjectionSkipped(Exception):
    """A projection is intentionally ineligible rather than failed."""


def enqueue_run_memory_tx(conn, *, event_seq: int, run_id: str,
                          now: Optional[str] = None) -> int:
    """Register all independent memory targets in the run-completion transaction."""
    now = now or utcnow_iso()
    inserted = 0
    for target in MEMORY_TARGETS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO projection_jobs"
            " (id,event_seq,run_id,target,status,attempts_count,created_at,updated_at)"
            " VALUES (?,?,?,?, 'pending',0,?,?)",
            (new_id(), event_seq, run_id, target, now, now),
        )
        inserted += int(cur.rowcount or 0)
    return inserted


def _decode(value, default=None):
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def _param(snapshot: dict, key: str, default=None):
    item = (snapshot.get("params") or {}).get(key)
    return item.get("value", default) if isinstance(item, dict) else default


def _small_json(path: str, limit: int = 8 * 1024 * 1024) -> dict:
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > limit:
            return {}
        value = json.loads(file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _run_bundle(store, run_id: str) -> dict:
    def read(conn):
        head = conn.execute(
            "SELECT r.id,r.task_id,r.task_version,r.frozen_inputs,r.scenario_binding,"
            " r.status,r.created_at,r.updated_at,r.project_dir,r.run_label,"
            " t.user_id,t.project_id,t.conversation_id,t.capability,t.slots,t.label,"
            " c.legacy_conv_id FROM runs r JOIN tasks t ON t.id=r.task_id"
            " JOIN conversations c ON c.id=t.conversation_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if not head:
            return None
        keys = ("run_id", "task_id", "task_version", "frozen_inputs",
                "scenario_binding", "run_status", "run_created", "run_updated",
                "project_dir", "run_label", "user_id", "project_id",
                "conversation_id", "capability", "slots", "label", "legacy_conv_id")
        bundle = dict(zip(keys, head))
        node_rows = conn.execute(
            "SELECT id,node_key,node_type,status,result FROM nodes WHERE run_id=?"
            " ORDER BY exec_order", (run_id,)).fetchall()
        bundle["nodes"] = [dict(zip(("id", "key", "type", "status", "result"), row))
                           for row in node_rows]
        artifact_rows = conn.execute(
            "SELECT a.id,a.type,a.path,a.content_hash,a.availability,a.retention_class,"
            " n.node_type FROM artifacts a JOIN attempts x ON x.id=a.attempt_id"
            " JOIN nodes n ON n.id=x.node_id WHERE n.run_id=? ORDER BY a.created_at",
            (run_id,),
        ).fetchall()
        bundle["artifacts"] = [dict(zip(
            ("id", "type", "path", "content_hash", "availability",
             "retention_class", "node_type"), row)) for row in artifact_rows]
        decision_rows = conn.execute(
            "SELECT trigger_round,param_patch,rationale,budget_spent,created_at"
            " FROM decisions WHERE run_id=? ORDER BY trigger_seq", (run_id,)).fetchall()
        bundle["decisions"] = [dict(zip(
            ("round", "param_patch", "rationale", "budget_spent", "created_at"), row))
            for row in decision_rows]
        return bundle

    value = store.read(read)
    if value is None:
        raise ValueError(f"记忆写回找不到运行：{run_id}")
    return value


def build_memory_records(store, run_id: str) -> Dict[str, dict]:
    """Build truthful experiment/workflow records from persisted run facts."""
    bundle = _run_bundle(store, run_id)
    frozen = _decode(bundle["frozen_inputs"])
    snapshot = frozen.get("snapshot") or frozen
    scenario = _decode(bundle["scenario_binding"])
    region_value = _param(snapshot, "region", "")
    if isinstance(region_value, dict):
        region = (region_value.get("name") or region_value.get("label")
                  or Path(str(region_value.get("path") or "")).stem)
    else:
        region = str(region_value or "")
    time_value = _param(snapshot, "time", {})
    date_range = ["", ""]
    if isinstance(time_value, dict):
        date_range = [str(time_value.get("start") or ""),
                      str(time_value.get("end") or "")]

    contexts = []
    for node in bundle["nodes"]:
        result = _decode(node["result"])
        context = result.get("context")
        if isinstance(context, dict):
            contexts.append(context)
    rf_data = {}
    data_features = {}
    best_round = {}
    for context in contexts:
        if isinstance(context.get("rf_data"), dict):
            rf_data.update(context["rf_data"])
        if isinstance(context.get("data_features"), dict):
            data_features.update(context["data_features"])
        if isinstance(context.get("best_round"), dict):
            best_round.update(context["best_round"])

    metrics: Dict[str, dict] = {}
    if isinstance(rf_data.get("train_metrics"), dict):
        metrics["train"] = rf_data["train_metrics"]
    if isinstance(rf_data.get("test_metrics"), dict):
        metrics["test"] = rf_data["test_metrics"]
    feature_importance = list(rf_data.get("feature_importance") or [])
    closure = {}
    verified_artifacts = []
    for artifact in bundle["artifacts"]:
        item = {k: artifact[k] for k in ("id", "type", "path", "content_hash",
                                         "availability", "retention_class")}
        verified_artifacts.append(item)
        if artifact["type"] != "json" or artifact["availability"] != "available":
            continue
        data = _small_json(artifact["path"])
        filename = Path(artifact["path"]).name.lower()
        found_metrics = data.get("metrics")
        if isinstance(found_metrics, dict):
            if isinstance(found_metrics.get("train"), dict):
                metrics.setdefault("train", found_metrics["train"])
            if isinstance(found_metrics.get("val"), dict):
                metrics.setdefault("val", found_metrics["val"])
            if any(str(k).upper() in ("R2", "RMSE", "MAE", "MB")
                   for k in found_metrics):
                if "predict" in filename or "test" in filename:
                    metrics.setdefault("test", found_metrics)
        if not feature_importance and isinstance(data.get("feature_importance"), list):
            feature_importance = data["feature_importance"]
        if data.get("protocol") == "coarse_constraint_closure" or "closure" in filename:
            closure = (dict(data.get("closure"))
                       if isinstance(data.get("closure"), dict) else dict(data))
            for identity_key in ("protocol", "description", "tcr_mode", "value_range"):
                if identity_key in data:
                    closure[identity_key] = data[identity_key]

    pair = scenario.get("pair") if isinstance(scenario.get("pair"), dict) else {}
    pair = dict(pair)
    if scenario.get("mode"):
        pair.setdefault("composite", scenario["mode"])
    acquisition_mode = str(scenario.get("mode") or _param(snapshot, "product_mode", "")
                           or pair.get("composite") or "")
    if acquisition_mode not in ("monthly", "pair"):
        acquisition_mode = "monthly" if "month" in acquisition_mode else "pair"
    rf_params = (rf_data.get("params") if isinstance(rf_data.get("params"), dict)
                 else _param(snapshot, "rf_params", {})) or {}
    if isinstance(best_round.get("params"), dict):
        rf_params = best_round["params"]
    tuning_trace = []
    for decision in bundle["decisions"]:
        tuning_trace.append({
            "round": decision["round"],
            "params": _decode(decision["param_patch"]),
            "reason": decision["rationale"] or "",
            "budget": _decode(decision["budget_spent"]),
            "created_at": decision["created_at"],
        })
    experiment_id = f"exp_run_{run_id}"
    experiment = {
        "schema_version": 2,
        "experiment_id": experiment_id,
        "run_id": run_id,
        "task_id": bundle["task_id"],
        "task_version": int(bundle["task_version"]),
        "conv_id": bundle["legacy_conv_id"] or bundle["conversation_id"],
        "conversation_id": bundle["conversation_id"],
        "project_id": bundle["project_id"],
        "user_id": bundle["user_id"],
        "capability": bundle["capability"],
        "region": region,
        "date_range": date_range,
        "pair": pair,
        "acquisition_mode": acquisition_mode,
        "model": "rf" if metrics or rf_params else "",
        "params": rf_params,
        "metrics": metrics,
        "feature_importance": feature_importance,
        "data_features": data_features,
        "closure": closure,
        "tuning_trace": tuning_trace,
        "final_params": rf_params,
        "artifacts": verified_artifacts,
        "input_fingerprint": frozen.get("snapshot_hash") or hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "status": "success",
        "timestamp": bundle["run_updated"] or bundle["run_created"],
        "source": "state_kernel",
    }

    test = metrics.get("test") or {}
    test_r2 = test.get("R2", test.get("r2"))
    closure_metrics = closure.get("metrics") if isinstance(closure, dict) else {}
    closure_metrics = closure_metrics if isinstance(closure_metrics, dict) else {}
    workflow = {
        "schema_version": 2,
        "workflow_id": f"wf_run_{run_id}",
        "experiment_id": experiment_id,
        "run_id": run_id,
        "task_id": bundle["task_id"],
        "task_version": int(bundle["task_version"]),
        "conv_id": experiment["conv_id"],
        "project_id": bundle["project_id"],
        "region": region,
        "date_range": date_range,
        "exec_mode": str(_param(snapshot, "exec_mode", "") or ""),
        "pair": pair,
        "final_params": rf_params,
        "tuning_rounds": len(tuning_trace),
        "tuning_trace": tuning_trace,
        "metrics": {
            "test_r2": test_r2,
            "rmse": test.get("RMSE", test.get("rmse")),
            "closure_mb": closure_metrics.get("MB_K"),
            "closure_mae": closure_metrics.get("MAE_K"),
        },
        "artifacts": [a["id"] for a in verified_artifacts],
        "verdict": "good",
        "timestamp": experiment["timestamp"],
        "source": "state_kernel",
    }
    eligible = (bundle["capability"] == "full_lst" and
                workflow_experience.should_write(
                    status="success", eval_passed=bool(closure), test_r2=test_r2))
    return {"bundle": bundle, "experiment": experiment, "workflow": workflow,
            "workflow_eligible": eligible}


class ProjectionExecutor:
    def __init__(self, store, memory_factory: Callable[[str], Any], *,
                 max_attempts: int = 4, base_seconds: float = 2,
                 poll_seconds: float = 1, handler: Optional[Callable] = None):
        self.store = store
        self.memory_factory = memory_factory
        self.max_attempts = int(max_attempts)
        self.base_seconds = float(base_seconds)
        self.poll_seconds = float(poll_seconds)
        self.handler = handler
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def reconcile(conn):
            now = utcnow_iso()
            conn.execute(
                "UPDATE projection_jobs SET status='retry_wait',next_retry_at=?,"
                " last_error=COALESCE(last_error,'服务重启时回收未完成写回'),updated_at=?"
                " WHERE status='running' AND attempts_count < ?",
                (now, now, self.max_attempts))
            conn.execute(
                "UPDATE projection_jobs SET status='failed',next_retry_at=NULL,"
                " last_error=COALESCE(last_error,'服务中断且已达到写回重试上限'),"
                " finished_at=?,updated_at=? WHERE status='running'"
                " AND attempts_count >= ?", (now, now, self.max_attempts))
        self.store.submit_write(reconcile)
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name="memory-projection")
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)

    def notify(self):
        self.wake.set()

    def _loop(self):
        while not self.stopping.is_set():
            job = self._claim()
            if job:
                self._execute(job)
                continue
            self.wake.wait(self.poll_seconds)
            self.wake.clear()

    def _claim(self) -> Optional[dict]:
        now = utcnow_iso()

        def tx(conn):
            row = conn.execute(
                "SELECT id,run_id,target,attempts_count FROM projection_jobs"
                " WHERE target IN (?,?,?,?,?) AND attempts_count < ? AND"
                " (status='pending' OR (status='retry_wait' AND"
                " (next_retry_at IS NULL OR next_retry_at<=?)))"
                " ORDER BY created_at,id LIMIT 1",
                (*SUPPORTED_TARGETS, self.max_attempts, now),
            ).fetchone()
            if not row:
                return None
            attempts = int(row[3]) + 1
            conn.execute(
                "UPDATE projection_jobs SET status='running',attempts_count=?,"
                " claimed_at=?,updated_at=?,last_error=NULL WHERE id=?",
                (attempts, now, now, row[0]))
            return {"id": row[0], "run_id": row[1], "target": row[2],
                    "attempts_count": attempts}
        return self.store.submit_write(tx)

    def _execute(self, job: dict):
        try:
            if self.handler:
                result = self.handler(job)
            else:
                result = self._project(job)
            self._finish(job, "succeeded", result=result)
        except ProjectionSkipped as exc:
            self._finish(job, "skipped", error=str(exc))
        except Exception as exc:  # projection failure is intentionally isolated
            attempts = int(job["attempts_count"])
            if attempts < self.max_attempts:
                delay = self.base_seconds * (2 ** (attempts - 1))
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                self._finish(job, "retry_wait", error=str(exc), next_retry=next_retry)
            else:
                self._finish(job, "failed", error=str(exc))
            log.warning("记忆写回 %s 第 %s 次失败：%s", job["target"], attempts, exc)

    def _project(self, job: dict) -> dict:
        records = build_memory_records(self.store, job["run_id"])
        bundle = records["bundle"]
        target = job["target"]
        if target == "run_manifest_view":
            path = self.store.db_path.parent / "executions" / "runs" \
                / job["run_id"] / "run_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(str(path), {
                "schema_version": 2, "source": "state_kernel",
                "run_id": job["run_id"], "status": bundle["run_status"],
                "nodes": [{k: n[k] for k in ("id", "key", "type", "status")}
                          for n in bundle["nodes"]],
                "artifacts": bundle["artifacts"],
            })
            return {"path": str(path)}
        manager = self.memory_factory(bundle["user_id"])
        if target == "experiment_json":
            return manager.write_experiment_json(bundle["project_id"], records["experiment"])
        if target == "experiment_vector":
            return manager.write_experiment_vector(bundle["project_id"], records["experiment"])
        if not records["workflow_eligible"]:
            raise ProjectionSkipped("未满足完整流程、评价结果和测试 R²≥0.75 的入库门槛")
        if target == "workflow_json":
            return manager.write_workflow_json(bundle["project_id"], records["workflow"])
        if target == "workflow_vector":
            return manager.write_workflow_vector(bundle["project_id"], records["workflow"])
        raise ValueError(f"未知写回目标：{target}")

    def _finish(self, job: dict, status: str, *, result=None, error: str = "",
                next_retry: Optional[str] = None):
        now = utcnow_iso()

        def tx(conn):
            conn.execute(
                "UPDATE projection_jobs SET status=?,next_retry_at=?,last_error=?,"
                " result=?,finished_at=?,updated_at=? WHERE id=?",
                (status, next_retry, error[:2000] or None,
                 json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                 now if status in ("succeeded", "skipped", "failed") else None,
                 now, job["id"]),
            )
            owner = conn.execute(
                "SELECT t.user_id,t.conversation_id,t.id FROM runs r"
                " JOIN tasks t ON t.id=r.task_id WHERE r.id=?", (job["run_id"],)
            ).fetchone()
            if owner:
                append_event(
                    conn, type=f"projection.{status}", user_id=owner[0],
                    conversation_id=owner[1], task_id=owner[2], run_id=job["run_id"],
                    object_type="projection_job", object_id=job["id"],
                    payload={"target": job["target"], "attempt": job["attempts_count"],
                             "error": error[:500]},
                )
                if status in ("retry_wait", "failed"):
                    suffix = "，后台将继续重试" if status == "retry_wait" else "，已达到重试上限"
                    conn.execute(
                        "INSERT INTO task_logs (user_id,conversation_id,task_id,run_id,"
                        " text,created_at) VALUES (?,?,?,?,?,?)",
                        (owner[0], owner[1], owner[2], job["run_id"],
                         f"结果已生成，经验保存待重试：{job['target']}{suffix}", now),
                    )
        self.store.submit_write(tx)

    def drain_once(self) -> bool:
        """Synchronous test/maintenance hook; returns whether one job ran."""
        job = self._claim()
        if not job:
            return False
        self._execute(job)
        return True
