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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import auth
from core.ai_assistant import GeoThermoAI_Assistant
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
]

_AGENT_KEYWORDS = [
    "处理", "训练", "下载", "执行", "运行", "生成",
    "全流程", "一键", "开始", "计算", "导出", "评估",
]

_WORKFLOW_KEYWORDS = ["全流程", "一键", "跑完全流程", "执行全流程", "处理", "下载", "获取"]

# 工作流面板标签：单一来源在 core/agent/presentation.py（技术方案 9.4），
# 避免同一阶段在后端两处出现不一致的中文名
_WORKFLOW_LABELS = presentation.WORKFLOW_LABELS


def _is_agent_command(message: str) -> bool:
    """关键词路由。角色化后降级为「LLM 不可用时的兜底」，不删除（技术方案 10.3）。"""
    return any(kw in message for kw in _AGENT_KEYWORDS)


def _is_workflow_command(message: str) -> bool:
    return any(kw in message for kw in _WORKFLOW_KEYWORDS)


# 审批等待超时（秒）：默认取 settings.agent.approval_wait_seconds，
# 可用环境变量 GTAI_APPROVAL_WAIT_SECONDS 覆盖（技术方案 3.4c）
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
    elif streaming:
        parts.append("▍")
    return "\n\n".join(parts)


def strip_thinking(text: str) -> str:
    """从消息内容中剥离 <details> 思考链，供 LLM 上下文使用"""
    return re.sub(r"<details[^>]*>.*?</details>", "", text or "", flags=re.DOTALL).strip()


# ── 业务后端（移植自 GradioAPI，去除 Gradio 耦合） ─────────────

class AppBackend:
    def __init__(self):
        # 启动期启用 GDAL/OSR 异常模式（A-01）：把静默返回 None/错误码的契约问题
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
        # SSE 连接代际号：新连接递增代际，让旧（已断开但服务端未感知的）生成器自行退出
        self._stream_gen: Dict[str, int] = {}
        # 每个对话已累积的流式内容：断线重连/流结束后的重连用于补齐完整气泡
        self._stream_content: Dict[str, str] = {}

    # ── 内部工具 ───────────────────────────────────────────────

    def _register_builtin_skills(self):
        from core.skills.builtin import (
            DataAcquisitionSkill,
            DataPipelineSkill,
            TTRIComputeSkill,
            RFModelSkill,
            TCRComputeSkill,
            LSTExportSkill,
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
            AccuracyEvalSkill(),
            AIAssistantSkill(),
        ):
            self.registry.register(skill)

    # ── 用户维度（登录隔离） ────────────────────────────────────

    @staticmethod
    def _uid() -> str:
        return _uid_ctx.get() or "default"

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
        """项目数据根目录：环境变量 WORKSPACE_ROOT（可指向大容量盘），默认 data/users"""
        root = os.environ.get("WORKSPACE_ROOT", "").strip()
        if root:
            return Path(root)
        return _ROOT / "data" / "users"

    def _auto_project_dir(self, name: str) -> Path:
        """按用户隔离的项目目录：{WORKSPACE_ROOT}/{uid}/workspace/{name}

        路径完全由后端分配（升级规划 3.12），前端不接收用户自定义路径，
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
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except Exception:
                old = {}
            created_at = old.get("created_at", now)
            starred = old.get("starred", False)
        else:
            created_at = now
            starred = False
        data = {
            "id": conv_id,
            "project": project,
            "title": title,
            "messages": messages,
            "project_dir": project_dir,
            "created_at": created_at,
            "updated_at": now,
            "starred": starred,
        }
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
        忽略前端传入的 path（升级规划 3.12：目录物理隔离）"""
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
        # 级联删除记忆（experiments/preferences/ChromaDB Collection），失败仅告警不影响删除。
        # 仅当 memory 目录已存在时才删除——未初始化过记忆时直接跳过，
        # 避免为删除而触发 _memory_for() 的昂贵初始化（bge 模型加载 + 领域知识播种）
        try:
            project_id = self._project_id(pid)
            if project_id and (self._user_dir() / "memory").exists():
                self._memory_for().delete_project(project_id)
        except Exception as e:
            logging.warning(f"[memory] 删除项目记忆失败: {e}")
        for cid in list(convs[pid].keys()):
            if cid.startswith("__"):
                continue
            self._hard_delete_conversation(cid, pid, convs)
        projects = [p for p in self._load_projects() if p["name"] != pid]
        self._save_projects(projects)
        return {"ok": True, "message": f"项目「{pid}」已删除",
                "projects": [p["name"] for p in projects]}

    def create_conversation(self, project: str, title: str) -> dict:
        title = (title or "").strip() or "新对话"
        conv_id = uuid.uuid4().hex[:12]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        convs = self.load_conversations()
        if project not in convs:
            return {"ok": False, "message": "项目不存在，请先创建项目"}
        self._get_conv_state(conv_id)["project_dir"] = convs[project].get("__dir__", "")
        self._persist_conversation(conv_id, project, title, [], convs[project].get("__dir__", ""))
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

    def delete_conversation(self, cid: str, pid: str) -> dict:
        convs = self.load_conversations()
        if pid not in convs or cid not in convs[pid]:
            return {"ok": False, "message": "对话不存在"}
        # 级联删除该对话产生的实验记忆，失败仅告警不影响删除。
        # 仅当 memory 目录已存在时才删除（避免为删除而触发昂贵的记忆初始化）
        try:
            project_id = self._project_id(pid)
            if project_id and (self._user_dir() / "memory").exists():
                self._memory_for().delete_conversation(project_id, cid)
        except Exception as e:
            logging.warning(f"[memory] 删除对话记忆失败: {e}")
        self._hard_delete_conversation(cid, pid, convs)
        remaining = [k for k in convs[pid] if not k.startswith("__")]
        return {"ok": True, "message": "对话已彻底删除", "remaining": remaining}

    def save_project_dir(self, pid: str, path: str) -> dict:
        """保存项目目录。路径由后端按用户自动分配（与 create_project 一致），
        忽略前端传入路径，杜绝把项目指向他人目录（升级规划 3.12）。"""
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
            if cid in self._conv_states:
                self._conv_states[cid]["project_dir"] = normalized
            try:
                self._update_conversation_file(cid, pid, project_dir=normalized)
            except Exception:
                pass
        return {"ok": True, "message": "项目目录已保存", "path": normalized}

    def get_messages(self, pid: str, cid: str) -> list:
        convs = self.load_conversations()
        if pid in convs and cid in convs[pid]:
            self._get_conv_state(cid)["project_dir"] = convs[pid].get("__dir__", "")
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
            project_dir = convs[pid].get("__dir__", "")
            title = convs[pid][cid].get("title", "")
            self._persist_conversation(cid, pid, title, history, project_dir)
        except Exception:
            pass

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
            # 凭据不回传明文（升级规划 3.12.1）：只给掩码与长度（供前端按真实长度显示黑点）
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
        api_key = (payload.get("api_key") or "").strip()
        model_id = (payload.get("model_id") or "").strip()
        display_name = (payload.get("display_name") or "").strip() or model_id

        api["api_format"] = api_format
        api["api_base_url"] = base_url
        # 密钥留空则保持原值（前端回显的是掩码，不能写回）
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
        return {"ok": True, "message": "✅ API 设置已保存并应用",
                "display_name": api.get("display_name", "")}

    # ── 模型参数 ───────────────────────────────────────────────

    def get_model_params(self) -> dict:
        s = self._load_settings()
        return s.get("model", {})

    def save_model_params(self, params: dict) -> dict:
        s = self._load_settings()
        s["model"] = {k: v for k, v in params.items() if v is not None}
        self._save_settings(s)
        return {"ok": True, "message": "✅ 参数已保存"}

    # ── 研究区上传 ─────────────────────────────────────────────

    def save_uploaded_file(self, filename: str, content: bytes) -> str:
        self._study_dir().mkdir(parents=True, exist_ok=True)
        fname = os.path.basename(filename)
        dest = self._study_dir() / fname
        try:
            with open(dest, "wb") as f:
                f.write(content)
        except Exception as e:
            return f"✗ {fname}: {e}"
        ext = os.path.splitext(fname)[1].lower()
        if ext == ".shp":
            gj_path = self._study_dir() / (os.path.splitext(fname)[0] + ".geojson")
            ok, msg = self._shp_to_geojson(str(dest), str(gj_path))
            return f"✓ {fname}\n" + (f"✓ 已转换为 {gj_path.name}" if ok else f"⚠️ 转换失败: {msg}")
        return f"✓ {fname}"

    def _shp_to_geojson(self, shp_path: str, geojson_path: str) -> Tuple[bool, str]:
        try:
            import shapefile
            reader = shapefile.Reader(shp_path)
            fields = reader.fields[1:]
            features = []
            for sr in reader.iterShapeRecords():
                geom = sr.shape.__geo_interface__
                props = {}
                for i, field in enumerate(fields):
                    val = sr.record[i]
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="replace")
                    props[field[0]] = val
                features.append({"type": "Feature", "geometry": geom, "properties": props})
            geojson = {"type": "FeatureCollection", "features": features}
            with open(geojson_path, "w", encoding="utf-8") as f:
                json.dump(geojson, f, ensure_ascii=False, indent=2)
            return True, ""
        except ImportError:
            return False, "未安装 pyshp，请运行: pip install pyshp"
        except Exception as e:
            return False, str(e)

    def list_study_areas(self) -> list:
        if not self._study_dir().exists():
            return []
        files = sorted(self._study_dir().glob("*.geojson"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [f.name for f in files]

    # ── 工作流 / 精度 ──────────────────────────────────────────

    def get_workflow_status(self, cid: Optional[str]) -> List[dict]:
        """返回各 stage 状态；与固定 run_manifest.json 交叉核对（A-08 前端联动）。

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
                manifest_stages = run_manifest.load_manifest(project_dir).get("stages", {})
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
            rows.append({"id": s, "label": _WORKFLOW_LABELS.get(s, s), "status": final_status})
        return rows

    def _read_eval_json(self, path: str) -> dict:
        """读取评估 JSON，区分 missing（未生成）/ error（存在但损坏）/ ok（正常），
        不把缺失或损坏都填成 0（B-08：禁止把缺数据伪装成完美零误差）。"""
        if not os.path.isfile(path):
            return {"status": "missing"}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"status": "ok", "data": json.load(f)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_accuracy_summary(self, cid: Optional[str]) -> dict:
        """返回两套独立协议（A-07）：independent_prediction 与
        coarse_constraint_closure，不再混成一张表，也不再输出 5K 阈值/
        passed 等字段（用户确认第4/5条）。"""
        empty = {"independent_prediction": {"status": "missing"}, "coarse_constraint_closure": {"status": "missing"}}
        if not cid:
            return empty
        project_dir = self._get_project_dir(cid)
        if not project_dir:
            return empty
        results_dir = os.path.join(project_dir, "results")
        return {
            "independent_prediction": self._read_eval_json(os.path.join(results_dir, "independent_prediction.json")),
            "coarse_constraint_closure": self._read_eval_json(os.path.join(results_dir, "coarse_constraint_closure.json")),
        }

    # ── 地图 ──────────────────────────────────────────────────

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

    def list_project_files(self, project_dir: str) -> dict:
        project_dir = (project_dir or "").strip()
        # 归属校验：只能访问当前用户自己的项目目录（升级规划 3.12，堵越权读取）
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
                full = os.path.join(root, name)
                try:
                    if os.path.isfile(full):
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
        # 归属校验：只能下载当前用户自己的项目文件（升级规划 3.12，堵越权下载）
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
        if not os.path.isfile(target):
            return None, "文件不存在"
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

    def test_gdal_status(self) -> str:
        """GDAL 与投影链路自检（A-01 升级版）。

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
            ds = drv.Create(path, 4, 4, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"])
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

    def chat_start(self, pid: str, cid: str, user_msg: str, exec_mode: str = "") -> dict:
        user_msg = (user_msg or "").strip()
        convs = self.load_conversations()
        if pid not in convs or cid not in convs[pid]:
            return {"ok": False, "message": "对话不存在，请先选择对话"}
        history = convs[pid][cid].get("messages", [])
        history = history + [{"role": "user", "content": user_msg}]

        assistant = self._assistant_for()
        if not assistant.api_key and not assistant.api_base_url:
            history.append({"role": "assistant", "content": "⚠️ 请先在右侧「🔑 API 设置」配置模型。"})
            self._save_history(pid, cid, history)
            return {"ok": True, "messages": history}

        conv_state = self._get_conv_state(cid)
        project_dir = conv_state["project_dir"]

        agent_cfg = self._agent_settings()
        roles_enabled = agent_cfg["roles_enabled"]

        # 执行模式（技术方案 3.5）：本次请求 > 会话已记录 > settings 默认值
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
        # 角色化开启后交给规划 Agent 以对话方式引导（拍板结论 3），这里不再硬拦截。
        if (not roles_enabled) and _is_workflow_command(user_msg) and _is_agent_command(user_msg):
            uploaded = self.list_study_areas()
            if not uploaded:
                history.append({"role": "assistant",
                                "content": "⚠️ 请先上传研究区文件（Shapefile 或 GeoJSON），然后再发送指令。"})
                self._save_history(pid, cid, history)
                return {"ok": True, "messages": history}
            if not project_dir or not os.path.isdir(project_dir):
                history.append({"role": "assistant",
                                "content": "⚠️ 请先设置项目保存路径，然后再执行一键全流程。"})
                self._save_history(pid, cid, history)
                return {"ok": True, "messages": history}

        # 追加占位 assistant 气泡
        history = history + [{"role": "assistant", "content": "▍"}]
        self._save_history(pid, cid, history)

        # 创建流式队列 + 后台线程
        q: "queue.Queue" = queue.Queue()
        self._stream_queues[cid] = q
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
        wait_seconds = _approval_wait_seconds_from(agent_cfg)

        def _runner():
            ctx_token = _uid_ctx.set(uid)  # 后台线程不带请求 contextvars，需显式恢复用户
            try:
                if roles_enabled or _is_agent_command(user_msg):
                    def on_token(content: str):
                        q.put(("token", content))

                    def pause_callback(pause_data):
                        """等待用户在审批节点做出选择（技术方案 3.4c）。

                        角色路径：分片轮询 + 超时挂起，绝不替用户做决定（拍板结论 1）。
                        旧路径（roles_enabled=False）：保持改造前的 300 秒 + 静默选第一组。
                        """
                        q.put(("pause", pause_data))
                        timeout = wait_seconds if roles_enabled else _LEGACY_PAUSE_TIMEOUT
                        deadline = time.time() + timeout
                        while time.time() < deadline:
                            if pause_event.wait(timeout=5):
                                pause_event.clear()
                                break
                            if cid in self._deleted_convs:
                                return {"paused": True}
                        # pop 而非 get：一次运行可能有多个暂停点，
                        # 残留的上一次选择会让下一个暂停点被自动放行
                        selected = self._pause_responses.pop(cid, None)
                        if selected is not None:
                            return {"paused": False, "data": selected}
                        if not roles_enabled:
                            pairs = pause_data.get("pairs", []) if isinstance(pause_data, dict) else []
                            if pairs:
                                return {"paused": False, "data": pairs[0]}
                        return {"paused": True}

                    def workflow_callback(skill_name, status, idx, total):
                        wp = conv_state["workflow_progress"]
                        steps = []
                        for si, sn in enumerate(WORKFLOW_STEPS):
                            if sn == skill_name:
                                steps.append({"name": sn, "status": status})
                            elif si < idx:
                                steps.append({"name": sn, "status": "completed"})
                            else:
                                steps.append({"name": sn, "status": "pending"})
                        wp["status"] = "running" if status != "completed" else (
                            "completed" if idx + 1 >= total else "running"
                        )
                        wp["current_step"] = skill_name
                        wp["current_index"] = idx
                        wp["steps"] = steps
                        q.put(("workflow", None))

                    result = self._agent_for().process_command(
                        user_msg,
                        on_token=on_token,
                        on_log=lambda text: q.put(("log", text)),
                        pause_callback=pause_callback,
                        workflow_callback=workflow_callback,
                        project_dir=project_dir,
                        settings_path=str(self._user_settings_path()),
                        study_areas_dir=str(self._study_dir()),
                        conv_id=cid,
                        project_id=self._project_id(pid),
                        memory_manager=self._memory_for(),
                        exec_mode=resolved_mode,
                        prior_messages=prior_messages,
                    )
                    if result and ("⚠️" in result or "失败" in result or "未找到" in result):
                        q.put(("append", "\n\n" + result))
                    q.put(("done", None))
                else:
                    context = {
                        "workflow_status": conv_state["workflow_progress"],
                        "config": self._load_settings(),
                    }
                    self._assistant_for().ask_stream(
                        user_msg, lambda c: q.put(("token", c)),
                        context=context, prior_messages=prior_messages,
                    )
                    q.put(("done", None))
            except Exception as e:
                q.put(("error", str(e)))
            finally:
                _uid_ctx.reset(ctx_token)

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        self._agent_threads[cid] = thread
        self._stream_starts[cid] = time.time()
        return {"ok": True, "messages": history}

    def chat_stream(self, cid: str):
        """SSE 生成器：消费流式队列，输出事件流

        代际号接管：若上一个 SSE 连接已断开（如代理空闲超时）但服务端未感知
        （半开连接），新连接会递增代际号；旧生成器在下次读取队列时发现代际
        不符，把事件还给队列后自行退出，避免旧连接长期占用导致气泡冻结。
        """
        thread = self._agent_threads.get(cid)
        q = self._stream_queues.get(cid)
        pause_event = self._pause_events.get(cid)
        if not thread or not q or not pause_event:
            # 流已结束（含断线后重连）：把已累积内容一次性交付，避免气泡停在半截
            saved = self._stream_content.get(cid, "")
            if saved:
                yield ("event: done\ndata: " + json.dumps(
                    {"content": format_bubble("", saved, streaming=False)},
                    ensure_ascii=False) + "\n\n")
            else:
                yield "event: done\ndata: {}\n\n"
            return
        gen = self._stream_gen.get(cid, 0) + 1
        self._stream_gen[cid] = gen
        yield from self._stream_events(cid, thread, q, pause_event, gen)

    def _stream_events(self, cid, thread, q, pause_event, gen):
        paused = False
        convs = self.load_conversations()
        # 找到 pid/cid
        pid = None
        for p, items in convs.items():
            if cid in items:
                pid = p
                break
        accumulated = self._stream_content.get(cid, "")
        start_time = self._stream_starts.get(cid, time.time())
        last_emit = time.time()

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
                    # 心跳：SSE 注释行（前端忽略），防止代理/网关因空闲超时
                    # 断开长下载（单文件最长 120s 无事件）期间的连接
                    if time.time() - last_emit >= 10:
                        yield ": keepalive\n\n"
                        last_emit = time.time()
                    time.sleep(0.2)
                    continue
                # 已被新连接接管：把事件还给队列，让新连接继续处理
                if _stale():
                    q.put((event_type, data))
                    break
                last_emit = time.time()

                if event_type == "token":
                    accumulated = data
                    self._stream_content[cid] = accumulated
                    yield from _emit("token", {"content": format_bubble("", accumulated, streaming=True)})
                elif event_type == "append":
                    accumulated += data
                    self._stream_content[cid] = accumulated
                    yield from _emit("token", {"content": format_bubble("", accumulated, streaming=True)})
                elif event_type == "pause":
                    payload = data if isinstance(data, dict) else {}
                    pairs = payload.get("pairs", [])
                    if pairs:
                        # 保存待选配对，供 chat_resume 根据用户选择索引恢复
                        self._get_conv_state(cid)["pending_pairs"] = pairs
                        yield from _emit("pause", {"pairs": pairs})
                        paused = True
                        return
                    if payload.get("type") == "approval":
                        # 通用审批节点（技术方案 3.4a）：保存待处理载荷供 chat_resume 校验
                        self._get_conv_state(cid)["pending_approval"] = payload
                        yield from _emit("pause", {"approval": payload})
                        paused = True
                        return
                    # 既无 pairs 也非 approval：维持现有行为，直接放行
                    self._pause_responses[cid] = None
                    pause_event.set()
                elif event_type == "workflow":
                    yield from _emit("workflow", {"steps": self.get_workflow_status(cid)})
                elif event_type == "log":
                    yield from _emit("log", {"text": data})
                elif event_type == "done":
                    break
                elif event_type == "error":
                    accumulated += f"\n\n⚠️ 执行出错：{data}"
                    yield from _emit("done", {"content": format_bubble("", accumulated, streaming=False)})
                    return

            elapsed = time.time() - start_time
            final = format_bubble("", accumulated, streaming=False, elapsed=elapsed)
            yield from _emit("done", {"content": final})
            if pid:
                convs = self.load_conversations()
                if cid in convs.get(pid, {}):
                    convs[pid][cid]["messages"][-1]["content"] = final
                    self._save_history(pid, cid, convs[pid][cid]["messages"])
                    saved_normally = True
        except Exception as e:
            yield from _emit("error", {"message": str(e)})
        finally:
            # 异常结束（断线/报错/被接管等）时兜底持久化已累积内容，
            # 保证重新进入该对话时气泡不会因没走完 done 而消失
            if not saved_normally and accumulated and pid:
                try:
                    convs = self.load_conversations()
                    if cid in convs.get(pid, {}):
                        msgs = convs[pid][cid].get("messages", [])
                        if msgs and msgs[-1].get("role") == "assistant":
                            msgs[-1]["content"] = format_bubble("", accumulated, streaming=False)
                            self._save_history(pid, cid, msgs)
                except Exception:
                    pass
            # 只有当前代际的连接才清理对话流状态（被接管的旧连接不清理）
            if not paused and self._stream_gen.get(cid, 0) == gen:
                self._stream_queues.pop(cid, None)
                self._pause_events.pop(cid, None)
                self._pause_responses.pop(cid, None)
                self._agent_threads.pop(cid, None)
                self._stream_starts.pop(cid, None)
                self._stream_gen.pop(cid, None)
                # _stream_content 保留：流结束后的重连用于补齐完整气泡

    def chat_resume(self, cid: str, payload: dict) -> dict:
        """恢复被暂停的流，支持两种协议（技术方案 3.4b）。

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
# 占满 FastAPI 线程池、饿死其他 API 请求（见 render_layer_tile）
_TILE_RENDER_SEM = threading.BoundedSemaphore(3)

backend = AppBackend()

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import urllib.parse

app = FastAPI(title="GeoThermoAI Vue3 后端")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 鉴权中间件：/api/* 除白名单外必须携带有效 JWT ──────────────
# token 来源：Authorization: Bearer <token>，或 SSE/图片等无法带 Header 时用 ?token=
_AUTH_WHITELIST = {"/api/auth/login", "/api/auth/register", "/api/health"}


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
def login(payload: dict):
    user = auth.authenticate(payload.get("username", ""), payload.get("password", ""))
    if not user:
        return {"ok": False, "message": "账号或密码错误"}
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
    for f in files:
        content = await f.read()
        results.append(backend.save_uploaded_file(f.filename or "file", content))
    return {"ok": True, "message": "\n".join(results), "study_areas": backend.list_study_areas()}


@app.get("/api/study-areas")
def study_areas():
    return {"study_areas": backend.list_study_areas()}


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
    png = backend.render_layer_tile(layer_id, conv or None, z, x, y)
    if png is None:
        return Response(status_code=204)
    return Response(content=png, media_type="image/png")


@app.get("/api/map/html", response_class=HTMLResponse)
def map_html(conv: str = ""):
    return backend.build_map_html(conv or None)


# ── API：聊天 SSE ──────────────────────────────────────────────

@app.post("/api/chat/start")
def chat_start(payload: dict):
    return backend.chat_start(
        payload.get("project", ""), payload.get("conv", ""), payload.get("message", ""),
        exec_mode=payload.get("exec_mode", ""),
    )


@app.get("/api/chat/stream")
def chat_stream(conv: str = ""):
    if not conv:
        return JSONResponse({"error": "缺少 conv"}, status_code=400)
    return _sse(backend.chat_stream(conv))


@app.get("/api/chat/streaming")
def chat_streaming(conv: str = ""):
    """查询该对话当前是否有正在运行的流（前端重新进入对话时用于恢复订阅）"""
    return {"active": bool(conv) and conv in backend._stream_queues}


@app.get("/api/chat/current")
def chat_current(conv: str = ""):
    """返回该对话当前流式生成的累积内容（切回对话时立即显示最新气泡，
    避免先显示会话文件中的旧快照再等 SSE 慢慢同步）"""
    if not conv:
        return {"active": False, "content": ""}
    content = backend._stream_content.get(conv, "")
    return {
        "active": conv in backend._stream_queues,
        "content": format_bubble("", content, streaming=False) if content else "",
    }


@app.post("/api/chat/resume")
def chat_resume(payload: dict):
    return backend.chat_resume(payload.get("conv", ""), payload)


# ── API：文件列表 / 下载（根治"无权限"） ───────────────────────

@app.get("/api/files")
def files(project_dir: str = ""):
    return backend.list_project_files(project_dir)


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


# ── API：测试 ──────────────────────────────────────────────────

@app.post("/api/test/planetary")
def test_planetary():
    return {"result": backend.test_planetary_connection()}


@app.post("/api/test/gdal")
def test_gdal():
    return {"result": backend.test_gdal_status()}


# ── 静态资源（Vue 构建产物） ───────────────────────────────────

_DIST = _ROOT / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")


# ── 入口 ───────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="info")


if __name__ == "__main__":
    main()
