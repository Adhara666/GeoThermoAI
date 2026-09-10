# -*- coding: utf-8 -*-
"""状态内核 — 单一写入者存储层（升级第一阶段：状态内核）。

依据总体技术方案 §3.3「数据库写入方式」：

  - 全系统只有一个控制线程写任务库（本模块的写入者线程独占写连接）。
  - 写事务使用 BEGIN IMMEDIATE 短事务；任何错误显式回滚，不吞异常。
  - 事务内不做模型调用、下载、计算校验和大文件复制（调用方契约，
    由代码评审与测试约束）。
  - Web 状态查询走独立只读读连接 + 短事务，不在 SSE 生命周期内持读事务。
  - 默认回滚日志模式；不启用 WAL；不放在网络共享目录（调用方保证路径
    在本地持久盘）。

对象版本号（9.1 第 4 条）：tasks / runs / questions 等带 version 字段的表，
每次修改递增版本；携带旧版本号的更新请求被 StaleVersionError 拒绝，
防止迟到结果覆盖新状态。
"""

import queue
import sqlite3
import threading
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from core.state_kernel.schema import ensure_schema, open_connection

# 允许按版本更新的表：必须含 version 与 updated_at 列
VERSIONED_TABLES = ("tasks", "runs", "questions")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    """后端生成的对象编号：UUID（§3.2「所有业务对象使用后端生成的 UUID」）。"""
    return uuid.uuid4().hex


class StaleVersionError(Exception):
    """携带旧版本号的对象更新请求（9.1 第 4 条：旧版本的操作请求会被拒绝）。"""


class StateStore:
    """单一写入者的 SQLite 台账存储。

    用法（写入）::

        def _tx(conn):
            ...  # 纯 SQLite 操作，禁止重活
            return result

        result = store.submit_write(_tx, timeout=10.0)

    用法（只读查询）::

        rows = store.read(lambda conn: conn.execute("SELECT ...").fetchall())
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        ensure_schema(self.db_path)

        self._write_queue: "queue.Queue" = queue.Queue()
        self._closed = threading.Event()
        self._writer_ready = threading.Event()
        self._writer_error: Optional[BaseException] = None
        self._writer = threading.Thread(
            target=self._writer_loop, daemon=True, name="state-kernel-writer"
        )
        self._writer.start()
        self._writer_ready.wait(timeout=30.0)
        if self._writer_error is not None:
            raise RuntimeError(f"状态内核写入者线程启动失败：{self._writer_error}")

        # 独立读连接：短事务，带锁串行化（§3.3）。读取方来自多个线程
        # （Web 请求线程池、聊天任务线程），由 _read_lock 保证任意时刻只有
        # 一个读事务在用它，因此关闭 sqlite3 的同线程检查。
        self._read_conn = open_connection(self.db_path, allow_cross_thread=True)
        self._read_lock = threading.Lock()

    # ── 写入路径：唯一写连接由写入者线程独占 ────────────────────

    def _writer_loop(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = open_connection(self.db_path)
            self._writer_ready.set()
        except BaseException as e:  # noqa: BLE001 — 启动失败要原样传回主线程
            self._writer_error = e
            self._writer_ready.set()
            return

        while True:
            item = self._write_queue.get()
            if item is None:
                break
            fn, args, kwargs, fut = item
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = fn(conn, *args, **kwargs)
                conn.execute("COMMIT")
                fut.set_result(result)
            except BaseException as e:  # noqa: BLE001 — 任何错误显式回滚，不吞异常
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                fut.set_exception(e)
        conn.close()

    def submit_write(self, fn: Callable[..., Any], *args: Any,
                     timeout: float = 30.0, **kwargs: Any) -> Any:
        """把一个写事务投递给唯一写入者线程执行，阻塞到事务提交或回滚。

        fn 的第一个参数是写连接，在 BEGIN IMMEDIATE … COMMIT 之间执行。
        args/kwargs 原样透传给 fn。fn 内抛出的任何异常都会触发显式
        ROLLBACK 并原样传回本调用方。
        """
        if self._closed.is_set():
            raise RuntimeError("状态内核已关闭，拒绝写入")
        fut: Future = Future()
        self._write_queue.put((fn, args, kwargs, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeoutError:
            raise TimeoutError(
                f"状态内核写事务超时（>{timeout:.0f}s）：{getattr(fn, '__name__', fn)}"
            )

    # ── 读路径：独立连接 + 短事务 ────────────────────────────────

    def read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """只读短事务：BEGIN DEFERRED … COMMIT，不长期持有读事务。"""
        with self._read_lock:
            conn = self._read_conn
            conn.execute("BEGIN DEFERRED")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ── 生命周期 ────────────────────────────────────────────────

    def close(self) -> None:
        """停止写入者线程并关闭连接（服务退出/测试收尾时调用）。"""
        if self._closed.is_set():
            return
        self._closed.set()
        self._write_queue.put(None)
        self._writer.join(timeout=10.0)
        try:
            self._read_conn.close()
        except sqlite3.Error:
            pass


# ── 关键事件（9.1 第 5 条：带全局递增序号落库） ─────────────────


def append_event(
    conn: sqlite3.Connection,
    *,
    type: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    object_version: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """在当前写事务内追加一条关键事件，返回全局递增序号（events.seq 自增）。

    必须在打开的写事务内调用；事件载荷只放小型结构化数据，
    不写逐像元/逐 token 事件（§3.2 events 表约束）。
    """
    import json

    cur = conn.execute(
        "INSERT INTO events (occurred_at, user_id, conversation_id, task_id, run_id,"
        " type, object_type, object_id, object_version, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow_iso(),
            user_id,
            conversation_id,
            task_id,
            run_id,
            type,
            object_type,
            object_id,
            object_version,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return int(cur.lastrowid)


# ── 对象版本号（9.1 第 4 条） ───────────────────────────────────


def update_versioned(
    conn: sqlite3.Connection,
    table: str,
    object_id: str,
    expected_version: int,
    patch: Dict[str, Any],
) -> int:
    """按乐观锁更新带版本对象：版本不符抛 StaleVersionError，成功返回新版本。

    patch 的值为 None 的键跳过（不允许经此接口清列）；table 必须在白名单内。
    必须在打开的写事务内调用。
    """
    import json

    if table not in VERSIONED_TABLES:
        raise ValueError(f"表 {table} 不支持版本化更新（白名单：{VERSIONED_TABLES}）")
    row = conn.execute(
        f"SELECT version FROM {table} WHERE id = ?", (object_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"{table} 对象不存在：{object_id}")
    current = int(row[0])
    if current != int(expected_version):
        raise StaleVersionError(
            f"{table}({object_id}) 版本不匹配：期望 {expected_version}，"
            f"当前 {current}（迟到结果不允许覆盖新状态）"
        )

    if not patch:
        return current
    sets, vals = [], []
    for key, value in patch.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        sets.append(f"{key} = ?")
        vals.append(value)
    if not sets:
        return current
    sets.append("version = version + 1")
    sets.append("updated_at = ?")
    # 参数顺序与 SQL 占位符一致：补丁值 → updated_at → WHERE 的 id
    vals.extend([utcnow_iso(), object_id])
    cur = conn.execute(
        f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", vals
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"{table}({object_id}) 版本化更新未命中任何行（rowcount={cur.rowcount}）"
        )
    return current + 1


def insert_versioned(
    conn: sqlite3.Connection,
    table: str,
    *,
    object_id: str,
    fields: Dict[str, Any],
) -> str:
    """插入一条带版本对象（version 从 1 起）。必须在打开的写事务内调用。"""
    import json

    if table not in VERSIONED_TABLES:
        raise ValueError(f"表 {table} 不支持版本化插入（白名单：{VERSIONED_TABLES}）")
    fields = dict(fields)
    now = utcnow_iso()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    cols, vals = ["id", "version"], [object_id, 1]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        cols.append(key)
        vals.append(value)
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", vals
    )
    return object_id


def get_object(conn: sqlite3.Connection, table: str, object_id: str) -> Optional[Dict[str, Any]]:
    """读取一行对象为字典（供测试与排查；业务读取走各自的查询函数）。"""
    if table not in VERSIONED_TABLES:
        raise ValueError(f"表 {table} 不在版本化对象白名单内：{VERSIONED_TABLES}")
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    return dict(zip(cols, row))


def db_status(conn: sqlite3.Connection) -> Tuple[int, int]:
    """返回 (结构版本, 当前事件最大序号)，供台账巡检与验收使用。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
    return version, int(row[0])
