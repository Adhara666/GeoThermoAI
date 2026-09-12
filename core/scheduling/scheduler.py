"""唯一控制循环，SQLite 决定派发。阶段四只接受尝试暂存结果，不发布正式产物。"""
import hashlib
import json
import logging
import multiprocessing
import queue
import re
import threading
import time
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path

import psutil

from core.state_kernel.store import append_event, new_id, utcnow_iso
from .resources import Budget, Claim, ResourceLedger, estimate, process_memory
from .transfer import file_lock
from .worker import worker_main

log = logging.getLogger(__name__)
ACTIVE = ("pending", "ready", "waiting_resource", "retry_wait", "running", "waiting_input")


def rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def decode(value, default=None):
    return json.loads(value) if value else ({} if default is None else default)


def process_matches(pid, started):
    if not pid or started is None:
        return False
    try:
        p = psutil.Process(int(pid))
        return abs(p.create_time() - float(started)) < .02 and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return False


def stop_identified(identities, kill=False):
    for pid, started in identities.items():
        if process_matches(pid, started):
            try:
                p = psutil.Process(pid)
                p.kill() if kill else p.terminate()
            except psutil.NoSuchProcess:
                pass


def _event(conn, row, event, payload):
    append_event(conn, type=event, user_id=row["user_id"], conversation_id=row["conversation_id"],
                 task_id=row["task_id"], run_id=row["run_id"], object_type="node",
                 object_id=row["id"], payload=payload)


def _log_insert(conn, *, user_id, conversation_id, task_id, run_id, text):
    """持久写一条执行日志（task_logs）：跨刷新/重启/中断不丢失。

    写入前统一过滤 emoji（用户要求日志无图标表情）；文本存原文，
    展示时按用户时区盖时间戳（web 层负责）。"""
    conn.execute(
        "INSERT INTO task_logs (user_id, conversation_id, task_id, run_id, text, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, conversation_id, task_id or None, run_id or None,
         strip_emoji(text), utcnow_iso()))


# 生命周期日志模板（zh/en）：界面语言由 web 层注入读取器，默认中文。
# 日志不使用 emoji 图标（纯文本输出）。
_LIFECYCLE_TEXT = {
    "failed": ("[{node}] 执行失败：{detail}",
               "[{node}] Failed: {detail}"),
    "retry_wait": ("[{node}] 网络错误，稍后自动重试：{detail}",
                   "[{node}] Network error; will retry automatically: {detail}"),
    "waiting_input": ("[{node}] 等待用户选择：{detail}",
                      "[{node}] Waiting for your selection: {detail}"),
    "cancelled": ("[{node}] 已停止：{detail}",
                  "[{node}] Stopped: {detail}"),
    "retry": ("[{node}] 已提交重试，重新排队执行",
              "[{node}] Retry submitted; re-queued"),
    "interrupted": ("[{node}] 服务中断，旧尝试已停止，可重试本节点",
                    "[{node}] Service interrupted; previous attempt stopped. You can retry this node"),
    "task_completed": ("任务完成：{label}", "Task completed: {label}"),
    "task_failed": ("任务失败：{label}", "Task failed: {label}"),
    "node_done": ("[{node}] 完成", "[{node}] Done"),
}

# 日志文本 emoji 过滤（日志/报告均为纯文本）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U00002300-\U000023FF\U0000FE0F\U0000200D]+")


def strip_emoji(text: str) -> str:
    """去掉文本中的 emoji/图标字符并修正多余空白（日志/报告统一入口）。"""
    s = _EMOJI_RE.sub("", str(text or ""))
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"(?m)^[ \t]+", "", s)
    return s


class Scheduler:
    def __init__(self, store, root, budget=None, *, handler=None):
        self.store, self.root = store, Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = budget or Budget.detect(self.root)
        self.ledger = ResourceLedger(self.budget, self.root)
        self.handler = handler
        self.batch = new_id()
        self.ctx = multiprocessing.get_context("spawn")
        self.progress = self.ctx.Queue(maxsize=128)
        self.active = {}
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self._lock_context = None
        self._view_lock = threading.RLock()
        self.last_error = None
        self._model_queue = threading.BoundedSemaphore(max(2, self.budget.model_jobs * 4))
        # 界面语言读取器（zh/en）：由 web 层注入，决定生命周期日志的文案语言
        self._lang_lookup = None

    # ── 执行日志（task_logs）：语言与写入辅助 ──

    def log_lang(self, user_id: str) -> str:
        fn = getattr(self, "_lang_lookup", None)
        if fn:
            try:
                lang = str(fn(user_id or "") or "")
                if lang in ("zh", "en"):
                    return lang
            except Exception:
                pass
        return "zh"

    def _lifecycle_text(self, user_id: str, kind: str, **fmt) -> str:
        zh, en_t = _LIFECYCLE_TEXT[kind]
        tpl = zh if self.log_lang(user_id) == "zh" else en_t
        try:
            return tpl.format(**fmt)
        except (KeyError, ValueError):
            return tpl

    def _log_raw(self, row, text: str):
        """独立写一条原始日志（进度行），不依赖事务上下文。"""
        try:
            self.store.submit_write(lambda conn: _log_insert(
                conn, user_id=row.get("user_id"),
                conversation_id=row.get("conversation_id"),
                task_id=row.get("task_id"), run_id=row.get("run_id"),
                text=text))
        except Exception:
            pass

    def start(self):
        if self.thread is not None:
            return
        # §11.1：数据根上的应用实例锁（重复启动同一任务库应明确失败）
        from core.artifacts.lock import InstanceLock
        self._instance_lock = InstanceLock(self.store.db_path.parent)
        if not self._instance_lock.acquire():
            raise RuntimeError(
                f"数据根已被另一个服务实例锁定：{self._instance_lock.path}")
        self._lock_context = file_lock(str(self.store.db_path) + ".scheduler.lock", blocking=False)
        self._lock_context.__enter__()
        try:
            self.reconcile()
            self._reconcile_commits()
        except BaseException:
            self._lock_context.__exit__(None, None, None)
            self._instance_lock.release()
            raise
        log.info("调度器生效预算 %s", asdict(self.budget))
        self.thread = threading.Thread(target=self._loop, name="node-scheduler", daemon=True)
        self.thread.start()

    def notify(self):
        self.wake.set()

    @contextmanager
    def model_request(self, timeout=120):
        if not self._model_queue.acquire(blocking=False):
            raise RuntimeError("模型接收队列已满，请稍后重试")
        try:
            with self._model_slot(timeout):
                yield
        finally:
            self._model_queue.release()

    @contextmanager
    def _model_slot(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self.stopping.is_set():
                raise RuntimeError("服务正在停止")
            with self._view_lock:
                count = self.ledger.external_model_requests + sum(c.kind == "model" for c in self.ledger.claims.values())
                if count < self.budget.model_jobs:
                    self.ledger.external_model_requests += 1
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError("模型请求队列繁忙，请稍后重试")
            self.stopping.wait(.1)
        try:
            yield
        finally:
            with self._view_lock:
                self.ledger.external_model_requests -= 1
            self.notify()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("调度器仍有活动写入者，不能释放唯一控制锁")
            self.thread = None
        self.progress.close()
        self.progress.join_thread()
        if self._lock_context:
            self._lock_context.__exit__(None, None, None)
            self._lock_context = None
        if getattr(self, "_instance_lock", None):
            self._instance_lock.release()
            self._instance_lock = None

    def _loop(self):
        while not self.stopping.is_set() or self.active:
            started = time.monotonic()
            try:
                with self._view_lock:
                    self._poll()
                    self._refresh()
                    if not self.stopping.is_set():
                        self._dispatch_ready()
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                log.exception("调度循环失败，保留活动尝试并停止本次派发")
            self.wake.wait(max(.01, 1 - (time.monotonic() - started)))
            self.wake.clear()

    def _node_rows(self, conn, where="1=1", args=()):
        return rows(conn, "SELECT n.*, r.task_id, r.task_version, r.frozen_inputs, r.scenario_binding,"
                    " r.cancel_requested, r.superseded_by, r.created_at AS run_created,"
                    " t.user_id, t.project_id, t.conversation_id, t.priority, t.current_run_id,"
                    " t.version AS current_version, t.summary_status, t.slots"
                    " FROM nodes n JOIN runs r ON r.id=n.run_id JOIN tasks t ON t.id=r.task_id WHERE " + where, args)

    @staticmethod
    def _valid(row):
        if row["cancel_requested"] or row["superseded_by"] or row["current_run_id"] != row["run_id"] or row["summary_status"] == "cancelled":
            return False
        snapshot = decode(row["frozen_inputs"]).get("snapshot", {})
        slots = decode(row["slots"])
        # 状态/优先级更新可递增对象版本；科学身份及当前运行共同决定授权有效性。
        from core.agent.understanding.slotbook import SlotBook
        current, original = SlotBook(slots), SlotBook(snapshot.get("slot_values", {}))
        return all(current.value(k) == original.value(k) for k in ("region", "time", "product_mode", "datasets"))

    def _refresh(self):
        def tx(conn):
            for row in self._node_rows(conn, "n.status IN ('pending','ready','waiting_resource','retry_wait','waiting_input','blocked')"):
                state, reason = row["status"], row["wait_reason"]
                if not self._valid(row):
                    state, reason = "cancelled", "任务已取消或运行已被替代"
                elif state not in ("waiting_input",):
                    deps = [r[0] for r in conn.execute("SELECT p.status FROM node_edges e JOIN nodes p ON p.id=e.predecessor_id WHERE e.successor_id=?", (row["id"],))]
                    if any(d in ("failed", "cancelled", "blocked") for d in deps):
                        state, reason = "blocked", "上游节点失败或取消"
                    elif all(d == "succeeded" for d in deps):
                        if not row["retry_at"] or row["retry_at"] <= time.time():
                            state = "ready" if state != "waiting_resource" else state
                            if state == "ready":
                                reason = None
                    else:
                        state, reason = "pending", "等待上游节点"
                if (state, reason) != (row["status"], row["wait_reason"]):
                    conn.execute("UPDATE nodes SET status=?, wait_reason=?, ready_at=CASE WHEN ?='ready' THEN COALESCE(ready_at,?) ELSE ready_at END WHERE id=?",
                                 (state, reason, state, utcnow_iso(), row["id"]))
                    _event(conn, row, "node.state", {"status": state, "reason": reason})
            for run in rows(conn, "SELECT r.id,r.task_id,t.current_run_id,t.summary_status,"
                            " t.user_id,t.conversation_id,t.label FROM runs r JOIN tasks t ON t.id=r.task_id"
                            " WHERE r.status NOT IN ('completed','cancelled')"):
                states = [r[0] for r in conn.execute("SELECT status FROM nodes WHERE run_id=?", (run["id"],))]
                if not states:
                    continue
                if "running" in states:
                    status = "running"
                elif "waiting_input" in states:
                    status = "awaiting_info"
                elif "failed" in states or "blocked" in states:
                    status = "failed"
                elif all(s == "succeeded" for s in states):
                    status = "completed"
                elif "cancelled" in states:
                    status = "cancelled"
                else:
                    status = "queued"
                conn.execute("UPDATE runs SET status=?,updated_at=? WHERE id=? AND status<>?", (status, utcnow_iso(), run["id"], status))
                if run["current_run_id"] == run["id"] and run["summary_status"] != status:
                    conn.execute("UPDATE tasks SET summary_status=?,version=version+1,updated_at=? WHERE id=?", (status, utcnow_iso(), run["task_id"]))
                    # 任务总体完成/失败：持久一条汇总日志（用户实测：失败与完成
                    # 必须在日志里可见，且不因刷新/重启消失）
                    if status in ("completed", "failed"):
                        kind = "task_completed" if status == "completed" else "task_failed"
                        uid = run["user_id"]
                        label = run["label"]
                        zh, en_t = _LIFECYCLE_TEXT[kind]
                        tpl = zh if self.log_lang(uid) == "zh" else en_t
                        _log_insert(conn, user_id=uid,
                                    conversation_id=run["conversation_id"],
                                    task_id=run["task_id"], run_id=run["id"],
                                    text=tpl.format(label=label or run["task_id"]))
        self.store.submit_write(tx)

    def _context(self, row):
        ancestors = self.store.read(lambda c: rows(c,
            "WITH RECURSIVE ancestors(id) AS (SELECT predecessor_id FROM node_edges WHERE successor_id=?"
            " UNION SELECT e.predecessor_id FROM node_edges e JOIN ancestors a ON e.successor_id=a.id)"
            " SELECT n.id AS node_id,n.result,n.node_type FROM ancestors a JOIN nodes n ON n.id=a.id WHERE n.status='succeeded' ORDER BY n.exec_order", (row["id"],)))
        context = {}
        for parent in ancestors:
            result = decode(parent["result"])
            ctx = dict(result.get("context") or {})
            # 历史兼容：修复前提交的节点 result.context 被覆盖成提交信息，
            # 下游拿不到输入映射与数据上下文（files/pipeline_data 等）——
            # 从完成凭据补回完整 context，并把路径重定向到发布目录
            # （staging 产物已被 move）。新数据 context 完整，自动跳过。
            published = str(result.get("published_dir") or "")
            if published:
                recovered, staging_work = self._recover_context(parent["node_id"])
                if recovered:
                    if staging_work:
                        from core.artifacts.publisher import _rebase_paths
                        recovered = _rebase_paths(
                            recovered, staging_work.rstrip("/"),
                            published.rstrip("/"))
                    # 缺什么补什么（setdefault）：既不覆盖新数据，也能修复
                    # 旧合并只补了部分键（如只有 files、缺 pipeline_data）的情况
                    for k, v in recovered.items():
                        ctx.setdefault(k, v)
            context.update(ctx)
        return context

    def _recover_context(self, node_id: str):
        """从节点的完成凭据恢复完整上下文（历史提交兼容）。

        返回 (context, staging_work)；失败返回 ({}, "")。
        """
        try:
            attempt = self.store.read(lambda c: c.execute(
                "SELECT result_path, staging_dir FROM attempts WHERE node_id=?"
                " AND status='succeeded' ORDER BY attempt_no DESC LIMIT 1",
                (node_id,)).fetchone())
            if not attempt:
                return {}, ""
            staging_work = str(Path(attempt[1]) / "work") if attempt[1] else ""
            candidates = []
            if attempt[0]:
                candidates.append(Path(attempt[0]))
            if attempt[1]:
                candidates.append(Path(attempt[1]) / "completion.json")
            for path in candidates:
                try:
                    envelope = json.loads(path.read_text(encoding="utf-8"))
                    ctx = dict(((envelope.get("result") or {})
                                .get("context") or {}))
                    if ctx:
                        return ctx, staging_work
                except (OSError, ValueError):
                    continue
        except Exception:
            pass
        return {}, ""

    def _dispatch_ready(self):
        candidates = self.store.read(lambda c: self._node_rows(c, "n.status IN ('ready','waiting_resource')"))
        eligible = []
        prefetched = self.store.read(lambda c: c.execute(
            "SELECT COUNT(DISTINCT a.run_id) FROM nodes a WHERE a.node_type='acquire_asset' AND a.status IN ('running','succeeded')"
            " AND EXISTS (SELECT 1 FROM nodes p WHERE p.run_id=a.run_id AND p.node_type='prepare_local' AND p.status IN ('pending','ready','waiting_resource'))").fetchone()[0])
        for row in candidates:
            if not self._valid(row):
                continue
            context = self._context(row)
            peak = self.store.read(lambda c: c.execute("SELECT MAX(a.peak_memory_bytes) FROM attempts a JOIN nodes n ON n.id=a.node_id WHERE n.node_type=? AND a.status='succeeded'", (row["node_type"],)).fetchone()[0]) or 0
            claim = estimate(row["node_type"], decode(row["frozen_inputs"])["snapshot"], context, self.budget, peak)
            if self.handler:
                # 测试专用规模声明；没有生产 HTTP 入口可注入 handler 或资源需求。
                override = decode(row["params"]).get("test_claim")
                if override:
                    claim = Claim(**override)
            ok, reason = self.ledger.can_fit(claim)
            if row["node_type"] == "acquire_asset" and prefetched >= self.budget.prefetch:
                ok, reason = False, "下一任务预取包已达上限"
            if ok:
                eligible.append((row, claim, context))
            else:
                # 静态上限不满足（预估需求本身超过部署预算）：等待无意义，
                # 直接判失败并给出处置建议；其余情况排队等待
                if self.ledger.static_block(claim):
                    self._fail_static(row, reason)
                else:
                    self._wait_resource(row, reason)
        # 逐个派发并立即更新账本，每个计算机会只统计当时确实能执行的同优先级任务。
        while eligible:
            def key(item):
                row, claim, _ = item
                if claim.kind == "compute":
                    return (-row["priority"], 0 if row["protected_at"] else 1,
                            row["protected_at"] or "", row["node_type"] == "gapfill", -int(row["exec_order"] or 0), row["run_created"])
                return (-row["priority"], 1, "", False, 0, row["run_created"])
            eligible.sort(key=key)
            row, claim, context = eligible.pop(0)
            ok, reason = self.ledger.can_fit(claim)
            if not ok:
                if self.ledger.static_block(claim):
                    self._fail_static(row, reason)
                else:
                    self._wait_resource(row, reason)
                continue
            skipped = [other[0] for other in eligible if claim.kind == other[1].kind == "compute"
                       and other[0]["priority"] == row["priority"] and self.ledger.can_fit(other[1])[0]]
            self._launch(row, claim, context, skipped)
            if row["node_type"] == "acquire_asset":
                prefetched += 1
                if prefetched >= self.budget.prefetch:
                    eligible = [e for e in eligible if e[0]["node_type"] != "acquire_asset"]

    def _wait_resource(self, row, reason):
        if row["status"] == "waiting_resource" and row["wait_reason"] == reason:
            return
        def tx(conn):
            conn.execute("UPDATE nodes SET status='waiting_resource',wait_reason=? WHERE id=?", (reason, row["id"]))
            _event(conn, row, "node.waiting_resource", {"reason": reason})
        self.store.submit_write(tx)

    def _fail_static(self, row, reason):
        """静态资源上限不满足：直接判失败（不再无限排队）。

        与 _wait_resource 的区别：该原因与当前系统状态无关（预估需求本身
        超过部署预算），等待/重试永远不会满足——显式失败并附处置建议，
        用户修正部署内存后可用重试继续。
        """
        aid = new_id()

        def tx(conn):
            current = self._node_rows(conn, "n.id=?", (row["id"],))[0]
            if current["status"] not in ("ready", "waiting_resource") \
                    or not self._valid(current):
                return
            if conn.execute("SELECT 1 FROM attempts WHERE node_id=?"
                            " AND process_exited=0", (row["id"],)).fetchone():
                return
            number = conn.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts"
                " WHERE node_id=?", (row["id"],)).fetchone()[0]
            conn.execute(
                "INSERT INTO attempts(id,node_id,attempt_no,status,started_at,"
                " finished_at,error,error_kind,process_exited)"
                " VALUES(?,?,?,'failed',?,?,?,'resource',1)",
                (aid, row["id"], number, utcnow_iso(), utcnow_iso(), reason))
            conn.execute("UPDATE nodes SET status='failed', wait_reason=?"
                         " WHERE id=?", (reason, row["id"]))
            _event(conn, row, "node.failed", {
                "attempt_id": aid, "error_kind": "resource",
                "error": reason})
        self.store.submit_write(tx)

    def _launch(self, row, claim, context, skipped):
        aid, authorization = new_id(), new_id()
        stage = self.root / row["run_id"] / "staging" / aid
        stage.mkdir(parents=True, exist_ok=False)
        def tx(conn):
            current = self._node_rows(conn, "n.id=?", (row["id"],))[0]
            if current["status"] not in ("ready", "waiting_resource") or not self._valid(current):
                return False
            # 撤销授权但旧进程未退出时仍不允许重派。
            if conn.execute("SELECT 1 FROM attempts WHERE node_id=? AND process_exited=0", (row["id"],)).fetchone():
                return False
            number = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE node_id=?", (row["id"],)).fetchone()[0]
            conn.execute("INSERT INTO attempts(id,node_id,attempt_no,authorization,dispatch_batch,staging_dir,status,started_at,resource_claim,task_version) VALUES(?,?,?,?,?,?,'running',?,?,?)",
                         (aid, row["id"], number, authorization, self.batch, str(stage), utcnow_iso(), json.dumps(asdict(claim)), row["task_version"]))
            conn.execute("INSERT INTO resource_reservations(attempt_id,claim) VALUES(?,?)", (aid, json.dumps(asdict(claim))))
            conn.execute("UPDATE nodes SET status='running',wait_reason=NULL,skipped_opportunities=0,protected_at=NULL WHERE id=?", (row["id"],))
            for missed in skipped:
                conn.execute("UPDATE nodes SET skipped_opportunities=skipped_opportunities+1,protected_at=CASE WHEN skipped_opportunities>=1 THEN COALESCE(protected_at,?) ELSE protected_at END WHERE id=?", (utcnow_iso(), missed["id"]))
                if int(missed["skipped_opportunities"]) >= 1:
                    missed["protected_at"] = missed["protected_at"] or utcnow_iso()
                missed["skipped_opportunities"] += 1
            _event(conn, row, "node.dispatched", {"attempt_id": aid, "attempt_no": number, "claim": asdict(claim)})
            # 第五阶段（§10.4）：领取上游产物活动引用；尝试结束释放，
            # 运行级 pinned 持久依赖不受影响。
            conn.execute(
                "INSERT OR IGNORE INTO artifact_refs (artifact_id, consumer_type,"
                " consumer_id, usage, pinned) "
                "SELECT art.id, 'attempt', ?, 'node_input', 0 "
                "FROM node_edges e JOIN nodes pred ON pred.id = e.predecessor_id "
                "JOIN attempts pa ON pa.node_id = pred.id AND pa.status = 'succeeded' "
                "JOIN artifacts art ON art.attempt_id = pa.id "
                "WHERE e.successor_id = ? AND e.run_id = ?",
                (aid, row["id"], row["run_id"]))
            return True
        if not self.store.submit_write(tx):
            return
        self.ledger.claims[aid] = claim  # 已用最新读数通过准入并原子持久预留
        parent, child = self.ctx.Pipe(duplex=True)
        cancel = self.ctx.Event()
        spec = {"attempt_id": aid, "authorization": authorization, "batch": self.batch,
                "task_version": row["task_version"], "node": row, "staging_dir": str(stage),
                "snapshot": decode(row["frozen_inputs"])["snapshot"], "context": context,
                "claim": asdict(claim), "disk_margin": self.budget.disk_margin,
                "cache_root": str(self.root / "cache"), "cache_limit": self.budget.disk_cache,
                # 运行时交互模式（不随编译冻结）：“完全执行”下暂停点自动代选
                "runtime_exec_mode": self._runtime_exec_mode(row)}
        if self.handler:
            spec["handler"] = self.handler
        process = self.ctx.Process(target=worker_main, args=(spec, child, self.progress, cancel), name=f"gtai-node-{aid}")
        active = {"process": process, "control": parent, "cancel": cancel, "row": row,
                  "spec": spec, "start": time.monotonic(), "stopping": None, "reason": None, "started": False}
        try:
            process.start()
            self.active[aid] = active
        except Exception as e:
            self._finish(aid, active, {"status": "failed", "error": f"工作进程启动失败 {type(e).__name__}", "error_kind": "startup"})
            parent.close()
        finally:
            child.close()

    def _runtime_exec_mode(self, row) -> str:
        """读取该对话当前的交互模式（交互模式不随编译快照冻结）。

        “完全执行”下暂停点（如配对选择）自动代选；读取失败返回空串，
        调用方回退到编译快照里的值（保守行为不变）。
        """
        try:
            uid = str(row.get("user_id") or "")
            conv_pk = str(row.get("conversation_id") or "")
            if not uid or not conv_pk:
                return ""
            legacy = self.store.read(lambda c: c.execute(
                "SELECT legacy_conv_id FROM conversations WHERE id = ?",
                (conv_pk,)).fetchone())
            if not legacy or not legacy[0]:
                return ""
            conv_file = (self.store.db_path.parent.parent / "users" / uid
                         / "conversations" / f"{legacy[0]}.json")
            if not conv_file.is_file():
                return ""
            data = json.loads(conv_file.read_text(encoding="utf-8"))
            from core.agent.orchestrator.exec_mode import normalize
            return normalize(data.get("exec_mode"), default="")
        except Exception:
            return ""

    def _request_stop(self, aid, active, reason, error_kind):
        if active["stopping"] is not None:
            return
        active["stopping"] = time.monotonic()
        active["reason"] = {"status": "cancelled" if error_kind == "cancelled" else "failed", "error": reason, "error_kind": error_kind}
        active["cancel"].set()
        self.store.submit_write(lambda c: c.execute("UPDATE attempts SET authorization=NULL,status='cancelling',error=?,error_kind=? WHERE id=?", (reason, error_kind, aid)))

    def _poll(self):
        for _ in range(128):
            try:
                item = self.progress.get_nowait()
            except queue.Empty:
                break
            active = self.active.get(item["attempt_id"])
            if active:
                # 仅最新进度常驻，关键状态另走 events；不累计海量日志。
                active["progress"] = item["message"]
                # 进度日志持久化（task_logs）：变化才记，单次尝试限流 3 秒一条，
                # 避免下载分块进度刷爆日志表；刷新/重启后日志面板仍能看到过程。
                msg = item.get("message") or ""
                if msg and active.get("last_log_msg") != msg:
                    active["last_log_msg"] = msg
                    now_ts = time.monotonic()
                    if now_ts - active.get("last_log_at", 0.0) >= 3.0:
                        active["last_log_at"] = now_ts
                        row = active.get("row") or {}
                        node_key = str(row.get("node_key") or "")
                        taskid = {
                            "user_id": row.get("user_id"),
                            "conversation_id": row.get("conversation_id"),
                            "task_id": row.get("task_id"),
                            "run_id": row.get("run_id"),
                        }
                        self._log_raw(taskid, f"[{node_key}] {msg}" if node_key else msg)
        for aid, active in list(self.active.items()):
            process, control = active["process"], active["control"]
            writers = active.setdefault("writers", {})
            if process.is_alive():
                try:
                    for child in psutil.Process(process.pid).children(recursive=True):
                        writers[child.pid] = child.create_time()
                except psutil.NoSuchProcess:
                    pass
            try:
                while control.poll():
                    message = control.recv()
                    if isinstance(message, dict) and "pid" in message and not active["started"]:
                        if message["attempt_id"] != aid or message["authorization"] != active["spec"]["authorization"]:
                            raise RuntimeError("进程身份不匹配")
                        self.store.submit_write(lambda c: c.execute("UPDATE attempts SET process_pid=?,process_started_at=?,heartbeat_at=? WHERE id=?", (message["pid"], str(message["started"]), utcnow_iso(), aid)))
                        active["identity"] = message
                        control.send("start")
                        active["started"] = True
            except (EOFError, OSError):
                pass
            if process.is_alive():
                measured = process_memory(process.pid)
                self.ledger.measured[aid] = measured
                active["peak"] = max(active.get("peak", 0), measured)
                stage = Path(active["spec"]["staging_dir"])
                self.ledger.written[aid] = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and not p.is_symlink())
                current = self.store.read(lambda c: self._node_rows(c, "n.id=?", (active["row"]["id"],)))[0]
                if not self._valid(current) or self.stopping.is_set():
                    self._request_stop(aid, active, "任务取消或服务正在停止", "cancelled")
                elif time.monotonic() - active["start"] > self.budget.timeout:
                    self._request_stop(aid, active, "节点执行超时", "timeout")
                elif measured > self.budget.memory or self.ledger.pressure()["memory_pressure"]:
                    self._request_stop(aid, active, "节点内存超限或容器余量不足", "resource")
                elif shutil_disk_free(self.root) < self.budget.disk_margin:
                    self._request_stop(aid, active, "磁盘低于紧急余量", "resource")
                if active["stopping"] is not None and time.monotonic() - active["stopping"] > 5:
                    stop_identified(writers, kill=time.monotonic() - active["stopping"] > 10)
                    process.terminate()
                    if time.monotonic() - active["stopping"] > 10:
                        process.kill()
                self.store.submit_write(lambda c: c.execute("UPDATE attempts SET heartbeat_at=?,peak_memory_bytes=? WHERE id=?", (utcnow_iso(), active["peak"], aid)))
                continue
            process.join(timeout=0)
            if any(process_matches(pid, started) for pid, started in writers.items()):
                self._request_stop(aid, active, "节点主进程退出后仍有子写入者，正在收尾", "crash")
                stop_identified(writers, kill=time.monotonic() - active["stopping"] > 5)
                continue
            completion = Path(active["spec"]["staging_dir"]) / "completion.json"
            result = active["reason"]
            if result is None:
                if process.exitcode == 0 and completion.is_file():
                    try:
                        result = json.loads(completion.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        result = None
                if result is None:
                    result = {"status": "failed", "error": f"工作进程异常退出，退出码 {process.exitcode}", "error_kind": "crash"}
            self._finish(aid, active, result)
            control.close()
            process.close()
            del self.active[aid]

    def _finish(self, aid, active, envelope):
        row, spec = active["row"], active["spec"]
        # ── 第五阶段：有暂存产物的成功节点走两步提交（§10.2）──
        # 准备 → 发布 → 确认后节点才 succeeded；产物登记血缘。
        if envelope.get("status") == "succeeded":
            outputs = self._collect_staged_outputs(spec)
            if outputs:
                self._finish_commit(aid, active, envelope, outputs)
                self.ledger.release(aid)
                return
        def tx(conn):
            now = utcnow_iso()
            current = self._node_rows(conn, "n.id=?", (row["id"],))[0]
            a = rows(conn, "SELECT * FROM attempts WHERE id=?", (aid,))[0]
            status, error = envelope.get("status", "failed"), envelope.get("error")
            if status == "succeeded" and (not self._valid(current) or a["authorization"] != envelope.get("authorization")
                    or envelope.get("batch") != self.batch or envelope.get("task_version") != row["task_version"]):
                status, error = "cancelled", "迟到或失效授权的结果只保留在暂存区"
            result = envelope.get("result", {}) if status == "succeeded" else {}
            peak = max(active.get("peak", 0), envelope.get("peak_memory_bytes", 0))
            conn.execute("UPDATE attempts SET status=?,authorization=NULL,process_exited=1,finished_at=?,error=?,error_kind=?,peak_memory_bytes=?,result_path=? WHERE id=?",
                         (status, now, error, envelope.get("error_kind"), peak, str(Path(spec["staging_dir"]) / "completion.json"), aid))
            conn.execute("UPDATE resource_reservations SET released_at=? WHERE attempt_id=?", (now, aid))
            node_status = status
            if status == "failed" and envelope.get("error_kind") == "network" and a["attempt_no"] < self.budget.network_attempts:
                node_status = "retry_wait"
                conn.execute("UPDATE nodes SET retry_at=? WHERE id=?", (time.time() + min(30, 2 ** a["attempt_no"]), row["id"]))
            if status == "succeeded" and result.get("question"):
                from core.state_kernel.questions import create_question
                q = result["question"]
                question_version = current["current_version"] + 1
                conn.execute("UPDATE tasks SET summary_status='awaiting_info',version=?,updated_at=? WHERE id=?", (question_version, now, row["task_id"]))
                qid = create_question(conn, user_id=row["user_id"], conversation_id=row["conversation_id"], qtype="execution_approval", prompt=q["prompt"],
                                      targets=[{"task_id": row["task_id"], "task_version": question_version, "run_id": row["run_id"], "field": "execution"}],
                                      candidates=q.get("candidates", []), answer_constraint={"node_id": row["id"], "execution": True, "payload": q.get("payload")})
                node_status = "waiting_input"
                result["question_id"] = qid
                error = q["prompt"]
            if status == "succeeded" and result.get("selection"):
                selection = json.dumps(result["selection"], ensure_ascii=False, sort_keys=True)
                prior = current["scenario_binding"]
                if prior and prior != selection:
                    raise RuntimeError("同一运行的场景选择只能绑定一次")
                conn.execute("UPDATE runs SET scenario_binding=? WHERE id=? AND scenario_binding IS NULL", (selection, row["run_id"]))
            if status == "succeeded" and result.get("decision"):
                self._decision(conn, current, result)
            conn.execute("UPDATE nodes SET status=?,result=?,wait_reason=? WHERE id=?", (node_status, json.dumps(result, ensure_ascii=False), error, row["id"]))
            _event(conn, row, "node." + node_status, {"attempt_id": aid, "status": node_status, "error": error, "error_kind": envelope.get("error_kind"), "peak_memory_bytes": peak, "staged_only": True})
            # 生命周期日志持久化：失败原因/等待回答/停止都要在日志里可见（不因刷新消失）
            if node_status == "failed":
                _log_insert(conn, user_id=row["user_id"], conversation_id=row["conversation_id"],
                            task_id=row["task_id"], run_id=row["run_id"],
                            text=self._lifecycle_text(row["user_id"], "failed",
                                                      node=row["node_key"],
                                                      detail=str(error or "")[:300]))
            elif node_status == "retry_wait":
                _log_insert(conn, user_id=row["user_id"], conversation_id=row["conversation_id"],
                            task_id=row["task_id"], run_id=row["run_id"],
                            text=self._lifecycle_text(row["user_id"], "retry_wait",
                                                      node=row["node_key"],
                                                      detail=str(error or "")[:300]))
            elif node_status == "waiting_input":
                _log_insert(conn, user_id=row["user_id"], conversation_id=row["conversation_id"],
                            task_id=row["task_id"], run_id=row["run_id"],
                            text=self._lifecycle_text(row["user_id"], "waiting_input",
                                                      node=row["node_key"],
                                                      detail=str(error or "")[:300]))
            elif node_status == "cancelled":
                _log_insert(conn, user_id=row["user_id"], conversation_id=row["conversation_id"],
                            task_id=row["task_id"], run_id=row["run_id"],
                            text=self._lifecycle_text(row["user_id"], "cancelled",
                                                      node=row["node_key"],
                                                      detail=str(error or "")[:300]))
        self.store.submit_write(tx)
        self.ledger.release(aid)
        self._release_attempt_refs(aid)

    # ── 第五阶段：产物与恢复（§10–11）──────────────────

    def _collect_staged_outputs(self, spec):
        """从尝试暂存工作区收集本节点真实输出（排除输入映射/兼容清单）。"""
        from core.artifacts.publisher import collect_outputs
        work = Path(spec["staging_dir"]) / "work"
        if not work.is_dir():
            return []
        inputs_manifest = {}
        marker = Path(spec["staging_dir"]) / "inputs.json"
        if marker.is_file():
            try:
                inputs_manifest = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                inputs_manifest = {}
        return collect_outputs(work, inputs_manifest)

    def _finish_commit(self, aid, active, envelope, outputs):
        """两步提交：准备 → 文件发布 → 数据库确认（§10.2）。

        任一步失败或取消竞态：意向作废/节点如实失败，暂存保留，
        绝不让半成品进入正式结果（9.5 通过标准）。
        """
        from core.artifacts.publisher import (
            CommitError,
            abandon_commit_tx,
            bridge_artifacts,
            confirm_commit_tx,
            prepare_commit_tx,
            publish_outputs,
        )
        row, spec = active["row"], active["spec"]
        staging = Path(spec["staging_dir"])
        peak = max(active.get("peak", 0), envelope.get("peak_memory_bytes", 0))

        def prepare(conn):
            current = self._node_rows(conn, "n.id=?", (row["id"],))[0]
            a = rows(conn, "SELECT * FROM attempts WHERE id=?", (aid,))[0]
            if not self._valid(current) or a["authorization"] != envelope.get("authorization") \
                    or envelope.get("batch") != self.batch or envelope.get("task_version") != row["task_version"]:
                raise CommitError("迟到或失效授权的结果只保留在暂存区")
            # 恢复场景（§8.4/§11.2）：重启前已登记提交准备的，复用既有意向，
            # 不扩大输出清单；新提交才登记意向。
            existing = conn.execute(
                "SELECT id, outputs FROM commit_intents WHERE attempt_id = ?"
                " AND status = 'prepared'", (aid,)).fetchone()
            if existing:
                conn.execute("UPDATE attempts SET status='committing' WHERE id=?", (aid,))
                conn.execute("UPDATE nodes SET status='committing' WHERE id=?", (row["id"],))
                return existing[0], json.loads(existing[1])
            intent_id = prepare_commit_tx(
                conn, attempt_id=aid, staging_dir=str(staging), root=self.root,
                node_key=current["node_key"], attempt_no=a["attempt_no"],
                outputs=outputs, task_version=row["task_version"])
            # §8.2：Running → Committing（授权保留到确认前最后核对）
            conn.execute("UPDATE attempts SET status='committing' WHERE id=?", (aid,))
            conn.execute("UPDATE nodes SET status='committing' WHERE id=?", (row["id"],))
            return intent_id, outputs

        intent_id = None
        try:
            intent_id, outputs = self.store.submit_write(prepare)
            intent_dir = self.store.read(lambda c: c.execute(
                "SELECT target_dir FROM commit_intents WHERE id=?",
                (intent_id,)).fetchone()[0])
            published = publish_outputs(str(staging), intent_dir, outputs)

            def confirm(conn):
                result = confirm_commit_tx(conn, intent_id=intent_id, published=published)
                conn.execute(
                    "UPDATE attempts SET peak_memory_bytes=?, result_path=? WHERE id=?",
                    (peak, str(staging / "completion.json"), aid))
                return result

            confirmed = self.store.submit_write(confirm)
            # 第六阶段（§12.4）：正式产物桥接进项目视图目录（地图/精度/下载
            # 按产物编号解析）；失败只记日志，不影响产物本身。
            bridge_artifacts(confirmed.get("bridges") or [])
            self._release_attempt_refs(aid)
            return
        except BaseException as e:
            # 取消竞态/发布失败：意向作废，节点如实失败，暂存不清理（供对账）
            err_msg = str(e)

            def tx(conn):
                conn.execute(
                    "UPDATE attempts SET status='failed', authorization=NULL,"
                    " process_exited=1, error=?, error_kind='commit', finished_at=?"
                    " WHERE id=?", (err_msg[:1500], utcnow_iso(), aid))
                conn.execute(
                    "UPDATE resource_reservations SET released_at=? WHERE attempt_id=?",
                    (utcnow_iso(), aid))
                conn.execute(
                    "UPDATE nodes SET status='failed', wait_reason=? WHERE id=?",
                    (f"两步提交未完成，产物保留在暂存区：{err_msg[:300]}", row["id"]))
                _event(conn, row, "node.failed", {
                    "attempt_id": aid, "error_kind": "commit",
                    "error": err_msg[:500]})
                if intent_id:
                    abandon_commit_tx(conn, intent_id=intent_id, reason=err_msg[:300])
            self.store.submit_write(tx)
            self.ledger.release(aid)
            self._release_attempt_refs(aid)

    def _release_attempt_refs(self, aid):
        """尝试结束：释放其活动引用（run 级 pinned 持久依赖保留，§10.4）。"""
        from core.artifacts.refs import release_consumer_tx
        try:
            self.store.submit_write(
                lambda c: release_consumer_tx(c, consumer_type="attempt",
                                              consumer_id=aid))
        except Exception as e:  # noqa: BLE001 — 引用释放失败不影响结果
            log.warning("尝试 %s 活动引用释放失败：%s", aid, e)

    def _reconcile_commits(self):
        """启动对账补全（§11.2 提交中断处理表）：逐条核对提交意向。"""
        from core.artifacts.publisher import (
            CommitError,
            abandon_commit_tx,
            confirm_commit_tx,
            publish_outputs,
        )
        from core.artifacts.cleanup import cleanup_stale_intents_tx, sweep_missing_tx

        def _abandon(intent_id, node_id, reason):
            def tx(conn):
                abandon_commit_tx(conn, intent_id=intent_id, reason=reason)
                conn.execute(
                    "UPDATE nodes SET status='failed', wait_reason=? WHERE id=?",
                    (f"提交未完成（对账）：{reason[:200]}", node_id))
            self.store.submit_write(tx)

        intents = self.store.read(lambda c: rows(
            c,
            "SELECT ci.id, ci.status, ci.source_dir, ci.target_dir, ci.outputs,"
            " ci.attempt_id, a.authorization, a.status AS a_status, a.node_id,"
            " n.run_id, n.status AS node_status, r.cancel_requested,"
            " r.status AS run_status"
            " FROM commit_intents ci JOIN attempts a ON a.id = ci.attempt_id"
            " JOIN nodes n ON n.id = a.node_id JOIN runs r ON r.id = n.run_id"
            " WHERE ci.status IN ('prepared','abandoned')"))
        for it in intents:
            outputs = json.loads(it["outputs"])
            valid = it["cancel_requested"] in (0, None) and it["run_status"] not in ("cancelled", "superseded")
            if it["status"] == "abandoned":
                continue  # 未采用目录由 cleanup_stale_intents_tx 统一清理
            if not valid:
                _abandon(it["id"], it["node_id"], "运行已取消或被替代")
                continue
            target = Path(it["target_dir"])
            published_ok = target.exists() and all(
                (target / o["path"]).is_file() for o in outputs)
            if published_ok:
                # 「文件已发布，数据库未确认」→ 校验后原子补确认
                try:
                    self.store.submit_write(lambda c: confirm_commit_tx(
                        c, intent_id=it["id"],
                        published={"published_dir": str(target)}))
                except CommitError as e:
                    _abandon(it["id"], it["node_id"], str(e))
                continue
            staging = Path(it["source_dir"])
            work = staging / "work"
            if it["a_status"] in ("running", "committing") and it["authorization"] \
                    and all((work / o["path"]).is_file() for o in outputs):
                # 「已有提交准备，文件未移动」→ 验证仍有效后继续发布
                try:
                    published = publish_outputs(str(staging), str(target), outputs)
                    self.store.submit_write(lambda c: confirm_commit_tx(
                        c, intent_id=it["id"], published=published))
                except (CommitError, OSError) as e:
                    _abandon(it["id"], it["node_id"], str(e))
            else:
                # 暂存输出不完整且未发布：节点未成功；按对账政策标记失败
                _abandon(it["id"], it["node_id"], "暂存输出不完整且未发布")

        # 已确认产物可用性对账（只查登记过的，不扫全仓库，§11.1 第 4 条）
        try:
            self.store.submit_write(lambda c: sweep_missing_tx(c))
        except Exception as e:  # noqa: BLE001
            log.warning("产物可用性对账失败：%s", e)
        # abandoned 意向的未采用目录清理（限于运行根内）
        try:
            self.store.submit_write(lambda c: cleanup_stale_intents_tx(
                c, run_roots=[str(self.root)]))
        except Exception as e:  # noqa: BLE001
            log.warning("未采用目录清理失败：%s", e)

    def _decision(self, conn, row, result):
        decision = result["decision"]
        context = result.get("context", {})
        round_no = len(context.get("rounds", [])) - 1
        conn.execute("INSERT INTO decisions(id,run_id,trigger_node_id,trigger_round,trigger_seq,role,param_patch,rationale,budget_spent,created_at) VALUES(?,?,?,?,1,'train',?,?,?,?)",
                     (new_id(), row["run_id"], row["id"], round_no, json.dumps(decision.get("new_params", {})), decision.get("reason", ""), json.dumps(context.get("train_state", {})), utcnow_iso()))
        task_acc = decode(conn.execute("SELECT accumulated FROM tasks WHERE id=?", (row["task_id"],)).fetchone()[0])
        task_acc["tuning_continuity"] = context.get("train_state", {})
        task_acc["tuning_rounds_used"] = int(task_acc.get("tuning_rounds_used", 0)) + (1 if round_no > 0 else 0)
        conn.execute("UPDATE tasks SET accumulated=? WHERE id=?", (json.dumps(task_acc, ensure_ascii=False), row["task_id"]))
        if decision.get("action") != "adjust":
            return
        rf, decide = new_id(), new_id()
        successors = [r[0] for r in conn.execute("SELECT successor_id FROM node_edges WHERE predecessor_id=?", (row["id"],))]
        conn.execute("DELETE FROM node_edges WHERE predecessor_id=?", (row["id"],))
        # 后续节点顺延，避免浮点轮次在多轮后越过 promote_best 破坏上下文顺序。
        conn.execute("UPDATE nodes SET exec_order=exec_order+2 WHERE run_id=? AND exec_order>?", (row["run_id"], row["exec_order"]))
        for nid, kind, order in ((rf, "rf_round", row["exec_order"] + 1), (decide, "train_decision", row["exec_order"] + 2)):
            conn.execute("INSERT INTO nodes(id,run_id,node_key,node_type,params,status,exec_order) VALUES(?,?,?,?,?,'pending',?)",
                         (nid, row["run_id"], f"{kind}_{round_no+1}", kind, json.dumps({"rf_params": decision["new_params"], "round": round_no + 1}), order))
        for before, after in [(row["id"], rf), (rf, decide), *[(decide, s) for s in successors]]:
            conn.execute("INSERT INTO node_edges VALUES(?,?,?)", (row["run_id"], before, after))

    def reconcile(self):
        old = self.store.read(lambda c: rows(c, "SELECT * FROM attempts WHERE process_exited=0"))
        for attempt in old:
            aid = attempt["id"]
            identity = {"pid": attempt["process_pid"], "started": attempt["process_started_at"]}
            marker = Path(attempt["staging_dir"]) / "identity.json"
            if not identity["pid"] and marker.is_file():
                saved = json.loads(marker.read_text())
                if saved.get("attempt_id") == aid and saved.get("batch") == attempt["dispatch_batch"]:
                    identity = saved
            if process_matches(identity["pid"], identity["started"]):
                # 只有数据库身份与进程创建时间吻合才停止，PID 复用绝不误杀。
                process = psutil.Process(identity["pid"])
                writers = {p.pid: p.create_time() for p in process.children(recursive=True)}
                stop_identified(writers)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    if process_matches(identity["pid"], identity["started"]):
                        process.kill()
                        process.wait(timeout=5)
                stop_identified(writers, kill=True)
                children = []
                for p, s in writers.items():
                    if process_matches(p, s):
                        try:
                            children.append(psutil.Process(p))
                        except psutil.NoSuchProcess:
                            pass
                _, alive = psutil.wait_procs(children, timeout=5)
                if alive:
                    raise RuntimeError("旧尝试仍有子写入者，禁止释放预留")
            if process_matches(identity["pid"], identity["started"]):
                raise RuntimeError("旧工作进程尚未退出，保留预留并禁止重派")
            if self._recover_completion(attempt):
                continue
            def tx(conn):
                conn.execute("UPDATE attempts SET status='failed',authorization=NULL,process_exited=1,error='服务中断后的旧尝试已停止',error_kind='interrupted',finished_at=? WHERE id=?", (utcnow_iso(), aid))
                conn.execute("UPDATE resource_reservations SET released_at=? WHERE attempt_id=?", (utcnow_iso(), aid))
                conn.execute("UPDATE nodes SET status='failed',wait_reason='旧尝试已退出，可重试本节点；暂存产物尚未正式发布' WHERE id=?", (attempt["node_id"],))
                node_row = self._node_rows(conn, "n.id=?", (attempt["node_id"],))[0]
                _log_insert(conn, user_id=node_row["user_id"], conversation_id=node_row["conversation_id"],
                            task_id=node_row["task_id"], run_id=node_row["run_id"],
                            text=self._lifecycle_text(node_row["user_id"], "interrupted",
                                                      node=node_row["node_key"]))
            self.store.submit_write(tx)

    def _recover_completion(self, attempt):
        """只接收既有完成描述，不启动计算，不发布文件或恢复旧授权。"""
        target = Path(attempt["staging_dir"]) / "completion.json"
        try:
            raw = target.read_bytes()
            envelope = json.loads(raw)
        except (OSError, ValueError):
            return False
        if envelope.get("status") != "succeeded" or not attempt["authorization"] or any(
            envelope.get(k) != v for k, v in {"attempt_id": attempt["id"], "authorization": attempt["authorization"],
                                              "batch": attempt["dispatch_batch"], "task_version": attempt["task_version"]}.items()):
            return False
        digest, token = hashlib.sha256(raw).hexdigest(), new_id()
        def authorize(conn):
            current = self._node_rows(conn, "n.id=?", (attempt["node_id"],))[0]
            if not self._valid(current) or conn.execute("SELECT 1 FROM attempts WHERE node_id=? AND attempt_no>?", (attempt["node_id"], attempt["attempt_no"])).fetchone():
                return None
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return None
            conn.execute("UPDATE attempts SET authorization=?,dispatch_batch=? WHERE id=?", (token, self.batch, attempt["id"]))
            _event(conn, current, "attempt.recovery_authorized", {"attempt_id": attempt["id"], "completion_sha256": digest, "scope": "existing_completion_only"})
            return current
        row = self.store.submit_write(authorize)
        if row is None:
            return False
        envelope.update(authorization=token, batch=self.batch)
        self._finish(attempt["id"], {"row": row, "spec": {"staging_dir": attempt["staging_dir"]}}, envelope)
        return True

    def cancel_scope(self, user_id, project_id, conversation_id=None, timeout=20):
        """用户删除作用域前先停止其中写入者，避免目录被后台进程重新创建。"""
        def tx(conn):
            where, args = "t.user_id=? AND t.project_id=?", [user_id, project_id]
            if conversation_id:
                where += " AND t.conversation_id IN (SELECT id FROM conversations WHERE legacy_conv_id=?)"
                args.append(conversation_id)
            found = rows(conn, "SELECT t.id FROM tasks t WHERE " + where, args)
            for task in found:
                conn.execute("UPDATE runs SET cancel_requested=1 WHERE task_id=?", (task["id"],))
            return {t["id"] for t in found}
        ids = self.store.submit_write(tx)
        self.notify()
        deadline = time.monotonic() + timeout
        while True:
            live = self.store.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts a JOIN nodes n ON n.id=a.node_id JOIN runs r ON r.id=n.run_id WHERE a.process_exited=0 AND r.task_id IN (" + ",".join("?" for _ in ids) + ")", tuple(ids)).fetchone()[0]) if ids else 0
            if not live:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("计算进程尚未停止，本次删除未执行，请稍后重试")
            time.sleep(.1)

    def retry(self, node_id, user_id):
        # 预检：静态上限不满足（预估需求本身超过部署预算）时直接给出原因，
        # 避免"点击重试 → 瞬间再次失败"的困惑；修正部署内存或模型规模后才可通过。
        pre = self.store.read(lambda c: self._node_rows(
            c, "n.id=? AND t.user_id=?", (node_id, user_id)))
        if pre and pre[0]["status"] == "failed" and self._valid(pre[0]):
            try:
                node = pre[0]
                peak = self.store.read(lambda c: c.execute(
                    "SELECT MAX(a.peak_memory_bytes) FROM attempts a"
                    " JOIN nodes n ON n.id=a.node_id WHERE n.node_type=?"
                    " AND a.status='succeeded'",
                    (node["node_type"],)).fetchone()[0]) or 0
                claim = estimate(node["node_type"],
                                 decode(node["frozen_inputs"])["snapshot"],
                                 self._context(node), self.budget, peak)
                ok, reason = self.ledger.can_fit(claim)
                if not ok and self.ledger.static_block(claim):
                    raise ValueError(reason)
            except ValueError:
                raise
            except Exception:
                pass  # 预检自身异常不阻塞正常重试
        def tx(conn):
            found = self._node_rows(conn, "n.id=? AND t.user_id=?", (node_id, user_id))
            if not found:
                raise KeyError("节点不存在")
            row = found[0]
            if row["status"] != "failed" or not self._valid(row):
                raise ValueError("只有当前运行的失败节点可以重试")
            if conn.execute("SELECT 1 FROM attempts WHERE node_id=? AND process_exited=0", (node_id,)).fetchone():
                raise ValueError("旧进程还未退出")
            conn.execute("UPDATE nodes SET status='pending',retry_at=NULL,wait_reason=NULL WHERE id=?", (node_id,))
            _event(conn, row, "node.retry_requested", {})
            _log_insert(conn, user_id=row["user_id"], conversation_id=row["conversation_id"],
                        task_id=row["task_id"], run_id=row["run_id"],
                        text=self._lifecycle_text(row["user_id"], "retry",
                                                  node=row["node_key"]))
        self.store.submit_write(tx)
        self.notify()

    def answer(self, question_id, text, user_id, conversation_id):
        """审批答案在短事务内一次消费。等待期间没有执行线程或资源预留。"""
        from core.state_kernel import questions
        def tx(conn):
            q = questions.load_question(conn, question_id)
            if not q or q["user_id"] != user_id or q["conversation_id"] != conversation_id:
                return None
            constraint = q.get("answer_constraint", {})
            if not constraint.get("execution"):
                return None
            saved, targets = questions.validate_answerable(conn, question_id)
            current = self._node_rows(conn, "n.id=?", (constraint["node_id"],))[0]
            if not self._valid(current) or current["status"] != "waiting_input":
                raise ValueError("这张问题已失效")
            if isinstance(text, dict):
                answer = text
            else:
                answer = {"option_id": str(text).strip()}
                raw = str(text).strip()
                aliases = {"接受": "accept", "接受当前结果": "accept", "停止调优": "stop_tuning", "自动调优": "ai_tune", "继续下一轮": "next_round"}
                answer["option_id"] = aliases.get(raw, raw)
                for i, item in enumerate(saved["candidates"]):
                    if raw in (f"第{i+1}组", f"第{['一','二','三','四','五','六','七','八','九'][i]}组" if i < 9 else "", item.get("label", "")):
                        answer["option_id"] = item.get("id")
            valid = {str(item["id"]) for item in saved["candidates"]}
            if str(answer.get("option_id")) not in valid:
                raise ValueError("答案不在已保存候选中")
            if constraint.get("payload"):
                from core.agent.orchestrator.approval import parse_resume
                answer, error = parse_resume(constraint["payload"], answer)
                if error:
                    raise ValueError(error)
            params = decode(current["params"])
            params["execution_answer"] = answer
            questions.close_question(conn, question_id, saved["version"], status="answered", answer=answer)
            conn.execute("UPDATE nodes SET params=?,status='ready',wait_reason=NULL WHERE id=?", (json.dumps(params), current["id"]))
            _event(conn, current, "node.answer_received", {"question_id": question_id})
            return {"node_id": current["id"], "answer": answer}
        result = self.store.submit_write(tx)
        self.notify()
        return result

    def snapshot(self, user_id):
        with self._view_lock:
            nodes = self.store.read(lambda c: self._node_rows(c, "t.user_id=? AND t.current_run_id=r.id", (user_id,)))
            owned = {r["id"] for r in nodes}
            resource = self.ledger.snapshot()
            resource["reservations"] = {a: r for a, r in resource["reservations"].items() if self.active.get(a, {}).get("row", {}).get("id") in owned}
            return {"nodes": [{k: r[k] for k in ("id", "run_id", "task_id", "node_type", "status", "wait_reason", "result")} for r in nodes],
                    "resources": resource, "error": "调度控制循环异常，请查看服务日志" if self.last_error else None,
                    "staged_only": True}


def shutil_disk_free(path):
    import shutil
    return shutil.disk_usage(path).free
