# -*- coding: utf-8 -*-
"""Phase 7 compatibility import for legacy conversations and result files.

The JSON files remain the readable source used by the existing UI.  This module
adds an idempotent mapping into the state kernel and registers only verified
file references as historical artifacts.  It never labels a legacy run as a
scientifically completed run and never queues it for memory projection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from core.state_kernel.store import append_event, utcnow_iso


log = logging.getLogger(__name__)
_RESULT_EXTENSIONS = {".tif", ".tiff", ".json", ".csv", ".joblib", ".pkl", ".html"}
_RESULT_HINTS = (
    "lst", "ttri", "tcr", "model", "metric", "accuracy", "closure",
    "feature_importance", "report", "temperature", "地表温度", "精度",
)
_EXCLUDED_PARTS = {
    "raw", "input", "inputs", "cache", "tmp", "temp", "staging",
    "intermediate", "download", "downloads", ".git", "node_modules",
}


def _stable_id(prefix: str, *parts: str) -> str:
    value = "\x1f".join(str(p) for p in parts)
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _as_utc(value, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return fallback


def _register_import(conn, source: Path, kind: str, object_id: str,
                     status: str, details: dict, now: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO legacy_imports"
        " (source_path,kind,object_id,status,details,imported_at)"
        " VALUES (?,?,?,?,?,?)",
        (str(source.resolve()), kind, object_id or None, status,
         json.dumps(details, ensure_ascii=False, sort_keys=True), now),
    )


def import_legacy_conversations(store, users_root: Path,
                                stopping: Optional[threading.Event] = None) -> dict:
    """Import original message text and stable legacy-id mappings once."""
    counts = {"seen": 0, "imported": 0, "messages": 0, "invalid": 0}
    root = Path(users_root)
    if not root.is_dir():
        return counts
    for user_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        conv_dir = user_dir / "conversations"
        if not conv_dir.is_dir():
            continue
        for source in sorted(conv_dir.glob("*.json")):
            if source.name == "_projects.json":
                continue
            if stopping and stopping.is_set():
                return counts
            counts["seen"] += 1
            source_key = str(source.resolve())
            done = store.read(lambda c, key=source_key: c.execute(
                "SELECT 1 FROM legacy_imports WHERE source_path=? AND kind='conversation'",
                (key,)).fetchone())
            if done:
                continue
            now = utcnow_iso()
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("对话文件顶层不是对象")
                legacy_id = str(data.get("id") or source.stem)
                project = str(data.get("project") or "默认项目")
                messages = data.get("messages") or []
                if not isinstance(messages, list):
                    raise ValueError("messages 不是数组")
            except (OSError, ValueError, TypeError) as exc:
                def invalid_tx(conn, exc=exc):
                    _register_import(conn, source, "conversation", "", "invalid",
                                     {"error": str(exc)[:500]}, now)
                store.submit_write(invalid_tx)
                counts["invalid"] += 1
                continue

            def tx(conn):
                row = conn.execute(
                    "SELECT id,next_message_seq FROM conversations"
                    " WHERE user_id=? AND project_id=? AND legacy_conv_id=?",
                    (user_dir.name, project, legacy_id),
                ).fetchone()
                created = False
                if row:
                    conv_id, next_seq = str(row[0]), int(row[1])
                else:
                    conv_id = _stable_id("lcv_", user_dir.name, project, legacy_id)
                    next_seq = 1
                    created_at = _as_utc(data.get("created_at"), now)
                    conn.execute(
                        "INSERT INTO conversations"
                        " (id,user_id,project_id,legacy_conv_id,semantic_version,"
                        " next_message_seq,focus,created_at,updated_at)"
                        " VALUES (?,?,?,?,1,1,NULL,?,?)",
                        (conv_id, user_dir.name, project, legacy_id, created_at, now),
                    )
                    created = True

                # State-kernel commands may already contain some user messages.
                # A multiset prevents adding those again while retaining genuine
                # repeated messages from the legacy transcript.
                existing = Counter((str(r[0]), str(r[1])) for r in conn.execute(
                    "SELECT role,content FROM messages WHERE conversation_id=?",
                    (conv_id,)))
                added = 0
                for index, message in enumerate(messages):
                    if not isinstance(message, dict):
                        continue
                    role = str(message.get("role") or "assistant")
                    content = str(message.get("content") or "")
                    signature = (role, content)
                    if existing[signature] > 0:
                        existing[signature] -= 1
                        continue
                    message_id = _stable_id("lmsg_", source_key, str(index))
                    created_at = _as_utc(message.get("created_at"), now)
                    conn.execute(
                        "INSERT OR IGNORE INTO messages"
                        " (id,conversation_id,seq,role,content,command_id,status,created_at)"
                        " VALUES (?,?,?,?,?,NULL,'legacy',?)",
                        (message_id, conv_id, next_seq, role, content, created_at),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        next_seq += 1
                        added += 1
                conn.execute(
                    "UPDATE conversations SET next_message_seq=?,updated_at=? WHERE id=?",
                    (next_seq, now, conv_id),
                )
                _register_import(
                    conn, source, "conversation", conv_id, "imported",
                    {"legacy_conv_id": legacy_id, "project": project,
                     "messages_seen": len(messages), "messages_added": added,
                     "mapping_created": created}, now)
                append_event(
                    conn, type="legacy.conversation_imported", user_id=user_dir.name,
                    conversation_id=conv_id, object_type="conversation",
                    object_id=conv_id, payload={"messages_added": added},
                )
                return added

            added = int(store.submit_write(tx) or 0)
            counts["imported"] += 1
            counts["messages"] += added
    return counts


def _project_roots(user_dir: Path) -> Iterable[tuple[str, Path]]:
    projects = user_dir / "conversations" / "_projects.json"
    try:
        values = json.loads(projects.read_text(encoding="utf-8")).get("projects", [])
    except (OSError, ValueError, AttributeError):
        values = []
    found = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        name, raw = str(item.get("name") or ""), str(item.get("dir") or "")
        if not name or not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_dir() and str(path.resolve()) not in found:
            found.add(str(path.resolve()))
            yield name, path.resolve()
    # Some old installations only stored project_dir in conversation files.
    conv_dir = user_dir / "conversations"
    if conv_dir.is_dir():
        for source in conv_dir.glob("*.json"):
            if source.name == "_projects.json":
                continue
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                name, raw = str(data.get("project") or "默认项目"), str(data.get("project_dir") or "")
                path = Path(raw).expanduser()
            except (OSError, ValueError, AttributeError):
                continue
            if raw and path.is_dir() and str(path.resolve()) not in found:
                found.add(str(path.resolve()))
                yield name, path.resolve()


def _is_result_candidate(path: Path, project_root: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in _RESULT_EXTENSIONS:
        return False
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return False
    parts = {part.lower() for part in relative.parts[:-1]}
    if parts & _EXCLUDED_PARTS:
        return False
    low = path.name.lower()
    return any(hint in low for hint in _RESULT_HINTS) or bool(
        parts & {"result", "results", "output", "outputs", "artifacts", "runs"})


def _walk_project_files(project_root: Path) -> Iterable[Path]:
    """Walk without descending into raw/cache trees that can contain TB-scale input."""
    for dirpath, dirs, files in os.walk(project_root, followlinks=False):
        dirs[:] = [name for name in dirs if name.lower() not in _EXCLUDED_PARTS]
        base = Path(dirpath)
        for name in files:
            yield base / name


def _verify_legacy_file(path: Path) -> tuple[str, dict]:
    """Perform cheap truthful checks without executing opaque model files."""
    stat = path.stat()
    details = {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
               "provenance": "unknown", "content_hash": "not_computed"}
    if stat.st_size <= 0:
        return "invalid", {**details, "error": "空文件"}
    suffix = path.suffix.lower()
    try:
        if suffix in (".tif", ".tiff"):
            import rasterio
            with rasterio.open(path) as src:
                details.update({"driver": src.driver, "width": src.width,
                                "height": src.height, "bands": src.count,
                                "crs": str(src.crs or "")})
                if src.width <= 0 or src.height <= 0 or src.count <= 0:
                    raise ValueError("栅格尺寸或波段无效")
            details["verification"] = "raster_metadata_opened"
        elif suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
            details["verification"] = "json_parsed"
        elif suffix == ".csv":
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                header = handle.readline().strip()
            if not header:
                raise ValueError("CSV 缺少表头")
            details["verification"] = "csv_header_read"
        elif suffix in (".joblib", ".pkl"):
            details["verification"] = "opaque_file_not_executed"
        else:
            details["verification"] = "nonempty_file"
    except Exception as exc:  # noqa: BLE001 - verification result is persisted
        return "invalid", {**details, "error": str(exc)[:500]}
    return "verified_reference", details


def import_legacy_results(store, users_root: Path,
                          stopping: Optional[threading.Event] = None,
                          max_candidates: int = 5000) -> dict:
    """Register verified references as historical, explicitly unverified runs."""
    counts = {"seen": 0, "imported": 0, "invalid": 0, "truncated": False}
    root = Path(users_root)
    if not root.is_dir():
        return counts
    for user_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for project, project_root in _project_roots(user_dir):
            for source in _walk_project_files(project_root):
                if stopping and stopping.is_set():
                    return counts
                if not _is_result_candidate(source, project_root):
                    continue
                source_key = str(source.resolve())
                done = store.read(lambda c, key=source_key: c.execute(
                    "SELECT 1 FROM legacy_imports WHERE source_path=? AND kind='result'",
                    (key,)).fetchone())
                if done:
                    continue
                counts["seen"] += 1
                if counts["seen"] > max_candidates:
                    counts["truncated"] = True
                    return counts
                status, details = _verify_legacy_file(source)
                now = utcnow_iso()
                if status == "invalid":
                    store.submit_write(lambda c: _register_import(
                        c, source, "result", "", "invalid", details, now))
                    counts["invalid"] += 1
                    continue

                def tx(conn):
                    conv = conn.execute(
                        "SELECT id FROM conversations WHERE user_id=? AND project_id=?"
                        " ORDER BY created_at LIMIT 1", (user_dir.name, project)).fetchone()
                    conv_id = str(conv[0]) if conv else _stable_id(
                        "lcv_", user_dir.name, project, "__legacy_results__")
                    if not conv:
                        conn.execute(
                            "INSERT INTO conversations"
                            " (id,user_id,project_id,legacy_conv_id,semantic_version,"
                            " next_message_seq,created_at,updated_at)"
                            " VALUES (?,?,?,?,1,1,?,?)",
                            (conv_id, user_dir.name, project,
                             "__legacy_results__", now, now),
                        )
                    group = _stable_id("lgrp_", user_dir.name, str(project_root))
                    task_id, run_id = "ltask_" + group[5:], "lrun_" + group[5:]
                    node_id, attempt_id = "lnode_" + group[5:], "latt_" + group[5:]
                    conn.execute(
                        "INSERT OR IGNORE INTO tasks"
                        " (id,user_id,project_id,conversation_id,capability,version,slots,"
                        " ambiguity,summary_status,priority,current_run_id,accumulated,"
                        " created_at,updated_at,label)"
                        " VALUES (?,?,?,?, 'legacy_results',1,'{}',NULL,'legacy_unverified',"
                        " 0,?,'{}',?,?,?)",
                        (task_id, user_dir.name, project, conv_id, run_id, now, now,
                         f"历史结果 {project}"),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO runs"
                        " (id,task_id,task_version,template_version,frozen_inputs,"
                        " scenario_binding,superseded_by,status,cancel_requested,created_at,"
                        " updated_at,project_dir,run_label)"
                        " VALUES (?,?,1,'legacy','{}','{}',NULL,'legacy_unverified',0,?,?,?,?)",
                        (run_id, task_id, now, now, str(project_root), "历史结果未核验"),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO nodes"
                        " (id,run_id,node_key,node_type,params,input_contract,output_contract,"
                        " status,ready_at,exec_order,wait_reason,result)"
                        " VALUES (?,?,'legacy_import','legacy_import','{}',NULL,NULL,"
                        " 'legacy_unverified',NULL,0,'缺少原运行快照和输入来源','{}')",
                        (node_id, run_id),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO attempts"
                        " (id,node_id,attempt_no,authorization,dispatch_batch,process_pid,"
                        " process_started_at,staging_dir,status,error,peak_memory_bytes,"
                        " process_exited,started_at,finished_at,task_version,error_kind)"
                        " VALUES (?,?,1,NULL,'legacy',NULL,NULL,NULL,'legacy_imported',NULL,NULL,"
                        " 1,?,?,1,NULL)", (attempt_id, node_id, now, now),
                    )
                    artifact_id = _stable_id("lart_", source_key)
                    sources = {"legacy": True, "original_path": source_key,
                               "verification": details,
                               "unknown_fields": ["input_snapshot", "source_assets",
                                                  "scientific_completion", "content_hash"]}
                    conn.execute(
                        "INSERT OR IGNORE INTO artifacts"
                        " (id,attempt_id,type,path,content_hash,input_sources,availability,"
                        " retention_class,created_at) VALUES (?,?,?,?,NULL,?,'available',"
                        " 'keep_forever',?)",
                        (artifact_id, attempt_id, suffix_type(source), source_key,
                         json.dumps(sources, ensure_ascii=False, sort_keys=True), now),
                    )
                    existing = conn.execute(
                        "SELECT id FROM artifacts WHERE path=?", (source_key,)).fetchone()
                    object_id = str(existing[0]) if existing else artifact_id
                    _register_import(conn, source, "result", object_id,
                                     "historical_unverified", details, now)
                    append_event(
                        conn, type="legacy.result_registered", user_id=user_dir.name,
                        conversation_id=conv_id, task_id=task_id, run_id=run_id,
                        object_type="artifact", object_id=object_id,
                        payload={"status": "historical_unverified"},
                    )
                    return object_id

                store.submit_write(tx)
                counts["imported"] += 1
    return counts


def suffix_type(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return "geotiff" if suffix in ("tif", "tiff") else suffix or "file"


class LegacyImporter:
    """Bounded daemon wrapper used by application startup and shutdown."""

    def __init__(self, store, users_root: Path, max_candidates: int = 5000):
        self.store = store
        self.users_root = Path(users_root)
        self.max_candidates = int(max_candidates)
        self.stopping = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.result: Optional[dict] = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="legacy-import",
                                       daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            conversations = import_legacy_conversations(
                self.store, self.users_root, self.stopping)
            results = import_legacy_results(
                self.store, self.users_root, self.stopping, self.max_candidates)
            self.result = {"conversations": conversations, "results": results}
            log.info("旧数据兼容扫描完成 %s", self.result)
        except Exception as exc:  # startup remains usable; failure is explicit
            self.result = {"error": str(exc)}
            log.exception("旧数据兼容扫描失败")

    def close(self) -> None:
        self.stopping.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
