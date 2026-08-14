"""
run_manifest.json 读写与产物血缘工具（按用户确认固定命名）

固定文件名 ``run_manifest.json`` 位于项目 output_dir 根目录，记录：
    - 各 stage 的完成状态（pending/running/completed/failed/skipped_upstream）
    - 每个 stage 产出的关键文件路径、行数/尺寸等统计、输入签名 hash
    - 用于替代"按文件名排序/计数取最新"的不可靠推断

本模块只被各 Skill 包装器（core/skills/builtin/*.py）、core/pipeline.py 与
server.py 状态查询调用，不涉及 Agent 的规划/对话/工具选择逻辑。
文件名固定为 run_manifest.json，不是动态/自由命名。
"""

import hashlib
import os
import time
from typing import Any, Dict, List, Optional

from .atomic_io import atomic_write_json

MANIFEST_FILENAME = "run_manifest.json"

STAGE_ORDER: List[str] = [
    "data_acquisition",
    "data_pipeline",
    "ttri_compute",
    "rf_model",
    "tcr_compute",
    "lst_export",
    "accuracy_eval",
]

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED_UPSTREAM = "skipped_upstream"


def manifest_path(output_dir: str) -> str:
    return os.path.join(output_dir, MANIFEST_FILENAME)


def project_root_from_stage_output_dir(output_dir: str) -> str:
    """从 Agent 固定使用的 raw/processed/results 三个子目录名之一反推项目根目录。

    core/agent/geo_thermo_agent.py 的 SKILL_PATHS（Agent 规划/工具选择逻辑，
    本轮未修改）给不同 Skill 注入的 output_dir 分别是
    ``project_dir/raw``、``project_dir/processed``、``project_dir/results``
    三者之一，而不是同一个项目根目录；本函数据此约定反推出唯一的项目根目录，
    使 run_manifest.json 能固定写在 ``project_dir/run_manifest.json``，
    不论调用方传入的是哪一个子阶段目录都能收敛到同一份 manifest。
    找不到匹配的固定子目录名时（例如脚本直接传入自定义 output_dir），
    直接使用 output_dir 本身作为根目录。

    多轮调优（``project_dir/results/tuning/round_N``）也必须收敛到同一个项目根：
    因此不只看最后一段，而是自末尾向上找第一个固定子目录名。这样
    ``project_dir/results`` 与 ``project_dir/results/tuning/round_0`` 都返回
    ``project_dir``，run_manifest.json 仍固定写在项目根，阶段重建与清理也能找对目录。
    """
    if not output_dir:
        return output_dir
    normalized = os.path.normpath(output_dir)
    head = normalized
    while True:
        parent, base = os.path.split(head)
        if not parent or parent == head:
            return normalized
        if base in ("raw", "processed", "results"):
            return parent
        head = parent


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_manifest(output_dir: str) -> Dict[str, Any]:
    """读取 run_manifest.json；不存在或损坏时返回空骨架（不抛异常，调用方按空态处理）。"""
    path = manifest_path(output_dir)
    if not os.path.isfile(path):
        return {"schema_version": 1, "created_at": _now(), "updated_at": _now(), "stages": {}}
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("stages", {})
        data.setdefault("schema_version", 1)
        return data
    except Exception:
        return {
            "schema_version": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "stages": {},
            "load_error": "旧 manifest 文件损坏，已重置状态视图（不影响磁盘上已生成的产物文件）",
        }


def save_manifest(output_dir: str, manifest: Dict[str, Any]) -> str:
    manifest["updated_at"] = _now()
    return atomic_write_json(manifest_path(output_dir), manifest)


def record_stage(
    output_dir: str,
    stage: str,
    status: str,
    *,
    artifacts: Optional[Dict[str, str]] = None,
    stats: Optional[Dict[str, Any]] = None,
    input_signature: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """更新单个 stage 的状态并原子写回 run_manifest.json；供各 Skill 包装器调用。"""
    manifest = load_manifest(output_dir)
    entry = manifest["stages"].get(stage, {})
    entry["status"] = status
    entry["updated_at"] = _now()
    if artifacts is not None:
        entry["artifacts"] = artifacts
    if stats is not None:
        entry["stats"] = stats
    if input_signature is not None:
        entry["input_signature"] = input_signature
    if params is not None:
        entry["params"] = params
    if error is not None:
        entry["error"] = error
    elif status != STATUS_FAILED:
        entry.pop("error", None)
    manifest["stages"][stage] = entry
    save_manifest(output_dir, manifest)
    return manifest


def mark_downstream_skipped(
    output_dir: str, failed_stage: str, stage_order: Optional[List[str]] = None
) -> None:
    """把 failed_stage 之后（stage_order 顺序）尚未 completed 的 stage 标记为
    skipped_upstream，供前端区分"未执行（上游失败）"与旧行为"失败后仍显示继续完成"。

    stage_order 默认使用 Skill 级 STAGE_ORDER（server.py/skills/builtin 使用的粒度）；
    core.pipeline.EasyLSTPipeline 的步骤粒度不同，调用时会显式传入自己的步骤顺序。
    """
    order = stage_order if stage_order is not None else STAGE_ORDER
    if failed_stage not in order:
        return
    manifest = load_manifest(output_dir)
    idx = order.index(failed_stage)
    changed = False
    for later in order[idx + 1:]:
        entry = manifest["stages"].get(later, {})
        if entry.get("status") == STATUS_COMPLETED:
            continue
        entry["status"] = STATUS_SKIPPED_UPSTREAM
        entry["updated_at"] = _now()
        entry["reason"] = f"上游 {failed_stage} 失败，未执行"
        manifest["stages"][later] = entry
        changed = True
    if changed:
        save_manifest(output_dir, manifest)


def get_stage_status(output_dir: str, stage: str) -> str:
    manifest = load_manifest(output_dir)
    return manifest.get("stages", {}).get(stage, {}).get("status", STATUS_PENDING)


def get_stage_entry(output_dir: str, stage: str) -> Dict[str, Any]:
    manifest = load_manifest(output_dir)
    return manifest.get("stages", {}).get(stage, {})


def resolve_artifact(output_dir: str, stage: str, key: str) -> Optional[str]:
    """从 manifest 精确取某 stage 的产物路径；只有 status=completed 才返回，
    避免用目录"最新文件名排序/计数+1"做不可靠推断。"""
    entry = get_stage_entry(output_dir, stage)
    if entry.get("status") != STATUS_COMPLETED:
        return None
    return entry.get("artifacts", {}).get(key)


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_inputs(*values: Any) -> str:
    """对一组输入（文件路径会展开为 path:size:mtime，其余按字符串处理）计算稳定签名，
    用于 stage 判断"复用已存在的固定产物"时输入是否与上次一致。"""
    h = hashlib.sha256()
    for v in values:
        if isinstance(v, str) and os.path.isfile(v):
            try:
                st = os.stat(v)
                h.update(f"{v}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
                continue
            except OSError:
                pass
        h.update(str(v).encode("utf-8"))
    return h.hexdigest()
