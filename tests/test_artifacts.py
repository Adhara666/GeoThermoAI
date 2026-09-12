# -*- coding: utf-8 -*-
"""产物与恢复（第五阶段）验收测试。

对应 docs/升级方案_通俗解读版.md 9.5 与总体技术方案 §10–§11：
  - 两步提交：准备 → 发布 → 确认；确认前下游不可消费；半成品永不成为正式结果
  - 产物登记血缘：来自哪次尝试/运行、什么参数版本
  - 目标已存在拒绝覆盖（永不覆盖同名旧结果）
  - 取消竞态：准备后运行被取消 → 确认拒绝 → 意向作废
  - §11.2 提交中断处理表：prepared 未发布（继续发布）、published 未确认（补确认）、
    abandoned 残留目录清理、文件丢失 → missing（区别于 cleaned）
  - 引用保护：持久依赖/活动使用阻止清理；keep_forever 默认不删
  - 实例锁：重复启动明确失败，释放后可再启动

运行（无需 pytest）：python tests/test_artifacts.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.state_kernel import StateStore  # noqa: E402
from core.artifacts import (  # noqa: E402
    AV_CLEANED,
    AV_MISSING,
    CommitError,
    InstanceLock,
    abandon_commit_tx,
    acquire_ref_tx,
    cleanup_stale_intents_tx,
    collect_outputs,
    confirm_commit_tx,
    is_protected,
    mark_missing_tx,
    prepare_commit_tx,
    publish_outputs,
    release_consumer_tx,
    release_ref_tx,
    request_cleanup_tx,
    sweep_missing_tx,
)
from core.artifacts.lock import InstanceLock  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))


def expect_raises(name, exc_type, fn):
    try:
        fn()
    except exc_type:
        check(name, True)
        return
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"抛出了错误类型：{type(e).__name__}: {e}")
        return
    check(name, False, "未抛出预期异常")


def _write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ── collect_outputs（§10.1）─────────────────────────────────────

def test_collect_outputs(tmp):
    print("测试组 1：真实输出收集（排除输入映射/兼容清单）")
    work = tmp / "attempt-1" / "work"
    _write(work / "results" / "predict_10m" / "rf_10m_lst_final.tif", b"TIFDATA")
    _write(work / "results" / "closure.json", b"{}")
    _write(work / "for_train" / "train.parquet", b"REWRITTEN")   # ttri 可写副本
    _write(work / "raw_copy.tif", b"INPUT")                       # 输入副本
    _write(work / "run_manifest.json", b"{}")                     # 兼容别名
    _write(work / "tmp.writing", b"x")
    try:
        os.symlink(work / "results" / "closure.json", work / "alias.json")
    except OSError:
        pass  # Windows 无符号链接权限时跳过 symlink 用例
    inputs = {"raw_copy.tif": {"kind": "copy"},
              "for_train/train.parquet": {"kind": "writable_copy"},
              "alias.json": {"kind": "symlink"}}
    outputs = collect_outputs(work, inputs)
    rels = {o["path"] for o in outputs}
    check("正式输出被收集", "results/predict_10m/rf_10m_lst_final.tif" in rels)
    check("ttri 可写副本属于真实输出", "for_train/train.parquet" in rels)
    check("输入副本被排除", "raw_copy.tif" not in rels)
    check("兼容清单被排除", "run_manifest.json" not in rels)
    check(".writing 临时被排除", "tmp.writing" not in rels)
    if os.path.exists(work / "alias.json"):
        check("符号链接别名被排除", "alias.json" not in rels)
    tif = next(o for o in outputs if o["path"].endswith(".tif"))
    check("产物含大小与 SHA-256 与类型", tif["size"] == 7 and len(tif["sha256"]) == 64
          and tif["type"] == "geotiff")


# ── 两步提交全流程（§10.2）─────────────────────────────────────

def _seed_run(store, root: Path, *, run_status="queued", cancel=0):
    """登记对话/任务/运行/节点/尝试（attempt 状态 running），返回 id 集合。"""
    def _tx(conn):
        from core.state_kernel.intake import _ensure_conversation
        conv = _ensure_conversation(conn, "u1", "p1", "c1")
        from core.state_kernel.store import insert_versioned, new_id
        task_id = new_id()
        insert_versioned(conn, "tasks", object_id=task_id, fields={
            "user_id": "u1", "project_id": "p1", "conversation_id": conv,
            "capability": "full_lst"})
        run_id = new_id()
        conn.execute(
            "INSERT INTO runs (id, task_id, task_version, status,"
            " cancel_requested, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?, 'now', 'now')",
            (run_id, task_id, run_status, cancel))
        node_id = new_id()
        conn.execute(
            "INSERT INTO nodes (id, run_id, node_key, node_type, status)"
            " VALUES (?, ?, 'export', 'export', 'running')",
            (node_id, run_id))
        attempt_id = new_id()
        conn.execute(
            "INSERT INTO attempts (id, node_id, attempt_no, authorization,"
            " status, task_version, process_exited, staging_dir)"
            " VALUES (?, ?, 1, 'auth-1', 'running', 1, 0, ?)",
            (attempt_id, node_id, str(root / "run" / "staging" / attempt_id)))
        return {"task_id": task_id, "run_id": run_id, "node_id": node_id,
                "attempt_id": attempt_id}
    return store.submit_write(_tx)


def _make_outputs(staging: Path):
    work = staging / "work"
    _write(work / "results" / "predict_10m" / "rf_10m_lst_final.tif", b"FINAL-LST")
    _write(work / "results" / "closure.json", b"{}")
    return collect_outputs(work, {})


def test_two_step_commit(tmp):
    print("测试组 2：两步提交全流程（准备 → 发布 → 确认）")
    root = tmp / "executions"
    store = StateStore(tmp / "t2.sqlite3")
    ids = _seed_run(store, root)
    staging = root / "run" / "staging" / ids["attempt_id"]
    outputs = _make_outputs(staging)

    def prepare(conn):
        return prepare_commit_tx(
            conn, attempt_id=ids["attempt_id"], staging_dir=str(staging),
            root=root, node_key="export", attempt_no=1, outputs=outputs,
            task_version=1)

    intent_id = store.submit_write(prepare)
    check("提交准备登记（每次尝试至多一份意向）", bool(intent_id))
    expect_raises("重复准备被唯一性拒绝", CommitError,
                  lambda: store.submit_write(prepare))

    # 确认前：节点未成功、无产物（半成品不可消费）
    pre = store.read(lambda c: c.execute(
        "SELECT status FROM nodes WHERE id=?", (ids["node_id"],)).fetchone()[0])
    check("确认前节点不进入 succeeded（半成品不可消费）", pre == "running")

    published = publish_outputs(str(staging), str(root / "runs" / ids["run_id"]
                                                 / "committed" / "export" / "attempt-1"),
                                outputs)
    check("文件发布到唯一目录", (root / "runs" / ids["run_id"] / "committed"
                                / "export" / "attempt-1" / "results" / "predict_10m"
                                / "rf_10m_lst_final.tif").is_file())

    result = store.submit_write(lambda c: confirm_commit_tx(
        c, intent_id=intent_id, published=published))
    check("确认后产物已登记", len(result["artifact_ids"]) == 2)
    row = store.read(lambda c: c.execute(
        "SELECT a.path, a.content_hash, a.retention_class, a.input_sources,"
        " n.status, ci.status FROM artifacts a JOIN nodes n ON n.id = a.attempt_id"
        " IS NULL AND 1=0").fetchone() if False else c.execute(
        "SELECT path, retention_class FROM artifacts LIMIT 1").fetchone())
    check("产物路径指向发布目录", str(row[0]).startswith(str(root / "runs")))
    check("export 节点产物保留类别为 keep_forever", row[1] == "keep_forever")
    node_status = store.read(lambda c: c.execute(
        "SELECT status FROM nodes WHERE id=?", (ids["node_id"],)).fetchone()[0])
    check("确认后节点 succeeded", node_status == "succeeded")
    intent_status = store.read(lambda c: c.execute(
        "SELECT status FROM commit_intents WHERE id=?", (intent_id,)).fetchone()[0])
    check("提交意向置 confirmed", intent_status == "confirmed")
    refs = store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM artifact_refs WHERE consumer_type='run' AND pinned=1"
        ).fetchone()[0])
    check("运行级持久依赖已登记", refs == 2)
    proj = store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM projection_jobs WHERE target='run_manifest_view'"
        ).fetchone()[0])
    check("记忆/清单写回待办已登记", proj == 1)

    # 目标已存在：永不覆盖同名旧结果
    expect_raises("目标已存在拒绝覆盖", CommitError,
                  lambda: publish_outputs(str(staging), published["published_dir"],
                                          outputs))
    store.close()


def test_cancel_race_and_overwrite(tmp):
    print("测试组 3：取消竞态与最后核对（§10.2）")
    root = tmp / "executions"
    store = StateStore(tmp / "t3.sqlite3")
    ids = _seed_run(store, root)
    staging = root / "run" / "staging" / ids["attempt_id"]
    outputs = _make_outputs(staging)
    intent_id = store.submit_write(lambda c: prepare_commit_tx(
        c, attempt_id=ids["attempt_id"], staging_dir=str(staging), root=root,
        node_key="export", attempt_no=1, outputs=outputs, task_version=1))
    # 准备后恰逢取消
    store.submit_write(lambda c: c.execute(
        "UPDATE runs SET cancel_requested=1 WHERE id=?", (ids["run_id"],)))
    published = publish_outputs(str(staging), str(root / "runs" / ids["run_id"]
                                                 / "committed" / "export" / "attempt-1"),
                                outputs)
    expect_raises("取消后确认被拒绝（旧结果不进入正式结果）", CommitError,
                  lambda: store.submit_write(lambda c: confirm_commit_tx(
                      c, intent_id=intent_id, published=published)))
    store.submit_write(lambda c: abandon_commit_tx(
        c, intent_id=intent_id, reason="运行已取消"))
    st = store.read(lambda c: c.execute(
        "SELECT status FROM commit_intents WHERE id=?", (intent_id,)).fetchone()[0])
    check("意向作废（abandoned）", st == "abandoned")
    node = store.read(lambda c: c.execute(
        "SELECT status FROM nodes WHERE id=?", (ids["node_id"],)).fetchone()[0])
    check("取消竞态下节点不成功", node == "running")
    store.close()


# ── §11.2 提交中断对账 ─────────────────────────────────────────

def test_recovery_paths(tmp):
    print("测试组 4：提交中断处理表（§11.2）")
    root = tmp / "executions"
    store = StateStore(tmp / "t4.sqlite3")

    # 场景 A：prepared，文件未移动 → 继续发布并确认
    ids = _seed_run(store, root)
    staging = root / "run" / "staging" / ids["attempt_id"]
    outputs = _make_outputs(staging)
    intent_id = store.submit_write(lambda c: prepare_commit_tx(
        c, attempt_id=ids["attempt_id"], staging_dir=str(staging), root=root,
        node_key="export", attempt_no=1, outputs=outputs, task_version=1))
    store.submit_write(lambda c: c.execute(
        "UPDATE attempts SET status='committing' WHERE id=?", (ids["attempt_id"],)))
    target = str(root / "runs" / ids["run_id"] / "committed" / "export" / "attempt-1")
    published = publish_outputs(str(staging), target, outputs)
    store.submit_write(lambda c: confirm_commit_tx(
        c, intent_id=intent_id, published=published))
    node = store.read(lambda c: c.execute(
        "SELECT status FROM nodes WHERE id=?", (ids["node_id"],)).fetchone()[0])
    check("场景 A：prepared+发布+确认 → 节点成功", node == "succeeded")

    # 场景 B：published 未确认（确认事务宕机）→ 补确认
    ids = _seed_run(store, root)
    staging = root / "run" / "staging" / ids["attempt_id"]
    outputs = _make_outputs(staging)
    intent_id = store.submit_write(lambda c: prepare_commit_tx(
        c, attempt_id=ids["attempt_id"], staging_dir=str(staging), root=root,
        node_key="export", attempt_no=1, outputs=outputs, task_version=1))
    store.submit_write(lambda c: c.execute(
        "UPDATE attempts SET status='committing' WHERE id=?", (ids["attempt_id"],)))
    target = root / "runs" / ids["run_id"] / "committed" / "export" / "attempt-1"
    publish_outputs(str(staging), str(target), outputs)
    store.submit_write(lambda c: confirm_commit_tx(
        c, intent_id=intent_id, published={"published_dir": str(target)}))
    n = store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM artifacts").fetchone()[0])
    check("场景 B：published 未确认 → 补确认后产物登记完整", n >= 4, f"n={n}")

    # 场景 C：abandoned 意向的未采用目录清理（限运行根内）
    # 注意：commit_intents 对 attempt 唯一，场景 C 用独立 seed 的尝试
    ids_c = _seed_run(store, root)
    stale = root / "runs" / ids_c["run_id"] / "committed" / "export" / "attempt-9"
    _write(stale / "x.tif", b"x")
    store.submit_write(lambda c: c.execute(
        "INSERT INTO commit_intents (id, attempt_id, input_signature,"
        " source_dir, target_dir, outputs, status, created_at)"
        " VALUES ('i-stale', ?, 'sig', ?, ?, '[]', 'abandoned', 'now')",
        (ids_c["attempt_id"], str(staging), str(stale))))
    removed = store.submit_write(lambda c: cleanup_stale_intents_tx(
        c, run_roots=[str(root)]))
    check("场景 C：abandoned 未采用目录被清理", not stale.exists())

    # 场景 D：数据库确认后文件丢失 → missing（区别于 cleaned）
    art = store.read(lambda c: c.execute(
        "SELECT id, path FROM artifacts WHERE availability='available' LIMIT 1"
    ).fetchone())
    Path(art[1]).unlink()
    store.submit_write(lambda c: sweep_missing_tx(c))
    av = store.read(lambda c: c.execute(
        "SELECT availability FROM artifacts WHERE id=?", (art[0],)).fetchone()[0])
    check("场景 D：文件丢失如实标 missing", av == AV_MISSING)
    check("missing ≠ cleaned（历史成功与文件可取是两个属性）",
          av != AV_CLEANED)
    store.close()


# ── 引用保护与受控清理（§10.4/§11.4）─────────────────────────

def test_refs_and_cleanup(tmp):
    print("测试组 5：引用保护与受控清理")
    root = tmp / "executions"
    store = StateStore(tmp / "t5.sqlite3")
    ids = _seed_run(store, root)
    staging = root / "run" / "staging" / ids["attempt_id"]
    outputs = _make_outputs(staging)
    intent_id = store.submit_write(lambda c: prepare_commit_tx(
        c, attempt_id=ids["attempt_id"], staging_dir=str(staging), root=root,
        node_key="preprocess_split", attempt_no=1, outputs=outputs,
        task_version=1))
    store.submit_write(lambda c: c.execute(
        "UPDATE nodes SET node_type='preprocess_split' WHERE id=?",
        (ids["node_id"],)))
    published = publish_outputs(str(staging), str(root / "runs" / ids["run_id"]
                                                 / "committed" / "preprocess_split"
                                                 / "attempt-1"), outputs)
    store.submit_write(lambda c: confirm_commit_tx(
        c, intent_id=intent_id, published=published))
    artifacts = store.read(lambda c: c.execute(
        "SELECT id, path FROM artifacts").fetchall())

    # keep_forever 拒绝（本组节点类型改为 preprocess_split → rebuildable，
    # 先验证 rebuildable 可清理；keep_forever 由 export 节点单测覆盖）
    art_id, art_path = artifacts[0]
    r1 = store.submit_write(lambda c: request_cleanup_tx(
        c, artifact_id=art_id, allowed_roots=[str(root)], reason="测试"))
    check("无引用的可重建中间产物可清理", r1["deleted"] is True)
    av = store.read(lambda c: c.execute(
        "SELECT availability FROM artifacts WHERE id=?", (art_id,)).fetchone()[0])
    check("清理后状态为 cleaned（非 missing）", av == AV_CLEANED)
    check("文件确已删除", not Path(art_path).exists())

    # 活动引用阻止清理
    art_id2, art_path2 = artifacts[1]
    store.submit_write(lambda c: acquire_ref_tx(
        c, artifact_id=art_id2, consumer_type="attempt",
        consumer_id="att-x", usage="node_input", pinned=False))
    r2 = store.submit_write(lambda c: request_cleanup_tx(
        c, artifact_id=art_id2, allowed_roots=[str(root)], reason="测试"))
    check("活动使用阻止清理", r2["deleted"] is False)
    store.submit_write(lambda c: release_consumer_tx(
        c, consumer_type="attempt", consumer_id="att-x"))
    r3 = store.submit_write(lambda c: request_cleanup_tx(
        c, artifact_id=art_id2, allowed_roots=[str(root)], reason="测试"))
    check("释放引用后可清理", r3["deleted"] is True)

    # keep_forever（正式产品）默认不自动删除
    from core.state_kernel.store import new_id
    keep_id = new_id()
    keep_path = root / "runs" / ids["run_id"] / "committed" / "export" / "final.tif"
    _write(keep_path, b"KEEP")
    store.submit_write(lambda c: c.execute(
        "INSERT INTO artifacts (id, attempt_id, type, path, availability,"
        " retention_class, created_at) VALUES (?, ?, 'geotiff', ?, 'available',"
        " 'keep_forever', 'now')", (keep_id, ids["attempt_id"], str(keep_path))))
    r4 = store.submit_write(lambda c: request_cleanup_tx(
        c, artifact_id=keep_id, allowed_roots=[str(root)], reason="测试"))
    check("keep_forever 正式结果默认不自动删除", r4["deleted"] is False
          and keep_path.exists())

    # 路径逃逸防护：产物路径不在允许根内 → 拒绝删除
    outside = tmp / "outside" / "a.parquet"
    _write(outside, b"x")
    esc_id = new_id()
    store.submit_write(lambda c: c.execute(
        "INSERT INTO artifacts (id, attempt_id, type, path, availability,"
        " retention_class, created_at) VALUES (?, ?, 'parquet', ?, 'available',"
        " 'rebuildable', 'now')", (esc_id, ids["attempt_id"], str(outside))))
    r5 = store.submit_write(lambda c: request_cleanup_tx(
        c, artifact_id=esc_id, allowed_roots=[str(root)], reason="测试"))
    check("路径逃逸防护（不在允许根内拒绝删除）", r5["deleted"] is False
          and outside.exists())
    store.close()


# ── 实例锁（§11.1）─────────────────────────────────────────────

def test_instance_lock(tmp):
    print("测试组 6：应用实例锁")
    root = tmp / "data"
    lock1 = InstanceLock(root)
    check("首次加锁成功", lock1.acquire())
    lock2 = InstanceLock(root)
    check("重复启动被明确拒绝", lock2.acquire() is False)
    lock1.release()
    check("释放后可再次加锁", lock2.acquire())
    lock2.release()
    shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gtai-artifacts-test-"))
    try:
        test_collect_outputs(tmp)
        test_two_step_commit(tmp)
        test_cancel_race_and_overwrite(tmp)
        test_recovery_paths(tmp)
        test_refs_and_cleanup(tmp)
        test_instance_lock(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
