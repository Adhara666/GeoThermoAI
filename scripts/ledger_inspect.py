# -*- coding: utf-8 -*-
"""状态内核台账巡检命令（升级第一阶段验收工具）。

用法：
    python scripts/ledger_inspect.py                     # 概览：结构版本 + 各表行数
    python scripts/ledger_inspect.py --tail 20           # 最近 20 条命令/事件
    python scripts/ledger_inspect.py --table messages    # 指定表最近记录
    python scripts/ledger_inspect.py --db <路径>          # 指定台账文件
                                                   （默认 data/state_kernel/ledger.sqlite3，
                                                     或环境变量 GTAI_STATE_DB）

只读打开（SELECT 查询），不写库、不改结构。
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.state_kernel.schema import SCHEMA_VERSION, open_connection  # noqa: E402

LEDGER_TABLES = [
    "conversations", "messages", "commands", "tasks", "runs", "nodes",
    "node_edges", "attempts", "questions", "question_targets", "decisions",
    "artifacts", "artifact_refs", "commit_intents", "events",
    "projection_jobs", "schema_migrations",
]


def resolve_db_path(args_db: str) -> Path:
    if args_db:
        return Path(args_db)
    env = os.environ.get("GTAI_STATE_DB", "").strip()
    if env:
        return Path(env)
    return _ROOT / "data" / "state_kernel" / "ledger.sqlite3"


def print_overview(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"数据库结构版本：{version}（程序支持到 {SCHEMA_VERSION}）")
    if version > SCHEMA_VERSION:
        print("⚠️ 警告：库版本高于程序支持版本，请升级程序后再操作")
    print()
    print(f"{'表':<20}{'行数':>10}")
    print("-" * 30)
    for table in LEDGER_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            print(f"{table:<20}{'(无此表)':>10}")
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:<20}{n:>10}")
    print()
    latest = conn.execute(
        "SELECT version, applied_at, description FROM schema_migrations ORDER BY version"
    ).fetchall()
    if latest:
        print("迁移记录：")
        for v, at, desc in latest:
            print(f"  v{v}  {at}  {desc}")


def print_tail(conn: sqlite3.Connection, limit: int) -> None:
    for table, order_col in (("commands", "created_at"), ("events", "seq"),
                             ("messages", "created_at")):
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
        print(f"\n== {table}（最近 {len(rows)} 条）==")
        for row in rows:
            d = dict(zip(cols, row))
            keep = {k: v for k, v in d.items()
                    if k in ("id", "seq", "dedup_key", "operation_type", "status",
                             "type", "role", "content", "created_at", "occurred_at",
                             "processed_at", "object_id", "user_id")}
            print(" ", keep)


def print_table(conn: sqlite3.Connection, table: str, limit: int) -> None:
    if table not in LEDGER_TABLES:
        print(f"未知表：{table}（可选：{', '.join(LEDGER_TABLES)}）")
        sys.exit(2)
    rows = conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,)).fetchall()
    cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    print(f"== {table}（前 {len(rows)} 条）==")
    for row in rows:
        print(" ", dict(zip(cols, row)))


def main() -> int:
    parser = argparse.ArgumentParser(description="状态内核台账巡检（只读）")
    parser.add_argument("--db", default="", help="台账数据库路径（默认 data/state_kernel/ledger.sqlite3）")
    parser.add_argument("--tail", type=int, default=0, help="显示最近 N 条命令/事件/消息")
    parser.add_argument("--table", default="", help="查看指定表的前若干条记录")
    parser.add_argument("--limit", type=int, default=20, help="--table/--tail 的条数（默认 20）")
    args = parser.parse_args()

    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"台账文件不存在：{db_path}")
        print("提示：服务启动并处理过至少一条消息后会自动创建")
        return 1

    conn = open_connection(db_path)
    try:
        if args.table:
            print_table(conn, args.table, max(1, args.limit))
        elif args.tail:
            print_tail(conn, max(1, args.tail))
        else:
            print_overview(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
