# -*- coding: utf-8 -*-
"""状态内核 — SQLite 台账 schema 与迁移（升级第一阶段：状态内核）。

依据 docs/agent_task_orchestration_总体技术方案.md §3.2「必需的表和关键约束」。

约定：
  - 所有业务对象使用后端生成的 UUID 编号；时间存 UTC（ISO-8601 字符串）。
  - 结构版本号同时写入 PRAGMA user_version 与 schema_migrations 表。
  - 改表结构必须走带备份的迁移（MIGRATIONS 列表），不直接改（9.1 第 6 条）。
"""

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# 当前数据库结构版本：每次结构变更（新增表/字段/约束）必须 +1，
# 并在 MIGRATIONS 末尾追加对应的迁移条目。
SCHEMA_VERSION = 2

# 建库时冻结的默认连接参数（§3.3）：
#   - 默认回滚日志模式（不启用 WAL）
#   - 开启外键检查、完整同步写入、写锁等待 5 秒
BUSY_TIMEOUT_MS = 5000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 版本 1：§3.2 全部必需表 ────────────────────────────────────
# 每张表上方注释为「表语义契约」（对应总体方案 §3.2 的中文说明）。

_DDL_V1 = """
-- 数据库结构迁移登记：结构版本号同时保存在 PRAGMA user_version
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT
);

-- 对话控制状态：用户/项目/旧对话编号映射、语义版本、下一消息序号、对话焦点
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    project_id        TEXT NOT NULL,
    legacy_conv_id    TEXT,
    semantic_version  INTEGER NOT NULL DEFAULT 1,
    next_message_seq  INTEGER NOT NULL DEFAULT 1,
    focus             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversations_user_project
    ON conversations (user_id, project_id, legacy_conv_id);

-- 消息：对话、序号、角色、原文或最终回答、对应命令、生成状态
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    command_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'final',
    created_at      TEXT NOT NULL,
    UNIQUE (conversation_id, seq)
);

-- 命令：用户、请求去重键、载荷指纹、消息、操作类型、接收/处理状态、结果
-- 约束：用户与去重键唯一；同键不同载荷报冲突
CREATE TABLE IF NOT EXISTS commands (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    dedup_key           TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    message_id          TEXT REFERENCES messages(id),
    operation_type      TEXT NOT NULL,
    payload             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'received',
    result              TEXT,
    created_at          TEXT NOT NULL,
    processed_at        TEXT,
    UNIQUE (user_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_commands_user_status ON commands (user_id, status);

-- 任务草稿：用户/项目/对话、目标能力、任务版本、槽位、歧义、汇总状态、优先级、
-- 当前运行、累计记录（重规划次数/调优额度等，阶段 3 起使用）
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    capability      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    slots           TEXT NOT NULL DEFAULT '{}',
    ambiguity       TEXT,
    summary_status  TEXT NOT NULL DEFAULT 'draft',
    priority        INTEGER NOT NULL DEFAULT 0,
    current_run_id  TEXT,
    accumulated     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_conversation ON tasks (conversation_id, summary_status);

-- 一次运行：任务及版本、模板版本、冻结输入、场景绑定、替代运行、状态、取消标记
-- 约束：同任务允许历史运行；新版本不能改旧快照
CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    task_version     INTEGER NOT NULL,
    template_version TEXT,
    frozen_inputs    TEXT NOT NULL DEFAULT '{}',
    scenario_binding TEXT,
    superseded_by    TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs (task_id, status);

-- 节点与依赖：节点类型、参数、输入/输出契约、状态、就绪时间、执行顺序、前驱/后继
-- 约束：运行内节点键唯一；依赖必须同运行或显式已授权产物引用；禁止环
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    node_key        TEXT NOT NULL,
    node_type       TEXT NOT NULL,
    params          TEXT NOT NULL DEFAULT '{}',
    input_contract  TEXT,
    output_contract TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    ready_at        TEXT,
    exec_order      INTEGER,
    UNIQUE (run_id, node_key)
);
CREATE TABLE IF NOT EXISTS node_edges (
    run_id         TEXT NOT NULL,
    predecessor_id TEXT NOT NULL REFERENCES nodes(id),
    successor_id   TEXT NOT NULL REFERENCES nodes(id),
    PRIMARY KEY (run_id, predecessor_id, successor_id)
);

-- 执行尝试：节点、次数、有效授权、启动批次、进程编号及启动时间、暂存目录、
-- 状态、错误、峰值
-- 约束：节点最多一个有提交权的尝试；重试新建记录
CREATE TABLE IF NOT EXISTS attempts (
    id                 TEXT PRIMARY KEY,
    node_id            TEXT NOT NULL REFERENCES nodes(id),
    attempt_no         INTEGER NOT NULL,
    authorization      TEXT,
    dispatch_batch     TEXT,
    process_pid        INTEGER,
    process_started_at TEXT,
    staging_dir        TEXT,
    status             TEXT NOT NULL DEFAULT 'running',
    error              TEXT,
    peak_memory_bytes  INTEGER,
    process_exited     INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT,
    finished_at        TEXT,
    UNIQUE (node_id, attempt_no)
);

-- 问题及覆盖目标：类型、候选内容、回答约束、状态；目标任务、任务版本、可选运行、
-- 字段
-- 约束：一个共享问题的目标列表固定，答案一次消费
CREATE TABLE IF NOT EXISTS questions (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    conversation_id   TEXT NOT NULL REFERENCES conversations(id),
    qtype             TEXT NOT NULL,
    candidates        TEXT NOT NULL DEFAULT '[]',
    answer_constraint TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS question_targets (
    question_id  TEXT NOT NULL REFERENCES questions(id),
    task_id      TEXT,
    task_version INTEGER,
    run_id       TEXT,
    field        TEXT,
    PRIMARY KEY (question_id, task_id, field)
);

-- 角色决定：运行、触发节点/轮次、角色、选项、参数补丁、理由、预算消耗
-- 约束：同触发点同决定序号唯一；恢复不重复调优
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    trigger_node_id TEXT NOT NULL DEFAULT '',
    trigger_round   INTEGER NOT NULL DEFAULT 0,
    trigger_seq     INTEGER NOT NULL,
    role            TEXT NOT NULL,
    options         TEXT,
    param_patch     TEXT,
    rationale       TEXT,
    budget_spent    TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (run_id, trigger_node_id, trigger_round, trigger_seq)
);

-- 产物：生成尝试、类型、目录/路径、内容标识、输入来源、可用性、保留类别
-- 约束：正式路径唯一；按任务/运行/类型查询
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL REFERENCES attempts(id),
    type            TEXT NOT NULL,
    path            TEXT NOT NULL,
    content_hash    TEXT,
    input_sources   TEXT NOT NULL DEFAULT '[]',
    availability    TEXT NOT NULL DEFAULT 'available',
    retention_class TEXT NOT NULL DEFAULT 'default',
    created_at      TEXT NOT NULL,
    UNIQUE (path)
);

-- 使用关系：产物、消费者类型和编号、用途、是否固定使用
-- 约束：一个消费者同用途引用唯一；活动引用禁止清理
CREATE TABLE IF NOT EXISTS artifact_refs (
    artifact_id   TEXT NOT NULL REFERENCES artifacts(id),
    consumer_type TEXT NOT NULL,
    consumer_id   TEXT NOT NULL,
    usage         TEXT NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (artifact_id, consumer_id, usage)
);

-- 文件提交意向：尝试、输入签名、源/目标目录、输出清单、提交状态
-- 约束：每次尝试至多一份意向，恢复据此对账
CREATE TABLE IF NOT EXISTS commit_intents (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL REFERENCES attempts(id),
    input_signature TEXT NOT NULL,
    source_dir      TEXT NOT NULL,
    target_dir      TEXT NOT NULL,
    outputs         TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    UNIQUE (attempt_id)
);

-- 关键事件：全局递增序号、归属、类型、对象版本、小型载荷
-- 约束：按用户、对话、序号读取；不写逐像元/逐 token 事件
CREATE TABLE IF NOT EXISTS events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at     TEXT NOT NULL,
    user_id         TEXT,
    conversation_id TEXT,
    task_id         TEXT,
    run_id          TEXT,
    type            TEXT NOT NULL,
    object_type     TEXT,
    object_id       TEXT,
    object_version  INTEGER,
    payload         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_user_seq ON events (user_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_conv_seq ON events (conversation_id, seq);

-- 可靠写回待办：事件/运行、目标存储、状态、次数、下次重试时间
-- 约束：来源事件与目标唯一；经验写回重复请求只记一次
CREATE TABLE IF NOT EXISTS projection_jobs (
    id             TEXT PRIMARY KEY,
    event_seq      INTEGER REFERENCES events(seq),
    run_id         TEXT,
    target         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (event_seq, run_id, target)
);
"""


# ── 版本 2：理解层（升级第二阶段）所需字段与索引 ────────────────
# 阶段 1 只建了表骨架；阶段 2 开始真正写入 tasks / questions，需要补：
#   - tasks：任务标签、原始消息、来源命令（§5.2「任务最少保存…原消息」）
#   - questions：问题正文、答案、回答时间、被谁接替、来源命令
#     （§4.4「问题保存真实候选…答案一次消费」需要答案落库）
#   - 按对话+状态查开放问题、按任务查问题目标的索引

_DDL_V2 = """
ALTER TABLE tasks ADD COLUMN label TEXT;
ALTER TABLE tasks ADD COLUMN origin_message_id TEXT;
ALTER TABLE tasks ADD COLUMN origin_command_id TEXT;

ALTER TABLE questions ADD COLUMN prompt TEXT;
ALTER TABLE questions ADD COLUMN answer TEXT;
ALTER TABLE questions ADD COLUMN answered_at TEXT;
ALTER TABLE questions ADD COLUMN superseded_by TEXT;
ALTER TABLE questions ADD COLUMN origin_command_id TEXT;

CREATE INDEX IF NOT EXISTS ix_questions_conv_status
    ON questions (conversation_id, status);
CREATE INDEX IF NOT EXISTS ix_question_targets_task
    ON question_targets (task_id);
"""


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """按 §3.3 设置连接参数。读/写连接共用（默认回滚日志模式，不用 WAL）。"""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=DELETE")


def migrations() -> List[Tuple[int, str, str]]:
    """返回 [(目标版本, 说明, DDL), ...]，按版本升序。"""
    return [
        (1, "初始表结构（总体技术方案 §3.2 全部必需表）", _DDL_V1),
        (2, "理解层：任务标签/原始消息、问题正文/答案/接替关系与查询索引", _DDL_V2),
    ]


def _backup_database(db_path: Path, to_version: int) -> Optional[Path]:
    """迁移前备份数据库文件，备份名带目标版本号，便于回退时定位。"""
    if not db_path.exists():
        return None
    stamp = _utcnow_iso().replace(":", "").replace("-", "").replace("+", "Z")
    backup = db_path.with_name(f"{db_path.name}.bak-v{to_version}-{stamp}")
    shutil.copy2(db_path, backup)
    return backup


def ensure_schema(db_path: Path) -> int:
    """确保数据库结构为当前版本；需要时执行带备份的迁移。

    每个迁移版本在单个事务内原子应用，迁移前把数据库文件备份到同目录
    （备份名带目标版本号，便于回退时定位）。返回应用后的最终结构版本。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    try:
        _apply_connection_pragmas(conn)
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库结构版本 {current} 高于程序支持的 {SCHEMA_VERSION}，"
                f"禁止用旧程序打开新库：{db_path}"
            )
        pending = [m for m in migrations() if m[0] > current]
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not pending:
        return current

    for version, description, ddl in pending:
        if db_path.exists():
            _backup_database(db_path, version)
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        try:
            _apply_connection_pragmas(conn)
            # executescript 不受 Python 侧事务控制：把整个迁移包进脚本内
            # 事务，失败时显式回滚，避免留下半套表
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + ddl + "\nCOMMIT;"
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description)"
                " VALUES (?, ?, ?)",
                (version, _utcnow_iso(), description),
            )
            conn.execute(f"PRAGMA user_version={version}")
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return SCHEMA_VERSION


def open_connection(db_path: Path) -> sqlite3.Connection:
    """按 §3.3 打开一个连接（读连接或测试用；写连接由 StateStore 的写入者线程独占）。"""
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    _apply_connection_pragmas(conn)
    return conn
