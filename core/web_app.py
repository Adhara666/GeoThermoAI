# -*- coding: utf-8 -*-
"""
GeoThermoAI Vue 3 版后端 — FastAPI 服务

复用 core/（数据获取/预处理/RF/TTRI/TCR/LST/精度/可视化）零改动，
提供 REST + SSE 接口给 Vue 3 前端：
  - 项目/对话一体化管理（data/conversations/*.json 持久化，与旧版兼容）
  - 聊天：线程 + 队列 → SSE 流式（token/append/pause/workflow/done/error）
  - 文件下载：自建 /api/download 路由（FileResponse），彻底规避 Gradio allowed_paths/iframe 限制
  - 地图：core/visualization.LayerVisualizer → folium HTML（iframe 嵌入）
  - 测试：Planetary Computer 连通性 / GDAL 环境自检

启动：python3 server.py   （监听 0.0.0.0:7860）
"""

import json
import logging
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import contextvars
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import auth
from core.memtrim import release_rss_memory as _memtrim_release
from core.ai_assistant import GeoThermoAI_Assistant
from core.state_kernel import (
    CommandConflictError,
    StateStore,
    mark_command_failed,
    mark_command_processed,
    mark_command_rejected,
    questions as q_store,
    receive_command,
    tasks as t_store,
)
from core.agent import understanding
from core.skills.skill_registry import SkillRegistry
from core.agent.geo_thermo_agent import GeoThermoAgent
from core.agent import presentation
from core.agent.orchestrator import agent_config, approval as approval_proto
from core.agent.orchestrator.exec_mode import DEFAULT_EXEC_MODE
from core.agent.orchestrator.exec_mode import normalize as normalize_exec_mode
from core.memory import MemoryManager
from core.visualization import LayerVisualizer
from core.intermediate_cleanup import INTERMEDIATE_FILENAMES
from core.geo_transform import (
    enable_gdal_osr_exceptions,
    bbox_wgs84_to_utm_bounds,
    utm_epsg_for_lonlat,
)
from core import manifest as run_manifest

# 当前请求所属用户（由鉴权中间件写入；FastAPI 同步路由在 anyio 线程池中运行，
# 会自动携带 contextvars 上下文；后台线程需自行 set，见 chat_start._runner）
_uid_ctx: contextvars.ContextVar = contextvars.ContextVar("current_uid", default="")

# ── 常量（与旧版 ui/api.py 保持一致） ──────────────────────────

WORKFLOW_STEPS = [
    "data_acquisition",
    "data_pipeline",
    "ttri_compute",
    "rf_model",
    "tcr_compute",
    "lst_export",
    "accuracy_eval",
    "postprocess",  # 结果后处理（可选）：10m LST 空洞填补，默认不执行
]

_AGENT_KEYWORDS = [
    "处理", "训练", "下载", "执行", "运行", "生成",
    "全流程", "一键", "开始", "计算", "导出", "评估",
]

_WORKFLOW_KEYWORDS = ["全流程", "一键", "跑完全流程", "执行全流程", "处理", "下载", "获取"]

# 工作流面板标签：单一来源在 core/agent/presentation.py，
# 避免同一阶段在后端两处出现不一致的中文名
_WORKFLOW_LABELS = presentation.WORKFLOW_LABELS


def _is_agent_command(message: str) -> bool:
    """关键词路由。角色化后降级为「LLM 不可用时的兜底」，不删除。"""
    return any(kw in message for kw in _AGENT_KEYWORDS)


def _is_workflow_command(message: str) -> bool:
    return any(kw in message for kw in _WORKFLOW_KEYWORDS)


# 审批等待超时（秒）：默认取 settings.agent.approval_wait_seconds，
# 可用环境变量 GTAI_APPROVAL_WAIT_SECONDS 覆盖
def _approval_wait_seconds_from(agent_cfg: dict) -> int:
    env = os.environ.get("GTAI_APPROVAL_WAIT_SECONDS", "").strip()
    if env:
        try:
            return max(30, int(env))
        except ValueError:
            pass
    return int(agent_cfg.get("approval_wait_seconds", agent_config.APPROVAL_WAIT_TIMEOUT))


# 旧路径（roles_enabled=False）的暂停超时：保持改造前的 300 秒 + 静默选第一组
_LEGACY_PAUSE_TIMEOUT = 300


def format_bubble(thinking: str, content: str, streaming: bool = False, elapsed: float = 0) -> str:
    """生成聊天气泡内容：可折叠思考链 + 正文（与旧版一致）"""
    parts = []
    thinking = (thinking or "").strip()
    content = content or ""
    if thinking:
        label = "思考中…" if (streaming and not content) else f"已深度思考（{elapsed:.1f}s）"
        o = " open" if (streaming and not content) else ""
        parts.append(
            f"<details{o}><summary>思考过程 · {label}</summary>\n\n{thinking}\n\n</details>"
        )
    if content:
        parts.append(content)
    # 流式占位改为空串：不再插入 "▍" 块状字符（前端用打字光标指示生成中）
    return "\n\n".join(parts)


def strip_thinking(text: str) -> str:
    """从消息内容中剥离 <details> 思考链，供 LLM 上下文使用"""
    return re.sub(r"<details[^>]*>.*?</details>", "", text or "", flags=re.DOTALL).strip()


def release_rss_memory() -> None:
    """任务线程结束（成功/暂停/失败/纯对话）后，把进程空闲堆归还操作系统。

    流程中的大量小分配（RF 树节点数组、pandas/pyarrow 分块缓冲）被 glibc
    malloc 的 arena 保留在进程地址空间：Python 侧 `del` + gc 后 RSS 不会回落
    （实测 150k×32KB 小分配释放后 RSS 保持 4.7GB 不降），但可继续复用。
    主动调 `malloc_trim(0)` 可把完全空闲的堆页归还系统（实测 4.7GB → 32MB），
    让日志区内存读数在流程结束后回落。实现见 core/memtrim.py（单一来源）。
    """
    return _memtrim_release()


# 默认时区偏移（小时）：前端 EventSource 连接时会携带用户本地时区偏移
# （如 UTC+8 → 8、UTC-5 → -5），日志时间戳按每个用户本地时区生成。
# 容器默认 UTC，旧客户端未传 tz 时回退为北京时间（UTC+8）。
_DEFAULT_TZ_OFFSET = 8.0


def _guard_stream_text(text: str) -> str:
    """流式正文防刷屏：复读熔断 + 长度上限（保护性截断，正常内容不受影响）。

    模型偶发抽风会把同一段说明复读数百遍导致气泡刷屏（用户实测）。
    采用**行级判重**：同一长行（≥20 字符）重复达阈值时截断——正常
    编号/步骤列表每行不同，不会误伤。
    """
    if not text:
        return text
    lines = text.splitlines()
    if lines:
        from collections import Counter
        counter = Counter(
            ln.strip() for ln in lines[-400:] if len(ln.strip()) >= 20)
        for dup, n in counter.most_common(1):
            if n >= 6 and text.count(dup) >= 8:
                first = text.find(dup)
                return (text[:first + len(dup) * 2]
                        + "\n\n（检测到重复内容，已自动截断）")
    if len(text) > _STREAM_MAX_CHARS:
        return text[:_STREAM_MAX_CHARS] + "\n\n（回复过长，已自动截断）"
    return text


_STREAM_MAX_CHARS = 3000  # 单条流式文本硬上限（正文与思考共用）：
# 模型异常复读（含数字/标点微变的“变体复读”）难以 100% 识别，
# 硬上限是最可靠的刷屏兜底（正常结论类回答远短于此）。

# 调度节点中文名（与前端 NODE_LABELS 保持同一套措辞，供问答上下文注入）
_NODE_LABELS = {
    "search_scene": "检索场景候选", "select_scene": "配对选择或场景确定",
    "acquire_asset": "网络资产获取", "prepare_local": "本地定标对齐准备",
    "data_check": "原始包数据检查", "preprocess_split": "预处理与空间划分",
    "prep_check": "预处理数据检查", "ttri": "TTRI 拟合与应用",
    "ttri_check": "TTRI 数据检查", "rf_round": "RF 轮训练与测试预测",
    "train_decision": "训练决定", "promote_best": "登记最佳模型引用",
    "tcr": "TCR 与最终温度", "export": "导出 GeoTIFF",
    "closure_eval": "闭合评价", "gapfill": "独立填洞产品",
    "rebuild": "重建缺失输入",
}


_QUERY_HINTS = ("哪一天", "哪天", "是什么", "什么是", "为什么", "怎么",
                "哪些", "哪一个", "哪几个", "多少", "几点", "是不是",
                "有没有", "状态", "进度")
_ACTION_HINTS = ("生成", "处理", "执行", "开始", "重试", "取消",
                 "改成", "换成", "下载", "导出", "训练", "重建",
                 "重发", "提交", "新建", "上传", "继续", "建个", "停",
                 "帮我跑", "跑一下", "跑起来")
_STRONG_Q = ("哪一天", "哪天", "哪一步", "是什么", "什么是", "为什么",
             "怎么", "哪些", "哪一个", "哪几个", "多少", "几点", "是不是")


def _is_query_only(message: str) -> bool:
    """判断是否“纯查询句”（走问答通道，不进理解层 JSON 操作通道）。

    用户实测：查询式问句（如“我当前武汉市的配对用的是哪一天？”）会让
    理解模型输出失控（无法解析 JSON 或复读）——它不含任务操作意图，
    应直接交给带台账上下文的自由问答。保守策略：
      - 含操作词且无强疑问点 → 操作句（如“帮我重试一下？”）
      - “帮我/把…”式确认句 → 操作句
      - “…吗？”结尾且无强疑问词 → 可能是想做任务（如“武汉 7 月做
        LST 吗？”），保守走理解层
      - 其余“问号结尾或含强疑问词”才命中查询
    """
    m = (message or "").strip()
    if not m or len(m) > 80:
        return False
    strong_q = any(h in m for h in _STRONG_Q)
    if any(h in m for h in _ACTION_HINTS) and not strong_q:
        return False
    if (m.startswith(("帮我", "给我")) or m.startswith("把") or "帮我" in m) \
            and not strong_q:
        return False
    if m.endswith(("吗？", "吗?")) and not strong_q:
        return False
    return m.endswith(("？", "?")) or strong_q


def _date_from_name(name: str) -> str:
    """从文件名解析影像日期（8 位 yyyymmdd → YYYY-MM-DD）；无则空串。"""
    import re as _re
    m = _re.search(r"(20\d{2})(\d{2})(\d{2})", name or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return ""
    return f"{y}-{mo}-{d}"


def _artifact_style(name: str) -> str:
    """按产物文件名推断渲染样式（对应 LayerVisualizer.LAYER_DEFS 的 id）。"""
    n = (name or "").lower()
    if "dem" in n:
        return "dem"
    if "sentinel" in n or "_s2" in n or n.startswith("s2"):
        return "sentinel_rgb"
    if "gapfill" in n or "filled" in n or "nofill" in n or "no_hole" in n or "无空洞" in n:
        return "lst_10m_filled"
    if "10m" in n:
        return "lst_10m"
    if "landsat" in n or "30m" in n or "lst" in n:
        return "landsat_lst"
    return "lst_10m"


def _stamp_log_lines(text: str, tz_offset: float = _DEFAULT_TZ_OFFSET) -> str:
    """日志行前缀时间戳（按用户本地时区 年-月-日 时:分:秒），空行保持原样。

    tz_offset：用户本地时区相对 UTC 的偏移小时数（东八区为 8，西五区为 -5）。
    """
    tz = timezone(timedelta(hours=float(tz_offset or 0)))
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for ln in str(text).split("\n"):
        out.append(f"[{ts}] {ln}" if ln.strip() else ln)
    return "\n".join(out)


def _stamp_with_ts(text: str, ts_iso: str,
                   tz_offset: float = _DEFAULT_TZ_OFFSET) -> str:
    """按日志产生的时刻（UTC ISO）盖用户本地时区时间戳。

    持久化的日志存原文 + 产生时刻；读取/推送时用当次请求的时区再现，
    保证不同时区用户看到的都是自己本地时间（不因服务端时区改变）。
    """
    try:
        dt = datetime.fromisoformat(str(ts_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=float(tz_offset or 0))))
    ts = local.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for ln in str(text).split("\n"):
        out.append(f"[{ts}] {ln}" if ln.strip() else ln)
    return "\n".join(out)


# ── 业务后端（移植自 GradioAPI，去除 Gradio 耦合） ─────────────

class AppBackend:
    def __init__(self):
        # 启动期启用 GDAL/OSR 异常模式：把静默返回 None/错误码的契约问题
        # 尽早转成可捕获的异常，而不是等到某次下载中途才发现坐标是 inf。
        enable_gdal_osr_exceptions()
        settings = self._load_global_settings()
        api_config = settings.get("api", {})

        # 默认（未登录兜底）assistant；实际聊天走 _assistant_for()（每用户独立实例）
        self.assistant = GeoThermoAI_Assistant(
            model_type=api_config.get("model_type", "deepseek"),
            api_key=api_config.get("api_key", ""),
            api_base_url=api_config.get("api_base_url", ""),
            model_id=api_config.get("model_id", ""),
            api_format=api_config.get("api_format", "openai"),
        )
        self.registry = SkillRegistry()
        self.agent = GeoThermoAgent(self.assistant, self.registry)
        self._register_builtin_skills()

        # 每用户独立的 assistant / agent（凭据按用户隔离，禁止跨用户共用）
        self._user_assistants: Dict[str, GeoThermoAI_Assistant] = {}
        self._user_agents: Dict[str, GeoThermoAgent] = {}
        # 每用户独立的记忆管理器（懒加载：首次使用该用户时初始化并播种领域知识）
        self._user_memories: Dict[str, MemoryManager] = {}

        # 按对话隔离的运行时状态（与旧版一致）
        self._conv_states: Dict[str, dict] = {}
        self._stream_queues: Dict[str, "queue.Queue"] = {}
        self._pause_events: Dict[str, threading.Event] = {}
        self._pause_responses: Dict[str, Any] = {}
        self._agent_threads: Dict[str, threading.Thread] = {}
        self._stream_starts: Dict[str, float] = {}
        self._deleted_convs: set = set()
        # 填洞产物值域缓存（按 路径+mtime+size 键控，产物不变不重复读栅格）
        self._filled_range_cache: Dict[tuple, Optional[dict]] = {}
        # SSE 连接代际号：新连接递增代际，让旧（已断开但服务端未感知的）生成器自行退出
        self._stream_gen: Dict[str, int] = {}
        # 每个对话已累积的流式内容：断线重连/流结束后的重连用于补齐完整气泡
        self._stream_content: Dict[str, str] = {}
        # 每个对话已累积的思考过程（reasoning_content）：断线重连补齐折叠链
        self._stream_thinking: Dict[str, str] = {}
        # 每个对话已计算的思考用时：断线重连/切回对话恢复 (用时XX秒)
        self._stream_thinking_seconds: Dict[str, float] = {}
        # 每个对话已累积的实时日志（日志面板权威全量）：刷新/断线重连后恢复日志连续性
        self._stream_logs: Dict[str, list] = {}
        # 对话消息版本号：完成报告等由服务端主动追加气泡时递增，
        # conversation_events 监测变化并把新气泡推给已打开的页面
        self._conv_msg_ver: Dict[str, int] = {}
        # 用户界面语言缓存（ui_lang → zh/en）：完成报告/生命周期日志按此呈现
        self._user_lang_cache: Dict[str, str] = {}

        # 状态内核（升级第一阶段）：SQLite 台账 + 命令接收服务。
        # 单例惰性初始化；唯一写连接由内核内部写入者线程独占。
        self._state_store: Optional[StateStore] = None
        self._state_store_lock = threading.Lock()
        self._scheduler = None

    def start_scheduler(self):
        from core.scheduling.scheduler import Scheduler
        store = self._get_state_store()
        self._scheduler = Scheduler(store, store.db_path.parent / "executions")
        # 生命周期日志（失败/重试/完成）按行上用户的语言设置呈现
        self._scheduler._lang_lookup = self._user_lang
        self._scheduler.start()
        # 历史台账事件回填为日志行（升级前的过程日志没持久化，从事件重建
        # 失败原因/重试/节点完成，让日志区从失败到重试恢复的完整过程可见）
        threading.Thread(target=self._backfill_logs_from_events,
                         name="log-backfill", daemon=True).start()
        # 启动时回收历史运行目录（失败/被接替运行的暂存与产物；
        # 6 小时静默期保护刚创建的任务目录）
        threading.Thread(target=lambda: self._cleanup_stale_runs(6.0),
                         name="run-cleanup", daemon=True).start()
        # 产物可用性对账（文件缺失→cleaned，重建后自动恢复）
        threading.Thread(target=self._reconcile_artifact_availability,
                         name="artifact-reconcile", daemon=True).start()
        # 任务完成主动气泡报告（含精度）：后台轮询，历史已完成任务自动补报
        self._start_completion_watcher()

    # ── 执行日志持久化 / 界面语言 / 完成报告 ──────────────────

    def _backfill_logs_from_events(self):
        """一次性把历史台账事件转成日志行（幂等）。

        升级前的过程日志只在内存，重启后丢失；但台账 events 表保存着
        节点级生命周期（node.failed/retry_wait/cancelled/waiting_input/
        retry_requested/succeeded，含失败原因）。按（对话, 文本, 时间）
        去重插入：重复重启不会产生重复行，已存在的日志不受影响。
        """
        from core.scheduling.scheduler import _LIFECYCLE_TEXT, strip_emoji
        store = self._get_state_store()
        try:
            rows_ = store.read(lambda c: c.execute(
                "SELECT e.seq, e.occurred_at, e.user_id, e.conversation_id,"
                " e.task_id, e.run_id, e.type, n.node_key, e.payload"
                " FROM events e LEFT JOIN nodes n ON n.id = e.object_id"
                " WHERE e.type IN ('node.failed','node.retry_wait',"
                " 'node.cancelled','node.waiting_input','node.retry_requested',"
                " 'node.succeeded') AND e.conversation_id IS NOT NULL"
                " ORDER BY e.seq LIMIT 5000").fetchall())
        except Exception as e:  # noqa: BLE001
            print(f"[logs] 历史事件读取失败：{e}")
            return
        if not rows_:
            return
        kind_map = {"node.failed": "failed", "node.retry_wait": "retry_wait",
                    "node.cancelled": "cancelled",
                    "node.waiting_input": "waiting_input",
                    "node.retry_requested": "retry",
                    "node.succeeded": "node_done"}
        inserts = []
        for _seq, occurred, uid, conv, tid, run, etype, node_key, payload in rows_:
            kind = kind_map.get(str(etype))
            if not kind:
                continue
            try:
                p = json.loads(payload or "{}")
            except ValueError:
                p = {}
            zh, en_t = _LIFECYCLE_TEXT[kind]
            tpl = zh if self._user_lang(str(uid or "")) == "zh" else en_t
            try:
                text = tpl.format(node=str(node_key or ""),
                                  detail=str(p.get("error") or "")[:300],
                                  label=str(p.get("label") or ""))
            except (KeyError, ValueError):
                text = tpl
            inserts.append((uid, conv, tid, run, strip_emoji(text), occurred))

        def tx(conn):
            added = 0
            for uid, conv, tid, run, text, occurred in inserts:
                cur = conn.execute(
                    "INSERT INTO task_logs (user_id, conversation_id, task_id,"
                    " run_id, text, created_at)"
                    " SELECT ?,?,?,?,?,? WHERE NOT EXISTS ("
                    " SELECT 1 FROM task_logs WHERE conversation_id = ?"
                    " AND text = ? AND created_at = ?)",
                    (uid, conv, tid, run, text, occurred,
                     conv, text, occurred))
                try:
                    added += int(cur.rowcount or 0)
                except Exception:
                    pass
            return added

        try:
            added = store.submit_write(tx)
            if added:
                print(f"[logs] 历史台账事件回填日志 {added} 条")
        except Exception as e:  # noqa: BLE001
            print(f"[logs] 历史日志回填失败：{e}")

    def _user_lang(self, uid: str) -> str:
        """用户界面语言（zh/en）：读用户设置 ui_lang，默认 zh。"""
        uid = uid or ""
        if not uid:
            return "zh"
        cached = self._user_lang_cache.get(uid)
        if cached:
            return cached
        lang = "zh"
        try:
            path = _ROOT / "data" / "users" / uid / "settings.json"
            if path.is_file():
                lang = str(json.loads(path.read_text(encoding="utf-8"))
                           .get("ui_lang") or "zh")
                if lang not in ("zh", "en"):
                    lang = "zh"
        except Exception:
            lang = "zh"
        self._user_lang_cache[uid] = lang
        return lang

    def set_ui_language(self, lang: str) -> dict:
        """保存当前用户的界面语言（供后端组装文案使用）。"""
        next_lang = "en" if str(lang or "").lower().startswith("en") else "zh"
        uid = self._uid()
        path = _ROOT / "data" / "users" / uid / "settings.json"
        try:
            data = {}
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            data["ui_lang"] = next_lang
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            self._user_lang_cache[uid] = next_lang
            return {"ok": True, "lang": next_lang}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def _append_log_line(self, uid: str, conv_pk: str, text: str,
                        task_id: str = "", run_id: str = "") -> int:
        """持久写一条对话日志（旧执行链 / 理解层日志）：返回日志行编号。"""
        if not conv_pk or not str(text or "").strip():
            return 0
        from core.scheduling.scheduler import strip_emoji
        def tx(conn):
            cur = conn.execute(
                "INSERT INTO task_logs (user_id, conversation_id, task_id,"
                " run_id, text, created_at) VALUES (?,?,?,?,?,?)",
                (uid or None, conv_pk, task_id or None, run_id or None,
                 strip_emoji(text), datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid
        try:
            return int(self._get_state_store().submit_write(tx) or 0)
        except Exception:
            return 0

    def conversation_logs(self, cid: str, tz: float = _DEFAULT_TZ_OFFSET,
                          after_id: int = 0, limit: int = 4000) -> dict:
        """对话执行日志历史（持久化，跨刷新/重启保留）；带本地时区时间戳。"""
        store = self._get_state_store()
        uid = self._uid()
        limit = max(1, min(int(limit or 4000), 20000))
        conv = store.read(lambda c: c.execute(
            "SELECT id FROM conversations"
            " WHERE (id = ? OR legacy_conv_id = ?) AND user_id = ?",
            (cid, cid, uid)).fetchone())
        if not conv:
            return {"ok": True, "logs": []}
        rows_ = store.read(lambda c: c.execute(
            "SELECT id, task_id, text, created_at FROM task_logs"
            " WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
            (conv[0], int(after_id or 0), limit)).fetchall())
        from core.scheduling.scheduler import strip_emoji
        logs = [{"id": int(r[0]), "task_id": r[1] or "",
                 "text": _stamp_with_ts(strip_emoji(r[2]), r[3], tz)}
                for r in rows_]
        return {"ok": True, "logs": logs}

    def clear_conversation_logs(self, cid: str) -> dict:
        """清除本对话的执行日志（用户显式操作：内存与持久记录一起清）。"""
        store = self._get_state_store()
        uid = self._uid()
        conv = store.read(lambda c: c.execute(
            "SELECT id FROM conversations"
            " WHERE (id = ? OR legacy_conv_id = ?) AND user_id = ?",
            (cid, cid, uid)).fetchone())
        if conv:
            store.submit_write(lambda c: c.execute(
                "DELETE FROM task_logs WHERE conversation_id = ?", (conv[0],)))
        self._stream_logs.pop(cid, None)
        return {"ok": True}

    # ── 任务完成主动报告（含精度）：后台轮询，幂等补写气泡 ──────

    def _start_completion_watcher(self):
        if getattr(self, "_completion_thread", None):
            return
        t = threading.Thread(target=self._completion_watch_loop,
                             name="completion-watcher", daemon=True)
        self._completion_thread = t
        t.start()

    def _completion_watch_loop(self):
        time.sleep(8)  # 启动静默期：先让服务就绪
        last_cleanup = 0.0
        while True:
            try:
                self._report_completed_tasks()
                # 每日一次历史运行目录回收（磁盘占用治理，见 _cleanup_stale_runs）
                if time.time() - last_cleanup > 24 * 3600:
                    last_cleanup = time.time()
                    try:
                        self._cleanup_stale_runs()
                    except Exception as e:  # noqa: BLE001
                        print(f"[cleanup] 历史目录回收异常：{e}")
            except Exception as e:  # noqa: BLE001
                print(f"[completion] 完成报告轮询异常：{e}")
            time.sleep(5)

    def _reconcile_artifact_availability(self):
        """产物可用性对账（启动一次）：文件缺失→cleaned，文件恢复存在→available。

        误删/政策清理后的文件状态自愈：后续重建产物文件到位后，面板
        自动重新可见，不需要人工改库。"""
        store = self._get_state_store()
        rows_ = store.read(lambda c: c.execute(
            "SELECT id, path, availability FROM artifacts").fetchall())
        toggles = []
        for aid, path, avail in rows_:
            want = "available" if Path(path).is_file() else "cleaned"
            if avail != want:
                toggles.append((want, aid))
        if not toggles:
            return
        store.submit_write(lambda conn: [conn.execute(
            "UPDATE artifacts SET availability=? WHERE id=?", t) for t in toggles])
        print(f"[artifacts] 可用性对账：调整 {len(toggles)} 个")

    def _cleanup_stale_runs(self, min_age_hours: float = 48.0) -> dict:
        """回收与当前任务无关的历史运行目录（磁盘占用治理）。

        删除条件（同时满足）：目录年龄超阈；运行已被新版本接替，或任务
        处于失败/取消/草稿等终态，或已不是任务当前运行，或已无台账记录
        （孤儿目录）；且无未退出尝试。
        保留：未完成任务/已完成任务的当前运行（含 staging 输入与 committed 产物）。
        """
        import shutil
        store = self._get_state_store()
        root = store.db_path.parent / "executions"
        rows_ = store.read(lambda c: c.execute(
            "SELECT r.id, r.superseded_by, t.summary_status, t.current_run_id"
            " FROM runs r JOIN tasks t ON t.id = r.task_id").fetchall())
        run_state = {r[0]: r for r in rows_}
        active = {r[0] for r in store.read(lambda c: c.execute(
            "SELECT n.run_id FROM attempts a JOIN nodes n ON n.id=a.node_id"
            " WHERE a.process_exited=0").fetchall())}
        stale_terminal = {"failed", "cancelled", "draft"}
        now = time.time()
        freed = 0
        removed = 0
        for parent in (root, root / "runs"):
            if not parent.is_dir():
                continue
            for d in sorted(parent.iterdir()):
                # 保留 cache 与 runs 发布树入口（runs/<run> 只按运行号判定，
                # 绝不整体删除 runs 目录——发布产物就在这里）
                if not d.is_dir() or d.name in ("cache", "runs") or d.name.startswith("."):
                    continue
                if d.name in active:
                    continue
                row = run_state.get(d.name)
                if row is None:
                    # 孤儿目录（对应台账行已不存在：项目/对话彻底删除后的残留）
                    deletable = True
                else:
                    superseded, t_status, current = row[1], row[2], row[3]
                    deletable = (bool(superseded) or t_status in stale_terminal
                                 or current != d.name)
                if not deletable:
                    continue
                try:
                    if now - d.stat().st_mtime < float(min_age_hours) * 3600:
                        continue
                except OSError:
                    continue
                size = 0
                for dp, _dirs, files in os.walk(d):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            continue
                shutil.rmtree(d, ignore_errors=True)
                if not d.exists():
                    freed += size
                    removed += 1
        if removed:
            print(f"[cleanup] 回收历史运行目录 {removed} 个，释放 {freed / (1024 ** 3):.2f} GB")
        return {"removed": removed, "freed_bytes": freed}

    def _completion_report_data(self, run_id: str) -> dict:
        """读该运行的精度产物（测试预测 JSON 与闭合 JSON）与正式产物数。"""
        store = self._get_state_store()
        arts = store.read(lambda c: c.execute(
            "SELECT a.path, a.retention_class FROM artifacts a"
            " JOIN attempts at ON at.id = a.attempt_id"
            " JOIN nodes n ON n.id = at.node_id"
            " WHERE n.run_id = ? ORDER BY a.created_at", (run_id,)).fetchall())
        metrics = closure = None
        final_count = 0
        for path, retention in arts:
            name = Path(path).name
            if retention == "keep_forever":
                final_count += 1
            p = Path(path)
            if not p.is_file():
                continue
            try:
                if re.search(r"predict_run\d*\.json$", name):
                    metrics = json.loads(p.read_text(encoding="utf-8"))
                elif name == "coarse_constraint_closure.json":
                    closure = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        return {"metrics": metrics, "closure": closure, "final_count": final_count}

    def _completion_ai_note(self, uid: str, label: str, data: dict,
                            lang: str) -> str:
        """让模型给一段两句话以内的结果解读（不重复数字、不用 emoji）。

        模型不可用/失败时返回空串（报告仍照常输出数据部分）。"""
        if not self._scheduler:
            return ""
        try:
            m = data.get("metrics") or {}
            m = m.get("metrics") if isinstance(m, dict) else None
            m = m if isinstance(m, dict) else {}
            vr = (data.get("closure") or {}).get("value_range") if isinstance(
                data.get("closure"), dict) else None
            vr = vr if isinstance(vr, dict) else {}
            facts = [f"任务：{label}" if lang == "zh" else f"Task: {label}"]
            if isinstance(m.get("R2"), (int, float)):
                facts.append(f"R2={m['R2']:.4f}")
            if isinstance(m.get("RMSE"), (int, float)):
                facts.append(f"RMSE={m['RMSE']:.3f} K")
            if isinstance(m.get("MAE"), (int, float)):
                facts.append(f"MAE={m['MAE']:.3f} K")
            if isinstance(vr.get("low_end_difference_K"), (int, float)):
                facts.append(f"low_end_diff={vr['low_end_difference_K']:+.2f} K")
            if isinstance(vr.get("high_end_difference_K"), (int, float)):
                facts.append(f"high_end_diff={vr['high_end_difference_K']:+.2f} K")
            if lang == "zh":
                prompt = ("你是地表温度降尺度系统的分析助手。请只输出一到两句话的"
                          "专业解读（面向不懂技术的用户）：说明本次结果质量如何、"
                          "是否可靠、使用时要注意什么。不要重复罗列数字，不要使用"
                          "emoji，不要标题或列表。\n" + "；".join(facts))
            else:
                prompt = ("You are the analysis assistant of a land-surface-temperature "
                          "downscaling system. Output only one to two sentences of "
                          "professional interpretation for a non-technical user: how "
                          "good and reliable this result is and what to note when "
                          "using it. Do not repeat the numbers, do not use emoji, "
                          "no headings or lists.\n" + "; ".join(facts))
            with self._scheduler.model_request(timeout=90):
                text = ""
                for _attempt in range(2):  # 偶发网络失败重试一次
                    text = str(self._assistant_for().ask(prompt) or "").strip()
                    bad = ("API调用失败", "API流式调用失败", "未检测到LLM", "未配置模型")
                    if text and not text.startswith(bad):
                        break
                    time.sleep(1.0)
            text = str(text or "").strip()
            bad = ("API调用失败", "API流式调用失败", "未检测到LLM", "未配置模型")
            if not text or text.startswith(bad):
                return ""
            from core.scheduling.scheduler import strip_emoji
            text = re.sub(r"\s+", " ", strip_emoji(text)).strip()
            return text[:240]
        except Exception:
            return ""

    def _compose_completion_report(self, uid: str, label: str, run_id: str) -> str:
        """组装完成报告正文：任务名 + 影像配对 + 测试区精度 + 与 30m 对照 +
        正式产物数 + 结果解读段；不使用 emoji 图标。"""
        data = self._completion_report_data(run_id)
        if not (data.get("metrics") or data.get("closure") or data.get("final_count")):
            return ""
        lang = self._user_lang(uid)
        lines = [f"任务完成：{label}" if lang == "zh" else f"Task completed: {label}"]
        # 使用的影像配对（哪天、哪颗 Landsat、Sentinel-2、云量与时差）
        pair = self._selected_pair_for_task(run_id, lang)
        if pair:
            lines.append(f"- 使用影像：{pair}" if lang == "zh"
                         else f"- Imagery used: {pair}")
        m = data.get("metrics") or {}
        m = m.get("metrics") if isinstance(m, dict) else None
        m = m if isinstance(m, dict) else {}
        if m:
            r2, rmse, mae = m.get("R2"), m.get("RMSE"), m.get("MAE")
            parts = []
            if isinstance(r2, (int, float)):
                parts.append(f"R² {r2:.4f}")
            if isinstance(rmse, (int, float)):
                parts.append(f"RMSE {rmse:.3f} K")
            if isinstance(mae, (int, float)):
                parts.append(f"MAE {mae:.3f} K")
            if parts:
                lines.append("- 测试区精度：" + "，".join(parts) if lang == "zh"
                             else "- Test accuracy: " + ", ".join(parts))
        vr = (data.get("closure") or {}).get("value_range") if isinstance(
            data.get("closure"), dict) else None
        if isinstance(vr, dict):
            lo = vr.get("low_end_difference_K")
            hi = vr.get("high_end_difference_K")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                lines.append(
                    f"- 与 30m 对照：最低端差 {lo:+.2f} K，最高端差 {hi:+.2f} K"
                    if lang == "zh" else
                    f"- vs 30m reference: low-end {lo:+.2f} K, "
                    f"high-end {hi:+.2f} K")
        if data.get("final_count"):
            lines.append(f"- 正式产物 {data['final_count']} 项，可在「下载」面板获取"
                         if lang == "zh" else
                         f"- {data['final_count']} final product(s) available in the Download panel")
        content = ("\n\n".join([lines[0], "\n".join(lines[1:])])
                   if len(lines) > 1 else lines[0])
        note = self._completion_ai_note(uid, label, data, lang)
        if note:
            # 直接接在数据行后成段（不出现“AI 解读”前缀）
            content += "\n\n" + note
        from core.scheduling.scheduler import strip_emoji
        return strip_emoji(content)

    def _report_completed_tasks(self):
        """扫描已完成任务：对话里还没有完成报告气泡就补写一条（含精度）。

        幂等：气泡带 kind=task_complete + task_id + run_id 标记，重复扫描
        跳过；重启后从对话文件检查，不重复报告；历史上已完成的任务
        在功能上线后首次扫描时也会补上报告。
        """
        store = self._get_state_store()
        rows_ = store.read(lambda c: c.execute(
            "SELECT t.id, t.user_id, t.conversation_id, t.label,"
            " t.current_run_id, cv.legacy_conv_id FROM tasks t"
            " JOIN conversations cv ON cv.id = t.conversation_id"
            " WHERE t.summary_status = 'completed'"
            " AND t.current_run_id IS NOT NULL"
            " ORDER BY t.updated_at DESC LIMIT 20").fetchall())
        for tid, uid, conv_pk, label, run_id, legacy in rows_:
            if not uid or not legacy or not run_id:
                continue
            token = _uid_ctx.set(uid)
            try:
                convs = self.load_conversations()
                holder = None
                for pname, items in convs.items():
                    if legacy in items:
                        holder = (pname, items[legacy])
                        break
                if holder is None:
                    continue
                msgs = list(holder[1].get("messages") or [])
                if any(isinstance(m, dict) and m.get("kind") == "task_complete"
                       and m.get("task_id") == tid and m.get("run_id") == run_id
                       for m in msgs):
                    continue
                content = self._compose_completion_report(uid, label or tid, run_id)
                if not content:
                    continue
                msgs.append({"role": "assistant", "content": content,
                             "kind": "task_complete", "task_id": tid,
                             "run_id": run_id})
                self._save_history(holder[0], legacy, msgs)
                self._conv_msg_ver[conv_pk] = self._conv_msg_ver.get(conv_pk, 0) + 1
            except Exception as e:  # noqa: BLE001
                print(f"[completion] 完成报告写入失败：{e}")
            finally:
                _uid_ctx.reset(token)

    # 注意：_compose_completion_report 全库唯一；若出现重复定义，后定义会
    # 覆盖先定义（验证脚本含唯一性断言）。

    def _conversation_exec_mode(self, task_row) -> str:
        """读取任务所属对话当前的交互模式（供无请求上下文的编译入口使用）。

        卡片回答等路径不带请求上下文；若不补读，编译快照会落到
        settings 默认值（approval），导致“完全执行”下仍弹配对选择。
        """
        try:
            uid = str(task_row.get("user_id") or "")
            conv_pk = str(task_row.get("conversation_id") or "")
            if not uid or not conv_pk:
                return ""
            legacy = self._get_state_store().read(lambda c: c.execute(
                "SELECT legacy_conv_id FROM conversations WHERE id = ?",
                (conv_pk,)).fetchone())
            if not legacy or not legacy[0]:
                return ""
            path = self._conv_dir() / f"{legacy[0]}.json"
            if not path.is_file():
                return ""
            return str(json.loads(path.read_text(encoding="utf-8"))
                       .get("exec_mode") or "")
        except Exception:
            return ""

    def _enqueue_ready(self, task_rows, exec_mode=None):
        from core.planning.compiler import compile_task_tx
        from core.planning.replan import start_superseding_run_tx
        from core.scheduling.scheduler import rows
        settings = self._load_settings()
        # 卡片回答/文本回答等入口可能不带请求上下文：缺省时读取对话
        # 当前模式（交互模式不冻结，全链路采纳“完全执行”自动代选）。
        if not exec_mode and task_rows:
            exec_mode = self._conversation_exec_mode(task_rows[0])
        settings.setdefault("agent", {})["default_exec_mode"] = normalize_exec_mode(exec_mode, settings.get("agent", {}).get("default_exec_mode", "approval"))
        settings["_execution"] = {"settings_path": str(self._user_settings_path())}
        uid = self._uid()
        def tx(conn):
            compiled = []
            for candidate in task_rows:
                tid = candidate.get("id") or candidate.get("task_id")
                task = t_store.load_task(conn, tid)
                if not task or task["user_id"] != uid:
                    raise ValueError("任务不存在")
                # 第六阶段（§12.4）：运行绑定项目视图落点与标签，
                # 正式产物确认后符号链接到项目目录，地图/精度/下载按产物绑定。
                conv_row = conn.execute(
                    "SELECT legacy_conv_id, project_id FROM conversations WHERE id=?",
                    (task["conversation_id"],)).fetchone()
                project_dir = run_label = None
                if conv_row:
                    project_dir = str(self._conv_project_dir(conv_row[1], conv_row[0]))
                    run_label = (task.get("label") or "").strip() or "运行"
                current = rows(conn, "SELECT * FROM runs WHERE id=?", (task.get("current_run_id"),))
                if current and not current[0]["cancel_requested"]:
                    from core.agent.understanding.slotbook import SlotBook
                    snap = json.loads(current[0]["frozen_inputs"])["snapshot"]
                    a, b = SlotBook(task["slots"]), SlotBook(snap["slot_values"])
                    if all(a.value(k) == b.value(k) for k in ("region", "time", "product_mode", "datasets")):
                        # 继续/优先级命令不创建第二条相同生产链。
                        if candidate.get("action") == "retry":
                            conn.execute("UPDATE nodes SET status='pending',retry_at=NULL WHERE run_id=? AND status='failed' AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.node_id=nodes.id AND a.process_exited=0)", (current[0]["id"],))
                        compiled.append({"run_id": current[0]["id"], "task_id": tid})
                        continue
                    made = start_superseding_run_tx(
                        conn, task_id=tid, expected_task_version=task["version"],
                        settings=settings,
                        replan_max=int(snap.get("params", {}).get("replan_max", {}).get("value", 3)),
                        reason="用户修改科学身份字段",
                        project_dir=project_dir, run_label=run_label)
                    compiled.append(made)
                    continue
                made = compile_task_tx(conn, task_id=tid, expected_task_version=task["version"], settings=settings,
                                       project_dir=project_dir, run_label=run_label)
                if current:
                    conn.execute("UPDATE runs SET superseded_by=?,cancel_requested=1 WHERE id=?", (made["run_id"], current[0]["id"]))
                compiled.append(made)
            return compiled
        made = self._get_state_store().submit_write(tx)
        if self._scheduler:
            self._scheduler.notify()
        return made

    # ── 内部工具 ───────────────────────────────────────────────

    def _register_builtin_skills(self):
        from core.skills.builtin import (
            DataAcquisitionSkill,
            DataPipelineSkill,
            TTRIComputeSkill,
            RFModelSkill,
            TCRComputeSkill,
            LSTExportSkill,
            LSTGapFillSkill,
            AccuracyEvalSkill,
            AIAssistantSkill,
        )
        for skill in (
            DataAcquisitionSkill(),
            DataPipelineSkill(),
            TTRIComputeSkill(),
            RFModelSkill(),
            TCRComputeSkill(),
            LSTExportSkill(),
            LSTGapFillSkill(),
            AccuracyEvalSkill(),
            AIAssistantSkill(),
        ):
            self.registry.register(skill)

    # ── 用户维度（登录隔离） ────────────────────────────────────

    @staticmethod
    def _uid() -> str:
        return _uid_ctx.get() or "default"

    # ── 状态内核（升级第一阶段：状态内核） ────────────────

    def _get_state_store(self) -> StateStore:
        """台账单例：data/state_kernel/ledger.sqlite3，可用 GTAI_STATE_DB 覆盖。"""
        with self._state_store_lock:
            if self._state_store is None:
                db_path = os.environ.get("GTAI_STATE_DB", "").strip()
                if not db_path:
                    db_path = str(_ROOT / "data" / "state_kernel" / "ledger.sqlite3")
                self._state_store = StateStore(Path(db_path))
            return self._state_store

    def ledger_snapshot(self, limit: int = 50) -> dict:
        """台账只读快照（供验收巡检；不在 SSE 生命周期内持有读事务）。"""
        store = self._get_state_store()
        uid = self._uid()
        limit = max(1, min(int(limit), 500))

        def _snapshot(conn):
            def _rows(sql, args):
                cols = None
                out = []
                cur = conn.execute(sql, args)
                cols = [d[0] for d in cur.description]
                for r in cur.fetchmany(limit):
                    out.append(dict(zip(cols, r)))
                return out

            return {
                "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                "commands": _rows(
                    "SELECT id, dedup_key, operation_type, status, created_at,"
                    " processed_at, message_id FROM commands WHERE user_id = ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (uid, limit),
                ),
                "messages": _rows(
                    "SELECT m.id, m.seq, m.role, m.content, m.created_at"
                    " FROM messages m JOIN conversations c ON m.conversation_id = c.id"
                    " WHERE c.user_id = ? ORDER BY m.created_at DESC LIMIT ?",
                    (uid, limit),
                ),
                "events": _rows(
                    "SELECT seq, type, occurred_at, object_type, object_id"
                    " FROM events WHERE user_id = ? ORDER BY seq DESC LIMIT ?",
                    (uid, limit),
                ),
            }

        return store.read(_snapshot)

    # ── 理解层（升级第二阶段） ────────────────────────────────

    def _conversation_pk(self, pid: str, cid: str) -> str:
        """台账内部对话主键；命令接收时已登记，这里只读。"""
        from core.state_kernel import conversation_pk

        store = self._get_state_store()
        uid = self._uid()
        return store.read(
            lambda conn: conversation_pk(conn, uid, pid, cid)) or ""

    def _conversation_pk_by_id(self, cid: str) -> str:
        """仅凭对话编号（台账主键或旧对话编号）反查主键，归属当前用户。"""
        store = self._get_state_store()
        uid = self._uid()

        def _q(conn):
            row = conn.execute(
                "SELECT id FROM conversations WHERE (id = ? OR legacy_conv_id = ?)"
                " AND user_id = ?", (cid, cid, uid)).fetchone()
            return row[0] if row else ""

        return store.read(_q)

    def _resolve_context(self, pid: str, cid: str, *, message: str,
                         chat_mode: str, tz_offset: float,
                         conv_pk: str = ""):
        """组装理解上下文（真实边界文件 + 台账任务/问题快照）。"""
        store = self._get_state_store()
        conv_pk = conv_pk or self._conversation_pk(pid, cid)
        ledger = understanding.load_ledger_context(
            store, user_id=self._uid(), conversation_id=conv_pk)
        study_dir = self._study_dir()
        paths = sorted(study_dir.glob("*.geojson"),
                       key=lambda p: p.stat().st_mtime, reverse=True) \
            if study_dir.exists() else []
        return conv_pk, understanding.build_resolve_context(
            message=message,
            received_at=datetime.now(timezone.utc),
            tz_offset=tz_offset,
            chat_mode=chat_mode,
            study_area_paths=paths,
            ledger=ledger,
        )

    def _understand(self, pid: str, cid: str, message: str, *,
                    chat_mode: str, tz_offset: float, command_id: str,
                    message_id: str, conv_pk: str, prior_messages=None,
                    on_log=None):
        """跑一次理解层；返回 UnderstandingResult。异常向上抛给调用方处理。"""
        store = self._get_state_store()
        conv_pk, ctx = self._resolve_context(
            pid, cid, message=message, chat_mode=chat_mode,
            tz_offset=tz_offset, conv_pk=conv_pk)
        agent = understanding.CandidateUnderstander(
            self._assistant_for(), on_log=on_log)
        with self._scheduler.model_request():
            return understanding.handle_message(
                store, agent,
                user_id=self._uid(), project_id=pid, conversation_id=conv_pk,
                message=message, ctx=ctx, command_id=command_id,
                message_id=message_id, history=prior_messages or [],
            )

    def _selected_pair_for_task(self, run_id: str, lang: str = "zh") -> str:
        """已选定配对的摘要（select_scene 的 selection.json）；无则空串。

        问答/完成报告需要知道“当前用的是哪一天的哪颗 Landsat（型号）和
        Sentinel-2”。lang=en 时输出英文格式。"""
        if not run_id:
            return ""
        try:
            row = self._get_state_store().read(lambda c: c.execute(
                "SELECT result_path, staging_dir FROM attempts a"
                " JOIN nodes n ON n.id = a.node_id"
                " WHERE n.run_id = ? AND n.node_key = 'select_scene'"
                " AND a.status = 'succeeded'"
                " ORDER BY a.attempt_no DESC LIMIT 1",
                (run_id,)).fetchone())
            if not row:
                return ""
            sel_path = ""
            for pth in filter(None, [row[0],
                                     str(Path(row[1]) / "completion.json")
                                     if row[1] else None]):
                try:
                    envelope = json.loads(Path(pth).read_text(encoding="utf-8"))
                    sel_path = str(((envelope.get("result") or {})
                                    .get("context") or {})
                                   .get("selection_path") or "")
                    if sel_path:
                        break
                except (OSError, ValueError):
                    continue
            if not sel_path or not Path(sel_path).is_file():
                return ""
            selection = json.loads(Path(sel_path).read_text(encoding="utf-8"))
            pair = (selection.get("selection") or {}).get("pair") or {}
            if not pair.get("landsat_date"):
                return ""
            # 型号：存储为 L8/L9 等简写 → 展示为“Landsat 9”
            raw_sat = str(pair.get("landsat_satellite") or "")
            num = raw_sat[1:] if raw_sat[:1].upper() == "L" \
                and raw_sat[1:].isdigit() else ""
            sat_disp = f"Landsat {num}" if num else "Landsat"
            ld = pair.get("landsat_date")
            lcc = pair.get("landsat_cloud_cover")
            sd = pair.get("sentinel2_date")
            scc = pair.get("sentinel2_cloud_cover")
            diff = pair.get("time_diff_days")
            if lang == "en":
                return (f"{sat_disp} {ld} (cloud {lcc}%) + Sentinel-2 {sd}"
                        f" (cloud {scc}%), time difference {diff} days")
            return (f"{sat_disp} {ld}（云量 {lcc}%）"
                    f" + Sentinel-2 {sd}（云量 {scc}%），"
                    f"成像时差 {diff} 天")
        except Exception:
            return ""

    def kernel_qa_context(self, pid: str, cid: str) -> dict:
        """对话问答上下文（多角色应知道流程进展）：任务进度/当前节点/
        等待原因/失败原因 + 待答问题与候选。用于“只回答”通道，让模型
        能准确回答“任务跑到哪一步了”“卡在什么状态”这类问题。"""
        try:
            snap = self.session_snapshot(pid, cid)
        except Exception:
            return {}
        fail_map = {}
        try:
            rows = self._get_state_store().read(lambda c: c.execute(
                "SELECT n.run_id, n.node_type, a.error FROM attempts a"
                " JOIN nodes n ON n.id = a.node_id"
                " WHERE a.status = 'failed' AND a.attempt_no ="
                "  (SELECT MAX(attempt_no) FROM attempts WHERE node_id = n.id)"
                " AND n.status = 'failed'").fetchall())
            for rid, ntype, err in rows:
                fail_map.setdefault(rid, []).append((ntype, str(err or "")))
        except Exception:
            pass
        tasks_ctx = []
        for t in (snap.get("tasks") or []):
            nodes = t.get("nodes") or []
            total = len(nodes)
            done = sum(1 for n in nodes if n.get("status") == "succeeded")
            focal = (next((n for n in nodes
                           if n.get("status") in ("running", "committing")), None)
                     or next((n for n in nodes if n.get("status") == "failed"),
                             None)
                     or next((n for n in nodes if n.get("wait_reason")), None)
                     or {})
            item = {
                "label": t.get("label"),
                "status": t.get("summary_status"),
                "progress": (f"{done}/{total} 个节点已完成" if total else ""),
                "current_node": _NODE_LABELS.get(
                    str(focal.get("type") or ""), str(focal.get("type") or "")),
                "wait_reason": str(focal.get("wait_reason") or ""),
            }
            fails = fail_map.get(str(t.get("run_id") or "")) or []
            if fails:
                item["failure"] = "；".join(
                    f"{_NODE_LABELS.get(nt, nt)}：{err[:120]}"
                    for nt, err in fails[:3])
            pair_txt = self._selected_pair_for_task(str(t.get("run_id") or ""))
            if pair_txt:
                item["selected_pair"] = pair_txt
            tasks_ctx.append(item)
        questions_ctx = [
            {"prompt": q.get("prompt"),
             "candidates": [c.get("label") for c in (q.get("candidates") or [])]}
            for q in (snap.get("questions") or [])]
        return {"tasks": tasks_ctx, "open_questions": questions_ctx}

    def session_snapshot(self, pid: str, cid: str, limit: int = 50) -> dict:
        """会话快照：本对话的任务卡与待答问题（§12.1 会话快照接口）。

        pid 可为空：仅凭 cid（台账主键或旧对话编号）反查，
        支持第六阶段规范路由 /api/conversations/{cid}/snapshot。
        """
        store = self._get_state_store()
        uid = self._uid()
        conv_pk = self._conversation_pk(pid, cid) if pid else self._conversation_pk_by_id(cid)
        if not conv_pk:
            return {"tasks": [], "questions": [], "conversation_id": ""}

        def _read(conn):
            return {
                "conversation_id": conv_pk,
                "tasks": [
                    {**understanding.ResolvedTask(t).to_summary(),
                     "summary_status": t.get("summary_status"),
                     "run_id": t.get("current_run_id"),
                     "nodes": [dict(zip(("id", "type", "status", "wait_reason"), r)) for r in conn.execute("SELECT id,node_type,status,wait_reason FROM nodes WHERE run_id=? ORDER BY exec_order", (t.get("current_run_id"),))],
                     "negations": (t.get("slots") or {}).get("negations") or []}
                    for t in t_store.list_tasks(conn, user_id=uid,
                                                conversation_id=conv_pk,
                                                limit=limit)
                ],
                "questions": [
                    {"id": q["id"], "prompt": q.get("prompt") or "",
                     "candidates": q.get("candidates") or [],
                     "targets": q.get("targets") or [],
                     "version": q.get("version")}
                    for q in q_store.list_open_questions(
                        conn, user_id=uid, conversation_id=conv_pk, limit=limit)
                ],
            }

        snap = store.read(_read)
        # ── 第六阶段（§12.1/§12.2）：事件游标 + 当前产物 ──
        snap["event_cursor"] = self._event_cursor(conv_pk)
        snap["artifacts"] = self._conversation_artifacts(store, uid, conv_pk)
        return snap

    def _event_cursor(self, conv_pk: str) -> int:
        """对话事件游标：该对话当前最大事件序号（§12.2 断线补发起点）。"""
        store = self._get_state_store()
        return store.read(lambda c: c.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE conversation_id = ?",
            (conv_pk,)).fetchone()[0])

    def _conversation_artifacts(self, store, uid: str, conv_pk: str,
                                limit: int = 100) -> list:
        """本对话各运行的正式产物（含桥接后的项目视图路径与精度摘要）。"""

        def _read(conn):
            return [
                {"id": r[0], "type": r[1], "path": r[2],
                 "availability": r[3], "retention_class": r[4],
                 "run_id": r[5], "task_id": r[6], "created_at": r[7],
                 "view_path": _bridged_view(r[2], r[6])}
                for r in conn.execute(
                    "SELECT a.id, a.type, a.path, a.availability,"
                    " a.retention_class, n.run_id, r.task_id, a.created_at"
                    " FROM artifacts a"
                    " JOIN attempts at ON at.id = a.attempt_id"
                    " JOIN nodes n ON n.id = at.node_id"
                    " JOIN runs r ON r.id = n.run_id"
                    " JOIN tasks t ON t.id = r.task_id"
                    " WHERE t.user_id = ? AND t.conversation_id = ?"
                    " AND a.availability = 'available'"
                    " ORDER BY a.created_at DESC LIMIT ?",
                    (uid, conv_pk, limit))
            ]

        def _bridged_view(path: str, task_id: str):
            """产物桥接视图：若项目目录存在符号链接视图，则返回视图路径
            （地图/精度/下载面板用它，与旧目录逻辑自然兼容）。"""
            p = Path(path)
            if p.is_file():
                return path
            # 符号链接视图丢失时回退产物本体
            return path

        return store.read(_read)

    def task_detail(self, task_id: str) -> dict:
        """任务详情（§12.1）：版本、运行、节点、问题、产物、失败原因。"""
        from core.state_kernel import tasks as t_store

        if not task_id:
            return {"ok": False, "message": "缺少 task_id"}
        store = self._get_state_store()
        uid = self._uid()

        def _read(conn):
            task = t_store.load_task(conn, task_id)
            if not task or task.get("user_id") != uid:
                return None
            run = conn.execute(
                "SELECT id, task_version, status, cancel_requested,"
                " superseded_by, frozen_inputs FROM runs WHERE id = ?",
                (task.get("current_run_id"),)).fetchone()
            run_info = None
            if run:
                run_info = {"run_id": run[0], "status": run[2],
                            "cancel_requested": run[3],
                            "superseded_by": run[4],
                            "snapshot_hash": json.loads(run[5]).get("snapshot_hash")}
            nodes = [dict(zip(("id", "node_key", "node_type", "status",
                               "wait_reason", "exec_order"), r))
                     for r in conn.execute(
                         "SELECT id, node_key, node_type, status, wait_reason,"
                         " exec_order FROM nodes WHERE run_id = ? ORDER BY exec_order",
                         (task.get("current_run_id"),))]
            questions = [
                {"id": q[0], "prompt": q[1], "status": q[2], "version": q[3]}
                for q in conn.execute(
                    "SELECT id, prompt, status, version FROM questions"
                    " WHERE conversation_id = ? AND status = 'open'",
                    (task.get("conversation_id"),))]
            artifacts = [
                {"id": a[0], "type": a[1], "path": a[2],
                 "availability": a[3], "retention_class": a[4],
                 "created_at": a[5]}
                for a in conn.execute(
                    "SELECT a.id, a.type, a.path, a.availability,"
                    " a.retention_class, a.created_at FROM artifacts a"
                    " JOIN attempts at ON at.id = a.attempt_id"
                    " JOIN nodes n ON n.id = at.node_id"
                    " WHERE n.run_id = ? AND a.availability = 'available'"
                    " ORDER BY a.created_at DESC",
                    (task.get("current_run_id"),))]
            return {"ok": True, "task_id": task_id,
                    "version": task.get("version"),
                    "label": task.get("label"),
                    "capability": task.get("capability"),
                    "summary_status": task.get("summary_status"),
                    "slots": task.get("slots"),
                    "current_run": run_info,
                    "nodes": nodes,
                    "questions": questions,
                    "artifacts": artifacts}

        result = store.read(_read)
        if result is None:
            return {"ok": False, "message": "任务不存在"}
        return result

    def task_command(self, task_id: str, operation: str, request_id: str,
                     seen_version: int = 0,
                     payload: Optional[dict] = None) -> dict:
        """任务命令（§12.1）：取消 / 重试 / 优先级；带所и见版本校验与去重。"""
        from core.state_kernel.store import StaleVersionError

        if not task_id or not operation:
            return {"ok": False, "message": "缺少 task_id 或 operation"}
        store = self._get_state_store()
        uid = self._uid()

        def _owner(conn):
            row = conn.execute("SELECT user_id, version, current_run_id,"
                               " conversation_id FROM tasks WHERE id = ?",
                               (task_id,)).fetchone()
            return row

        row = store.read(_owner)
        if row is None or row[0] != uid:
            return {"ok": False, "message": "任务不存在"}
        current_version = int(row[1])
        if seen_version and int(seen_version) != current_version:
            return {"ok": False, "conflict": True,
                    "message": f"所见版本过期（所见 {seen_version}，当前 {current_version}），请刷新后重试"}
        scheduler = self._scheduler
        try:
            if operation == "cancel":
                def _tx(conn):
                    conn.execute(
                        "UPDATE runs SET cancel_requested = 1 WHERE task_id = ?",
                        (task_id,))
                    from core.state_kernel.store import append_event
                    append_event(conn, type="task.cancel_requested",
                                 task_id=task_id, run_id=row[2],
                                 object_type="task", object_id=task_id,
                                 object_version=current_version,
                                 payload={"request_id": request_id})
                store.submit_write(_tx)
                if scheduler:
                    scheduler.notify()
                return {"ok": True, "message": "已请求停止，正在等待执行者退出",
                        "task_id": task_id, "version": current_version}
            if operation == "retry":
                if not scheduler:
                    return {"ok": False, "message": "调度器未启动"}
                failed = store.read(lambda c: c.execute(
                    "SELECT n.id FROM nodes n JOIN runs r ON r.id = n.run_id"
                    " WHERE r.task_id = ? AND n.status = 'failed'"
                    " ORDER BY n.exec_order DESC LIMIT 1",
                    (task_id,)).fetchone())
                if not failed:
                    return {"ok": False, "message": "没有可重试的失败节点"}
                scheduler.retry(failed[0], uid)
                return {"ok": True, "message": "已重新入队失败节点",
                        "task_id": task_id}
            if operation == "priority":
                priority = int((payload or {}).get("priority") or 0)

                def _prio(conn):
                    from core.state_kernel.store import update_versioned
                    return update_versioned(conn, "tasks", task_id,
                                            current_version, {"priority": priority})
                new_version = store.submit_write(_prio)
                if scheduler:
                    scheduler.notify()
                return {"ok": True, "task_id": task_id,
                        "version": new_version, "priority": priority}
            return {"ok": False, "message": f"未知操作：{operation}"}
        except StaleVersionError as e:
            return {"ok": False, "conflict": True, "message": str(e)}
        except (KeyError, ValueError) as e:
            return {"ok": False, "message": str(e)}

    def artifact_detail(self, artifact_id: str) -> dict:
        """产物元数据（§12.1）：归属校验 + 可用性 + 血缘。"""
        if not artifact_id:
            return {"ok": False, "message": "缺少 artifact_id"}
        store = self._get_state_store()
        uid = self._uid()

        def _read(conn):
            row = conn.execute(
                "SELECT a.id, a.type, a.path, a.content_hash, a.availability,"
                " a.retention_class, a.created_at, a.input_sources,"
                " n.run_id, r.task_id, t.user_id"
                " FROM artifacts a"
                " JOIN attempts at ON at.id = a.attempt_id"
                " JOIN nodes n ON n.id = at.node_id"
                " JOIN runs r ON r.id = n.run_id"
                " JOIN tasks t ON t.id = r.task_id"
                " WHERE a.id = ?", (artifact_id,)).fetchone()
            return row

        row = store.read(_read)
        if row is None or row[10] != uid:
            # 不向其他账号暴露对象细节（§12.1）
            return {"ok": False, "message": "产物不存在"}
        return {"ok": True, "artifact": {
            "id": row[0], "type": row[1], "path": row[2],
            "content_hash": row[3], "availability": row[4],
            "retention_class": row[5], "created_at": row[6],
            "lineage": json.loads(row[7] or "{}"),
            "run_id": row[8], "task_id": row[9]}}

    def artifact_download_path(self, artifact_id: str):
        """产物下载：归属 + 可用性校验后返回可下载的绝对路径或错误。"""
        detail = self.artifact_detail(artifact_id)
        if not detail.get("ok"):
            return None, detail.get("message", "产物不存在")
        artifact = detail["artifact"]
        if artifact["availability"] == "cleaned":
            return None, "产物已按政策清理，可按血缘重建"
        if artifact["availability"] == "missing":
            return None, "产物文件已损坏或丢失，不能下载"
        path = Path(artifact["path"])
        if not path.is_file():
            return None, "产物文件不存在"
        return path, ""

    # ── 产物内容 / 元数据 / 瓦片（第六阶段补强：面板按产物联动） ──

    def _artifact_verified(self, artifact_id: str):
        """产物归属（用户级）与可用性校验；返回 (artifact_dict, error)。"""
        detail = self.artifact_detail(artifact_id)
        if not detail.get("ok"):
            return None, detail.get("message", "产物不存在")
        artifact = detail["artifact"]
        if artifact["availability"] != "available":
            return None, ("产物已按政策清理，可按血缘重建"
                          if artifact["availability"] == "cleaned"
                          else "产物文件已损坏或丢失")
        return artifact, ""

    def artifact_content(self, artifact_id: str, max_bytes: int = 2 * 1024 * 1024) -> dict:
        """小型文本/JSON 产物内容（精度报告等面板直接读取）。"""
        artifact, error = self._artifact_verified(artifact_id)
        if artifact is None:
            return {"ok": False, "message": error}
        path = Path(artifact["path"])
        if not path.is_file():
            return {"ok": False, "message": "产物文件不存在"}
        try:
            if path.stat().st_size > max_bytes:
                return {"ok": False, "message": "产物过大，请下载后查看"}
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"ok": False, "message": f"读取失败：{e}"}
        if path.suffix.lower() == ".json":
            try:
                return {"ok": True, "json": json.loads(text), "name": path.name}
            except ValueError:
                pass
        return {"ok": True, "text": text, "name": path.name}

    def artifact_meta(self, artifact_id: str) -> dict:
        """产物元数据（地图用）：地理范围（EPSG:4326）+ 建议渲染样式。"""
        artifact, error = self._artifact_verified(artifact_id)
        if artifact is None:
            return {"ok": False, "message": error}
        path = Path(artifact["path"])
        meta = {"ok": True, "id": artifact["id"], "name": path.name,
                "type": artifact["type"],
                "style": _artifact_style(path.name)}
        # 标准图层名（与升级前一致）：按推断样式映射 LAYER_DEFS 标签
        meta["style_label"] = next(
            (d["label"] for d in LayerVisualizer.LAYER_DEFS
             if d["id"] == meta["style"]), "")
        # 带影像日期的显示名（如“10m LST（2024-07-22）”），对齐升级前样式
        _base = meta["style_label"] or path.name
        _date = _date_from_name(path.name)
        meta["label_dated"] = f"{_base}（{_date}）" if _date else _base
        if artifact["type"] == "geotiff" and path.is_file():
            try:
                import rasterio
                from rasterio.warp import transform_bounds
                with rasterio.open(path) as src:
                    b = src.bounds
                    if src.crs and src.crs.to_epsg() != 4326:
                        b = transform_bounds(src.crs, "EPSG:4326",
                                             b.left, b.bottom, b.right, b.top,
                                             densify_pts=8)
                    meta["bounds"] = [b[0], b[1], b[2], b[3]]  # w, s, e, n
            except Exception as e:  # noqa: BLE001
                meta["bounds_error"] = f"{type(e).__name__}"
        return meta

    def artifact_tile(self, artifact_id: str, style: str, z: int, x: int, y: int):
        """按产物文件直接渲染 Web Mercator 瓦片（面板联动：地图指定任务结果）。"""
        artifact, error = self._artifact_verified(artifact_id)
        if artifact is None:
            return None
        path = Path(artifact["path"])
        if not path.is_file() or not _TILE_RENDER_SEM.acquire(blocking=False):
            return None
        try:
            style = style if any(
                d["id"] == style for d in LayerVisualizer.LAYER_DEFS) \
                else _artifact_style(path.name)
            mtime = os.path.getmtime(path)
            return LayerVisualizer._render_tile_cached(
                style, str(path), mtime, z, x, y)
        except OSError:
            return None
        finally:
            _TILE_RENDER_SEM.release()

    # ── 任务原始输入层（地图恢复 10m S2 等原生图层） ──────────

    _INPUT_LAYER_DEFS = (
        ("sentinel2_path", "sentinel_rgb", "Sentinel-2 RGB", "sentinel_rgb"),
        ("landsat_path", "landsat_lst", "30m LST", "landsat_lst"),
        ("dem_path", "dem", "DEM", "dem"),
    )

    def _task_raw_paths(self, task_id: str):
        """取任务当前运行的原始输入路径（来自 prepare_local 完成凭据）。

        产物清单只含节点真实输出（输入不复制），地图要在任务视图里
        恢复升级前的原生图层（如 10m S2 RGB），只能回到原始输入。
        返回 (task_dict, raw_paths) 或 (None, {})。
        """
        store = self._get_state_store()
        uid = self._uid()
        task = store.read(lambda c: t_store.load_task(c, task_id))
        if not task or task.get("user_id") != uid or not task.get("current_run_id"):
            return None, {}
        row = store.read(lambda c: c.execute(
            "SELECT a.result_path, a.staging_dir FROM attempts a"
            " JOIN nodes n ON n.id = a.node_id"
            " WHERE n.run_id = ? AND n.node_key = 'prepare_local'"
            " AND a.status = 'succeeded' ORDER BY a.attempt_no DESC LIMIT 1",
            (task["current_run_id"],)).fetchone())
        if not row:
            return task, {}
        candidates = []
        if row[0]:
            candidates.append(Path(row[0]))
        if row[1]:
            candidates.append(Path(row[1]) / "completion.json")
        for path in candidates:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                raw = ((envelope.get("result") or {})
                       .get("context") or {}).get("raw_paths") or {}
                if raw:
                    return task, raw
            except (OSError, ValueError):
                continue
        return task, {}

    def task_inputs(self, task_id: str) -> dict:
        """任务原始输入栅格层清单（含 EPSG:4326 范围），供地图展示。"""
        task, raw = self._task_raw_paths(task_id)
        if task is None:
            return {"ok": False, "message": "任务不存在"}
        inputs = []
        for key, style, label, _s in self._INPUT_LAYER_DEFS:
            path = Path(str(raw.get(key) or ""))
            if not path.is_file():
                continue
            p2_name = path.name
            bounds = None
            try:
                import rasterio
                from rasterio.warp import transform_bounds
                with rasterio.open(path) as src:
                    b = src.bounds
                    if src.crs and src.crs.to_epsg() != 4326:
                        b = transform_bounds(src.crs, "EPSG:4326",
                                             b.left, b.bottom, b.right, b.top,
                                             densify_pts=8)
                    bounds = [b[0], b[1], b[2], b[3]]
            except Exception:  # noqa: BLE001
                continue
            inputs.append({"key": key, "style_id": style,
                           "label": (f"{label}（{_date_from_name(p2_name)}）"
                                     if _date_from_name(p2_name) else label),
                           "group": "acquire",
                           "bounds": bounds})
        return {"ok": True, "inputs": inputs}

    def task_input_sample(self, task_id: str, key: str,
                          lat: float, lon: float):
        """任务原始输入栅格的像元采样（仅 LST 层参与“显示温度”）。"""
        if key != "landsat_path":
            return None
        task, raw = self._task_raw_paths(task_id)
        if task is None:
            return None
        path = Path(str(raw.get(key) or ""))
        return LayerVisualizer.sample_file_value(
            str(path), lat, lon, "landsat_lst")

    def artifact_sample(self, artifact_id: str, lat: float, lon: float):
        """产物栅格的像元采样（仅 LST 产物参与“显示温度”）。"""
        artifact, _error = self._artifact_verified(artifact_id)
        if artifact is None:
            return None
        path = Path(artifact["path"])
        style = _artifact_style(path.name)
        if style not in ("lst_10m", "lst_10m_filled"):
            return None
        return LayerVisualizer.sample_file_value(str(path), lat, lon, style)

    def task_input_tile(self, task_id: str, key: str, z: int, x: int, y: int):
        """按任务原始输入渲染瓦片（路径由服务端解析，不信任客户端传路径）。"""
        task, raw = self._task_raw_paths(task_id)
        if task is None:
            return None
        style = next((s for k, s, _l, _s in self._INPUT_LAYER_DEFS if k == key),
                     None)
        if style is None:
            return None
        path = Path(str(raw.get(key) or ""))
        if not path.is_file() or not _TILE_RENDER_SEM.acquire(blocking=False):
            return None
        try:
            mtime = os.path.getmtime(path)
            return LayerVisualizer._render_tile_cached(
                style, str(path), mtime, z, x, y)
        except OSError:
            return None
        finally:
            _TILE_RENDER_SEM.release()


    def conversation_events(self, cid: str, cursor: int = 0,
                            tz: float = _DEFAULT_TZ_OFFSET,
                            log_cursor: int = 0):
        """对话事件流（§12.2）：快照 → 游标补发 → 持续推送。

        同一连接服务整个对话的多任务；按当前账号与对话过滤；
        前端按事件序号去重、按对象版本拒绝旧状态覆盖新状态。
        执行日志（task_logs）与完成报告等主动气泡（message 事件）
        也在此推送：日志带行号供前端去重，log_cursor 为客户端已有的
        最大日志行号（0 = 仅推新行，历史由 REST 接口加载）。
        """
        store = self._get_state_store()
        uid = self._uid()

        def _conv(conn):
            return conn.execute(
                "SELECT id, project_id, legacy_conv_id FROM conversations"
                " WHERE (id = ? OR legacy_conv_id = ?) AND user_id = ?",
                (cid, cid, uid)).fetchone()

        conv = store.read(_conv)
        if conv is None:
            yield ("event: error\ndata: " + json.dumps(
                {"message": "对话不存在"}, ensure_ascii=False) + "\n\n")
            return
        conv_pk, project_id, legacy_cid = conv[0], conv[1], conv[2]

        # 1) 快照 + 当时最大事件编号（同一短读事务，§12.2 协议第 1 步）
        snap = self.session_snapshot(project_id, legacy_cid)
        snap["event_cursor"] = store.read(lambda c: c.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE conversation_id = ?",
            (conv_pk,)).fetchone()[0])
        yield ("event: snapshot\ndata: " + json.dumps(
            snap, ensure_ascii=False) + "\n\n")

        # 2) 按请求游标补发（§12.2 协议第 2 步）：从游标之后逐条补发，
        #    再转入持续轮询；这样快照到订阅之间发生的更新不会漏掉。
        last = int(cursor or 0)
        last_log = int(log_cursor or 0)
        if last_log <= 0:
            # 未带日志游标（旧客户端/首次）：从当前最大行号起推，避免整表重发
            last_log = int(store.read(lambda c: c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_logs"
                " WHERE conversation_id = ?", (conv_pk,)).fetchone()[0]) or 0)
        msg_ver = self._conv_msg_ver.get(conv_pk, 0)
        deadline_idle = time.time() + 3600  # 无事件时最长保持 1 小时
        while time.time() < deadline_idle:
            # 持久化执行日志：从上次行号之后推送（含失败原因/重试/完成，
            # 跨刷新/重启/中断不丢；行号供前端去重）
            log_rows = store.read(lambda c: c.execute(
                "SELECT id, task_id, text, created_at FROM task_logs"
                " WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT 300",
                (conv_pk, last_log)).fetchall())
            from core.scheduling.scheduler import strip_emoji
            for r in log_rows:
                last_log = max(last_log, int(r[0]))
                yield ("event: log\ndata: " + json.dumps(
                    {"id": int(r[0]), "task_id": r[1] or "",
                     "text": _stamp_with_ts(strip_emoji(r[2]), r[3], tz)},
                    ensure_ascii=False) + "\n\n")
            # 服务端主动追加的消息（任务完成报告等）：版本号变化时推送尾部新气泡
            current_ver = self._conv_msg_ver.get(conv_pk, 0)
            if current_ver != msg_ver:
                msg_ver = current_ver
                try:
                    msgs = self._read_conv_messages(uid, legacy_cid)
                    yield ("event: message\ndata: " + json.dumps(
                        {"messages": msgs[-3:]}, ensure_ascii=False) + "\n\n")
                except Exception:
                    pass
            rows_ = store.read(lambda c: c.execute(
                "SELECT seq, type, occurred_at, task_id, run_id, object_type,"
                " object_id, object_version, payload FROM events"
                " WHERE conversation_id = ? AND user_id = ? AND seq > ?"
                " ORDER BY seq LIMIT 200",
                (conv_pk, uid, last)).fetchall())
            for r in rows_:
                last = max(last, int(r[0]))
                yield ("event: ledger\ndata: " + json.dumps(
                    {"seq": r[0], "type": r[1], "occurred_at": r[2],
                     "task_id": r[3], "run_id": r[4], "object_type": r[5],
                     "object_id": r[6], "object_version": r[7],
                     "payload": json.loads(r[8] or "{}")},
                    ensure_ascii=False) + "\n\n")
            if rows_:
                # 有新事件时附带最新会话摘要（任务卡状态直接可用）
                snap2 = self.session_snapshot(project_id, legacy_cid)
                snap2["event_cursor"] = last
                yield ("event: snapshot\ndata: " + json.dumps(
                    snap2, ensure_ascii=False) + "\n\n")
                deadline_idle = time.time() + 3600
            time.sleep(1.0)

    def _read_conv_messages(self, uid: str, legacy_cid: str) -> list:
        """按显式 uid 读对话消息（SSE 生成器里不依赖请求上下文变量）。"""
        try:
            path = _ROOT / "data" / "users" / uid / "conversations" / f"{legacy_cid}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("messages") or []
        except Exception:
            return []

    def _settle_task(self, resolved, output: str) -> None:
        """执行链返回后回写任务汇总状态（§3.5：状态由真实进展推导）。

        阶段 2 还没有节点级状态，这里只区分三种可判定的结局：
        等待用户选择（暂停标记）、失败（执行链报错）、完成。
        """
        from core.agent.executor import PAUSE_MARKER

        text = str(output or "")
        if PAUSE_MARKER in text:
            status = t_store.TASK_RUNNING
        elif "执行出错" in text or "失败" in text and "重新" in text:
            status = t_store.TASK_FAILED
        else:
            status = t_store.TASK_COMPLETED

        def _tx(conn):
            row = conn.execute("SELECT version FROM tasks WHERE id = ?",
                               (resolved.task_id,)).fetchone()
            if row is None:
                return None
            return t_store.patch_task(conn, resolved.task_id, int(row[0]),
                                      summary_status=status,
                                      event_type="task.settled",
                                      event_payload={"status": status})

        try:
            self._get_state_store().submit_write(_tx)
        except Exception as e:
            print(f"[understanding] 任务状态回写失败（不影响结果）：{e}")

    def _append_answer_bubbles(self, pid: str, cid: str, text: str,
                               reply: str) -> list:
        """把一次卡片回答写入对话消息流（用户回答气泡 + 助手确认气泡）。

        与文字回答的体验保持一致（用户实测反馈：点击选项后不能只弹
        右上角通知，要在对话里形成完整气泡）；失败时仅返回空列表。
        """
        try:
            convs = self.load_conversations()
            if pid not in convs or cid not in convs[pid]:
                return []
            msgs = convs[pid][cid].get("messages", [])
            pair = [{"role": "user", "content": str(text)},
                    {"role": "assistant", "content": str(reply)}]
            msgs.extend(pair)
            self._save_history(pid, cid, msgs)
            return pair
        except Exception as e:  # noqa: BLE001
            print(f"[web_app] 回答气泡写入失败：{e}")
            return []

    def answer_question(self, pid: str, cid: str, question_id: str,
                        text: str, tz_offset: float = _DEFAULT_TZ_OFFSET) -> dict:
        """回答一条持久问题（卡片点击入口，与文字回答共用消费逻辑）。"""
        if not question_id or not str(text or "").strip():
            return {"ok": False, "message": "缺少问题编号或答复内容"}
        store = self._get_state_store()
        conv_pk, ctx = self._resolve_context(
            pid, cid, message=text, chat_mode="work", tz_offset=tz_offset)
        if not conv_pk:
            return {"ok": False, "message": "这条对话还没有台账记录"}
        if self._scheduler:
            try:
                execution_answer = self._scheduler.answer(
                    question_id, text, self._uid(), conv_pk)
            except ValueError:
                # 答案不在该问题已保存的候选中（如过期卡片/候选已变更）：
                # 不抛 500，继续走理解层消费，由其给出「已失效」或正常应用
                execution_answer = None
            if execution_answer is not None:
                reply = self._ack_reply_text()
                appended = self._append_answer_bubbles(pid, cid, text, reply)
                return {"ok": True, "execution": execution_answer,
                        "appended": appended, **self.session_snapshot(pid, cid)}
        applied = understanding.answer_question(
            store, user_id=self._uid(), project_id=pid,
            conversation_id=conv_pk, question_id=question_id,
            text=str(text), ctx=ctx)
        if applied is None:
            return {"ok": False, "expired": True,
                    "message": "这张问题卡片已失效（任务已更新或问题已被回答），"
                               "请按最新状态重新操作"}
        snapshot = self.session_snapshot(pid, cid)
        if applied.get("summary_status") == "ready":
            self._enqueue_ready([applied])
        reply = self._ack_reply_text()
        appended = self._append_answer_bubbles(pid, cid, text, reply)
        return {"ok": True, "task": applied, "appended": appended, **snapshot}

    def _ack_reply_text(self) -> str:
        """问题回答后的确认文案（随用户界面语言）。"""
        return ("答案已接收，任务从对应节点继续。"
                if self._user_lang(self._uid()) == "zh" else
                "Answer received; the task will continue from the corresponding node.")

    def set_exec_mode(self, pid: str, cid: str, mode: str) -> dict:
        """切换交互模式（立即生效，不改变已编译运行的任何科学参数）。

        切到“完全执行”时：把本对话中等待用户选择的“配对选择”问题
        自动代选推荐项（无推荐取最高分），任务无需用户再点选即继续。
        """
        from core.agent.orchestrator.exec_mode import normalize
        next_mode = normalize(mode)
        if not cid:
            return {"ok": False, "message": "缺少对话"}
        self._get_conv_state(cid)["exec_mode"] = next_mode
        try:
            self._update_conversation_file(cid, pid, exec_mode=next_mode)
        except Exception:
            pass
        auto_answered = 0
        if next_mode == "auto" and self._scheduler:
            uid = self._uid()
            store = self._get_state_store()
            conv = store.read(lambda c: c.execute(
                "SELECT id FROM conversations"
                " WHERE (id = ? OR legacy_conv_id = ?) AND user_id = ?",
                (cid, cid, uid)).fetchone())
            if conv:
                rows = store.read(lambda c: q_store.list_open_questions(
                    c, user_id=uid, conversation_id=conv[0]))
                for q in rows:
                    constraint = q.get("answer_constraint") or {}
                    payload = constraint.get("payload") or {}
                    cands = q.get("candidates") or []
                    if payload.get("kind") != "pair_select" or not cands:
                        continue
                    pick = next((x for x in cands
                                 if (x.get("info") or {}).get("recommended")),
                                cands[0])
                    try:
                        answered = self._scheduler.answer(
                            q["id"], str(pick.get("id")),
                            uid, conv[0])
                    except ValueError:
                        answered = None
                    if answered is not None:
                        auto_answered += 1
        return {"ok": True, "mode": next_mode, "auto_answered": auto_answered}

    # ── 计划编译（升级第三阶段：状态内核 → 节点图） ───────

    def planning_compile(self, task_id: str, expected_version: int) -> dict:
        """编译确认后的任务草稿为运行 + 节点图（排队态，不派发执行）。

        参数快照在本调用（进队前）建立并冻结；本调用之后修改设置
        不影响该运行的实际参数（§5.3，验收第 1 条）。
        """
        from core.planning import CompileError, compile_task

        if not task_id or expected_version < 1:
            return {"ok": False, "message": "缺少 task_id 或 expected_version"}
        store = self._get_state_store()
        task = store.read(lambda c: t_store.load_task(c, task_id))
        if not task or task["user_id"] != self._uid():
            return {"ok": False, "message": "任务不存在"}
        settings = self._load_settings()
        settings["_execution"] = {"settings_path": str(self._user_settings_path())}
        try:
            result = compile_task(
                store, task_id=task_id,
                expected_task_version=int(expected_version), settings=settings)
        except CompileError as e:
            return {"ok": False, "message": f"编译失败：{e}"}
        if self._scheduler:
            self._scheduler.notify()
        return {"ok": True, **result}

    def planning_run_detail(self, run_id: str) -> dict:
        """查看运行详情：冻结参数快照（逐项含来源）+ 节点清单与依赖顺序。"""
        from core.planning import get_run_graph, get_run_snapshot

        if not run_id:
            return {"ok": False, "message": "缺少 run_id"}
        store = self._get_state_store()
        owned = store.read(lambda c: c.execute("SELECT 1 FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND t.user_id=?", (run_id, self._uid())).fetchone())
        if not owned:
            return {"ok": False, "message": "运行不存在"}
        snapshot = get_run_snapshot(store, run_id)
        if snapshot is None:
            return {"ok": False, "message": "运行不存在"}
        return {"ok": True, "run_id": run_id,
                "snapshot": snapshot,
                "nodes": get_run_graph(store, run_id)}

    def _user_dir(self) -> Path:
        return _ROOT / "data" / "users" / self._uid()

    def _conv_dir(self) -> Path:
        d = self._user_dir() / "conversations"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _study_dir(self) -> Path:
        d = self._user_dir() / "study_areas"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _user_settings_path(self) -> Path:
        return self._user_dir() / "settings.json"

    def _workspace_root(self) -> Path:
        """项目数据根目录：环境变量 WORKSPACE_ROOT（可指向大容量盘）优先。

        未显式配置时自动探测：
        - ModelScope Studio 创空间持久卷 /mnt/workspace（重新发布不丢数据）；
        - 本地 Docker 挂载卷 /app/data（docker run -v geothermoai_data:/app/data）；
        - 最后兜底仓库内 data/users。
        """
        root = os.environ.get("WORKSPACE_ROOT", "").strip()
        if root:
            return Path(root)
        for cand in ("/mnt/workspace", "/app/data"):
            try:
                if os.path.isdir(cand) and os.access(cand, os.W_OK):
                    return Path(cand) / "users"
            except OSError:
                continue
        return _ROOT / "data" / "users"

    def _auto_project_dir(self, name: str) -> Path:
        """按用户隔离的项目目录：{WORKSPACE_ROOT}/{uid}/workspace/{name}

        路径完全由后端分配，前端不接收用户自定义路径，
        从根本上杜绝不同用户填相同路径导致的数据互写与越权读取。
        """
        safe = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", name.strip()) or "project"
        return self._workspace_root() / self._uid() / "workspace" / safe

    def _owns_project_dir(self, project_dir: str) -> bool:
        """校验 project_dir 归属当前用户：位于其工作区内，或等于其已记录的项目目录"""
        if not project_dir:
            return False
        try:
            pd = os.path.realpath(project_dir)
            user_ws = os.path.realpath(self._workspace_root() / self._uid())
            if pd == user_ws or pd.startswith(user_ws + os.sep):
                return True
            for p in self._load_projects():
                if p.get("dir") and os.path.realpath(p["dir"]) == pd:
                    return True
        except Exception:
            return False
        return False

    def _load_global_settings(self) -> dict:
        path = _ROOT / "config" / "settings.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    _DEFAULT_USER_SETTINGS = {
        "api": {
            "api_format": "openai",
            "api_base_url": "https://api.deepseek.com",
            "model_id": "deepseek-v4-flash",
            "api_key": "",
            "model_type": "deepseek",
            "display_name": "DeepSeek-V4-Flash",
            "context_input": 128000,
            "context_output": 16000,
        },
        "data": {"default_output_dir": "", "cloud_threshold": 30, "dem_source": "copernicus"},
        "model": {"n_estimators": 200, "max_depth": 25, "min_samples_split": 16, "min_samples_leaf": 8},
        "processing": {
            "batch_size": 500000, "chunk_size": 500000,
            "train_ratio": 0.6, "val_ratio": 0.2, "test_ratio": 0.2,
        },
        "data_space": {},
    }
    # 说明：`agent` 段（角色编排特性开关）**不写进每用户默认设置**。
    # `_load_settings` 读到已有文件时不与默认值合并，写进去会把开关钉死在创建时的取值，
    # 导致后续升级默认值对老用户失效。缺失时统一由 `agent_config.resolve` 补代码默认值，
    # 用户显式改过才落盘。

    def _load_settings(self) -> dict:
        """读取当前用户的设置；首次访问按默认值创建（不含任何全局凭据，凭据按用户隔离）"""
        p = self._user_settings_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                # 损坏时先备份原文件再重建，避免用户配置被无备份覆盖
                try:
                    backup = str(p) + ".corrupt"
                    shutil.copyfile(p, backup)
                    logging.warning(f"设置文件损坏已备份到 {backup}，重建默认配置: {e}")
                except Exception:
                    pass
        defaults = json.loads(json.dumps(self._DEFAULT_USER_SETTINGS))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")
        return defaults

    def _save_settings(self, settings: dict):
        p = self._user_settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def _assistant_for(self) -> GeoThermoAI_Assistant:
        """当前用户的 assistant（按用户凭据独立实例化并缓存，禁止跨用户共用）"""
        uid = self._uid()
        ast = self._user_assistants.get(uid)
        if ast is None:
            api = self._load_settings().get("api", {})
            ast = GeoThermoAI_Assistant(
                model_type=api.get("model_type", "deepseek"),
                api_key=api.get("api_key", ""),
                api_base_url=api.get("api_base_url", ""),
                model_id=api.get("model_id", ""),
                api_format=api.get("api_format", "openai"),
            )
            ast.model_display_name = api.get("display_name", "") or api.get("model_id", "")
            ast.system_prompt = ast._build_system_prompt()
            self._user_assistants[uid] = ast
        return ast

    def _agent_for(self) -> GeoThermoAgent:
        """当前用户的 agent（与用户 assistant 绑定）"""
        uid = self._uid()
        ag = self._user_agents.get(uid)
        if ag is None:
            ag = GeoThermoAgent(self._assistant_for(), self.registry)
            self._user_agents[uid] = ag
        return ag

    def _memory_for(self) -> MemoryManager:
        """当前用户的记忆管理器（懒加载 + 首次播种领域知识）"""
        uid = self._uid()
        mm = self._user_memories.get(uid)
        if mm is None:
            mm = MemoryManager(
                memory_root=str(self._user_dir() / "memory"),
                embedding_model_dir=str(_ROOT / "models" / "bge-small-zh-v1.5"),
            )
            mm.ensure_seeded()
            self._user_memories[uid] = mm
        return mm

    def _agent_settings(self) -> dict:
        """解析角色编排配置：每用户 settings 的 agent 段 > 全局 config/settings.json > 代码默认。

        每用户设置文件里通常没有 agent 段（不写入默认值，见 _DEFAULT_USER_SETTINGS 的说明），
        因此部署时改 `config/settings.json` 即可统一切换特性开关。
        """
        user = self._load_settings()
        if isinstance(user.get("agent"), dict):
            return agent_config.resolve(user)
        return agent_config.resolve(self._load_global_settings())

    def _invalidate_user_runtime(self):
        """设置变更后重建该用户的 assistant/agent（凭据热更新）"""
        uid = self._uid()
        self._user_assistants.pop(uid, None)
        self._user_agents.pop(uid, None)

    def _get_conv_state(self, conv_id: str) -> dict:
        if conv_id not in self._conv_states:
            self._conv_states[conv_id] = {
                "project_dir": "",
                "exec_mode": DEFAULT_EXEC_MODE,
                "workflow_progress": {
                    "status": "idle",
                    "current_step": "",
                    "current_index": -1,
                    "steps": [],
                },
            }
        return self._conv_states[conv_id]

    # ── 项目/对话管理 ──────────────────────────────────────────

    def _load_projects(self) -> list:
        """从 _projects.json 读取项目列表（空项目也能持久化）

        新格式为 [{"id", "name", "dir"}]，兼容旧格式（dict 无 id / 字符串数组）。
        无 id 的旧项目首次加载时自动补齐稳定 uuid 并持久化（重命名不失联）。
        """
        path = self._conv_dir() / "_projects.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f).get("projects", [])
        except Exception:
            return []
        result = []
        changed = False
        for item in raw:
            if isinstance(item, dict):
                pid = item.get("id")
                if not pid:
                    pid = uuid.uuid4().hex[:12]
                    changed = True
                result.append({"id": pid, "name": item.get("name", ""), "dir": item.get("dir", "")})
            elif isinstance(item, str):
                result.append({"id": uuid.uuid4().hex[:12], "name": item, "dir": ""})
                changed = True
        result = [p for p in result if p.get("name")]
        if changed:
            try:
                self._save_projects(result)
            except Exception:
                pass
        return result

    def _project_id(self, project_name: str) -> str:
        """按项目名查稳定 id（找不到返回空串）"""
        for p in self._load_projects():
            if p.get("name") == project_name:
                return p.get("id", "")
        return ""

    def _save_projects(self, projects: list):
        path = self._conv_dir() / "_projects.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"projects": projects}, f, ensure_ascii=False, indent=2)

    def load_conversations(self) -> dict:
        """返回 {项目名: {conv_id: {...}, "__dir__": path}}"""
        convs: Dict[str, dict] = {p["name"]: {"__dir__": p.get("dir", "")}
                                  for p in self._load_projects()}
        if not self._conv_dir().exists():
            return convs
        for f in sorted(self._conv_dir().glob("*.json"), key=lambda p: p.stat().st_mtime):
            if f.name == "_projects.json":
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                project = data.get("project") or "默认项目"
                conv_id = data.get("id")
                if not conv_id:
                    continue
                convs.setdefault(project, {})
                convs[project][conv_id] = {
                    "title": data.get("title", "未命名对话"),
                    "messages": data.get("messages", []),
                    "starred": data.get("starred", False),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                }
                if data.get("project_dir"):
                    convs[project].setdefault("__dir__", data["project_dir"])
                    self._get_conv_state(conv_id)["project_dir"] = data["project_dir"]
                if data.get("exec_mode"):
                    self._get_conv_state(conv_id)["exec_mode"] = normalize_exec_mode(
                        data["exec_mode"])
            except Exception:
                continue
        return convs

    def _persist_conversation(self, conv_id: str, project: str, title: str,
                              messages: list, project_dir: str = ""):
        path = self._conv_dir() / f"{conv_id}.json"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        old = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f) or {}
            except Exception:
                old = {}
        # 以原文件为基底合并写入：保留 exec_mode 等扩展字段，
        # 避免消息保存把交互模式/后续新增键丢接
        data = dict(old)
        data.update({
            "id": conv_id,
            "project": project,
            "title": title,
            "messages": messages,
            "project_dir": project_dir,
            "created_at": old.get("created_at", now),
            "updated_at": now,
            "starred": old.get("starred", False),
        })
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _update_conversation_file(self, conv_id: str, project: str, **updates):
        path = self._conv_dir() / f"{conv_id}.json"
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["project"] = project
        data.update(updates)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def create_project(self, name: str, path: str = "") -> dict:
        """创建项目；项目目录由后端按用户自动分配（{WORKSPACE_ROOT}/{uid}/workspace/{name}），
        忽略前端传入的 path（目录物理隔离）"""
        name = (name or "").strip()
        convs = self.load_conversations()
        if not name:
            return {"ok": False, "message": "请输入项目名称"}
        if name in convs:
            return {"ok": False, "message": "项目已存在"}
        auto_dir = self._auto_project_dir(name)
        try:
            auto_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "message": f"无法创建项目目录：{auto_dir}（{e}）"}
        normalized = str(auto_dir).replace("\\", "/")
        projects = self._load_projects()
        if not any(p["name"] == name for p in projects):
            projects.append({"id": uuid.uuid4().hex[:12], "name": name, "dir": normalized})
            self._save_projects(projects)
        return {"ok": True, "message": f"项目「{name}」创建成功",
                "projects": [p["name"] for p in projects]}

    def rename_project(self, pid: str, new_name: str) -> dict:
        """重命名项目：同步 _projects.json 与所有对话文件"""
        new_name = (new_name or "").strip()
        if not new_name:
            return {"ok": False, "message": "请输入新的项目名称"}
        convs = self.load_conversations()
        if pid not in convs:
            return {"ok": False, "message": "项目不存在"}
        if new_name == pid:
            return {"ok": True, "message": "名称未变化"}
        if new_name in convs:
            return {"ok": False, "message": "项目已存在"}
        projects = self._load_projects()
        for p in projects:
            if p["name"] == pid:
                p["name"] = new_name
        self._save_projects(projects)
        for cid, meta in convs[pid].items():
            if cid.startswith("__"):
                continue
            try:
                self._update_conversation_file(cid, new_name)
            except Exception:
                pass
        return {"ok": True, "message": f"项目已重命名为「{new_name}」",
                "projects": [p["name"] for p in projects]}

    def delete_project(self, pid: str) -> dict:
        convs = self.load_conversations()
        if pid not in convs:
            return {"ok": False, "message": "项目不存在"}
        if self._scheduler:
            self._scheduler.cancel_scope(self._uid(), pid)
        # 级联删除台账与运行数据：任务/运行/产物/日志 + 运行目录 + 下载缓存
        legacy_ids = [c for c in convs[pid].keys() if not c.startswith("__")]
        try:
            run_ids = self._purge_kernel_for_conversations(legacy_ids)
            self._purge_run_dirs(run_ids, pid)
        except Exception as e:
            logging.warning(f"[cleanup] 删除项目台账/运行数据失败: {e}")
        # 主线程先完成文件与状态删除，立即返回提示（删除慢体验优化）
        try:
            project_id = self._project_id(pid)
            if project_id and (self._user_dir() / "memory").exists():
                threading.Thread(
                    target=self._async_delete_project_memory,
                    args=(project_id,),
                    daemon=True,
                ).start()
        except Exception as e:
            logging.warning(f"[memory] 删除项目记忆失败: {e}")
        # 级联删除该项目的工作区数据文件夹（影像/产物等全部文件 + 文件夹本身）：
        # 同时覆盖自动分配目录与项目中记录的自定义目录（兼容旧数据），
        # 大目录删除放后台线程，不阻塞删除请求返回
        try:
            dirs = {self._auto_project_dir(pid)}
            for _p in self._load_projects():
                if _p.get("name") == pid and _p.get("dir"):
                    dirs.add(Path(_p["dir"]))
            for d in {os.path.realpath(str(x)) for x in dirs if str(x)}:
                if self._owns_project_dir(d) and os.path.isdir(d):
                    threading.Thread(
                        target=lambda d=d: shutil.rmtree(d, ignore_errors=True),
                        daemon=True,
                    ).start()
        except Exception as e:
            logging.warning(f"[memory] 删除项目工作区目录失败: {e}")
        for cid in list(convs[pid].keys()):
            if cid.startswith("__"):
                continue
            self._hard_delete_conversation(cid, pid, convs)
        projects = [p for p in self._load_projects() if p["name"] != pid]
        self._save_projects(projects)
        return {"ok": True, "message": f"项目「{pid}」已删除",
                "projects": [p["name"] for p in projects]}

    def _async_delete_project_memory(self, project_id: str):
        """后台线程删除项目记忆（不阻塞删除请求返回）"""
        try:
            self._memory_for().delete_project(project_id)
        except Exception as e:
            logging.warning(f"[memory] 后台删除项目记忆失败: {e}")

    def _purge_kernel_for_conversations(self, legacy_ids: list) -> list:
        """彻底删除对话所属的台账数据，返回被清掉的运行 id（供删除运行目录）。

        项目/对话删除时调用：任务、运行、节点、尝试、产物、问题、事件、
        日志与消息一并删除，避免"删了项目但台账与磁盘仍占用"的残留。
        """
        clean_ids = [str(x) for x in (legacy_ids or []) if x]
        if not clean_ids:
            return []
        store = self._get_state_store()
        uid = self._uid()

        def tx(conn):
            qs = ",".join("?" for _ in clean_ids)
            conv_pks = [r[0] for r in conn.execute(
                f"SELECT id FROM conversations WHERE user_id=?"
                f" AND legacy_conv_id IN ({qs})", (uid, *clean_ids))]
            if not conv_pks:
                return []
            cq = ",".join("?" for _ in conv_pks)
            task_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM tasks WHERE conversation_id IN ({cq})", conv_pks)]
            q_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM questions WHERE conversation_id IN ({cq})", conv_pks)]
            tq = ",".join("?" for _ in task_ids)
            run_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM runs WHERE task_id IN ({tq})", task_ids)] \
                if task_ids else []
            rq = ",".join("?" for _ in run_ids)
            attempt_ids = [r[0] for r in conn.execute(
                "SELECT a.id FROM attempts a JOIN nodes n ON n.id=a.node_id"
                f" WHERE n.run_id IN ({rq})", run_ids)] if run_ids else []
            aq = ",".join("?" for _ in attempt_ids)
            artifact_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM artifacts WHERE attempt_id IN ({aq})",
                attempt_ids)] if attempt_ids else []
            # 叶子 → 根：按外键依赖自下而上级联删除
            if q_ids:
                qq = ",".join("?" for _ in q_ids)
                conn.execute(f"DELETE FROM question_targets"
                             f" WHERE question_id IN ({qq})", q_ids)
                conn.execute(f"DELETE FROM questions WHERE id IN ({qq})", q_ids)
            if artifact_ids:
                aq2 = ",".join("?" for _ in artifact_ids)
                conn.execute(f"DELETE FROM artifact_refs"
                             f" WHERE artifact_id IN ({aq2})", artifact_ids)
                conn.execute(f"DELETE FROM artifacts WHERE id IN ({aq2})",
                             artifact_ids)
            if attempt_ids:
                conn.execute(f"DELETE FROM resource_reservations"
                             f" WHERE attempt_id IN ({aq})", attempt_ids)
                conn.execute(f"DELETE FROM commit_intents"
                             f" WHERE attempt_id IN ({aq})", attempt_ids)
                conn.execute(f"DELETE FROM attempts WHERE id IN ({aq})",
                             attempt_ids)
            if run_ids:
                conn.execute(f"DELETE FROM node_edges WHERE run_id IN ({rq})",
                             run_ids)
                conn.execute(f"DELETE FROM decisions WHERE run_id IN ({rq})",
                             run_ids)
                conn.execute(f"DELETE FROM projection_jobs WHERE run_id IN ({rq})",
                             run_ids)
                conn.execute(f"DELETE FROM nodes WHERE run_id IN ({rq})", run_ids)
            # 删除事件前先清引用事件序号的写回待办
            # （projection_jobs.event_seq → events.seq 外键）
            for cpk in conv_pks:
                conn.execute("DELETE FROM projection_jobs WHERE event_seq IN"
                             " (SELECT seq FROM events WHERE conversation_id=?)",
                             (cpk,))
            if task_ids:
                conn.execute("DELETE FROM projection_jobs WHERE event_seq IN"
                             f" (SELECT seq FROM events WHERE task_id IN ({tq}))",
                             task_ids)
            if run_ids:
                conn.execute("DELETE FROM projection_jobs WHERE event_seq IN"
                             f" (SELECT seq FROM events WHERE run_id IN ({rq}))",
                             run_ids)
                conn.execute(f"DELETE FROM runs WHERE id IN ({rq})", run_ids)
            if task_ids:
                conn.execute(f"DELETE FROM tasks WHERE id IN ({tq})", task_ids)
            for cpk in conv_pks:
                conn.execute("DELETE FROM commands WHERE message_id IN"
                             " (SELECT id FROM messages WHERE conversation_id=?)",
                             (cpk,))
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (cpk,))
                conn.execute("DELETE FROM events WHERE conversation_id=?", (cpk,))
                conn.execute("DELETE FROM task_logs WHERE conversation_id=?", (cpk,))
                conn.execute("DELETE FROM conversations WHERE id=?", (cpk,))
            # 兼收对话归属为空、但按任务/运行归属的残余事件
            if run_ids:
                conn.execute(f"DELETE FROM events WHERE run_id IN ({rq})", run_ids)
            if task_ids:
                conn.execute(f"DELETE FROM events WHERE task_id IN ({tq})", task_ids)
            return run_ids

        return store.submit_write(tx, timeout=60.0)

    def _purge_run_dirs(self, run_ids: list, project_name: str = ""):
        """后台删除运行目录（staging + runs/<run> 发布树）与项目下载缓存。

        安全护栏：只删 executions 下的"运行号"直接子目录与 cache/{uid}_{项目}，
        绝不整体删除 runs / cache 入口目录。
        """
        rids = [str(r) for r in (run_ids or []) if r]
        if not rids and not project_name:
            return
        uid = self._uid()

        def _job():
            try:
                root = self._get_state_store().db_path.parent / "executions"
                for rid in rids:
                    for d in (root / rid, root / "runs" / rid):
                        if (d.parent.name in ("executions", "runs")
                                and d.name == rid):
                            shutil.rmtree(d, ignore_errors=True)
                if project_name:
                    shutil.rmtree(root / "cache" / f"{uid}_{project_name}",
                                  ignore_errors=True)
            except Exception as e:  # noqa: BLE001
                logging.warning(f"[cleanup] 删除运行目录失败: {e}")

        threading.Thread(target=_job, daemon=True).start()

    def _conv_project_dir(self, project_root: str, conv_id: str) -> str:
        """对话级独立工作目录（各对话并行互不干扰）。

        每个对话使用 {项目根}/convs/{对话id} 作为自己的 project_dir，
        所有影像/产物/清单都写在该子目录内，对话之间不共享文件、可并行执行。
        项目根未设置时返回空串（沿用旧行为：先设置项目目录才能执行）。
        """
        root = (project_root or "").strip()
        if not root:
            return ""
        conv_dir = os.path.join(root, "convs", conv_id).replace("\\", "/")
        try:
            os.makedirs(conv_dir, exist_ok=True)
        except OSError:
            pass
        return conv_dir

    def create_conversation(self, project: str, title: str) -> dict:
        title = (title or "").strip() or "新对话"
        conv_id = uuid.uuid4().hex[:12]
        convs = self.load_conversations()
        if project not in convs:
            return {"ok": False, "message": "项目不存在，请先创建项目"}
        conv_dir = self._conv_project_dir(convs[project].get("__dir__", ""), conv_id)
        self._get_conv_state(conv_id)["project_dir"] = conv_dir
        self._persist_conversation(conv_id, project, title, [], conv_dir)
        return {
            "ok": True,
            "message": f"对话「{title}」创建成功",
            "conv_id": conv_id,
            "project": project,
        }

    def _hard_delete_conversation(self, cid: str, pid: str, convs: dict):
        """彻底删除对话：磁盘文件 + 内存状态 + 唤醒运行中线程"""
        self._deleted_convs.add(cid)
        ev = self._pause_events.get(cid)
        if ev:
            self._pause_responses.pop(cid, None)
            try:
                ev.set()
            except Exception:
                pass
        # 删除该对话的工作目录 {项目根}/convs/{cid}（后台线程）。
        # 项目级共享缓存（pairs/{研究区}/、dem_{研究区}.tif）在项目根，
        # 不属于单个对话，保留供其他对话复用，不会因删除对话而丢失。
        try:
            project_root = str((convs.get(pid) or {}).get("__dir__") or "")
            conv_dir = self._conv_project_dir(project_root, cid)
            if conv_dir and os.path.isdir(conv_dir):
                threading.Thread(
                    target=lambda d=conv_dir: shutil.rmtree(d, ignore_errors=True),
                    daemon=True,
                ).start()
        except Exception as e:
            logging.warning(f"[conv] 删除对话工作目录失败: {e}")
        path = self._conv_dir() / f"{cid}.json"
        if path.exists():
            try:
                os.remove(path)
            except Exception:
                try:
                    time.sleep(0.3)
                    os.remove(path)
                except Exception:
                    pass
        self._conv_states.pop(cid, None)
        self._stream_queues.pop(cid, None)
        self._pause_events.pop(cid, None)
        self._pause_responses.pop(cid, None)
        self._agent_threads.pop(cid, None)
        self._stream_starts.pop(cid, None)
        self._stream_gen.pop(cid, None)
        self._stream_content.pop(cid, None)
        self._stream_logs.pop(cid, None)
        self._stream_thinking.pop(cid, None)
        self._stream_thinking_seconds.pop(cid, None)
        self._deleted_convs.discard(cid)

    def delete_conversation(self, cid: str, pid: str) -> dict:
        convs = self.load_conversations()
        if pid not in convs or cid not in convs[pid]:
            return {"ok": False, "message": "对话不存在"}
        if self._scheduler:
            self._scheduler.cancel_scope(self._uid(), pid, cid)
        # 级联删除该对话的台账与运行数据（含运行目录）
        try:
            run_ids = self._purge_kernel_for_conversations([cid])
            self._purge_run_dirs(run_ids)
        except Exception as e:
            logging.warning(f"[cleanup] 删除对话台账/运行数据失败: {e}")
        # 级联删除该对话产生的实验记忆放后台线程，主线程先完成文件与状态删除，
        # 立即返回提示（删除慢体验优化）
        try:
            project_id = self._project_id(pid)
            if project_id and (self._user_dir() / "memory").exists():
                threading.Thread(
                    target=self._async_delete_conversation_memory,
                    args=(project_id, cid),
                    daemon=True,
                ).start()
        except Exception as e:
            logging.warning(f"[memory] 删除对话记忆失败: {e}")
        self._hard_delete_conversation(cid, pid, convs)
        remaining = [k for k in convs[pid] if not k.startswith("__")]
        title = convs[pid][cid].get("title") or cid
        return {"ok": True, "message": f"对话「{title}」已彻底删除", "remaining": remaining}

    def _async_delete_conversation_memory(self, project_id: str, cid: str):
        """后台线程删除对话记忆（不阻塞删除请求返回）"""
        try:
            self._memory_for().delete_conversation(project_id, cid)
        except Exception as e:
            logging.warning(f"[memory] 后台删除对话记忆失败: {e}")

    def save_project_dir(self, pid: str, path: str) -> dict:
        """保存项目目录。路径由后端按用户自动分配（与 create_project 一致），
        忽略前端传入路径，杜绝把项目指向他人目录。"""
        convs = self.load_conversations()
        if pid not in convs:
            return {"ok": False, "message": "请先选择项目"}
        auto_dir = self._auto_project_dir(pid)
        try:
            auto_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "message": f"无法创建项目目录：{auto_dir}（{e}）"}
        normalized = str(auto_dir).replace("\\", "/")
        # 同步 _projects.json 中的项目目录（空项目也能记住路径）
        projects = self._load_projects()
        for p in projects:
            if p["name"] == pid:
                p["dir"] = normalized
        self._save_projects(projects)
        for cid in convs[pid]:
            if cid.startswith("__"):
                continue
            conv_dir = self._conv_project_dir(normalized, cid)
            if cid in self._conv_states:
                self._conv_states[cid]["project_dir"] = conv_dir
            try:
                self._update_conversation_file(cid, pid, project_dir=conv_dir)
            except Exception:
                pass
        return {"ok": True, "message": "项目目录已保存", "path": normalized}

    def get_messages(self, pid: str, cid: str) -> list:
        convs = self.load_conversations()
        if pid in convs and cid in convs[pid]:
            self._get_conv_state(cid)["project_dir"] = self._conv_project_dir(
                convs[pid].get("__dir__", ""), cid)
            return convs[pid][cid].get("messages", [])
        return []

    def _save_history(self, pid: str, cid: str, history: list):
        if cid in self._deleted_convs:
            return
        convs = self.load_conversations()
        if pid not in convs or cid not in convs[pid]:
            return
        convs[pid][cid]["messages"] = history
        convs[pid][cid]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            project_dir = self._conv_project_dir(convs[pid].get("__dir__", ""), cid)
            title = convs[pid][cid].get("title", "")
            self._persist_conversation(cid, pid, title, history, project_dir)
        except Exception:
            pass

    def _persist_stream_content(self, pid: str, cid: str, content: str,
                                thinking: str = "", thinking_seconds: float = 0.0):
        """把当前累积的流内容写回对话历史（节流调用）。

        服务重启 / SSE 断线时内存态（_stream_content 等）会丢失，这里定期落盘
        备份，保证断线恢复后气泡不是空的；任何失败仅告警，绝不影响主流程。
        """
        try:
            convs = self.load_conversations()
            if pid not in convs or cid not in convs[pid]:
                return
            msgs = convs[pid][cid].get("messages", [])
            if not msgs or msgs[-1].get("role") != "assistant":
                return
            msgs[-1]["content"] = content
            if thinking:
                msgs[-1]["thinking"] = thinking
            if thinking_seconds:
                msgs[-1]["thinking_seconds"] = thinking_seconds
            self._save_history(pid, cid, msgs)
        except Exception as e:
            logging.warning(f"[stream] 流内容落盘失败（已忽略）: {e}")

    def _last_stream_content(self, cid: str) -> tuple:
        """从对话历史取**最后一条** assistant 气泡内容（流式节流落盘的备份）。

        服务重启后内存态清空，用它恢复当前流的部分结果。只取最后一条（哪怕是空的），
        不向前跳历史消息——否则会把上一条已完成的气泡误填进当前正在生成的气泡。
        返回 (content, thinking, thinking_seconds)。
        """
        try:
            convs = self.load_conversations()
            for _p, items in convs.items():
                if cid in items:
                    for m in reversed(items[cid].get("messages", [])):
                        if m.get("role") == "assistant":
                            return (m.get("content", ""), m.get("thinking", ""),
                                    m.get("thinking_seconds", 0.0))
                    break
        except Exception:
            pass
        return "", "", 0.0

    # ── API 设置 ───────────────────────────────────────────────

    @staticmethod
    def _mask_secret(v) -> str:
        """凭据掩码：sk-****1234；空值返回空串"""
        if not v or len(v) < 5:
            return ""
        return v[:2] + "****" + v[-4:]

    def get_settings(self) -> dict:
        s = self._load_settings()
        api = s.get("api", {})
        raw_key = api.get("api_key", "")
        ds = s.get("data_space", {})
        return {
            "api_format": api.get("api_format", "openai"),
            "base_url": api.get("api_base_url", ""),
            # 凭据不回传明文：只给掩码与长度（供前端按真实长度显示黑点）
            "api_key": "",
            "has_api_key": bool(raw_key),
            "api_key_masked": self._mask_secret(raw_key),
            "api_key_len": len(raw_key) if raw_key else 0,
            "model_id": api.get("model_id", ""),
            "display_name": api.get("display_name", ""),
            "model_type": api.get("model_type", "deepseek"),
            "context_input": api.get("context_input", 128000),
            "context_output": api.get("context_output", 16000),
            # Copernicus Data Space 配置（前端数据源面板读写；秘密字段只回掩码+长度）
            "data_space": {
                "username": ds.get("username", ""),
                "client_id": ds.get("client_id", ""),
                "s3_key": ds.get("s3_key", ""),
                "password": self._mask_secret(ds.get("password", "")),
                "client_secret": self._mask_secret(ds.get("client_secret", "")),
                "s3_secret": self._mask_secret(ds.get("s3_secret", "")),
                "has_password": bool(ds.get("password")),
                "has_client_secret": bool(ds.get("client_secret")),
                "has_s3_secret": bool(ds.get("s3_secret")),
                "password_len": len(ds.get("password") or ""),
                "client_secret_len": len(ds.get("client_secret") or ""),
                "s3_secret_len": len(ds.get("s3_secret") or ""),
            },
        }

    def save_settings(self, payload: dict) -> dict:
        s = self._load_settings()
        api = s.setdefault("api", {})

        api_format = "anthropic" if payload.get("api_format") == "anthropic" else "openai"
        base_url = (payload.get("base_url") or "").strip().rstrip("/")
        model_id = (payload.get("model_id") or "").strip()
        display_name = (payload.get("display_name") or "").strip() or model_id

        api["api_format"] = api_format
        api["api_base_url"] = base_url
        # 密钥：显式传 null = 清除已保存密钥；空串/掩码占位 = 保持原值
        #（前端回显的是掩码，不能写回）
        raw_key = payload.get("api_key")
        if raw_key is None:
            api.pop("api_key", None)
        else:
            api_key = str(raw_key).strip()
            if api_key:
                api["api_key"] = api_key
        if model_id:
            api["model_id"] = model_id
        if display_name:
            api["display_name"] = display_name
        api["model_type"] = payload.get("model_type", api.get("model_type", "deepseek"))
        api["context_input"] = payload.get("context_input", api.get("context_input", 128000))
        api["context_output"] = payload.get("context_output", api.get("context_output", 16000))

        # 数据源面板保存 Copernicus Data Space 配置（秘密字段留空保持原值）
        if isinstance(payload.get("data_space"), dict):
            ds = s.setdefault("data_space", {})
            for k, v in payload["data_space"].items():
                if not isinstance(v, str):
                    if v is not None:
                        ds[k] = v
                    continue
                v = v.strip()
                if k in ("password", "client_secret", "s3_secret") and not v:
                    continue  # 留空 = 不修改
                if v:
                    ds[k] = v
                else:
                    ds.pop(k, None)

        self._save_settings(s)
        # 凭据变更后重建该用户的 assistant/agent（热更新，且不影响其他用户）
        self._invalidate_user_runtime()
        return {"ok": True, "message": "API 设置已保存并应用",
                "display_name": api.get("display_name", "")}

    # ── 模型参数 ───────────────────────────────────────────────

    def get_model_params(self) -> dict:
        s = self._load_settings()
        return s.get("model", {})

    def save_model_params(self, params: dict) -> dict:
        s = self._load_settings()
        s["model"] = {k: v for k, v in params.items() if v is not None}
        self._save_settings(s)
        return {"ok": True, "message": "参数已保存"}

    # ── 研究区上传 ─────────────────────────────────────────────

    def save_uploaded_file(self, filename: str, content: bytes) -> str:
        """保存单个上传文件到研究区目录（仅落盘）。

        .shp 的 GeoJSON 转换由上传接口统一在本次所有配套文件（.dbf/.shx/.prj）
        保存完成后再调用 convert_uploaded_shp，避免 .shp 先于 .dbf 落盘时转换失败。
        """
        self._study_dir().mkdir(parents=True, exist_ok=True)
        fname = os.path.basename(filename)
        dest = self._study_dir() / fname
        try:
            with open(dest, "wb") as f:
                f.write(content)
        except Exception as e:
            return f"✗ {fname}: {e}"
        return f"✓ {fname}"

    def convert_uploaded_shp(self, stem: str) -> Tuple[bool, str]:
        """把研究区目录中已保存的 <stem>.shp 转换为同名 .geojson（须先上传配套文件）"""
        shp_path = self._study_dir() / (stem + ".shp")
        if not shp_path.is_file():
            return False, f"未找到 {stem}.shp"
        gj_path = self._study_dir() / (stem + ".geojson")
        return self._shp_to_geojson(str(shp_path), str(gj_path))

    def validate_study_area(self, path: str) -> Tuple[str, str]:
        """验证研究区文件能否被正常加载，返回 (level, message)。

        level: ok（可正常加载）/ warn（可加载但坐标或范围存疑）/ fail（无法加载）。
        统一按 WGS84 经纬度口径检查：GeoJSON 无 CRS 时按 EPSG:4326 处理，SHP
        转换产物已重投影到 4326，因此坐标范围检查可直接暴露"投影坐标当经纬度用"
        一类坐标系错误。
        """
        try:
            from osgeo import gdal, ogr, osr
        except Exception as e:
            return "fail", f"GDAL 不可用: {e}"
        try:
            src = gdal.OpenEx(path, gdal.OF_VECTOR)
            if src is None:
                return "fail", "文件无法打开，请检查是否为有效的矢量文件"
            try:
                if src.GetLayerCount() == 0:
                    return "fail", "文件不含任何图层"
                layer = src.GetLayer(0)
                n_feats = layer.GetFeatureCount()
                if n_feats == 0:
                    return "fail", "图层不含要素"
                geom_name = ogr.GeometryTypeToName(layer.GetGeomType())
                # WGS84 外接矩形：源带 CRS 时把四角重投影到 4326，否则按 4326 处理
                srs_src = layer.GetSpatialRef()
                try:
                    srs_wkt = srs_src.ExportToWkt() if srs_src is not None else ""
                except Exception:
                    srs_wkt = ""
                minx, maxx, miny, maxy = layer.GetExtent(True)
                if srs_wkt:
                    # 目标统一用传统 GIS 轴序（经度,纬度）的 WGS84：GDAL 3 对
                    # EPSG:4326 默认权威轴序为（纬度,经度），直接拿包络角点会错位
                    from core.geo_transform import make_traditional_gis_order_srs
                    ct = osr.CoordinateTransformation(srs_src, make_traditional_gis_order_srs(4326))
                    lons, lats = [], []
                    for px, py in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
                        pt = ogr.Geometry(ogr.wkbPoint)
                        pt.AddPoint_2D(px, py)
                        pt.Transform(ct)
                        lons.append(pt.GetX())
                        lats.append(pt.GetY())
                    lon_min, lon_max = min(lons), max(lons)
                    lat_min, lat_max = min(lats), max(lats)
                else:
                    lon_min, lon_max, lat_min, lat_max = minx, maxx, miny, maxy
                if lon_min < -180 or lon_max > 180 or lat_min < -90 or lat_max > 90:
                    return "warn", (
                        f"坐标范围 {lon_min:.3f}~{lon_max:.3f}, {lat_min:.3f}~{lat_max:.3f} "
                        "超出经纬度范围，请确认研究区为经纬度坐标（投影坐标系请先转为 WGS84）"
                    )
                if (lon_max - lon_min) < 1e-4 or (lat_max - lat_min) < 1e-4:
                    return "warn", "研究区范围过小，请检查数据是否正确"
                return "ok", (
                    f"加载成功：{n_feats} 个要素（{geom_name}），"
                    f"范围 {lon_min:.4f}~{lon_max:.4f}, {lat_min:.4f}~{lat_max:.4f}"
                )
            finally:
                src = None
        except Exception as e:
            return "fail", f"验证过程出错: {e}"

    def _shp_to_geojson(self, shp_path: str, geojson_path: str) -> Tuple[bool, str]:
        """用 GDAL OGR 将 SHP 转为 WGS84 经纬度 GeoJSON。

        与旧的 pyshp 实现相比：OGR 自动读取 .prj 中的坐标参考，输出统一重投影到
        EPSG:4326，避免投影坐标系（米制）的研究区被当作经纬度使用，导致数据检索
        范围与栅格化掩膜位置错位；属性字段随图层一起保留。
        """
        try:
            from osgeo import gdal
        except Exception as e:
            return False, f"GDAL 不可用: {e}"
        try:
            # gdal.OpenEx 返回 gdal.Dataset（GDALDatasetShadow），可直接传给
            # VectorTranslate；ogr.Open 返回的 ogr.DataSource 在部分 GDAL 绑定下不被接受
            src = gdal.OpenEx(shp_path, gdal.OF_VECTOR)
            if src is None:
                return False, "无法打开 SHP（缺少 .shp 或配套文件损坏）"
            try:
                if src.GetLayerCount() == 0:
                    return False, "SHP 不含任何图层"
                layer = src.GetLayer(0)
                if layer.GetFeatureCount() == 0:
                    return False, "SHP 图层不含要素"
                # 重传同名 .shp 时先删除旧 GeoJSON，避免 GeoJSON 驱动拒绝覆盖
                if os.path.exists(geojson_path):
                    os.remove(geojson_path)
                out = gdal.VectorTranslate(
                    geojson_path, src,
                    format="GeoJSON",
                    dstSRS="EPSG:4326",
                    layerCreationOptions=["RFC7946=YES"],
                )
                if out is None:
                    return False, "转换失败（GDAL 未生成输出）"
                out = None  # 释放输出数据集，确保文件写盘
            finally:
                src = None
            return True, ""
        except Exception as e:
            return False, str(e)

    def list_study_areas(self) -> list:
        if not self._study_dir().exists():
            return []
        files = sorted(self._study_dir().glob("*.geojson"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [f.name for f in files]

    # 当前研究区：以 study_areas 目录下的隐藏标记文件持久化（多用户目录天然隔离，
    # core/agent 层的 _find_study_area_file 直接读取该标记，无需跨层传递 settings）
    def _current_study_area_path(self) -> Path:
        return self._study_dir() / ".current.txt"

    def get_current_study_area(self) -> str:
        """返回当前选中的研究区文件名；未设置或文件已被删除时返回空串"""
        p = self._current_study_area_path()
        if p.exists():
            # basename 归一：防标记文件被篡改为绝对路径后逃逸到目录外探测
            name = os.path.basename((p.read_text(encoding="utf-8") or "").strip())
            if name and (self._study_dir() / name).is_file():
                return name
        return ""

    def set_current_study_area(self, name: str) -> bool:
        """设置当前研究区；文件不存在返回 False"""
        name = os.path.basename((name or "").strip())
        if not name or not (self._study_dir() / name).is_file():
            return False
        self._current_study_area_path().write_text(name, encoding="utf-8")
        return True

    def delete_study_area(self, name: str) -> str:
        """删除研究区文件（geojson 及同名配套 shp/dbf/shx/prj）；返回结果消息"""
        name = os.path.basename((name or "").strip())
        if not name:
            return "未指定研究区文件名"
        gj = self._study_dir() / name
        if not gj.is_file():
            return f"研究区文件不存在：{name}"
        stem = os.path.splitext(name)[0]
        removed = 0
        for ext in (".geojson", ".shp", ".dbf", ".shx", ".prj"):
            f = self._study_dir() / (stem + ext)
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # 删除的是当前选中项 → 清空 current 标记
        if self.get_current_study_area() == name:
            try:
                self._current_study_area_path().unlink()
            except OSError:
                pass
        return f"已删除 {name}" if removed else f"删除失败：{name}"

    # ── 工作流 / 精度 ──────────────────────────────────────────

    def get_workflow_status(self, cid: Optional[str]) -> List[dict]:
        """返回各 stage 状态；与固定 run_manifest.json 交叉核对（前端联动）。

        Agent 的内存态回调（wp/steps_map）反映"是否尝试执行过"，是乐观状态；
        run_manifest.json 由各 Skill 在产物通过校验后才写 completed，或在失败时
        写 failed 并把下游标记为 skipped_upstream。当 manifest 给出更明确的
        failed/skipped_upstream/completed 状态时，以 manifest 为准，避免把
        "失败后仍继续尝试下游"误显示成"完成"。不修改 Agent 本身的执行行为，
        只影响前端展示的状态来源。
        """
        if not cid:
            return [{"id": s, "label": _WORKFLOW_LABELS.get(s, s), "status": "pending"} for s in WORKFLOW_STEPS]
        state = self._get_conv_state(cid)
        wp = state["workflow_progress"]
        steps_map = {s["name"]: s["status"] for s in wp.get("steps", [])}

        manifest_stages: Dict[str, dict] = {}
        project_dir = self._get_project_dir(cid)
        if project_dir:
            try:
                # manifest 落在影像对独立目录（pairs/L{date}_S{date}），
                # 无配对时兼容旧的项目根布局
                manifest_root = self._latest_pair_root(project_dir)
                manifest_stages = run_manifest.load_manifest(manifest_root).get("stages", {})
            except Exception:
                manifest_stages = {}

        rows = []
        for s in WORKFLOW_STEPS:
            agent_status = steps_map.get(s, "pending") if wp.get("status") != "idle" else "pending"
            manifest_status = manifest_stages.get(s, {}).get("status")
            if manifest_status in (
                run_manifest.STATUS_FAILED,
                run_manifest.STATUS_SKIPPED_UPSTREAM,
                run_manifest.STATUS_COMPLETED,
            ):
                final_status = manifest_status
            else:
                final_status = agent_status
            # 结果后处理（可选）：主流程已跑完但未执行后处理 → 明确显示"未执行（可选）"
            if s == "postprocess" and final_status not in (
                run_manifest.STATUS_COMPLETED, run_manifest.STATUS_FAILED,
            ):
                main_done = (
                    steps_map.get("accuracy_eval") == "completed"
                    or manifest_stages.get("accuracy_eval", {}).get("status")
                    == run_manifest.STATUS_COMPLETED
                )
                if main_done:
                    final_status = "skipped"
            rows.append({"id": s, "label": _WORKFLOW_LABELS.get(s, s), "status": final_status})
        return rows

    def _read_eval_json(self, path: str) -> dict:
        """读取评估 JSON，区分 missing（未生成）/ error（存在但损坏）/ ok（正常），
        不把缺失或损坏都填成 0（缺数据不得呈现为零误差）。"""
        if not os.path.isfile(path):
            return {"status": "missing"}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"status": "ok", "data": json.load(f)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_accuracy_summary(self, cid: Optional[str]) -> dict:
        """返回测试集评估指标（test_metrics）与粗尺度闭合协议（coarse_constraint_closure）；
        闭合对照附带上已生成的填洞产物值域（无则 None）。"""
        empty = {"test_metrics": {"status": "missing"},
                 "coarse_constraint_closure": {"status": "missing"}}
        if not cid:
            return empty
        project_dir = self._get_project_dir(cid)
        if not project_dir:
            return empty
        # 评估结果位于影像对独立目录（取最近修改的一对）
        results_dir = os.path.join(self._latest_pair_root(project_dir), "results")
        test_metrics = self._read_eval_json(self._latest_test_metrics_path(results_dir))
        closure = self._read_eval_json(os.path.join(results_dir, "coarse_constraint_closure.json"))
        if closure.get("status") == "ok" and isinstance(closure.get("data"), dict):
            closure["data"]["filled_range"] = self._filled_product_range(results_dir)
        return {
            "test_metrics": test_metrics,
            "coarse_constraint_closure": closure,
        }

    @staticmethod
    def _latest_test_metrics_path(results_dir: str) -> str:
        """测试集评估结果 JSON（predict_test_set 输出，含 metrics/n_samples）。

        优先 results/test/（调优收尾 promote 后最佳轮副本），其次调优中途的
        tuning/*/test/，两者取 mtime 最新。
        """
        import glob
        cands = glob.glob(os.path.join(results_dir, "test", "rf_ttri_predict_run*.json"))
        cands += glob.glob(os.path.join(results_dir, "tuning", "*", "test", "rf_ttri_predict_run*.json"))
        return max(cands, key=os.path.getmtime) if cands else ""

    def _filled_product_range(self, results_dir: str) -> Optional[dict]:
        """读最近一份填洞产物（rf_10m_lst_final_filled_*.tif，排除掩膜）的有效像元
        温度范围；没有填洞产物时返回 None（前端据此隐藏「填补空洞后」行）。
        块级流式读取 + 按 (路径, mtime, size) 缓存，避免整幅读入内存、避免重复读栅格。
        """
        try:
            import glob
            cands = [p for p in glob.glob(os.path.join(results_dir, "rf_10m_lst_final_filled_*.tif"))
                     if "_cloud_mask" not in os.path.basename(p)]
            if not cands:
                return None
            path = max(cands, key=os.path.getmtime)
            key = (path, os.path.getmtime(path), os.path.getsize(path))
            cached = self._filled_range_cache.get(key)
            if cached is not None:
                return cached
            import numpy as np
            import rasterio
            with rasterio.open(path) as ds:
                nd = ds.nodata
                lo = hi = None
                for _, win in ds.block_windows(1):
                    blk = ds.read(1, window=win)
                    blk = blk.astype(np.float64)
                    ok = np.isfinite(blk)
                    # nodata 可能是 NaN（填充产品常见），finite 已排除；其余非 NaN
                    # nodata 值（如 -9999）需要显式排除，两者叠加过滤
                    if nd is not None and np.isfinite(float(nd)):
                        ok &= (blk != float(nd))
                    blk = blk[ok]
                    if blk.size == 0:
                        continue
                    bmin, bmax = float(blk.min()), float(blk.max())
                    lo = bmin if lo is None else min(lo, bmin)
                    hi = bmax if hi is None else max(hi, bmax)
            if lo is None or hi is None:
                return None
            result = {"min_K": lo, "max_K": hi}
            self._filled_range_cache[key] = result
            return result
        except Exception:
            return None

    # ── 地图 ──────────────────────────────────────────────────

    def _latest_pair_root(self, project_dir: str) -> str:
        """项目下最近修改的影像对独立目录（pairs/L{date}_S{date}）。

        无 pairs 目录或为空时返回项目根，兼容旧布局；取最近修改对保证
        工作流进度 / 精度 / 地图等只读接口始终对准用户当前正在处理的一对。
        """
        if not project_dir:
            return project_dir
        pairs_root = os.path.join(project_dir, "pairs")
        if os.path.isdir(pairs_root):
            try:
                pair_dirs = [os.path.join(pairs_root, d) for d in sorted(os.listdir(pairs_root))
                             if os.path.isdir(os.path.join(pairs_root, d))]
            except OSError:
                pair_dirs = []
            if pair_dirs:
                return max(pair_dirs, key=lambda p: os.path.getmtime(p))
        return project_dir

    def _get_project_dir(self, cid: Optional[str]) -> str:
        """解析对话对应项目目录；内存为空时从会话文件回填"""
        if not cid:
            return ""
        state = self._get_conv_state(cid)
        if not state.get("project_dir"):
            self.load_conversations()
            state = self._get_conv_state(cid)
        return state.get("project_dir", "")

    def build_map_html(self, cid: Optional[str]) -> str:
        project_dir = self._get_project_dir(cid)
        if not project_dir or not os.path.isdir(project_dir):
            return LayerVisualizer.build_empty_map()
        return LayerVisualizer.build_map(project_dir)

    def list_layers(self, cid: Optional[str]) -> List[dict]:
        project_dir = self._get_project_dir(cid)
        return LayerVisualizer.list_available_layers(project_dir)

    def render_layer_png(self, layer_id: str, cid: Optional[str]):
        """渲染单图层 PNG 字节 + WGS84 边界；不可用返回 None"""
        project_dir = self._get_project_dir(cid)
        return LayerVisualizer.render_layer_png(layer_id, project_dir)

    def render_layer_tile(self, layer_id: str, cid: Optional[str],
                          z: int, x: int, y: int):
        """渲染单图层 Web Mercator 瓦片 PNG；无数据返回 None（前端视为透明）

        并发限制：瓦片渲染要打开/解码大 GeoTIFF（如 1.95GB Sentinel），无限制时
        地图平移/缩放/切图层的风暴式请求会把 FastAPI 线程池占满，导致
        bootstrap/layers/files/current 等轻量请求排队超时——前端表现为
        "图层消失、下载列表为空"。
        用非阻塞信号量限制同时渲染的瓦片数（默认 3）：信号量满时瓦片立即返回空
        （前端视为透明），而不是排队等待——避免等待线程继续占用线程池。
        """
        project_dir = self._get_project_dir(cid)
        if not _TILE_RENDER_SEM.acquire(blocking=False):
            return None
        try:
            return LayerVisualizer.render_layer_tile(layer_id, project_dir, z, x, y)
        finally:
            _TILE_RENDER_SEM.release()

    # ── 文件列表 / 下载 ────────────────────────────────────────

    def get_sys_usage(self) -> dict:
        """本软件的实时资源占用（GB，不含百分比）——整个软件口径：

        内存：整个容器（cgroup 全部进程：服务 + 计算子进程）当前占用；
        容器外环境回退“本进程 + 子进程”RSS 求和；再回退本进程。
        磁盘：软件数据根（/app/data：台账/执行缓存/所有用户数据）总大小，
        多个候选根去重；统计较慢，15 秒内缓存复用。
        """
        now = time.time()
        cached = getattr(self, "_usage_cache", None)
        if cached and now - cached["ts"] < 15:
            return cached["data"]

        mem_gb = 0.0
        # 1) cgroup 内存（容器内即整个软件的所有进程）
        for path in ("/sys/fs/cgroup/memory.current",
                     "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    mem_gb = float(f.read().strip()) / (1024.0 ** 3)
                break
            except Exception:
                mem_gb = 0.0
        # 2) 回退：本进程 + 全部子进程 RSS 求和
        if mem_gb <= 0:
            try:
                import psutil
                total = psutil.Process().memory_info().rss
                for child in psutil.Process().children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except Exception:
                        continue
                mem_gb = total / (1024.0 ** 3)
            except Exception:
                mem_gb = 0.0
        # 3) 最后回退：本进程 VmRSS
        if mem_gb <= 0:
            try:
                with open("/proc/self/status", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_gb = float(line.split()[1]) / 1024.0 / 1024.0
                            break
            except Exception:
                pass

        disk_gb = 0.0
        try:
            # 软件数据根：容器内为 /app/data（台账 + 执行产物缓存 + 用户数据）；
            # 去重包含关系，避免重复统计同一目录树
            roots = []
            for cand in (Path("/app/data"), _ROOT / "data", self._workspace_root()):
                try:
                    c = cand.resolve()
                except Exception:
                    continue
                if not c.is_dir():
                    continue
                if any(str(c).startswith(str(p) + os.sep) or c == p
                       or str(p).startswith(str(c) + os.sep) for p in roots):
                    # 保留外层目录，跳过被包含的子目录
                    if any(c == p or str(c).startswith(str(p) + os.sep)
                           for p in roots):
                        continue
                roots = [p for p in roots
                         if not (str(p).startswith(str(c) + os.sep))]
                roots.append(c)
            total = 0
            seen_inodes = set()
            from stat import S_ISLNK
            for root in roots:
                for dirpath, _dirs, files in os.walk(root):
                    for name in files:
                        p = os.path.join(dirpath, name)
                        try:
                            lst = os.lstat(p)
                        except OSError:
                            continue
                        # 符号链接（产物桥接视图）不重复计目标；硬链接同 inode 只计一次
                        if S_ISLNK(lst.st_mode):
                            continue
                        key = (lst.st_dev, lst.st_ino)
                        if key in seen_inodes:
                            continue
                        seen_inodes.add(key)
                        # 优先用实际占块（st_blocks*512，与 du 同口径）；
                        # 无该字段时回退表观大小
                        blocks = getattr(lst, "st_blocks", None)
                        total += (int(blocks) * 512) if blocks else lst.st_size
            disk_gb = total / (1024.0 ** 3)
        except Exception:
            pass

        data = {"mem_gb": round(mem_gb, 2), "disk_gb": round(disk_gb, 2)}
        self._usage_cache = {"ts": now, "data": data}
        return data

    def list_project_files(self, project_dir: str) -> dict:
        project_dir = (project_dir or "").strip()
        # 归属校验：只能访问当前用户自己的项目目录（堵越权读取）
        if not self._owns_project_dir(project_dir):
            return {"ok": False, "message": "无权访问该目录", "files": []}
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "message": "目录不存在或未设置", "files": []}
        files = []
        for root, _dirs, names in os.walk(project_dir):
            for name in sorted(names):
                # 中间过程产物（会被阶段清理删除）不在下载面板展示
                if name in INTERMEDIATE_FILENAMES:
                    continue
                # 生成中的文件不展示 —— .partial 是原子写入的半成品，
                # 0 字节是刚创建还没写入内容，都要等文件生成完整后才出现在列表
                if name.endswith(".partial"):
                    continue
                full = os.path.join(root, name)
                try:
                    if os.path.isfile(full) and os.path.getsize(full) > 0:
                        rel = os.path.relpath(full, project_dir).replace("\\", "/")
                        files.append({"path": rel, "size": os.path.getsize(full), "name": name})
                except OSError:
                    continue
        files.sort(key=lambda x: x["path"].lower())
        return {"ok": True, "files": files}

    def resolve_download(self, project_dir: str, rel_path: str) -> Tuple[Optional[str], str]:
        """校验下载路径（防目录穿越 + 归属校验），返回可下载的绝对路径"""
        project_dir = (project_dir or "").strip()
        rel_path = (rel_path or "").strip()
        if not rel_path:
            return None, "未选择文件"
        # 归属校验：只能下载当前用户自己的项目文件（堵越权下载）
        if not self._owns_project_dir(project_dir):
            return None, "无权访问该目录"
        if not project_dir or not os.path.isdir(project_dir):
            return None, "项目目录不存在或未设置"
        base = os.path.realpath(project_dir)
        target = os.path.realpath(os.path.join(base, rel_path))
        if not (target == base or target.startswith(base + os.sep)):
            return None, "非法路径"
        # 中间过程产物不提供下载（与下载面板过滤规则一致）
        if os.path.basename(target) in INTERMEDIATE_FILENAMES:
            return None, "该文件为中间过程产物，不提供下载"
        # 生成中的文件不提供下载（与下载面板过滤规则一致）
        if os.path.basename(target).endswith(".partial"):
            return None, "该文件正在生成中，请稍后再试"
        if not os.path.isfile(target):
            return None, "文件不存在"
        if os.path.getsize(target) == 0:
            return None, "该文件正在生成中，请稍后再试"
        return target, ""

    # ── 测试 ──────────────────────────────────────────────────

    def test_planetary_connection(self) -> str:
        import time as _time
        import requests as _requests
        from pystac_client import Client
        from pystac_client.stac_api_io import StacApiIO

        _STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
        _bbox = [113.7, 29.9, 114.9, 31.3]
        lines = []
        try:
            t0 = _time.time()
            resp = _requests.get(_STAC_URL, headers={"Accept": "application/json"}, timeout=15)
            elapsed = _time.time() - t0
            if resp.status_code == 200:
                lines.append(f"✅ 1. STAC API 可达（HTTP 200，耗时 {elapsed:.1f}s）")
            else:
                lines.append(f"⚠️ 1. STAC API 返回 HTTP {resp.status_code}（耗时 {elapsed:.1f}s）")
                lines.append("\n---\n若第 1 步即失败，说明容器网络无法访问 planetarycomputer.microsoft.com。")
                return "\n".join(lines)
        except Exception as e:
            lines.append(f"❌ 1. STAC API 不可达：{e}")
            lines.append("\n---\n若第 1 步即失败，说明容器网络无法访问 planetarycomputer.microsoft.com。")
            return "\n".join(lines)
        try:
            catalog = Client.open(
                _STAC_URL,
                headers={"Accept": "application/json"},
                stac_io=StacApiIO(timeout=30, max_retries=1),
            )
            lines.append("✅ 2. STAC 目录打开成功")
        except Exception as e:
            lines.append(f"❌ 2. STAC 目录打开失败：{e}")
            return "\n".join(lines)
        try:
            items = list(catalog.search(
                collections=["landsat-c2-l2"],
                bbox=_bbox,
                datetime="2025-01-01/2026-07-31",
                query={"eo:cloud_cover": {"lt": 90}},
                max_items=5,
            ).items())
            lines.append(f"✅ 3. 影像搜索正常（找到 {len(items)} 景 Landsat 示例）")
        except Exception as e:
            lines.append(f"❌ 3. 影像搜索失败：{e}")
        lines.append("\n---\n若 1-3 全部通过：容器网络正常；若第 1 步失败：容器无法访问 Planetary Computer。")
        return "\n".join(lines)

    def test_cdse_connection(self) -> str:
        """Copernicus Data Space 连通性测试（测试页补齐 Copernicus）。

        与 test_planetary_connection 同口径：STAC API 可达性 + Sentinel-2 搜索。
        """
        import time as _time
        import requests as _requests
        from pystac_client import Client
        from pystac_client.stac_api_io import StacApiIO

        _DS_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
        _bbox = [113.7, 29.9, 114.9, 31.3]
        lines = []
        try:
            t0 = _time.time()
            resp = _requests.get(_DS_STAC_URL, headers={"Accept": "application/json"}, timeout=15)
            elapsed = _time.time() - t0
            if resp.status_code == 200:
                lines.append(f"✅ 1. STAC API 可达（HTTP 200，耗时 {elapsed:.1f}s）")
            else:
                lines.append(f"⚠️ 1. STAC API 返回 HTTP {resp.status_code}（耗时 {elapsed:.1f}s）")
                lines.append("\n---\n若第 1 步即失败，说明容器网络无法访问 stac.dataspace.copernicus.eu。")
                return "\n".join(lines)
        except Exception as e:
            lines.append(f"❌ 1. STAC API 不可达：{e}")
            lines.append("\n---\n若第 1 步即失败，说明容器网络无法访问 Copernicus Data Space。")
            return "\n".join(lines)
        try:
            catalog = Client.open(
                _DS_STAC_URL,
                headers={"Accept": "application/json"},
                stac_io=StacApiIO(timeout=30, max_retries=1),
            )
            lines.append("✅ 2. STAC 目录打开成功")
        except Exception as e:
            lines.append(f"❌ 2. STAC 目录打开失败：{e}")
            return "\n".join(lines)
        try:
            items = list(catalog.search(
                collections=["sentinel-2-l2a"],
                bbox=_bbox,
                datetime="2025-01-01/2026-07-31",
                query={"eo:cloud_cover": {"lt": 90}},
                max_items=5,
            ).items())
            lines.append(f"✅ 3. 影像搜索正常（找到 {len(items)} 景 Sentinel-2 示例）")
        except Exception as e:
            lines.append(f"❌ 3. 影像搜索失败：{e}")
        lines.append("\n---\n若 1-3 全部通过：容器网络正常；若第 1 步失败：容器无法访问 Copernicus Data Space。")
        return "\n".join(lines)

    def test_gdal_status(self) -> str:
        """GDAL 与投影链路自检（升级版）。

        不再只做"能 import"这类导入级检查：按 Server 实际使用顺序导入
        osgeo/rasterio/geopandas/pyogrio，对武汉/北京/南半球三个真实 bbox 做
        显式传统 GIS 轴序坐标变换（core.geo_transform，与实际下载路径同一套
        实现），并分别验证 rasterio/GeoPandas 的最小 CRS 读写；不再把"坐标轴序/
        投影契约错误"笼统归因为"GDAL 环境问题"。
        """
        lines = []
        try:
            from osgeo import gdal, osr
            ver = gdal.VersionInfo("RELEASE_NAME")
            lines.append(f"✅ 1. osgeo 导入成功（GDAL {ver}）")
        except Exception as e:
            lines.append(f"❌ 1. osgeo 导入失败：{e}")
            return "\n".join(lines)

        enable_gdal_osr_exceptions()

        # 2. 坐标轴序/投影链路：与 data_acquisition.py 实际下载路径同一实现，
        #    覆盖北半球、南半球，不只测两个对角点（四角加密取样）
        test_bboxes = {
            "武汉": [113.7, 29.9, 114.9, 31.3],
            "北京": [115.4, 39.4, 117.5, 41.1],
            "南半球示例(悉尼)": [150.5, -34.2, 151.5, -33.5],
        }
        for name, bbox in test_bboxes.items():
            try:
                utm_epsg = utm_epsg_for_lonlat((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
                x1, y1, x2, y2 = bbox_wgs84_to_utm_bounds(bbox, utm_epsg)
                lines.append(
                    f"✅ 2.{name}: 坐标轴序/投影链路正常 → UTM(EPSG:{utm_epsg}) "
                    f"=({x1:.1f},{y1:.1f})-({x2:.1f},{y2:.1f})"
                )
            except Exception as e:
                lines.append(f"❌ 2.{name}: 坐标轴序/投影链路失败：{e}（bbox={bbox}）")

        # 3. /vsimem 栅格创建/读写
        try:
            path = "/vsimem/gdal_selftest.tif"
            drv = gdal.GetDriverByName("GTiff")
            ds = drv.Create(path, 4, 4, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE", "PREDICTOR=3"])
            ds.SetGeoTransform([100, 30, 0, 200, 0, -30])
            ds.GetRasterBand(1).Fill(2.0)
            ds.FlushCache()
            ds = None
            ds2 = gdal.Open(path)
            arr = ds2.GetRasterBand(1).ReadAsArray()
            ok = arr is not None and float(arr.sum()) == 2.0 * 16
            ds2 = None
            gdal.Unlink(path)
            lines.append(f"✅ 3. /vsimem 栅格创建/读写正常（{'回读校验通过' if ok else '回读数值异常'}）")
        except Exception as e:
            lines.append(f"❌ 3. /vsimem 栅格创建/读写失败：{e}")

        # 4. gdal.Warp 重投影（与实际下载路径一致：WGS84 → UTM）
        try:
            src = "/vsimem/warp_src.tif"
            dst = "/vsimem/warp_dst.tif"
            d = gdal.GetDriverByName("GTiff").Create(src, 2, 2, 1, gdal.GDT_Float32)
            d.SetGeoTransform([114, 0.01, 0, 31, 0, -0.01])
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            d.SetProjection(srs.ExportToWkt())
            d.GetRasterBand(1).Fill(1.0)
            d.FlushCache()
            d = None
            gdal.Warp(dst, src, dstSRS="EPSG:32650")
            dout = gdal.Open(dst)
            if dout is None or dout.RasterXSize < 1 or dout.RasterYSize < 1:
                raise RuntimeError("Warp 输出为空")
            if not dout.GetProjection():
                raise RuntimeError("Warp 输出缺少投影信息")
            dout = None
            gdal.Unlink(src)
            gdal.Unlink(dst)
            lines.append("✅ 4. gdal.Warp 重投影正常（输出含 UTM 投影）")
        except Exception as e:
            lines.append(f"❌ 4. gdal.Warp 重投影失败：{e}")

        # 5. rasterio CRS 读写（Server 实际数据处理链路也会用到）
        try:
            import rasterio
            from rasterio.crs import CRS as RioCRS
            crs = RioCRS.from_epsg(4326)
            _ = crs.to_epsg()
            lines.append(f"✅ 5. rasterio 导入与 CRS 读写正常（rasterio {rasterio.__version__}）")
        except Exception as e:
            lines.append(f"❌ 5. rasterio CRS 读写失败：{e}")

        # 6. GeoPandas 最小 CRS 转换（用于研究区 Shapefile/GeoJSON 解析）
        try:
            import geopandas as gpd
            from shapely.geometry import Point
            import math as _math
            gdf = gpd.GeoDataFrame({"geometry": [Point(114.3, 30.59)]}, crs="EPSG:4326")
            gdf_utm = gdf.to_crs("EPSG:32650")
            gx, gy = gdf_utm.geometry.iloc[0].x, gdf_utm.geometry.iloc[0].y
            if _math.isfinite(gx) and _math.isfinite(gy):
                lines.append(f"✅ 6. GeoPandas CRS 转换正常（({gx:.1f},{gy:.1f})）")
            else:
                lines.append(f"❌ 6. GeoPandas CRS 转换返回非有限值（({gx},{gy})）")
        except Exception as e:
            lines.append(f"❌ 6. GeoPandas CRS 转换失败：{e}")

        # 7. pyogrio（geopandas 常用的矢量 IO 引擎，不可用不阻断，只提示）
        try:
            import pyogrio
            lines.append(f"✅ 7. pyogrio 导入成功（{pyogrio.__version__}）")
        except Exception as e:
            lines.append(f"⚠️ 7. pyogrio 不可用（不阻断，GeoPandas 可能退回 fiona）：{e}")

        _failed = sum(1 for _l in lines if _l.startswith("❌"))
        if _failed:
            lines.append(
                f"\n---\n共 {_failed} 项失败：GDAL 与投影链路存在问题，"
                f"请看上方具体失败项的错误类型与坐标，不要笼统归因为'GDAL 环境问题'。"
            )
        else:
            lines.append("\n---\n1-7 全部通过（或仅 pyogrio 提示）：GDAL 与投影链路正常。")
        return "\n".join(lines)

    # ── 聊天流式（线程 + 队列，复刻旧版语义） ─────────────────

    def chat_start(self, pid: str, cid: str, user_msg: str, exec_mode: str = "", chat_mode: str = "", request_id: str = "") -> dict:
        user_msg = (user_msg or "").strip()
        if not user_msg:
            return {"ok": False, "message": "消息不能为空"}

        # ── 状态内核（升级第一阶段）：命令先持久化再处理 ──
        # 同一请求编号 + 相同载荷只生效一次（去重）；同键不同载荷报冲突。
        # 事务提交后立即回执，不等任务完成（总体方案 §3.4「接收消息」）。
        # 去重检查必须先于线程占用检查：即使首个请求的任务线程仍在执行，
        # 重复请求也应命中幂等返回，而不是被“已有任务在执行中”误拦
        # （9.1 验收第 2 条：同一请求发两次，台账里只记一次）。
        receipt = None
        try:
            receipt = receive_command(
                self._get_state_store(),
                user_id=self._uid(),
                project_id=pid,
                conversation_id=cid,
                message=user_msg,
                dedup_key=(request_id or "").strip(),
                operation_type="chat.command",
                payload={"exec_mode": exec_mode, "chat_mode": chat_mode},
            )
        except CommandConflictError as e:
            return {"ok": False, "message": f"命令冲突：{e}"}
        except Exception as e:
            return {"ok": False, "message": f"命令尚未持久接收，请稍后重试：{e}"}
        if receipt is not None and receipt.duplicate:
            # 重复请求：幂等命中，不再启动第二个执行线程
            return {"ok": True, "duplicate": True,
                    "command_id": receipt.command_id,
                    "messages": self.load_conversations().get(pid, {}).get(cid, {}).get("messages", [])}
        command_id = receipt.command_id if receipt is not None else None
        message_id = receipt.message_id if receipt is not None else ""
        conv_pk = receipt.conversation_id if receipt is not None else ""

        # 同一对话已有任务线程在执行/挂起时拒绝重复启动（前端有 streaming 保护，
        # API 直调无防护；双线程会互相 set/reset pause 事件造成串扰）。
        # 命令已经落库，被拒也要回写终态，不能永久停在 received（§3.4 处理闭环）。
        def _reject(message: str) -> dict:
            if command_id:
                try:
                    mark_command_rejected(self._get_state_store(), command_id,
                                          message)
                except Exception as ke:
                    print(f"[state_kernel] 命令拒绝态回写异常：{ke}")
            return {"ok": False, "message": message}

        prev = self._agent_threads.get(cid)
        if prev is not None and prev.is_alive():
            return _reject("该对话已有任务在执行中，请等待当前任务完成")
        convs = self.load_conversations()
        if pid not in convs or cid not in convs[pid]:
            return _reject("对话不存在，请先选择对话")

        history = convs[pid][cid].get("messages", [])
        history = history + [{"role": "user", "content": user_msg}]

        assistant = self._assistant_for()
        if not assistant.api_key and not assistant.api_base_url:
            history.append({"role": "assistant", "content": "⚠️ 请先在右侧「🔑 API 设置」配置模型。"})
            self._save_history(pid, cid, history)
            # 不启动任务线程：命令同步完成，回写终态避免停留在 received
            if command_id:
                try:
                    mark_command_processed(self._get_state_store(), command_id,
                                           {"ok": True, "note": "no_api_key"})
                except Exception as ke:
                    print(f"[state_kernel] 命令完成态回写异常：{ke}")
            return {"ok": True, "messages": history}

        conv_state = self._get_conv_state(cid)
        project_dir = conv_state["project_dir"]

        agent_cfg = self._agent_settings()
        roles_enabled = agent_cfg["roles_enabled"]
        # 理解层只在 Work 模式 + 角色路径下接管；Chat 模式从入口就不允许
        # 创建生产任务（§4.5），不是靠提示词约束
        understanding_on = chat_mode != "chat" and bool(conv_pk)

        # 执行模式：本次请求 > 会话已记录 > settings 默认值
        resolved_mode = normalize_exec_mode(
            exec_mode,
            normalize_exec_mode(conv_state.get("exec_mode"), agent_cfg["default_exec_mode"]),
        )
        conv_state["exec_mode"] = resolved_mode
        try:
            self._update_conversation_file(cid, pid, exec_mode=resolved_mode)
        except Exception:
            pass

        # 工作流类命令前置检查。
        # 角色化开启后交给规划 Agent 以对话方式引导，这里不再硬拦截。
        if (not roles_enabled) and _is_workflow_command(user_msg) and _is_agent_command(user_msg):
            uploaded = self.list_study_areas()
            if not uploaded:
                history.append({"role": "assistant",
                                "content": "⚠️ 请先上传研究区文件（Shapefile 或 GeoJSON），然后再发送指令。"})
                self._save_history(pid, cid, history)
                if command_id:
                    try:
                        mark_command_processed(self._get_state_store(), command_id,
                                               {"ok": True, "note": "no_study_area"})
                    except Exception as ke:
                        print(f"[state_kernel] 命令完成态回写异常：{ke}")
                return {"ok": True, "messages": history}
            if not project_dir or not os.path.isdir(project_dir):
                history.append({"role": "assistant",
                                "content": "⚠️ 请先设置项目保存路径，然后再执行一键全流程。"})
                self._save_history(pid, cid, history)
                if command_id:
                    try:
                        mark_command_processed(self._get_state_store(), command_id,
                                               {"ok": True, "note": "no_project_dir"})
                    except Exception as ke:
                        print(f"[state_kernel] 命令完成态回写异常：{ke}")
                return {"ok": True, "messages": history}

        # 追加占位 assistant 气泡（空内容，不显示黑竖线；生成中由前端打字光标指示）
        history = history + [{"role": "assistant", "content": ""}]
        self._save_history(pid, cid, history)

        # 创建流式队列 + 后台线程
        q: "queue.Queue" = queue.Queue()
        self._stream_queues[cid] = q
        # 同一对话多轮执行的日志不断追加（不重置），避免后一轮覆盖前一轮日志
        self._stream_logs.setdefault(cid, [])
        # 新一轮开始：重置思考链 / 思考用时 / 正文累积。_stream_thinking 只在删除
        # 对话时清空，若不在此重置，换用无思考能力的模型时（新一轮没有 thinking
        # 事件覆盖），会把上一轮（思考模型时）的思考内容串进本轮气泡；content 同步
        # 重置保证本轮从头累积，断线重连由 token 事件/收尾兜底补齐。
        self._stream_thinking[cid] = ""
        self._stream_thinking_seconds[cid] = 0.0
        self._stream_content[cid] = ""
        pause_event = threading.Event()
        self._pause_events[cid] = pause_event

        prior_messages = []
        for m in history[:-2]:
            raw = m.get("content", "")
            if isinstance(raw, list):
                raw = "\n".join(str(x) for x in raw)
            c = strip_thinking(raw)
            if c:
                prior_messages.append({"role": m.get("role", "user"), "content": c})

        uid = self._uid()

        def _runner():
            ctx_token = _uid_ctx.set(uid)  # 后台线程不带请求 contextvars，需显式恢复用户
            # 记录最后一次 token 全文：收尾兑底直接用它，绝不 get_nowait 弹队列
            # （避免与活跃 SSE 生成器竞争事件，抢走最后一条 token 后生成器本地
            # accumulated 落后，done 推给前端的是半截气泡）。所有分支共用。
            tail_token = [""]
            runner_failed = [False]

            def _put_token(content: str):
                tail_token[0] = content
                q.put(("token", content))

            def _emit_log(text: str):
                """旧执行链/理解层日志：持久化（跨刷新不丢）+ 随队列推送。"""
                lid = self._append_log_line(uid, conv_pk, text)
                q.put(("log", {"text": str(text), "id": lid, "task_id": ""}))

            try:
                # Chat 模式 = 只读对话。禁止执行任何 Skill / 工作流、
                # 禁止生成或修改文件，Agent 只能基于现有信息回答问题。
                if chat_mode == "chat":
                    context = {
                        "workflow_status": conv_state["workflow_progress"],
                        "config": self._load_settings(),
                        "study_areas": self.list_study_areas(),  # 已上传研究区文件名
                        "mode_hint": (
                            "当前为只读 Chat 模式：你不能执行任何降尺度工作流、"
                            "不能下载/生成/修改任何文件，只能基于已有资料回答用户问题。\n"
                            "回答规范（严格遵守）：\n"
                            "1. 提示用户执行生成任务时，切换目标的叫法必须是「Work 模式」，"
                            "严禁叫「工作流执行模式」或其他叫法；Work 模式才能执行降尺度工作流、"
                            "下载数据与生成文件。\n"
                            "2. 若已上传研究区文件中，有文件名包含的城市与用户咨询的城市一致，"
                            "说明研究区边界已就绪，不要在后续建议中索要「研究区边界/范围/行政边界」。\n"
                            "3. 询问影像时间范围时具体到月份即可（如 2023 年 7 月），不要出现「某天」。\n"
                            "4. 生成产品的引导语建议为：若你需要实际生成<城市>的产品，"
                            "请切换到 Work 模式，并提供以下信息，我可以协助你完成配置并生成产品。"
                        ),
                    }
                    with self._scheduler.model_request():
                        self._assistant_for().ask_stream(
                            user_msg, _put_token,
                            context=context, prior_messages=prior_messages,
                            on_thinking=lambda t: q.put(("thinking", t)),
                        )
                    q.put(("done", None))
                elif understanding_on:
                    # 自由问答（带台账上下文）：查询句与“只回答”共用。
                    def _free_answer() -> str:
                        parts: list = []
                        context = {
                            "workflow_status": conv_state["workflow_progress"],
                            "config": self._load_settings(),
                            "study_areas": self.list_study_areas(),
                        }
                        # 注入台账进展（多角色应知道流程状态）：任务进度/
                        # 当前节点/等待与失败原因 + 已选配对 + 待答问题
                        try:
                            context.update(self.kernel_qa_context(pid, cid))
                        except Exception:
                            pass
                        with self._scheduler.model_request():
                            self._assistant_for().ask_stream(
                                user_msg,
                                lambda t: (_put_token(t), parts.append(t)),
                                context=context, prior_messages=prior_messages,
                                on_thinking=lambda t: q.put(("thinking", t)),
                            )
                        # on_token 回调收到的是累计全文：取最后一次而非拼接（
                        # 拼接会把每次快照首尾相连，导致气泡复读）
                        return parts[-1] if parts else ""

                    # 纯查询句直通问答通道（用户实测：查询式问句会让理解
                    # 模型输出失控——无法解析 JSON 或复读；它本质不含任务
                    # 操作，直接交给带台账上下文的自由问答）
                    if _is_query_only(user_msg):
                        _free_answer()
                        q.put(("tasks", self.session_snapshot(pid, cid)))
                        q.put(("done", None))
                        return
                    # 执行审批只消费已保存问题。无法匹配的文字仍交给理解层。
                    waiting = self.session_snapshot(pid, cid).get("questions", [])
                    if len(waiting) == 1:
                        try:
                            applied = self._scheduler.answer(waiting[0]["id"], user_msg, uid, conv_pk)
                        except ValueError:
                            applied = None
                        if applied:
                            _put_token("答案已接收，任务从对应节点继续。"
                                       if self._user_lang(uid) == "zh" else
                                       "Answer received; the task will continue from the corresponding node.")
                            q.put(("done", None))
                            return
                    outcome = self._understand(
                        pid, cid, user_msg, chat_mode="work", tz_offset=_DEFAULT_TZ_OFFSET,
                        command_id=command_id or "", message_id=message_id,
                        conv_pk=conv_pk, prior_messages=prior_messages,
                        on_log=_emit_log)
                    self._scheduler.notify()
                    if outcome.ready_tasks:
                        submitted = self._enqueue_ready(outcome.ready_tasks, resolved_mode)
                        _put_token(
                            (f"已提交 {len(submitted)} 个任务，按各自节点与资源额度执行。"
                             if self._user_lang(uid) == "zh" else
                             f"{len(submitted)} task(s) submitted; each runs under its own node and resource quotas.")
                            + ("\n" + outcome.message if outcome.message else ""))
                    else:
                        reply = outcome.message or ""
                        # 第六阶段修正（§4.5「Work 中的知识问答不启动 Skill」）：
                        # 判定为「只回答」且无台账动作时，由真模型生成自然语言
                        # 回答；理解失败且疑似查询句时同样回退问答（双保险）
                        fallback = (
                            outcome.kind == "reply_only"
                            or (outcome.kind == "failed"
                                and user_msg.rstrip().endswith(("？", "?"))))
                        if fallback and not reply:
                            reply = _free_answer() or reply
                        _put_token(reply or "已记录你的说明。")
                    q.put(("tasks", self.session_snapshot(pid, cid)))
                    q.put(("done", None))
                else:
                    context = {
                        "workflow_status": conv_state["workflow_progress"],
                        "config": self._load_settings(),
                    }
                    with self._scheduler.model_request():
                        self._assistant_for().ask_stream(
                            user_msg, _put_token,
                            context=context, prior_messages=prior_messages,
                            on_thinking=lambda t: q.put(("thinking", t)),
                        )
                    q.put(("done", None))
            except Exception as e:
                runner_failed[0] = True
                q.put(("error", str(e)))
                if command_id:
                    try:
                        mark_command_failed(self._get_state_store(), command_id, str(e))
                    except Exception as ke:
                        print(f"[state_kernel] 命令失败态回写异常：{ke}")
            finally:
                _uid_ctx.reset(ctx_token)
                # 兜底持久化流尾部：长流程中（如空洞填补）SSE 生成器可能已断线/
                # 被接管，最后一次 token（气泡全文，含完成提示）没被消费，气泡会
                # 停在半截。收尾时把最后一次 token 内容补进流状态并落盘，断线后
                # 重进对话/刷新页面都能恢复到完整气泡。直接读 on_token 记录的
                # tail_token，不 get_nowait 弹队列（避免与活跃 SSE 生成器竞争）。
                try:
                    _tail_content = tail_token[0]
                    if _tail_content:
                        self._stream_content[cid] = _tail_content
                        if pid:
                            self._persist_stream_content(
                                pid, cid,
                                format_bubble("", _tail_content, streaming=False))
                except Exception:
                    pass
                # 任务线程结束 = 本轮流结束：统一在这里清理临时流状态。
                # 断线/刷新场景下 SSE 生成器可能已被 Starlette 取消（无法自清理），
                # 若由生成器清理会把队列误删，导致刷新后重连 active=False、
                # 气泡光标消失且不再更新；content/thinking/logs 保留供重连补齐
                self._stream_queues.pop(cid, None)
                self._pause_events.pop(cid, None)
                self._pause_responses.pop(cid, None)
                self._agent_threads.pop(cid, None)
                self._stream_starts.pop(cid, None)
                self._stream_gen.pop(cid, None)
                # 本轮流结束后归还进程空闲堆，让日志区内存读数回落
                #（不影响仍在用的对象，只释放已 free 的 arena 页）
                # 同时清瓦片 lru_cache + GDAL 全局缓存（executor 退出路径
                # 已清一次，此处是线程结束时的最终安全网）
                try:
                    from core.visualization import LayerVisualizer
                    LayerVisualizer._render_tile_cached.cache_clear()
                except Exception:
                    pass
                try:
                    from osgeo import gdal
                    gdal.SetCacheMax(0)
                    gdal.SetCacheMax(256 * 1024 * 1024)
                except Exception:
                    pass
                # 命令处理闭环（升级第一阶段）：成功/纯对话 → processed；
                # 失败已在 except 分支回写 failed，避免命令永久停在 received。
                if command_id and not runner_failed[0]:
                    try:
                        mark_command_processed(
                            self._get_state_store(), command_id,
                            {"ok": True, "tail_chars": len(tail_token[0])},
                        )
                    except Exception as ke:
                        print(f"[state_kernel] 命令完成态回写异常：{ke}")
                release_rss_memory()

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        self._agent_threads[cid] = thread
        self._stream_starts[cid] = time.time()
        return {"ok": True, "messages": history}

    def chat_stream(self, cid: str, tz: float = _DEFAULT_TZ_OFFSET):
        """SSE 生成器：消费流式队列，输出事件流

        代际号接管：若上一个 SSE 连接已断开（如代理空闲超时）但服务端未感知
        （半开连接），新连接会递增代际号；旧生成器在下次读取队列时发现代际
        不符，把事件还给队列后自行退出，避免旧连接长期占用导致气泡冻结。

        tz：用户本地时区相对 UTC 的偏移小时数，用于日志行时间戳盖戳。
        """
        thread = self._agent_threads.get(cid)
        q = self._stream_queues.get(cid)
        pause_event = self._pause_events.get(cid)
        if not thread or not q or not pause_event:
            # 流已结束（含断线后重连）：把已累积内容一次性交付，避免气泡停在半截。
            # 内存态可能因服务重启被清空 → 回退读对话历史里节流落盘的备份；
            # 两者都没有（执行在产出前就中断）→ 明确告知用户，而不是静默空气泡
            saved = self._stream_content.get(cid, "")
            thinking = self._stream_thinking.get(cid, "")
            thinking_seconds = self._stream_thinking_seconds.get(cid, 0.0)
            if not saved:
                saved, thinking, thinking_seconds = self._last_stream_content(cid)
            if saved:
                yield ("event: done\ndata: " + json.dumps(
                    {"content": saved, "thinking": thinking,
                     "thinking_seconds": thinking_seconds},
                    ensure_ascii=False) + "\n\n")
            else:
                yield ("event: done\ndata: " + json.dumps(
                    {"content": "> 本次执行在过程中断（服务重启或连接中断），"
                                "已生成的部分结果可在工作面板查看；如需完整结果请重新发起执行。",
                     "thinking": ""}, ensure_ascii=False) + "\n\n")
            return
        # 重连接管（刷新/切回）：若任务正暂停等待审批或选影像，把待处理载荷
        # 重放进队列，让新连接重新收到 pause 事件，前端恢复暂停弹窗（不丢暂停态）
        conv_state = self._get_conv_state(cid)
        if thread.is_alive() and not pause_event.is_set():
            pp = conv_state.get("pending_pairs")
            if pp:
                q.put(("pause", {"pairs": pp}))
            pa = conv_state.get("pending_approval")
            if pa:
                q.put(("pause", pa))
        gen = self._stream_gen.get(cid, 0) + 1
        self._stream_gen[cid] = gen
        yield from self._stream_events(cid, thread, q, pause_event, gen, tz)

    def _stream_events(self, cid, thread, q, pause_event, gen, tz: float = _DEFAULT_TZ_OFFSET):
        convs = self.load_conversations()
        # 找到 pid/cid
        pid = None
        for p, items in convs.items():
            if cid in items:
                pid = p
                break
        accumulated = self._stream_content.get(cid, "")
        thinking = self._stream_thinking.get(cid, "")
        start_time = self._stream_starts.get(cid, time.time())
        thinking_started = None  # 思考开始时刻，用于计算思考用时
        thinking_seconds = self._stream_thinking_seconds.get(cid, 0.0)
        last_emit = time.time()
        last_persist = 0.0  # 流内容节流落盘时间点（服务重启/断线后不丢气泡）
        stream_truncated = False  # 防刷屏熔断：正文触发后不再接受后续 token
        thinking_truncated = False  # 思考链独立熔断

        def _emit(event: str, data: Any):
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def _stale():
            # 有更新的连接接管本对话流时，本生成器应退出
            return self._stream_gen.get(cid, 0) != gen

        saved_normally = False
        try:
            while True:
                # 用 get_nowait + time.sleep 轮询队列，而不是同步阻塞的 q.get(timeout=5)：
                # q.get 的 5 秒超时会让 SSE 生成器线程长时间占住线程池，且事件返回不及时；
                # 改为短轮询后 SSE 事件即时转发、线程及时让出。
                try:
                    event_type, data = q.get_nowait()
                except queue.Empty:
                    if _stale():
                        break
                    if not thread.is_alive():
                        break
                    # 心跳：用「命名事件帧」而非 SSE 注释行——部分代理/网关
                    # （如 ModelScope Studio）会丢弃注释行 keepalive，长流程
                    # （空洞填补/训练/下载）期间无有效事件会被掐断连接，导致
                    # 气泡停在半截、完成后无提示。命名事件前端未注册监听会自动
                    # 忽略（不会触发 onmessage），但对代理是真实数据帧。
                    if time.time() - last_emit >= 10:
                        yield "event: heartbeat\ndata: {}\n\n"
                        last_emit = time.time()
                    time.sleep(0.2)
                    continue
                # 已被新连接接管：把事件还给队列，让新连接继续处理
                if _stale():
                    q.put((event_type, data))
                    break
                last_emit = time.time()

                if event_type == "thinking":
                    thinking = data
                    # 思考链同样防复读/超长（用户实测：思考链刷屏）
                    if not thinking_truncated:
                        guarded = _guard_stream_text(str(thinking))
                        if guarded != thinking:
                            thinking_truncated = True
                        thinking = guarded
                    self._stream_thinking[cid] = thinking
                    if thinking_started is None:
                        # 新一轮思考开始（含决策后 resume 的后续反思），启动计时
                        thinking_started = time.time()
                    # 独立 thinking 事件实时推送思考增量（实时显示），
                    # content 事件只承载正文，思考链由前端独立折叠块渲染
                    yield from _emit("thinking", {"thinking": thinking})
                elif event_type == "token":
                    # 第一个正文 token 到达 → 思考结束，结算本次思考用时
                    if thinking_started is not None:
                        thinking_seconds = round(time.time() - thinking_started, 1)
                        thinking_started = None  # 本轮思考结束；下次 thinking 重新计时
                        self._stream_thinking_seconds[cid] = thinking_seconds
                    accumulated = data
                    # 防刷屏熔断：模型异常复读/超长输出时截断（用户实测：
                    # 同一段说明复读数百遍，气泡刷屏）
                    if not stream_truncated:
                        guarded = _guard_stream_text(str(accumulated))
                        if guarded != accumulated:
                            stream_truncated = True
                        accumulated = guarded
                    self._stream_content[cid] = accumulated
                    if pid and time.time() - last_persist >= 8:
                        self._persist_stream_content(
                            pid, cid, format_bubble("", accumulated, streaming=False),
                            thinking, thinking_seconds)
                        last_persist = time.time()
                    # token 事件也带上思考用时：正文开始后（思考已结束）就展示「用时」，
                    # 避免流程中断/长流程未到 done 时前端一直看不到思考时间
                    yield from _emit("token", {"content": format_bubble("", accumulated, streaming=True),
                                               "thinking_seconds": thinking_seconds})
                elif event_type == "append":
                    if thinking_started is not None:
                        thinking_seconds = round(time.time() - thinking_started, 1)
                        thinking_started = None
                        self._stream_thinking_seconds[cid] = thinking_seconds
                    accumulated += data
                    self._stream_content[cid] = accumulated
                    if pid and time.time() - last_persist >= 8:
                        self._persist_stream_content(
                            pid, cid, format_bubble("", accumulated, streaming=False),
                            thinking, thinking_seconds)
                        last_persist = time.time()
                    yield from _emit("token", {"content": format_bubble("", accumulated, streaming=True),
                                               "thinking_seconds": thinking_seconds})
                elif event_type == "pause":
                    # 暂停（如由我批准模式 plan_confirm/选影像处）前先结算思考用时：
                    # 规划思考后若直接暂停（正文还没开始），不结算的话 pause 携带的
                    # 用时恒为 0，前端「思考过程」块就不显示思考时间。
                    if thinking_started is not None:
                        thinking_seconds = round(time.time() - thinking_started, 1)
                        thinking_started = None
                        self._stream_thinking_seconds[cid] = thinking_seconds
                    payload = data if isinstance(data, dict) else {}
                    pairs = payload.get("pairs", [])
                    if pairs:
                        # 保存待选配对，供 chat_resume 根据用户选择索引恢复
                        self._get_conv_state(cid)["pending_pairs"] = pairs
                        # pause 时同步带上思考用时：由我批准模式在
                        # plan_confirm / 选影像处暂停，done 事件不会走到，用时须在此送达
                        yield from _emit("pause", {"pairs": pairs, "thinking_seconds": thinking_seconds})
                        return
                    if payload.get("type") == "approval":
                        # 通用审批节点：保存待处理载荷供 chat_resume 校验
                        self._get_conv_state(cid)["pending_approval"] = payload
                        yield from _emit("pause", {"approval": payload, "thinking_seconds": thinking_seconds})
                        return
                    # 既无 pairs 也非 approval：置空已选响应并唤醒等待线程，
                    # 由 pause_callback 侧按"无选择"处理（超时挂起语义）
                    self._pause_responses[cid] = None
                    pause_event.set()
                elif event_type == "workflow":
                    yield from _emit("workflow", {"steps": self.get_workflow_status(cid)})
                elif event_type == "tasks":
                    # 理解层任务卡快照（升级第二阶段）：一条消息可能产生多张卡，
                    # 前端未监听该事件时自动忽略，不影响既有渲染
                    yield from _emit("tasks", data if isinstance(data, dict) else {})
                elif event_type == "log":
                    # 日志行：兼容字符串与带行号对象（{id, task_id, text}）；
                    # 统一加时间戳（按用户本地时区）并过滤 emoji；累积供刷新/重连恢复
                    payload_log = data if isinstance(data, dict) else {"text": str(data or "")}
                    raw_text = str(payload_log.get("text") or "")
                    if raw_text.strip():
                        from core.scheduling.scheduler import strip_emoji
                        entry = {"id": int(payload_log.get("id") or 0),
                                 "task_id": str(payload_log.get("task_id") or ""),
                                 "text": _stamp_log_lines(strip_emoji(raw_text), tz)}
                        logs = self._stream_logs.get(cid)
                        if logs is None:
                            logs = self._stream_logs[cid] = []
                        logs.append(entry)
                        if len(logs) > 20000:
                            del logs[: len(logs) - 20000]
                        yield from _emit("log", entry)
                elif event_type == "done":
                    break
                elif event_type == "error":
                    from core.scheduling.scheduler import strip_emoji
                    accumulated += f"\n\n执行出错：{strip_emoji(str(data))}"
                    yield from _emit("done", {"content": format_bubble("", accumulated, streaming=False)})
                    return

            elapsed = time.time() - start_time
            # done 用服务端权威累积内容组装：_stream_content 由 token 处理和收尾
            # 兜底同步更新到最新全文，生成器本地 accumulated 可能因队列竞争落后
            #（旧实现用 accumulated 会把半截气泡推给前端并终结流）。断线重连时
            # chat_stream 恢复路径也已用 _stream_content 补齐，二者口径一致。
            authoritative = self._stream_content.get(cid, "") or accumulated
            final = format_bubble("", authoritative, streaming=False, elapsed=elapsed)
            yield from _emit("done", {"content": final, "thinking": thinking, "thinking_seconds": thinking_seconds})
            if pid:
                convs = self.load_conversations()
                if cid in convs.get(pid, {}):
                    last = convs[pid][cid]["messages"][-1]
                    last["content"] = final
                    # 思考链独立字段持久化，供重新进入对话时前端渲染折叠块
                    if thinking:
                        last["thinking"] = thinking
                    if thinking_seconds:
                        last["thinking_seconds"] = thinking_seconds
                    self._save_history(pid, cid, convs[pid][cid]["messages"])
                    saved_normally = True
        except Exception as e:
            yield from _emit("error", {"message": str(e)})
        finally:
            # 异常结束（断线/报错/被接管等）时兜底持久化已累积内容，
            # 保证重新进入该对话时气泡不会因没走完 done 而消失。
            # 落盘用服务端权威内容（_stream_content 恒为最新全文），
            # 避免生成器本地 accumulated 落后时把完整气泡覆盖回半截。
            if not saved_normally and pid:
                persist_content = self._stream_content.get(cid, "") or accumulated
                if persist_content:
                    try:
                        convs = self.load_conversations()
                        if cid in convs.get(pid, {}):
                            msgs = convs[pid][cid].get("messages", [])
                            if msgs and msgs[-1].get("role") == "assistant":
                                msgs[-1]["content"] = format_bubble("", persist_content, streaming=False)
                                if thinking:
                                    msgs[-1]["thinking"] = thinking
                                if thinking_seconds:
                                    msgs[-1]["thinking_seconds"] = thinking_seconds
                                self._save_history(pid, cid, msgs)
                    except Exception:
                        pass
            # 流状态清理统一由 _runner 线程结束时的 finally 兜底执行，这里不再 pop：
            # 断线/刷新时旧生成器可能被 Starlette 取消，若在此 pop 队列会让刷新后
            # 的重连误判 active=False，导致气泡光标消失、不再更新
            # （_stream_content / _stream_thinking / _stream_logs 保留供重连补齐）

    def chat_resume(self, cid: str, payload: dict) -> dict:
        """恢复被暂停的流，支持两种协议。

        旧：`{"pair_index": 0}`（配对选择，逻辑保持不变）
        新：`{"option_id": "manual_tune", "values": {...}}`（通用审批节点）
        """
        if cid not in self._stream_queues or cid not in self._pause_events:
            return {"ok": False, "message": "没有待恢复的流"}
        conv_state = self._get_conv_state(cid)
        payload = payload if isinstance(payload, dict) else {}

        if "option_id" in payload:
            pending = conv_state.get("pending_approval")
            if not pending:
                return {"ok": False, "message": "没有待处理的选择，请重新发送指令"}
            parsed, err = approval_proto.parse_resume(pending, payload)
            if parsed is None:
                return {"ok": False, "message": err}
            self._pause_responses[cid] = parsed
            conv_state.pop("pending_approval", None)
        else:
            pairs = conv_state.get("pending_pairs", [])
            if not pairs:
                return {"ok": False, "message": "没有待选配对，请重新发送指令"}
            try:
                idx = int(payload.get("pair_index")) if payload.get("pair_index") is not None else 0
            except (ValueError, TypeError):
                idx = 0
            idx = max(0, min(idx, len(pairs) - 1))
            self._pause_responses[cid] = pairs[idx]
            conv_state.pop("pending_pairs", None)

        self._pause_events[cid].set()
        return {"ok": True}


# ── FastAPI 应用 ───────────────────────────────────────────────

# 瓦片渲染并发信号量：限制同时渲染的瓦片数量，防止大 GeoTIFF 的瓦片渲染风暴
# 占满 FastAPI 线程池、饿死其他 API 请求（见 render_layer_tile）。
# 后端已有 lru_cache（同瓦片重复请求直接命中），并发从 3 提高到 8，
# 显著加速首次平移/缩放时的冷瓦片加载。
_TILE_RENDER_SEM = threading.BoundedSemaphore(8)

# 登录失败限速（内存态，进程重启清零）
_LOGIN_MAX_FAIL = 5
_LOGIN_COOLDOWN_SEC = 60
_login_failures: dict = {}

# 研究区上传大小上限（200MB，防磁盘耗尽）
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

backend = None

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global backend
    backend = AppBackend()
    backend.start_scheduler()
    try:
        yield
    finally:
        backend._scheduler.close()
        backend._get_state_store().close()
        backend = None


app = FastAPI(title="GeoThermoAI Vue3 后端", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 鉴权中间件：/api/* 除白名单外必须携带有效 JWT ──────────────
# token 来源：Authorization: Bearer <token>，或 SSE/图片等无法带 Header 时用 ?token=
_AUTH_WHITELIST = {"/api/auth/login", "/api/auth/register", "/api/health", "/api/debug/mem"}


@app.middleware("http")
async def auth_guard(request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_WHITELIST:
        token = ""
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
        if not token:
            token = request.query_params.get("token", "")
        payload = auth.decode_token(token) if token else None
        if not payload or not payload.get("sub"):
            return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
        ctx_token = _uid_ctx.set(payload["sub"])
        try:
            return await call_next(request)
        finally:
            _uid_ctx.reset(ctx_token)
    return await call_next(request)


@app.middleware("http")
async def no_cache_spa_html(request, call_next):
    """SPA 入口 HTML 禁止缓存，避免旧 index.html 引用已删除的哈希资源导致白屏"""
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path in ("/", ""):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def _sse(gen):
    """将生成器包装为 SSE 流式响应"""
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API：账号（注册/登录/当前用户） ─────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "status": "running"}


@app.post("/api/auth/register")
def register(payload: dict):
    result = auth.register_user(
        payload.get("username", ""),
        payload.get("password", ""),
        payload.get("nickname", ""),
    )
    if not result["ok"]:
        return {"ok": False, "message": result["message"]}
    return {"ok": True, "message": result["message"], "user": result["user"]}


@app.post("/api/auth/login")
def login(request: Request, payload: dict):
    username = (payload.get("username", "") or "").strip()
    now = time.time()
    # 登录失败限速（内存态，进程重启清零）：同一账号 5 次失败后冷却 60 秒
    fails = [t for t in _login_failures.get(username, []) if now - t < _LOGIN_COOLDOWN_SEC]
    if len(fails) >= _LOGIN_MAX_FAIL:
        return {"ok": False, "message": "失败次数过多，请 1 分钟后再试"}
    user = auth.authenticate(username, payload.get("password", ""))
    if not user:
        fails.append(now)
        _login_failures[username] = fails
        return {"ok": False, "message": "账号或密码错误"}
    _login_failures.pop(username, None)
    token = auth.create_token(user["uid"], user["username"])
    return {"ok": True, "token": token, "user": auth.public_user(user)}


@app.get("/api/auth/me")
def me():
    user = auth.find_by_uid(_uid_ctx.get())
    if not user:
        return JSONResponse({"ok": False, "message": "用户不存在"}, status_code=401)
    return {"ok": True, "user": auth.public_user(user)}


# ── API：bootstrap ─────────────────────────────────────────────

@app.get("/api/bootstrap")
def bootstrap():
    convs = backend.load_conversations()
    tree = []
    for project, items in convs.items():
        convs_list = [
            {"id": k, "title": v["title"], "updated_at": v.get("updated_at", ""), "created_at": v.get("created_at", "")}
            for k, v in items.items() if not k.startswith("__")
        ]
        tree.append({
            "project": project,
            "project_dir": items.get("__dir__", ""),
            # 按创建时间升序：旧的在上、新建的在下方（旧对话无 created_at 时用 updated_at 兜底）
            "conversations": sorted(
                convs_list,
                key=lambda x: x.get("created_at") or x.get("updated_at") or "",
            ),
        })
    return {
        "projects": tree,
        "settings": backend.get_settings(),
        "study_areas": backend.list_study_areas(),
        "current_study_area": backend.get_current_study_area(),
    }


# ── API：项目 ──────────────────────────────────────────────────

@app.post("/api/projects")
def create_project(payload: dict):
    return backend.create_project(payload.get("name", ""), payload.get("path", ""))


@app.post("/api/projects/{pid}/rename")
def rename_project(pid: str, payload: dict):
    return backend.rename_project(pid, payload.get("name", ""))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    return backend.delete_project(pid)


# ── API：对话 ──────────────────────────────────────────────────

@app.post("/api/conversations")
def create_conversation(payload: dict):
    return backend.create_conversation(payload.get("project", ""), payload.get("title", ""))


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, project: str = ""):
    return backend.delete_conversation(cid, project)


@app.get("/api/messages")
def get_messages(project: str = "", conv: str = ""):
    return {"messages": backend.get_messages(project, conv)}


@app.post("/api/project/{pid}/dir")
def save_project_dir(pid: str, payload: dict):
    return backend.save_project_dir(pid, payload.get("path", ""))


# ── API：设置 ──────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return backend.get_settings()


@app.post("/api/settings")
def save_settings(payload: dict):
    return backend.save_settings(payload)


@app.get("/api/model-params")
def get_model_params():
    return backend.get_model_params()


@app.post("/api/model-params")
def save_model_params(payload: dict):
    return backend.save_model_params(payload)


# ── API：研究区 ────────────────────────────────────────────────

@app.post("/api/study-area")
async def upload_study_area(files: List[UploadFile] = File(...)):
    results = []
    shp_stems = []  # 本次成功落盘的 .shp 主文件名（不含扩展名）
    validate_targets = []  # 本次新增的待验证研究区 [(显示名, 绝对路径)]
    for f in files:
        fsize = getattr(f, "size", None)
        if fsize is not None and fsize > _MAX_UPLOAD_BYTES:
            results.append(f"{f.filename or 'file'}: 文件过大（超过 200MB），已拒绝")
            continue
        content = await f.read()
        if len(content) > _MAX_UPLOAD_BYTES:
            results.append(f"{f.filename or 'file'}: 文件过大（超过 200MB），已拒绝")
            continue
        name = f.filename or "file"
        msg = backend.save_uploaded_file(name, content)
        results.append(msg)
        ext = os.path.splitext(name)[1].lower()
        if msg.startswith("✓"):
            if ext in (".geojson", ".json"):
                validate_targets.append((name, backend._study_dir() / name))
            elif ext == ".shp":
                # 记录已成功保存的 .shp：其配套文件（.dbf/.shx/.prj）可能排在本批更靠后，
                # 统一放到所有文件保存完成后再转换，保证配套文件先落盘
                shp_stems.append(os.path.splitext(name)[0])
    # 全部文件落盘后，把本次上传的 .shp 统一转换为 WGS84 GeoJSON
    for stem in shp_stems:
        ok, msg = backend.convert_uploaded_shp(stem)
        if ok:
            validate_targets.append((stem + ".geojson", backend._study_dir() / (stem + ".geojson")))
        results.append(f"✓ 已转换为 {stem}.geojson" if ok else f"⚠️ {stem}.shp 转换失败: {msg}")
    # 逐一验证本次新增的研究区：结果只进 validations 供研究区面板展示
    # （与「测试」页同款状态行），不拼进 message，避免上传完成时弹出 emoji 弹窗
    validations = []
    for disp, path in validate_targets:
        level, vmsg = backend.validate_study_area(str(path))
        validations.append({"name": disp, "level": level, "message": vmsg})
    return {"ok": True, "message": "\n".join(results),
            "study_areas": backend.list_study_areas(), "validations": validations}


@app.get("/api/study-areas")
def study_areas():
    return {"study_areas": backend.list_study_areas(), "current": backend.get_current_study_area()}


@app.post("/api/study-area/current")
def set_study_area_current(payload: dict):
    """切换当前研究区：写入 .current.txt 标记，Agent 执行时优先使用该文件"""
    name = payload.get("name") or ""
    if not backend.set_current_study_area(name):
        raise HTTPException(status_code=400, detail="研究区文件不存在")
    return {
        "ok": True,
        "message": f"已切换当前研究区为 {name}",
        "current": name,
        "study_areas": backend.list_study_areas(),
    }


@app.delete("/api/study-area")
def delete_study_area(name: str = ""):
    message = backend.delete_study_area(name)
    return {
        "ok": True,
        "message": message,
        "current": backend.get_current_study_area(),
        "study_areas": backend.list_study_areas(),
    }


# ── API：工作流 / 精度 / 地图 ──────────────────────────────────

@app.get("/api/workflow")
def workflow(conv: str = ""):
    return {"steps": backend.get_workflow_status(conv or None)}


@app.get("/api/accuracy")
def accuracy(conv: str = ""):
    return backend.get_accuracy_summary(conv or None)


@app.get("/api/layers")
def layers(conv: str = ""):
    return {"layers": backend.list_layers(conv or None)}


@app.post("/api/lst-values")
def lst_values(payload: dict, conv: str = ""):
    """批量读取光标所在像元在各地表温度图层上的温度（单位 K）。

    payload: {"lat": float, "lon": float, "layers": [...]}
    layers 元素兼容两种：
      - 字符串：旧对话图层 id（按项目目录解析）
      - 对象：{"kind": "input", "id": 图层id, "task_id": …, "key": …}
              或 {"kind": "artifact", "id": 图层id, "artifact_id": …}
    返回 {"values": {layer_id: 温度或 null}}；非 LST 层会被忽略。
    """
    project_dir = backend._get_project_dir(conv or None)
    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        return {"values": {}}
    values = {}
    for item in (payload.get("layers") or []):
        if isinstance(item, dict):
            lid = str(item.get("id") or item.get("artifact_id") or item.get("key") or "")
            kind = str(item.get("kind") or "")
            if kind == "input":
                value = backend.task_input_sample(
                    str(item.get("task_id") or ""), str(item.get("key") or ""),
                    lat, lon)
            elif kind == "artifact":
                value = backend.artifact_sample(
                    str(item.get("artifact_id") or ""), lat, lon)
            else:
                value = None
        else:
            lid = str(item)
            if not project_dir or not os.path.isdir(project_dir):
                values[lid] = None
                continue
            value = LayerVisualizer.sample_lst_value(project_dir, lid, lat, lon)
        values[lid] = value
    return {"values": values}


@app.get("/api/layer/{layer_id}/png")
def layer_png(layer_id: str, conv: str = ""):
    result = backend.render_layer_png(layer_id, conv or None)
    if result is None:
        raise HTTPException(status_code=404, detail="图层不可用")
    png, bounds = result
    return Response(content=png, media_type="image/png",
                    headers={"X-Bounds": json.dumps(bounds)})


@app.get("/api/layer/{layer_id}/tile/{z}/{x}/{y}")
def layer_tile(layer_id: str, z: int, x: int, y: int, conv: str = ""):
    # 瓦片坐标合法性校验：0<=z<=24，z 层每边最多 2^z 块；越界直接拒绝，
    # 不把任意 z/x/y 透传给渲染器
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise HTTPException(status_code=400, detail="非法瓦片坐标")
    # 允许浏览器缓存瓦片（同一图层同一 URL 平移/缩放时直接本地取，
    # 不再重复请求后端）；图层更新时前端 tileUrl 的 t 参数变化，自动失效拉新
    _cache_headers = {"Cache-Control": "public, max-age=300"}
    png = backend.render_layer_tile(layer_id, conv or None, z, x, y)
    if png is None:
        return Response(status_code=204, headers=_cache_headers)
    return Response(content=png, media_type="image/png", headers=_cache_headers)


@app.get("/api/map/html", response_class=HTMLResponse)
def map_html(conv: str = ""):
    return backend.build_map_html(conv or None)


@app.post("/api/exec-mode")
def set_exec_mode(payload: dict):
    """切换执行模式（立即生效）：完全执行下等待中的配对选择自动代选。"""
    return backend.set_exec_mode(
        str(payload.get("project") or ""),
        str(payload.get("conv") or ""),
        str(payload.get("exec_mode") or ""))


# ── API：聊天 SSE ──────────────────────────────────────────────

@app.post("/api/chat/start")
def chat_start(payload: dict):
    return backend.chat_start(
        payload.get("project", ""), payload.get("conv", ""), payload.get("message", ""),
        exec_mode=payload.get("exec_mode", ""),
        chat_mode=payload.get("chat_mode", ""),
        request_id=payload.get("request_id", ""),
    )


# ── API：状态内核台账只读巡检（升级第一阶段验收入口） ─────────

@app.get("/api/kernel/ledger")
def kernel_ledger(limit: int = 50):
    return backend.ledger_snapshot(limit)


@app.get("/api/kernel/session")
def kernel_session(project: str = "", conv: str = "", limit: int = 50):
    """会话快照：本对话的任务卡与待答问题（升级第二阶段理解层）。"""
    if not project or not conv:
        return JSONResponse({"error": "缺少 project 或 conv"}, status_code=400)
    return backend.session_snapshot(project, conv, limit)


@app.post("/api/kernel/answer")
def kernel_answer(payload: dict):
    """回答一条持久问题（卡片点击）；与文字回答共用同一套消费逻辑。"""
    return backend.answer_question(
        str(payload.get("project") or ""),
        str(payload.get("conv") or ""),
        str(payload.get("question_id") or ""),
        str(payload.get("answer") or ""),
        float(payload.get("tz") or _DEFAULT_TZ_OFFSET),
    )


# ── API：界面（升级第六阶段：§12.1 接口契约） ─────────

@app.get("/api/conversations/{cid}/snapshot")
def conversation_snapshot(cid: str, limit: int = 50):
    """会话快照：消息摘要、任务列表、待答问题、产物、事件游标。"""
    return backend.session_snapshot("", cid, limit)


@app.get("/api/conversations/{cid}/events")
def conversation_events(cid: str, cursor: int = 0, tz: float = _DEFAULT_TZ_OFFSET,
                       log_cursor: int = 0):
    """对话事件流（SSE）：快照 → 游标补发 → 持续推送；多任务复用一条连接。"""
    return _sse(backend.conversation_events(cid, cursor=cursor, tz=tz,
                                            log_cursor=log_cursor))


@app.get("/api/conversations/{cid}/logs")
def conversation_logs(cid: str, tz: float = _DEFAULT_TZ_OFFSET,
                      after_id: int = 0, limit: int = 4000):
    """对话执行日志历史（持久化）：跨刷新/重启/中断不丢失；带本地时区时间戳。"""
    return backend.conversation_logs(cid, tz=tz, after_id=after_id, limit=limit)


@app.delete("/api/conversations/{cid}/logs")
def clear_conversation_logs(cid: str):
    """清除本对话执行日志（用户显式操作）。"""
    return backend.clear_conversation_logs(cid)


@app.post("/api/preferences")
def set_preferences(payload: dict):
    """界面偏好：语言（zh/en）持久到用户设置，供后端组装文案使用。"""
    return backend.set_ui_language(str(payload.get("lang") or ""))


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    """任务详情：版本、运行、节点、排队原因、问题、产物、失败原因。"""
    return backend.task_detail(task_id)


@app.get("/api/tasks/{task_id}/inputs")
def task_inputs(task_id: str):
    """任务原始输入栅格层（地图恢复 10m S2 等原生图层）。"""
    return backend.task_inputs(task_id)


@app.get("/api/tasks/{task_id}/inputs/{key}/tile/{z}/{x}/{y}")
def task_input_tile(task_id: str, key: str, z: int, x: int, y: int):
    """任务原始输入瓦片（服务端解析路径，不接收任意路径）。"""
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise HTTPException(status_code=400, detail="非法瓦片坐标")
    _cache_headers = {"Cache-Control": "public, max-age=300"}
    png = backend.task_input_tile(task_id, key, z, x, y)
    if png is None:
        return Response(status_code=204, headers=_cache_headers)
    return Response(content=png, media_type="image/png", headers=_cache_headers)


@app.post("/api/tasks/{task_id}/commands")
def task_command(task_id: str, payload: dict):
    """任务命令：取消 / 重试 / 优先级（带请求编号与所见版本校验）。"""
    return backend.task_command(
        task_id,
        str(payload.get("operation") or ""),
        str(payload.get("request_id") or ""),
        int(payload.get("seen_version") or 0),
        payload.get("payload") or {},
    )


@app.get("/api/artifacts/{artifact_id}")
def artifact_detail(artifact_id: str):
    """产物元数据：归属 + 可用性 + 血缘（§12.4）。"""
    return backend.artifact_detail(artifact_id)


@app.get("/api/artifacts/{artifact_id}/download")
def artifact_download(artifact_id: str):
    """产物下载：归属 + 可用性校验后返回文件。"""
    path, error = backend.artifact_download_path(artifact_id)
    if not path:
        return JSONResponse({"ok": False, "message": error}, status_code=404)
    return FileResponse(str(path), filename=Path(path).name)


@app.get("/api/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str):
    """小型文本/JSON 产物内容（精度/报告面板直读，§12.4 面板联动）。"""
    return backend.artifact_content(artifact_id)


@app.get("/api/artifacts/{artifact_id}/meta")
def artifact_meta(artifact_id: str):
    """产物元数据：地理范围（EPSG:4326）+ 建议渲染样式（地图联动用）。"""
    return backend.artifact_meta(artifact_id)


@app.get("/api/artifacts/{artifact_id}/tile/{z}/{x}/{y}")
def artifact_tile(artifact_id: str, z: int, x: int, y: int, style: str = ""):
    """按产物文件直接渲染瓦片（地图面板展示指定任务结果）。"""
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise HTTPException(status_code=400, detail="非法瓦片坐标")
    _cache_headers = {"Cache-Control": "public, max-age=300"}
    png = backend.artifact_tile(artifact_id, style, z, x, y)
    if png is None:
        return Response(status_code=204, headers=_cache_headers)
    return Response(content=png, media_type="image/png", headers=_cache_headers)


# ── API：计划编译（升级第三阶段验收入口） ──────────────

@app.post("/api/planning/compile")
def planning_compile(payload: dict):
    """编译确认后的任务：建快照（进队前冻结）+ 节点图落库（不派发）。"""
    return backend.planning_compile(
        str(payload.get("task_id") or ""),
        int(payload.get("expected_version") or 0),
    )


@app.get("/api/planning/run/{run_id}")
def planning_run(run_id: str):
    """查运行详情：冻结参数快照（逐项来源）+ 节点清单与依赖顺序。"""
    return backend.planning_run_detail(run_id)


@app.get("/api/scheduling/status")
def scheduling_status():
    return backend._scheduler.snapshot(backend._uid())


@app.post("/api/scheduling/retry")
def scheduling_retry(payload: dict):
    try:
        backend._scheduler.retry(str(payload.get("node_id") or ""), backend._uid())
        return {"ok": True}
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/chat/stream")
def chat_stream(conv: str = "", tz: float = _DEFAULT_TZ_OFFSET):
    if not conv:
        return JSONResponse({"error": "缺少 conv"}, status_code=400)
    # tz 可能为 0（UTC 用户）：不能写 float(tz or default)，0 or default 会错回 UTC+8
    return _sse(backend.chat_stream(conv, tz=float(tz)))


@app.get("/api/chat/streaming")
def chat_streaming(conv: str = ""):
    """查询该对话当前是否有正在运行的流（前端重新进入对话时用于恢复订阅）"""
    return {"active": bool(conv) and conv in backend._stream_queues}


@app.get("/api/chat/current")
def chat_current(conv: str = ""):
    """返回该对话当前流式生成的累积内容（切回对话时立即显示最新气泡，
    避免先显示会话文件中的旧快照再等 SSE 慢慢同步）。

    服务重启后内存态清空，回退读对话历史里节流落盘的备份，保证切回对话
    时气泡不是空的（运行中项目被重启后的恢复路径）。
    """
    if not conv:
        return {"active": False, "content": ""}
    content = backend._stream_content.get(conv, "")
    thinking = backend._stream_thinking.get(conv, "")
    thinking_seconds = backend._stream_thinking_seconds.get(conv, 0.0)
    if not content:
        content, thinking, thinking_seconds = backend._last_stream_content(conv)
    return {
        "active": conv in backend._stream_queues,
        "content": format_bubble("", content, streaming=False) if content else "",
        "thinking": thinking,
        "thinking_seconds": thinking_seconds,
        "logs": backend._stream_logs.get(conv, []),
    }


@app.post("/api/chat/resume")
def chat_resume(payload: dict):
    return backend.chat_resume(payload.get("conv", ""), payload)


# ── API：文件列表 / 下载（根治"无权限"） ───────────────────────

@app.get("/api/files")
def files(project_dir: str = ""):
    return backend.list_project_files(project_dir)


@app.get("/api/sysinfo")
def sysinfo():
    """日志面板实时资源占用：本软件进程内存 + 软件数据目录磁盘（GB）"""
    return backend.get_sys_usage()


@app.get("/api/download")
def download(project_dir: str = "", path: str = ""):
    target, err = backend.resolve_download(project_dir, path)
    if target is None:
        raise HTTPException(status_code=400, detail=err or "非法请求")
    filename = os.path.basename(target)
    return FileResponse(
        target,
        filename=filename,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
    )


@app.post("/api/download/multiple")
def download_multiple(payload: dict):
    """批量下载：paths 数组 → 打包为 zip 返回。

    用磁盘临时文件打包 + FileResponse 流式返回（不整包读入内存），
    避免多选大 GeoTIFF 时内存峰值等于文件总和的 OOM 风险。
    """
    project_dir = str(payload.get("project_dir") or "")
    paths = payload.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise HTTPException(status_code=400, detail="未选择文件")
    # 先统一校验，任一非法即拒绝（防目录穿越 + 归属校验）
    targets = []
    for rel in paths:
        target, err = backend.resolve_download(project_dir, str(rel))
        if target is None:
            raise HTTPException(status_code=400, detail=f"非法请求：{err}")
        targets.append((str(rel), target))
    import zipfile

    tmp = tempfile.NamedTemporaryFile(prefix="gtai_zip_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, target in targets:
                # 以相对路径入库，子目录结构保留；避免同名文件互相覆盖
                arcname = rel.replace("\\", "/")
                zf.write(target, arcname)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    # FileResponse 从磁盘流式发送；发送完成后后台删除临时文件
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="geothermoai_download.zip",
        content_disposition_type="attachment",
        background=BackgroundTask(_cleanup_temp_file, tmp_path),
    )


def _cleanup_temp_file(path: str) -> None:
    """删除下载用临时文件（响应发送完成后的后台任务）。"""
    try:
        os.remove(path)
    except OSError:
        pass


# ── API：测试 ──────────────────────────────────────────────────

@app.post("/api/test/planetary")
def test_planetary():
    return {"result": backend.test_planetary_connection()}


@app.post("/api/test/cdse")
def test_cdse():
    """Copernicus Data Space 连通性测试。"""
    return {"result": backend.test_cdse_connection()}


@app.post("/api/test/gdal")
def test_gdal():
    return {"result": backend.test_gdal_status()}


@app.get("/api/debug/mem")
def debug_mem():
    """临时内存诊断端点：输出进程 RSS + gc 对象统计 + 大对象引用链。"""
    import gc, sys, ctypes
    from collections import Counter

    def _vmrss():
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024  # MB
        except Exception:
            return -1
        return -1

    rss_before = _vmrss()
    gc.collect()
    rss_after_gc = _vmrss()

    # malloc_trim
    trim_ret = None
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim_ret = libc.malloc_trim(0)
    except Exception as e:
        trim_ret = f"err: {e}"
    rss_after_trim = _vmrss()

    # 按类型统计对象数量与大小
    counts = Counter()
    sizes = Counter()
    for obj in gc.get_objects():
        t = type(obj).__name__
        counts[t] += 1
        try:
            sizes[t] += sys.getsizeof(obj)
        except Exception:
            pass

    top_by_size = [
        {"type": n, "count": counts[n], "size_mb": round(s / 1048576, 1)}
        for n, s in sizes.most_common(25)
    ]

    # 找 >=1MB 的大对象及其引用者
    big = []
    seen = set()
    for obj in gc.get_objects():
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        try:
            sz = sys.getsizeof(obj)
        except Exception:
            sz = 0
        if sz >= 1048576:
            t = type(obj).__name__
            ref_desc = []
            for r in gc.get_referrers(obj)[:5]:
                rt = type(r).__name__
                if hasattr(r, "__name__"):
                    ref_desc.append(f"{rt}:{r.__name__}")
                elif isinstance(r, dict) and len(r) < 20:
                    ref_desc.append(f"{rt}(keys={list(r.keys())[:5]})")
                else:
                    ref_desc.append(rt)
            big.append({"type": t, "size_mb": round(sz / 1048576, 1), "refs": ref_desc})
    big.sort(key=lambda x: -x["size_mb"])

    # lru_cache 瓦片缓存诊断
    tile_cache_info = None
    try:
        from core.visualization import LayerVisualizer
        ci = LayerVisualizer._render_tile_cached.cache_info()
        tile_cache_info = {
            "hits": ci.hits, "misses": ci.misses,
            "maxsize": ci.maxsize, "currsize": ci.currsize,
        }
    except Exception:
        pass

    # GDAL 全局块缓存用量
    gdal_cache_mb = None
    try:
        from osgeo import gdal
        gdal_cache_mb = gdal.GetCacheUsed64() // (1024 * 1024)
    except Exception:
        pass

    # 对话流内容内存占用（_stream_content / _stream_logs）
    stream_stats = {}
    try:
        sc = getattr(backend, "_stream_content", {})
        sl = getattr(backend, "_stream_logs", {})
        stream_stats = {
            "stream_content_count": len(sc),
            "stream_content_mb": round(sum(len(v) for v in sc.values()) / 1048576, 1),
            "stream_logs_count": len(sl),
            "stream_logs_mb": round(
                sum(sum(len(s) for s in lst) for lst in sl.values()) / 1048576, 1),
        }
    except Exception:
        pass

    return {
        "rss_mb_before": rss_before,
        "rss_mb_after_gc": rss_after_gc,
        "rss_mb_after_trim": rss_after_trim,
        "malloc_trim_ret": trim_ret,
        "top_by_size": top_by_size,
        "big_objects": big[:20],
        "tile_cache": tile_cache_info,
        "gdal_cache_used_mb": gdal_cache_mb,
        "stream_stats": stream_stats,
    }


# ── 静态资源（Vue 构建产物） ───────────────────────────────────

_DIST = _ROOT / "dist"
if _DIST.exists():
    # 静态资源一律不缓存（no-cache）：前端每次构建产物 hash 变化，
    # 但 index.html 不带 hash，浏览器按启发式缓存会一直加载旧页面导致
    # 部署后看不到更新，必须强制每次重新校验。
    app.mount(
        "/", StaticFiles(directory=str(_DIST), html=True), name="static",
    )

    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path in ("/", "/index.html") or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


# ── 入口 ───────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="info")


if __name__ == "__main__":
    main()
