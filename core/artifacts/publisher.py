# -*- coding: utf-8 -*-
"""产物与恢复（升级第五阶段）— 两步提交与产物登记。

依据 docs/agent_task_orchestration_总体技术方案.md §10.1–10.3：

  数据库事务无法包住文件系统操作，因此使用
  **准备记录 → 文件发布 → 数据库确认**。只有最后确认后，
  界面和下游才允许消费。

  - 准备（prepare_commit_tx）：核对尝试身份、运行有效、取消状态、
    输出全部位于私有暂存根下，写 commit_intents（每次尝试至多一份）。
  - 发布（publish_outputs）：在同一文件系统把输出移动到本尝试专属的
    唯一发布目录，不覆盖任何现有目录；跨卷先复制校验再发布。
  - 确认（confirm_commit_tx）：最后一个短事务再次核对授权与取消状态，
    原子登记产物（含血缘）、节点成功、事件与记忆写回待办。

文件准备成功后恰逢取消或目标修改，最后核对会拒绝旧结果；
已产生的目录作为未采用文件清理（§10.2 竞态规则）。
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.state_kernel.store import append_event, new_id, utcnow_iso

# 提交意向状态机：prepared → confirmed / abandoned
INTENT_PREPARED = "prepared"
INTENT_CONFIRMED = "confirmed"
INTENT_ABANDONED = "abandoned"

# 产物可用性（§10.3：历史节点是否成功、文件现在是否可取，是两个属性）
AV_AVAILABLE = "available"
AV_PENDING_DELETE = "pending_delete"
AV_CLEANED = "cleaned"    # 已按政策清理（历史节点仍成功）
AV_MISSING = "missing"    # 意外丢失/损坏（必须如实显示，不能静默换运行）

# 节点类型 → 保留类别（§11.4：正式结果默认不自动删除）
RETENTION_KEEP_FOREVER = "keep_forever"
RETENTION_REBUILDABLE = "rebuildable"
RETENTION_POLICY = {
    "export": RETENTION_KEEP_FOREVER,      # 正式 10m LST 主产品
    "gapfill": RETENTION_KEEP_FOREVER,     # 独立填洞产品
    "promote_best": RETENTION_KEEP_FOREVER,  # 最佳轮模型引用
    "rf_round": RETENTION_REBUILDABLE,
    "tcr": RETENTION_REBUILDABLE,
    "ttri": RETENTION_REBUILDABLE,
    "preprocess_split": RETENTION_REBUILDABLE,
    "prepare_local": RETENTION_REBUILDABLE,
    "acquire_asset": RETENTION_REBUILDABLE,
    "rebuild": RETENTION_REBUILDABLE,
}


class CommitError(Exception):
    """两步提交的校验失败（身份/授权/运行有效性/输出越界/目标冲突）。"""


def _run_valid(row: Dict[str, Any]) -> bool:
    """运行仍有效：未取消、未被替代（§10.2 最后核对条件）。"""
    return (row.get("cancel_requested") in (0, None)
            and row.get("status") not in ("cancelled", "superseded"))


def collect_outputs(work_dir: Path, inputs_manifest: Optional[Dict[str, Any]] = None
                    ) -> List[Dict[str, Any]]:
    """收集暂存工作区里的本节点真实输出（相对路径 + 大小 + SHA-256）。

    排除（§10.1「提交清单只包含本节点真实输出」）：
      - 输入映射：inputs_manifest 中 kind=symlink/copy 的相对路径
        （symlink 是别名，copy 是上游输入的只读副本——都不是新输出；
          kind=writable_copy 的原地改写副本属于真实输出，保留）；
      - run_manifest.json 兼容别名（正式清单由数据库生成，§10.3）；
      - .writing 临时文件、隐藏清单自身。
    """
    work = Path(work_dir)
    if not work.is_dir():
        return []
    inputs = inputs_manifest or {}
    excluded = set()
    for rel, meta in inputs.items():
        if isinstance(meta, dict) and meta.get("kind") == "writable_copy":
            continue
        excluded.add(str(rel))
    outputs = []
    for path in sorted(work.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(work).as_posix()
        if rel in excluded or rel == "run_manifest.json" \
                or rel.endswith(".writing") or rel.startswith("."):
            continue
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        outputs.append({"path": rel, "size": path.stat().st_size,
                        "sha256": h.hexdigest(),
                        "type": _artifact_type(rel)})
    return outputs


def _artifact_type(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1].lower()
    if name.endswith(".tif") or name.endswith(".tiff"):
        return "geotiff"
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".pkl"):
        return "model"
    return "file"


def prepare_commit_tx(conn, *, attempt_id: str, staging_dir: str,
                      root: Path, node_key: str, attempt_no: int,
                      outputs: List[Dict[str, Any]],
                      task_version: int) -> str:
    """提交准备（§10.2 第 3 步）：核对身份与有效性，登记 commit_intents。

    在 BEGIN IMMEDIATE 事务内执行。核对失败抛 CommitError。
    """
    row = conn.execute(
        "SELECT a.authorization, a.status, a.task_version, n.node_key, n.status AS node_status,"
        " r.cancel_requested, r.status AS run_status, r.task_id, r.id AS rid"
        " FROM attempts a JOIN nodes n ON n.id = a.node_id"
        " JOIN runs r ON r.id = n.run_id WHERE a.id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise CommitError(f"尝试不存在：{attempt_id}")
    (authorization, a_status, a_version, _nk, node_status,
     cancel_requested, run_status, task_id, _rid) = row
    if a_status not in ("running", "committing") or not authorization:
        raise CommitError(f"尝试 {attempt_id} 不在可提交状态（{a_status}）")
    if int(a_version or 0) != int(task_version):
        raise CommitError("尝试任务版本与当前任务版本不一致")
    if node_status not in ("running",):
        raise CommitError(f"节点不在执行状态（{node_status}），拒绝提交")
    if not _run_valid({"cancel_requested": cancel_requested, "status": run_status}):
        raise CommitError("运行已取消或被替代，拒绝提交旧结果")

    existing = conn.execute(
        "SELECT status FROM commit_intents WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if existing is not None:
        # 唯一性限制：一次尝试只能有一个有效提交（§10.2 第 3 条）
        raise CommitError(f"尝试 {attempt_id} 已有提交意向（{existing[0]}）")

    staging = Path(staging_dir).resolve()
    work = staging / "work"
    for item in outputs:
        target = (work / item["path"]).resolve()
        if not target.is_relative_to(work):
            raise CommitError(f"输出越界（不在私有暂存根下）：{item['path']}")

    intent_id = new_id()
    target_dir = Path(root) / "runs" / str(_rid) / "committed" / str(node_key) / f"attempt-{attempt_no}"
    conn.execute(
        "INSERT INTO commit_intents (id, attempt_id, input_signature, source_dir,"
        " target_dir, outputs, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent_id, attempt_id,
            hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest(),
            str(staging), str(target_dir),
            json.dumps(outputs, ensure_ascii=False, sort_keys=True),
            INTENT_PREPARED, utcnow_iso(),
        ),
    )
    append_event(
        conn, type="commit.prepared", run_id=str(_rid), task_id=str(task_id),
        object_type="commit_intent", object_id=intent_id,
        payload={"attempt_id": attempt_id, "outputs": len(outputs),
                 "target_dir": str(target_dir)},
    )
    return intent_id


def publish_outputs(staging_dir: str, target_dir: str,
                    outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """文件发布（§10.2 第 4–5 步，非事务）：把暂存工作区（staging/work）中
    的输出移动到唯一发布目录。

    同卷用移动；跨卷先复制到目标卷临时目录并校验再发布。
    目标目录已存在时拒绝（永不覆盖同名旧结果，§10.1）。
    """
    staging, target = Path(staging_dir), Path(target_dir)
    source = staging / "work"
    if target.exists():
        raise CommitError(f"发布目标已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    same_volume = _same_volume(source, target)
    if same_volume:
        for item in outputs:
            src = source / item["path"]
            dest = target / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
    else:
        staging_on_target = target.parent / f".publish-{target.name}-{new_id()[:8]}"
        staging_on_target.mkdir(parents=True)
        for item in outputs:
            src = source / item["path"]
            dest = staging_on_target / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            if hashlib.sha256(dest.read_bytes()).hexdigest() != item["sha256"]:
                raise CommitError(f"跨卷复制校验失败：{item['path']}")
        for item in outputs:
            (target / item["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging_on_target / item["path"]),
                        str(target / item["path"]))
        shutil.rmtree(staging_on_target, ignore_errors=True)

    return {"published_dir": str(target), "files": [o["path"] for o in outputs]}


def _same_volume(a: Path, b: Path) -> bool:
    try:
        import os
        return os.stat(a).st_dev == os.stat(b.parent).st_dev
    except (AttributeError, OSError):
        return True


def confirm_commit_tx(conn, *, intent_id: str,
                      published: Dict[str, Any]) -> Dict[str, Any]:
    """提交确认（§10.2 第 6 步）：最后核对后原子登记正式结果。

    同一短事务内：产物（含血缘）+ 引用（run 持久依赖）+ 节点成功 +
    提交意向确认 + 事件 + 记忆写回待办。任何核对失败抛 CommitError，
    不发布数据库索引，留给恢复对账。
    """
    intent = conn.execute(
        "SELECT ci.attempt_id, ci.target_dir, ci.outputs, ci.status,"
        " a.authorization, a.status AS a_status, a.node_id, a.task_version,"
        " n.run_id, r.cancel_requested, r.status AS run_status, r.task_id"
        " FROM commit_intents ci JOIN attempts a ON a.id = ci.attempt_id"
        " JOIN nodes n ON n.id = a.node_id JOIN runs r ON r.id = n.run_id"
        " WHERE ci.id = ?",
        (intent_id,),
    ).fetchone()
    if intent is None:
        raise CommitError(f"提交意向不存在：{intent_id}")
    (attempt_id, target_dir, outputs_raw, status, authorization, a_status,
     node_id, task_version, run_id, cancel_requested, run_status, task_id) = intent
    if status != INTENT_PREPARED:
        raise CommitError(f"提交意向不在 prepared 状态（{status}）")
    if a_status not in ("running", "committing") or not authorization:
        raise CommitError("尝试授权已失效，拒绝确认（迟到结果）")
    if not _run_valid({"cancel_requested": cancel_requested, "status": run_status}):
        raise CommitError("运行已取消或被替代，拒绝确认旧结果")
    outputs = json.loads(outputs_raw)
    if published.get("published_dir") != target_dir:
        raise CommitError("发布位置与提交意向不一致")

    now = utcnow_iso()
    task_version = int(task_version or 0)
    retention = RETENTION_POLICY.get(_node_type_of_node(conn, node_id),
                                     RETENTION_REBUILDABLE)
    artifact_ids = []
    for item in outputs:
        artifact_id = new_id()
        rel = item["path"]
        artifact_ids.append(artifact_id)
        conn.execute(
            "INSERT INTO artifacts (id, attempt_id, type, path, content_hash,"
            " input_sources, availability, retention_class, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id, attempt_id, item.get("type", "file"),
                str(Path(target_dir) / rel), item["sha256"],
                json.dumps({"run_id": run_id, "task_id": task_id,
                            "task_version": task_version},
                           ensure_ascii=False, sort_keys=True),
                AV_AVAILABLE,
                retention,
                now,
            ),
        )
        # 运行级持久依赖仅登记在正式结果上（§10.4：最终产品即使当前没有
        # 进程读取也不删除）；可重建中间产物靠「无活动使用 + 政策」清理。
        if retention == RETENTION_KEEP_FOREVER:
            conn.execute(
                "INSERT OR IGNORE INTO artifact_refs (artifact_id, consumer_type,"
                " consumer_id, usage, pinned) VALUES (?, 'run', ?, 'pipeline_output', 1)",
                (artifact_id, run_id),
            )

    conn.execute("UPDATE commit_intents SET status = ? WHERE id = ?",
                 (INTENT_CONFIRMED, intent_id))
    conn.execute(
        "UPDATE attempts SET status='succeeded', authorization=NULL,"
        " process_exited=1, finished_at=COALESCE(finished_at, ?) WHERE id=?",
        (now, attempt_id),
    )
    conn.execute("UPDATE nodes SET status='succeeded', result=? WHERE id=?",
                 (json.dumps({"published_dir": target_dir,
                              "artifacts": artifact_ids},
                             ensure_ascii=False), node_id))
    conn.execute(
        "INSERT INTO projection_jobs (id, run_id, target, status, created_at,"
        " updated_at) VALUES (?, ?, 'run_manifest_view', 'pending', ?, ?)",
        (new_id(), run_id, now, now),
    )
    append_event(
        conn, type="node.published", run_id=run_id, task_id=str(task_id),
        object_type="node", object_id=node_id,
        payload={"attempt_id": attempt_id, "intent_id": intent_id,
                 "published_dir": target_dir, "artifacts": artifact_ids},
    )
    return {"artifact_ids": artifact_ids, "published_dir": target_dir,
            "run_id": run_id, "node_id": node_id}


def _node_type_of_node(conn, node_id: str) -> str:
    row = conn.execute("SELECT node_type FROM nodes WHERE id = ?",
                       (node_id,)).fetchone()
    return str(row[0]) if row else ""


def abandon_commit_tx(conn, *, intent_id: str, reason: str) -> None:
    """提交意向作废（取消/失效竞态，§10.2）：已产生目录按未采用文件清理。"""
    row = conn.execute("SELECT status, attempt_id FROM commit_intents WHERE id=?",
                       (intent_id,)).fetchone()
    if row is None or row[0] != INTENT_PREPARED:
        return
    conn.execute("UPDATE commit_intents SET status = ? WHERE id = ?",
                 (INTENT_ABANDONED, intent_id))
    append_event(
        conn, type="commit.abandoned", object_type="commit_intent",
        object_id=intent_id, payload={"attempt_id": row[1], "reason": reason},
    )
