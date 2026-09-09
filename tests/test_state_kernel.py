# -*- coding: utf-8 -*-
"""状态内核（第一阶段）验收测试。

对应升级方案 9.1 的验收条款：
  - 重启持久化：登记的命令/消息/事件在关闭后重开仍在，事件序号不回退
  - 命令去重：同一去重键 + 相同载荷只记一次；同键不同载荷报冲突
  - 对象版本号：携带旧版本的更新请求被拒绝
  - 事务原子性：事务中途出错显式回滚，不留半套写入
  - 单一写入者串行化：并发提交全部成功且事件序号严格递增

运行（无需 pytest）：python tests/test_state_kernel.py
"""

import shutil
import sys
import tempfile
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.state_kernel import (  # noqa: E402
    CommandConflictError,
    SCHEMA_VERSION,
    StaleVersionError,
    StateStore,
    append_event,
    insert_versioned,
    mark_command_failed,
    mark_command_processed,
    receive_command,
    update_versioned,
)

PASS: list = []
FAIL: list = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def expect_raises(name: str, exc_type, fn):
    try:
        fn()
    except exc_type:
        check(name, True)
        return
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"抛出了错误类型：{type(e).__name__}: {e}")
        return
    check(name, False, "未抛出预期异常")


def make_store(tmp: Path, name: str) -> StateStore:
    return StateStore(tmp / name)


def test_schema_and_receipt(tmp: Path):
    print("测试组 1：建库 + 命令接收回执")
    store = make_store(tmp, "t1.sqlite3")

    def _schema_check(conn):
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return tables

    tables = store.read(_schema_check)
    expected = {
        "conversations", "messages", "commands", "tasks", "runs", "nodes",
        "node_edges", "attempts", "questions", "question_targets", "decisions",
        "artifacts", "artifact_refs", "commit_intents", "events",
        "projection_jobs", "schema_migrations",
    }
    check("§3.2 必需表全部建立", expected <= tables,
          f"缺失：{expected - tables}")
    check("结构版本号为当前版本", store.read(
        lambda c: c.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION)

    receipt = receive_command(
        store,
        user_id="u1", project_id="p1", conversation_id="c1",
        message="做武汉 7 月的 LST", dedup_key="req-001",
        operation_type="chat.command",
        payload={"exec_mode": "approval"},
    )
    check("接收回执 accepted", receipt.accepted and not receipt.duplicate)

    def _counts(conn):
        return (
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )

    msgs, cmds, evts = store.read(_counts)
    check("消息/命令/事件各落一条", (msgs, cmds, evts) == (1, 1, 1),
          f"实际 {(msgs, cmds, evts)}")
    store.close()


def test_dedup_and_conflict(tmp: Path):
    print("测试组 2：命令去重与冲突")
    store = make_store(tmp, "t2.sqlite3")
    base = dict(
        user_id="u1", project_id="p1", conversation_id="c1",
        message="做武汉 7 月的 LST", dedup_key="req-dup",
        operation_type="chat.command",
    )
    r1 = receive_command(store, **base)
    r2 = receive_command(store, **base)  # 同键同载荷 → 幂等
    check("重复请求幂等命中", (not r2.accepted) and r2.duplicate)
    check("幂等返回原命令编号", r2.command_id == r1.command_id)

    def _counts(conn):
        return conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]

    check("台账里命令只记一次", store.read(_counts) == 1)

    expect_raises(
        "同键不同载荷报冲突", CommandConflictError,
        lambda: receive_command(
            store, **{**base, "message": "改成南京 8 月", "payload": {"x": 1}},
        ),
    )
    store.close()


def test_version_guard(tmp: Path):
    print("测试组 3：对象版本号")
    store = make_store(tmp, "t3.sqlite3")
    # 外键约束：先经接收服务建立对话控制行，再插入其下属任务
    receive_command(store, user_id="u1", project_id="p1", conversation_id="c1",
                    message="建立对话", dedup_key="req-seed",
                    operation_type="chat.command")
    conv_pk = store.read(lambda c: c.execute(
        "SELECT id FROM conversations WHERE user_id='u1' AND legacy_conv_id='c1'"
    ).fetchone()[0])
    task_id = "task-v"

    def _create(conn):
        return insert_versioned(
            conn, "tasks", object_id=task_id,
            fields={"user_id": "u1", "project_id": "p1",
                    "conversation_id": conv_pk,
                    "capability": "full_lst", "slots": {"area": "武汉"}},
        )

    store.submit_write(_create)

    def _patch(conn, ver, slots):
        return update_versioned(conn, "tasks", task_id, ver, {"slots": slots})

    v2 = store.submit_write(_patch, 1, {"area": "南京"})
    check("版本 1 → 2 更新成功", v2 == 2)
    expect_raises(
        "旧版本号更新被拒绝（迟到结果不覆盖新状态）", StaleVersionError,
        lambda: store.submit_write(_patch, 1, {"area": "武汉"}),
    )
    check("拒绝后版本仍是 2", store.read(
        lambda c: c.execute("SELECT version FROM tasks WHERE id=?",
                            (task_id,)).fetchone()[0]) == 2)
    store.close()


def test_rollback(tmp: Path):
    print("测试组 4：事务原子性（出错回滚）")
    store = make_store(tmp, "t4.sqlite3")

    def _bad_tx(conn):
        conn.execute(
            "INSERT INTO messages (id, conversation_id, seq, role, content,"
            " status, created_at) VALUES ('m1', 'no-such-conv', 1, 'user', 'x',"
            " 'final', 'now')"
        )  # 外键约束：conversation 不存在
        conn.execute("INSERT INTO commands (id, user_id, dedup_key,"
                     " payload_fingerprint, operation_type, payload, created_at)"
                     " VALUES ('c1', 'u1', 'k1', 'fp', 't', '{}', 'now')")

    expect_raises("外键违规导致事务失败", Exception,
                  lambda: store.submit_write(_bad_tx))

    def _counts(conn):
        return (
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
        )

    msgs, cmds = store.read(_counts)
    check("回滚后无残留写入", (msgs, cmds) == (0, 0), f"实际 {(msgs, cmds)}")

    # 出错后内核仍可用
    r = receive_command(store, user_id="u1", project_id="p1",
                        conversation_id="c1", message="继续",
                        dedup_key="req-after-error",
                        operation_type="chat.command")
    check("失败事务后内核仍可用", r.accepted)
    store.close()


def test_serialized_writes(tmp: Path):
    print("测试组 5：并发写入串行化（单一写入者）")
    store = make_store(tmp, "t5.sqlite3")
    errors: list = []

    def _worker(i: int):
        try:
            def _tx(conn):
                append_event(conn, type=f"e{i}", user_id="u1")
                return i
            store.submit_write(_tx, timeout=60.0)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60.0)

    seqs = store.read(
        lambda c: [r[0] for r in c.execute("SELECT seq FROM events ORDER BY seq")]
    )
    check("20 个并发写事务全部成功", len(errors) == 0 and len(seqs) == 20,
          f"errors={errors[:3]} seqs={len(seqs)}")
    check("事件序号严格递增且唯一", seqs == sorted(set(seqs)) and
          seqs == list(range(1, 21)), f"seqs={seqs[:5]}…")
    store.close()


def test_restart_persistence(tmp: Path):
    print("测试组 6：重启持久化（关闭后重开，记录还在、序号不回退）")
    db = tmp / "t6.sqlite3"
    store = StateStore(db)
    r = receive_command(store, user_id="u1", project_id="p1",
                        conversation_id="c1", message="重启前登记",
                        dedup_key="req-restart", operation_type="chat.command")
    mark_command_processed(store, r.command_id, {"ok": True})
    store.close()

    # 模拟服务重启：重新打开同一数据库
    store2 = StateStore(db)

    def _snapshot(conn):
        return (
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            conn.execute("SELECT status, result FROM commands").fetchone(),
            conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0],
        )

    msgs, cmd, max_seq = store2.read(_snapshot)
    check("重启后消息仍在", msgs == 1)
    check("重启后命令状态为 processed 且结果保留",
          cmd is not None and cmd[0] == "processed" and '"ok"' in cmd[1],
          f"实际 {cmd}")
    check("重启前事件序号已推进", max_seq >= 2, f"实际 {max_seq}")

    r2 = receive_command(store2, user_id="u1", project_id="p1",
                         conversation_id="c1", message="重启后新消息",
                         dedup_key="req-after-restart",
                         operation_type="chat.command")
    check("重启后可继续接收新命令", r2.accepted and r2.message_seq == 2)
    seqs = store2.read(
        lambda c: [x[0] for x in c.execute("SELECT seq FROM events ORDER BY seq")]
    )
    # 事件：重启前 received+processed，重启后 received，共 3 条
    check("事件序号跨重启连续不回退", seqs == sorted(set(seqs)) and len(seqs) == 3,
          f"实际 {seqs}")
    store2.close()

    # 重复请求跨重启仍幂等
    store3 = StateStore(db)
    r3 = receive_command(store3, user_id="u1", project_id="p1",
                         conversation_id="c1", message="重启前登记",
                         dedup_key="req-restart", operation_type="chat.command")
    check("重启后同一请求仍被去重", (not r3.accepted) and r3.duplicate)
    store3.close()


def test_failed_command_closure(tmp: Path):
    print("测试组 7：命令处理闭环（失败也要留痕）")
    store = make_store(tmp, "t7.sqlite3")
    r = receive_command(store, user_id="u1", project_id="p1",
                        conversation_id="c1", message="会失败的任务",
                        dedup_key="req-fail", operation_type="chat.command")
    mark_command_failed(store, r.command_id, "模拟：执行失败")

    def _status(conn):
        return conn.execute("SELECT status FROM commands WHERE id=?",
                            (r.command_id,)).fetchone()[0]

    check("失败终态已落库", store.read(_status) == "failed")
    store.close()


def test_migration_v1_to_v2(tmp: Path):
    print("测试组 8：结构迁移（v1 → v2 带备份、保数据）")
    import sqlite3

    from core.state_kernel import schema as schema_mod

    db = tmp / "t8.sqlite3"
    # 造一个只有 v1 结构的旧库（模拟第一阶段部署过的现场数据）
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + schema_mod._DDL_V1 + "\nCOMMIT;")
        conn.execute("INSERT INTO schema_migrations (version, applied_at,"
                     " description) VALUES (1, 'seed', 'v1')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()

    store = StateStore(db)  # 打开即触发迁移
    check("迁移后结构版本为当前版本",
          store.read(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
          == SCHEMA_VERSION)

    def _columns(conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    task_cols = store.read(lambda c: _columns(c, "tasks"))
    question_cols = store.read(lambda c: _columns(c, "questions"))
    check("tasks 补齐 v2 新列",
          {"label", "origin_message_id", "origin_command_id"} <= task_cols,
          str(sorted(task_cols)))
    check("questions 补齐 v2 新列",
          {"prompt", "answer", "answered_at", "superseded_by",
           "origin_command_id"} <= question_cols, str(sorted(question_cols)))
    check("迁移登记表记录了 v2",
          store.read(lambda c: c.execute(
              "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
          ).fetchone()[0]) == 1)

    backups = list(tmp.glob("t8.sqlite3.bak-v2-*"))
    check("迁移前自动备份数据库文件", len(backups) == 1, str(backups))

    r = receive_command(store, user_id="u1", project_id="p1",
                        conversation_id="c1", message="迁移后仍可用",
                        dedup_key="req-migrated", operation_type="chat.command")
    check("迁移后内核照常接收命令", r.accepted)
    store.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gtai-kernel-test-"))
    try:
        test_schema_and_receipt(tmp)
        test_dedup_and_conflict(tmp)
        test_version_guard(tmp)
        test_rollback(tmp)
        test_serialized_writes(tmp)
        test_restart_persistence(tmp)
        test_failed_command_closure(tmp)
        test_migration_v1_to_v2(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
