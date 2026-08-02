"""
GeoThermoAI Gradio API 适配层

将旧版 PyWebView API（ui/api.py）的业务逻辑适配到 Gradio 事件处理：
- 对话管理：JSON 文件持久化（data/conversations/{conv_id}.json）
- 流式对话：threading + queue 实现 generator 流式输出
- Agent 集成：on_token / pause_callback / workflow_callback
- 项目目录管理：内存状态隔离（按对话）
- 研究区上传：gr.File → base64 → 保存
- 精度评估/工作流状态：复用旧版逻辑
- 地图渲染：调用 core.visualization.LayerVisualizer
"""

import base64
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.ai_assistant import GeoThermoAI_Assistant
from core.skills.skill_registry import SkillRegistry
from core.agent.geo_thermo_agent import GeoThermoAgent
from core.visualization import LayerVisualizer

# 工作流步骤（与旧版一致）
WORKFLOW_STEPS = [
    "data_acquisition",
    "data_pipeline",
    "ttri_compute",
    "rf_model",
    "tcr_compute",
    "lst_export",
    "accuracy_eval",
]

# Agent 命令关键词（与旧版一致）
_AGENT_KEYWORDS = [
    "处理", "训练", "下载", "执行", "运行", "生成",
    "全流程", "一键", "开始", "计算", "导出", "评估",
]

# 工作流类命令关键词（必须已上传研究区+已设置项目目录）
_WORKFLOW_KEYWORDS = ["全流程", "一键", "跑完全流程", "执行全流程", "处理", "下载", "获取"]

SYSTEM_PROMPT = (
    "你是GeoThermoAI智能助手，专注地表温度降尺度。"
    "核心算法：TTRI（地形热响应指数）、TCR（热约束残差）。"
    "请用简洁专业的中文回答。"
)


def _is_agent_command(message: str) -> bool:
    return any(kw in message for kw in _AGENT_KEYWORDS)


def _is_workflow_command(message: str) -> bool:
    return any(kw in message for kw in _WORKFLOW_KEYWORDS)


def format_bubble(thinking: str, content: str, streaming: bool = False, elapsed: float = 0) -> str:
    """生成聊天气泡 HTML：可折叠思考链 + 正文"""
    parts = []
    thinking = (thinking or "").strip()
    content = content or ""
    if thinking:
        label = "思考中…" if (streaming and not content) else f"已深度思考（{elapsed:.1f}s）"
        o = " open" if (streaming and not content) else ""
        parts.append(
            f"<details{o}><summary>💭 {label}</summary>\n\n{thinking}\n\n</details>"
        )
    if content:
        parts.append(content)
    elif streaming:
        parts.append("▍")
    return "\n\n".join(parts)


def strip_thinking(text: str) -> str:
    """从消息内容中剥离 <details> 思考链，供 LLM 上下文使用"""
    return re.sub(r"<details[^>]*>.*?</details>", "", text or "", flags=re.DOTALL).strip()


class GradioAPI:
    """Gradio 事件处理 API

    核心数据结构（与 demo app.py 一致）：
    - convs_state (gr.State): {项目名: {conv_id: {title, messages, starred}, "__dir__": path}}
    - current_state (gr.State): {"project": pid, "conv": cid}
    - settings_state (gr.State): {api_format, base_url, api_key, model_id, ...}
    """

    def __init__(self):
        # 加载 settings.json
        settings = self._load_settings()
        api_config = settings.get("api", {})

        # 初始化 AI 助手
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
        self.registry.load_third_party_skills(str(_ROOT / "skills"))

        # 对话持久化目录
        self._conversations_dir = _ROOT / "data" / "conversations"
        self._conversations_dir.mkdir(parents=True, exist_ok=True)
        self._study_areas_dir = _ROOT / "config" / "study_areas"
        self._study_areas_dir.mkdir(parents=True, exist_ok=True)

        # 按对话隔离的运行时状态
        self._conv_states: Dict[str, dict] = {}
        # 流式队列（generator 读取，agent 线程写入）
        self._stream_queues: Dict[str, "queue.Queue"] = {}
        # 用户输入事件（暂停/恢复）
        self._pause_events: Dict[str, threading.Event] = {}
        self._pause_responses: Dict[str, Any] = {}
        # Agent 线程引用 + 流式开始时间（用于 pause 后恢复消费）
        self._agent_threads: Dict[str, threading.Thread] = {}
        self._stream_starts: Dict[str, float] = {}
        # 已删除对话标记（防止运行中的 agent 线程通过 _save_history 重建磁盘文件）
        self._deleted_convs: set = set()

    # ── 内部工具 ───────────────────────────────────────────────

    def _get_conv_state(self, conv_id: str) -> dict:
        if conv_id not in self._conv_states:
            self._conv_states[conv_id] = {
                "project_dir": "",
                "workflow_progress": {
                    "status": "idle",
                    "current_step": "",
                    "current_index": -1,
                    "steps": [],
                },
                "thumbnail_cache": {},
            }
        return self._conv_states[conv_id]

    def _register_builtin_skills(self):
        """注册 8 个内置 Skill"""
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
        self.registry.register(DataAcquisitionSkill())
        self.registry.register(DataPipelineSkill())
        self.registry.register(TTRIComputeSkill())
        self.registry.register(RFModelSkill())
        self.registry.register(TCRComputeSkill())
        self.registry.register(LSTExportSkill())
        self.registry.register(AccuracyEvalSkill())
        self.registry.register(AIAssistantSkill())

    def _load_settings(self) -> dict:
        path = _ROOT / "config" / "settings.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self, settings: dict):
        path = _ROOT / "config" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)

    # ── 项目/对话管理 ───────────────────────────────────────────

    def load_conversations_from_disk(self) -> dict:
        """启动时从磁盘重建 convs_state"""
        convs: Dict[str, dict] = {}
        if not self._conversations_dir.exists():
            return convs
        for f in self._conversations_dir.glob("*.json"):
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
                # 恢复项目目录
                if data.get("project_dir"):
                    convs[project].setdefault("__dir__", data["project_dir"])
                    self._get_conv_state(conv_id)["project_dir"] = data["project_dir"]
            except Exception:
                continue
        return convs

    def _persist_conversation(self, conv_id: str, project: str, title: str,
                                messages: list, project_dir: str = ""):
        path = self._conversations_dir / f"{conv_id}.json"
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

    def create_project(self, name: str, convs: dict) -> Tuple[Any, dict, str]:
        name = (name or "").strip()
        if not name:
            gr_warning("请输入项目名称")
            return gr_update(), convs, ""
        if name in convs:
            gr_warning("项目已存在")
            return gr_update(), convs, ""
        convs = dict(convs)
        convs[name] = {}
        gr_info(f"项目「{name}」创建成功")
        return gr_update(choices=list(convs.keys()), value=name), convs, ""

    def select_project(self, pid: str, convs: dict) -> Tuple[Any, str]:
        if not pid or pid not in convs:
            return gr_update(choices=[], value=None), ""
        conv = convs[pid]
        choices = [(v["title"], k) for k, v in conv.items() if not k.startswith("__")]
        return gr_update(choices=choices, value=None), conv.get("__dir__", "")

    def create_conversation(self, pid: str, title: str, convs: dict) -> Tuple[Any, dict, str]:
        title = (title or "").strip()
        if not pid or pid not in convs:
            gr_warning("请先选择项目")
            return gr_update(), convs, ""
        if not title:
            gr_warning("请输入对话标题")
            return gr_update(), convs, ""
        conv_id = uuid.uuid4().hex[:12]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        convs = dict(convs)
        convs[pid] = dict(convs[pid])
        convs[pid][conv_id] = {
            "title": title,
            "messages": [],
            "starred": False,
            "created_at": now,
            "updated_at": now,
        }
        # 初始化隔离状态
        self._get_conv_state(conv_id)
        # 持久化到磁盘
        self._persist_conversation(conv_id, pid, title, [], convs[pid].get("__dir__", ""))
        choices = [(v["title"], k) for k, v in convs[pid].items() if not k.startswith("__")]
        gr_info(f"对话「{title}」创建成功")
        return gr_update(choices=choices, value=conv_id), convs, ""

    def select_conversation(self, cid: str, pid: str, convs: dict, current: dict) -> Tuple[list, dict, str]:
        if not pid or not cid or pid not in convs or cid not in convs[pid]:
            return [], current, "未选择对话"
        current = {"project": pid, "conv": cid}
        msgs = convs[pid][cid].get("messages", [])
        title = convs[pid][cid].get("title", "")
        # 恢复项目目录到隔离状态（确保 agent 能读取）
        project_dir = convs[pid].get("__dir__", "")
        self._get_conv_state(cid)["project_dir"] = project_dir
        return msgs, current, f"📄 {pid} / {title}"

    def save_project_dir(self, pid: str, path: str, convs: dict) -> Tuple[dict, str]:
        if not pid or pid not in convs:
            gr_warning("请先选择项目")
            return convs, ""
        path = (path or "").strip()
        # 目录不存在时自动创建（Studio 容器上无法手动建目录，如 /home/studio_service/PROJECT/output/...）
        if path:
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as e:
                gr_warning(f"无法创建目录：{path}（{e}）")
                return convs, ""
        normalized = path.replace("\\", "/")
        convs = dict(convs)
        convs[pid] = dict(convs[pid])
        convs[pid]["__dir__"] = normalized
        # 同步到该项目下所有对话的隔离状态 + 持久化 project_dir 到磁盘
        for cid, cdata in convs[pid].items():
            if cid.startswith("__"):
                continue
            if cid in self._conv_states:
                self._conv_states[cid]["project_dir"] = normalized
            # 更新磁盘 JSON 文件中的 project_dir 字段
            try:
                self._update_conversation_file(cid, pid, project_dir=normalized)
            except Exception:
                pass
        gr_info("项目目录已保存")
        return convs, normalized

    def _update_conversation_file(self, conv_id: str, project: str, **updates):
        """局部更新对话 JSON 文件中的字段（不修改 messages）"""
        path = self._conversations_dir / f"{conv_id}.json"
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

    def delete_conversation(self, cid: str, pid: str, convs: dict, current: dict) -> Tuple[dict, dict, list, str, Any]:
        """删除对话：彻底清除磁盘文件、内存状态、运行中的 agent 进程

        用户要求：删除对话 = 清除该对话所有进程和记忆。
        因此除删文件外，还要：唤醒并终止运行中的 agent 线程、
        打上已删除标记防止线程回调重建文件、清理全部隔离状态。
        """
        if not pid or not cid or pid not in convs or cid not in convs[pid]:
            return convs, current, [], "未选择对话", gr_update()
        # 打上删除标记：_save_history / _persist 将不再为此对话写文件
        self._deleted_convs.add(cid)
        # 唤醒阻塞中的 agent 线程（pause_event.wait），让它尽快退出
        ev = self._pause_events.get(cid)
        if ev:
            self._pause_responses.pop(cid, None)
            try:
                ev.set()
            except Exception:
                pass
        # 删除磁盘文件（失败时重试一次）
        path = self._conversations_dir / f"{cid}.json"
        delete_err = ""
        if path.exists():
            try:
                os.remove(path)
            except Exception as e1:
                try:
                    time.sleep(0.3)
                    os.remove(path)
                except Exception as e2:
                    delete_err = str(e2)
        # 清理内存
        convs = dict(convs)
        convs[pid] = dict(convs[pid])
        del convs[pid][cid]
        self._conv_states.pop(cid, None)
        self._stream_queues.pop(cid, None)
        self._pause_events.pop(cid, None)
        self._pause_responses.pop(cid, None)
        self._agent_threads.pop(cid, None)
        self._stream_starts.pop(cid, None)
        # 清空当前选择
        current = {"project": pid, "conv": None}
        choices = [(v["title"], k) for k, v in convs[pid].items() if not k.startswith("__")]
        if delete_err:
            gr_warning(f"对话已从界面删除，但磁盘文件删除失败：{delete_err}（重启应用后会自动清除）")
        else:
            gr_info("对话已彻底删除")
        return convs, current, [], "未选择对话", gr_update(choices=choices, value=None)

    def reload_conversations(self) -> Tuple[dict, Any, Any, str]:
        """页面加载/刷新时从磁盘重新加载对话，保证删除/创建与磁盘一致

        返回 (convs_state, proj_select_update, conv_select_update, proj_dir)
        """
        convs = self.load_conversations_from_disk()
        auto_project = next(iter(convs), None)
        auto_conv_choices = []
        proj_dir = ""
        if auto_project:
            auto_conv_choices = [
                (v["title"], k) for k, v in convs[auto_project].items()
                if not k.startswith("__")
            ]
            proj_dir = convs[auto_project].get("__dir__", "")
        return (
            convs,
            gr_update(choices=list(convs.keys()), value=auto_project),
            gr_update(choices=auto_conv_choices, value=None),
            proj_dir,
        )

    # ── API 设置 ───────────────────────────────────────────────

    def get_initial_settings(self) -> dict:
        """启动时从 settings.json 加载，供 settings_state 初始化"""
        s = self._load_settings()
        api = s.get("api", {})
        return {
            "api_format": api.get("api_format", "openai"),
            "base_url": api.get("api_base_url", ""),
            "api_key": api.get("api_key", ""),
            "model_id": api.get("model_id", ""),
            "display_name": api.get("display_name", ""),
            "context_input": api.get("context_input", 128000),
            "context_output": api.get("context_output", 16000),
        }

    def get_initial_api_form_values(self) -> dict:
        """供 UI 组件初始化"""
        s = self.get_initial_settings()
        fmt_label = (
            "Anthropic Messages 格式" if s["api_format"] == "anthropic"
            else "OpenAI Chat Completions 格式"
        )
        return {
            "fmt": fmt_label,
            "base_url": s["base_url"],
            "model_id": s["model_id"],
            "api_key": s["api_key"],
            "display_name": s["display_name"],
            "context_input": s["context_input"],
            "context_output": s["context_output"],
        }

    def save_api_settings(self, fmt_label: str, base_url: str, api_key: str,
                            model_id: str, display_name: str, ctx_in: int, ctx_out: int,
                            settings: dict) -> Tuple[dict, str, str]:
        model_id = (model_id or "").strip()
        display_name = (display_name or "").strip() or model_id
        api_format = "anthropic" if "Anthropic" in (fmt_label or "") else "openai"
        base_url = (base_url or "").strip().rstrip("/")
        api_key = (api_key or "").strip()

        # 更新运行时 assistant
        self.assistant.api_format = api_format
        self.assistant.api_base_url = base_url
        self.assistant.api_key = api_key
        self.assistant.model_id = model_id
        self.assistant.model_display_name = display_name or model_id
        self.assistant.system_prompt = self.assistant._build_system_prompt()

        # 更新 settings_state
        settings = dict(settings)
        settings.update({
            "api_format": api_format,
            "base_url": base_url,
            "api_key": api_key,
            "model_id": model_id,
            "display_name": display_name,
            "context_input": ctx_in,
            "context_output": ctx_out,
        })

        # 持久化到 settings.json
        s = self._load_settings()
        s.setdefault("api", {})
        s["api"].update({
            "api_format": api_format,
            "api_base_url": base_url,
            "api_key": api_key,
            "model_id": model_id,
            "display_name": display_name,
            "context_input": ctx_in,
            "context_output": ctx_out,
        })
        self._save_settings(s)

        label = f"🟢 当前模型：**{display_name}**" if model_id else "⚪ 未配置模型"
        return settings, label, "✅ API 设置已保存并应用"

    # ── 研究区上传 ───────────────────────────────────────────────

    def upload_study_area(self, file_objs: list) -> str:
        """接收 gr.File 上传的文件对象列表，保存到 config/study_areas/

        支持单个 .geojson/.json 文件，或 .shp + .dbf/.shx/.prj 组合
        """
        if not file_objs:
            return "⚠️ 未选择文件"
        self._study_areas_dir.mkdir(parents=True, exist_ok=True)
        results = []
        shp_main = None
        for fobj in file_objs:
            src_path = fobj.name if hasattr(fobj, "name") else str(fobj)
            fname = os.path.basename(src_path)
            ext = os.path.splitext(fname)[1].lower()
            dest = self._study_areas_dir / fname
            try:
                with open(src_path, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                results.append(f"✓ {fname}")
                if ext == ".shp":
                    shp_main = str(dest)
            except Exception as e:
                results.append(f"✗ {fname}: {e}")
        # 若是 shapefile，转换为 geojson
        if shp_main:
            gj_path = self._study_areas_dir / (
                os.path.splitext(os.path.basename(shp_main))[0] + ".geojson"
            )
            ok, msg = self._shp_to_geojson(shp_main, str(gj_path))
            if ok:
                results.append(f"✓ 已转换为 {gj_path.name}")
            else:
                results.append(f"⚠️ Shapefile 转换失败: {msg}")
        gr_info(f"已上传 {len(file_objs)} 个文件")
        return "\n".join(results)

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

    def list_study_areas(self) -> str:
        if not self._study_areas_dir.exists():
            return "未上传研究区文件"
        files = sorted(self._study_areas_dir.glob("*.geojson"))
        if not files:
            return "未上传研究区文件"
        latest = max(files, key=lambda p: p.stat().st_mtime)
        return f"✓ {latest.name}（共 {len(files)} 个）"

    # ── 工作流状态 / 精度评估 ───────────────────────────────────

    def get_workflow_status(self, current: dict) -> list:
        """获取当前对话的工作流进度，返回 dataframe 数据"""
        cid = current.get("conv") if current else None
        if not cid:
            return [[step, "⏳"] for step in WORKFLOW_STEPS]
        state = self._get_conv_state(cid)
        wp = state["workflow_progress"]
        steps_map = {s["name"]: s["status"] for s in wp.get("steps", [])}
        rows = []
        status_icon = {"completed": "✅", "running": "🔄", "failed": "❌", "pending": "⏳"}
        for step in WORKFLOW_STEPS:
            st = steps_map.get(step, "⏳") if wp.get("status") != "idle" else "⏳"
            if st not in status_icon:
                st = "⏳"
            rows.append([step, status_icon.get(st, "⏳")])
        return rows

    def get_accuracy_summary(self, current: dict) -> list:
        """读取当前对话项目目录下的精度评估结果"""
        cid = current.get("conv") if current else None
        if not cid:
            return [["R²", "—"], ["RMSE", "—"], ["MAE", "—"], ["样本数", "—"]]
        state = self._get_conv_state(cid)
        project_dir = state["project_dir"]
        if not project_dir:
            return [["R²", "—"], ["RMSE", "—"], ["MAE", "—"], ["样本数", "—"]]
        import glob
        results_dir = os.path.join(project_dir, "results")
        files = sorted(glob.glob(os.path.join(results_dir, "spatial_consistency_*.json")))
        if not files:
            return [["R²", "—"], ["RMSE", "—"], ["MAE", "—"], ["样本数", "—"]]
        try:
            with open(files[-1], encoding="utf-8") as f:
                data = json.load(f)
            sc = data.get("spatial_consistency", {})
            vr = data.get("value_range", {})
            dev = vr.get("deviation", {})
            # 读取 rf_model 测试指标
            test_r2 = 0
            test_rmse = 0
            test_mae = 0
            metrics_files = sorted(glob.glob(os.path.join(results_dir, "test", "rf_ttri_predict_*.json")))
            if metrics_files:
                with open(metrics_files[-1], encoding="utf-8") as f2:
                    mdata = json.load(f2)
                m = mdata.get("metrics", {})
                test_r2 = m.get("R2", 0)
                test_rmse = m.get("RMSE", 0)
                test_mae = m.get("MAE", 0)
            m_metrics = sc.get("metrics", {})
            return [
                ["测试 R²", f"{test_r2:.4f}" if test_r2 else "—"],
                ["测试 RMSE", f"{test_rmse:.4f}" if test_rmse else "—"],
                ["测试 MAE", f"{test_mae:.4f}" if test_mae else "—"],
                ["空间一致性 MB", f"{m_metrics.get('MB', 0):.4f}"],
                ["空间一致性 MAE", f"{m_metrics.get('MAE', 0):.4f}"],
                ["空间一致性 RMSE", f"{m_metrics.get('RMSE', 0):.4f}"],
                ["最大绝对偏差", f"{dev.get('max_abs_deviation', 0):.4f}"],
                ["通过验证", "✅ 是" if dev.get("passed", False) else "❌ 否"],
                ["匹配样本数", str(sc.get("n_matched", 0))],
            ]
        except Exception:
            return [["R²", "—"], ["RMSE", "—"], ["MAE", "—"], ["样本数", "—"]]

    # ── 地图渲染 ───────────────────────────────────────────────

    def build_map_html(self, current: dict) -> str:
        cid = current.get("conv") if current else None
        project_dir = ""
        if cid:
            project_dir = self._get_conv_state(cid).get("project_dir", "")
        if not project_dir or not os.path.isdir(project_dir):
            return LayerVisualizer.build_empty_map()
        return LayerVisualizer.build_map(project_dir)

    # ── 数据源连通性测试 ──────────────────────────────────────────

    def test_planetary_connection(self) -> str:
        """测试 Microsoft Planetary Computer 连通性（HTTP 探测 + STAC 目录 + 影像搜索）

        与 data_acquisition 实际下载链路使用相同端点与配置，
        用于在 Studio 上快速定位"未找到影像配对"是否由容器网络导致。
        """
        import time as _time
        import requests as _requests
        from pystac_client import Client
        from pystac_client.stac_api_io import StacApiIO

        _STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
        _bbox = [113.7, 29.9, 114.9, 31.3]  # 武汉
        lines = []

        # 1. HTTP 探测根端点
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

        # 2. 打开目录（与下载链路相同的 StacApiIO 配置）
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

        # 3. 影像搜索（武汉附近 Landsat，验证完整查询链路）
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

        lines.append("\n---\n若 1-3 全部通过：容器网络正常，"
                     "「未找到影像配对」需结合完整气泡日志继续排查；"
                     "若第 1 步就失败：容器无法访问 Planetary Computer，属网络问题。")
        return "\n".join(lines)

    def test_gdal_status(self) -> str:
        """测试 GDAL（osgeo）环境：导入、坐标转换、栅格读写、Warp 重投影

        覆盖 data_acquisition 实际用到的核心能力，
        用于确认捆绑版 GDAL wheel 在 Studio 上是否真正可用。
        """
        lines = []

        # 1. 导入 osgeo（捆绑 libgdal 加载失败会在这里暴露）
        try:
            from osgeo import gdal, osr
            ver = gdal.VersionInfo("RELEASE_NAME")
            lines.append(f"✅ 1. osgeo 导入成功（GDAL {ver}）")
        except Exception as e:
            lines.append(f"❌ 1. osgeo 导入失败：{e}")
            lines.append("\n---\nGDAL 环境异常：检查 wheels/GDAL-*.whl 是否正确安装"
                         "（捆绑版 wheel 应自带 libgdal 运行库，不依赖系统版本）。")
            return "\n".join(lines)

        # 2. 坐标转换（代码使用 osr.CoordinateTransformation）
        try:
            import math as _math
            srs_wgs = osr.SpatialReference()
            srs_wgs.ImportFromEPSG(4326)
            srs_utm = osr.SpatialReference()
            srs_utm.ImportFromEPSG(32650)
            ct = osr.CoordinateTransformation(srs_wgs, srs_utm)
            x, y, _z = ct.TransformPoint(114.3, 30.59)
            if _math.isfinite(x) and _math.isfinite(y):
                lines.append(f"✅ 2. 坐标转换正常（WGS84→UTM50N: ({x:.1f}, {y:.1f})）")
            else:
                lines.append(f"❌ 2. 坐标转换返回异常值（({x}, {y})）")
                lines.append("   可能原因：PROJ 数据库（proj.db）与 libproj 版本不匹配，"
                             "或 PROJ_LIB 指向错误。")
        except Exception as e:
            lines.append(f"❌ 2. 坐标转换失败：{e}")

        # 3. 栅格创建/写入/回读（/vsimem 内存文件，避免磁盘写入）
        try:
            path = "/vsimem/gdal_selftest.tif"
            drv = gdal.GetDriverByName("GTiff")
            ds = drv.Create(path, 4, 4, 1, gdal.GDT_Float32,
                            options=["COMPRESS=LZW"])
            ds.SetGeoTransform([100, 30, 0, 200, 0, -30])
            ds.GetRasterBand(1).Fill(2.0)
            ds.FlushCache()
            ds = None
            ds2 = gdal.Open(path)
            arr = ds2.GetRasterBand(1).ReadAsArray()
            ok = arr is not None and float(arr.sum()) == 2.0 * 16
            ds2 = None
            gdal.Unlink(path)
            lines.append(f"✅ 3. 栅格创建/读写正常（{'回读校验通过' if ok else '回读数值异常'}）")
        except Exception as e:
            lines.append(f"❌ 3. 栅格创建/读写失败：{e}")

        # 4. gdal.Warp 重投影（data_acquisition 的 mosaic/投影核心操作）
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
                raise RuntimeError(
                    f"Warp 输出为空（{dout.RasterXSize if dout else 0}x{dout.RasterYSize if dout else 0}）")
            if not dout.GetProjection():
                raise RuntimeError("Warp 输出缺少投影信息")
            dout = None
            gdal.Unlink(src)
            gdal.Unlink(dst)
            lines.append("✅ 4. gdal.Warp 重投影正常（输出含 UTM 投影）")
        except Exception as e:
            lines.append(f"❌ 4. gdal.Warp 重投影失败：{e}")

        _failed = sum(1 for _l in lines if _l.startswith("❌"))
        if _failed:
            lines.append(f"\n---\n共 {_failed} 项失败：GDAL 环境有问题。"
                         f"若在 Studio 上出现，请确认 wheels/GDAL-*.whl 已随项目上传并安装"
                         f"（捆绑版 wheel 自带 libgdal/proj.db，理论上自包含可运行）。")
        else:
            lines.append("\n---\n1-4 全部通过：GDAL 环境正常。")
        return "\n".join(lines)

    # ── 文件下载 ──────────────────────────────────────────────

    def list_project_files(self, project_dir: str) -> Tuple[Any, str, str]:
        """递归列出项目目录下的所有文件（含子目录，相对路径），供下载列表选择"""
        project_dir = (project_dir or "").strip()
        if not project_dir or not os.path.isdir(project_dir):
            return gr_update(choices=[], value=None), \
                f"📂 当前项目目录：{project_dir or '（未设置）'}", \
                "❌ 目录不存在或未设置：请先在左侧「项目保存路径」填写并点击保存。"
        files = []  # (相对路径, 大小)
        for root, _dirs, names in os.walk(project_dir):
            for name in sorted(names):
                full = os.path.join(root, name)
                try:
                    if os.path.isfile(full):
                        rel = os.path.relpath(full, project_dir).replace("\\", "/")
                        files.append((rel, os.path.getsize(full)))
                except OSError:
                    continue
        files.sort(key=lambda x: x[0].lower())
        if files:
            # 下拉选项：显示 相对路径（大小），值为相对路径；默认选中第一个，保证列表立即可见
            choices = [(f"{rel}（{_fmt_size(size)}）", rel) for rel, size in files]
            status = f"✅ 共发现 {len(files)} 个文件（含子目录，显示为「子目录/文件名」），可直接选择下载。"
        else:
            choices = []
            status = "⚠️ 项目目录为空，暂无文件可下载（先跑一次流程生成结果）。"
        return gr_update(choices=choices, value=files[0][0] if files else None), \
            f"📂 当前项目目录：{project_dir}", status

    def prepare_download(self, project_dir: str, rel_path: str) -> Tuple[str, str]:
        """校验选中文件并生成下载链接（新标签页打开，绕过创空间 iframe 拦截）"""
        project_dir = (project_dir or "").strip()
        rel_path = (rel_path or "").strip()
        if not rel_path:
            return _dl_html(""), "请先在文件列表中选择要下载的文件。"
        if not project_dir or not os.path.isdir(project_dir):
            return _dl_html(""), "❌ 项目目录不存在或未设置，请先保存项目路径。"
        base = os.path.realpath(project_dir)
        target = os.path.realpath(os.path.join(base, rel_path))
        # 防目录穿越：目标必须位于项目目录内
        if not (target == base or target.startswith(base + os.sep)):
            return _dl_html(""), f"❌ 非法路径：{rel_path}"
        if not os.path.isfile(target):
            return _dl_html(""), f"❌ 文件不存在：{rel_path}"
        try:
            # 复制到临时目录（保留子目录结构，避免同名冲突）
            # 该目录已在 app.py 的 launch(allowed_paths=...) 中登记，文件接口可访问
            dl_dir = os.path.join(tempfile.gettempdir(), "geothermoai_downloads")
            dst = os.path.join(dl_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(target, dst)
        except Exception as e:
            return _dl_html(""), f"❌ 准备下载失败：{e}"
        size = _fmt_size(os.path.getsize(dst))
        return _dl_html(dst, rel_path, size), f"✅ 已就绪：{rel_path}（{size}），点击上方下载链接。"

    # ── 流式对话核心 ───────────────────────────────────────────

    def user_send(self, msg: str, history: list) -> Tuple[str, list]:
        """用户发送消息：清空输入框，追加用户消息到历史"""
        if not msg or not msg.strip():
            return gr_update(), history
        return "", history + [{"role": "user", "content": msg}]

    def bot_respond(self, history: list, settings: dict, convs: dict, current: dict):
        """流式生成 AI/Agent 回复

        generator：每次 yield (history, convs, workflow_status_rows, accuracy_rows,
                            pair_box_update, pair_radio_update)

        当 Agent 触发暂停（选择影像配对）时，generator 保存上下文后结束，
        显示配对选择 UI；用户选择后由 resume_pair_select 恢复。
        """
        # 初始化 yield（显示 "▍"），同时确保配对选择框隐藏
        history = history + [{"role": "assistant", "content": "▍"}]
        yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()

        # 检查当前对话
        cid = current.get("conv") if current else None
        pid = current.get("project") if current else None
        if not cid or not pid or pid not in convs or cid not in convs[pid]:
            history[-1]["content"] = "⚠️ 请先选择一个对话"
            yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
            return

        # 获取用户消息
        if len(history) < 2:
            return
        user_msg = history[-2]["content"]
        if isinstance(user_msg, list):
            user_msg = "\n".join(str(x) for x in user_msg)
        user_msg = strip_thinking(user_msg)

        # 检查 API 配置
        if not settings.get("api_key") or not settings.get("base_url") or not settings.get("model_id"):
            history[-1]["content"] = "⚠️ 请先在右侧「🔑 API 设置」配置模型。"
            self._save_history(convs, current, history)
            yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
            return

        # 项目目录（从隔离状态读取）
        conv_state = self._get_conv_state(cid)
        project_dir = conv_state["project_dir"]

        # 工作流类命令的前置检查
        is_workflow = _is_workflow_command(user_msg) and _is_agent_command(user_msg)
        if is_workflow:
            # 检查研究区
            uploaded = list(self._study_areas_dir.glob("*.geojson")) if self._study_areas_dir.exists() else []
            if not uploaded:
                history[-1]["content"] = (
                    "⚠️ 请先上传研究区文件（Shapefile 或 GeoJSON），然后再发送指令。\n\n"
                    "操作路径：**左栏 → 研究区域 → 上传**"
                )
                self._save_history(convs, current, history)
                yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
                return
            # 检查项目目录
            if not project_dir or not os.path.isdir(project_dir):
                history[-1]["content"] = (
                    "⚠️ 请先设置项目保存路径，然后再执行一键全流程。\n\n"
                    "操作路径：**左栏 → 项目保存路径 → 输入路径并保存**"
                )
                self._save_history(convs, current, history)
                yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
                return

        # 准备上下文
        prior_messages = []
        for m in history[:-2]:
            raw = m["content"]
            if isinstance(raw, list):
                raw = "\n".join(str(x) for x in raw)
            c = strip_thinking(raw)
            if c:
                role = "assistant" if m["role"] == "assistant" else "user"
                prior_messages.append({"role": role, "content": c})

        # 创建流式队列
        q: "queue.Queue" = queue.Queue()
        self._stream_queues[cid] = q
        pause_event = threading.Event()
        self._pause_events[cid] = pause_event

        # 后台线程：执行 agent 或 LLM 流式调用
        def _runner():
            try:
                if _is_agent_command(user_msg):
                    def on_token(content: str):
                        q.put(("token", content))

                    def pause_callback(pause_data):
                        q.put(("pause", pause_data))
                        # 等待用户响应（带超时 5 分钟，避免永久阻塞）
                        if pause_event.wait(timeout=300):
                            pause_event.clear()
                        selected = self._pause_responses.get(cid)
                        if selected is not None:
                            return {"paused": False, "data": selected}
                        # 超时或无响应，自动选第一对
                        pairs = pause_data.get("pairs", []) if isinstance(pause_data, dict) else []
                        if pairs:
                            self._pause_responses[cid] = pairs[0]
                            return {"paused": False, "data": pairs[0]}
                        return {"paused": True}

                    def workflow_callback(skill_name, status, idx, total):
                        # 更新工作流进度状态
                        wp = conv_state["workflow_progress"]
                        step_names = WORKFLOW_STEPS
                        steps = []
                        for si, sn in enumerate(step_names):
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

                    result = self.agent.process_command(
                        user_msg,
                        on_token=on_token,
                        pause_callback=pause_callback,
                        workflow_callback=workflow_callback,
                        project_dir=project_dir,
                    )
                    if result and ("⚠️" in result or "失败" in result or "未找到" in result):
                        q.put(("append", "\n\n" + result))
                    q.put(("done", None))
                else:
                    # 纯 LLM 对话
                    context = {
                        "workflow_status": conv_state["workflow_progress"],
                        "config": self._load_settings(),
                    }
                    self.assistant.ask_stream(
                        user_msg, lambda c: q.put(("token", c)),
                        context=context, prior_messages=prior_messages,
                    )
                    q.put(("done", None))
            except Exception as e:
                q.put(("error", str(e)))

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        # 保存线程引用与开始时间（用于 pause 后 resume_pair_select 恢复消费）
        self._agent_threads[cid] = thread
        start_time = time.time()
        self._stream_starts[cid] = start_time

        # 消费队列：复用 _consume_stream
        yield from self._consume_stream(cid, history, convs, current, conv_state, start_time, "")

    def _consume_stream(self, cid: str, history: list, convs: dict, current: dict,
                         conv_state: dict, start_time: float, accumulated: str):
        """消费 agent 线程写入的流式队列。

        generator：每次 yield (history, convs, workflow_status, accuracy_rows,
                            pair_box_update, pair_radio_update)

        遇到 pause 事件时保存上下文、显示配对选择 UI、return（不清理状态）。
        遇到 done/error 时正常清理。
        """
        thread = self._agent_threads.get(cid)
        q = self._stream_queues.get(cid)
        pause_event = self._pause_events.get(cid)
        if not thread or not q or not pause_event:
            return

        paused = False
        try:
            while True:
                try:
                    event_type, data = q.get(timeout=5)
                except queue.Empty:
                    # 长时间下载期间无事件：不 yield（避免高频无变化 yield 导致
                    # Gradio 事件流过载/中断），只低频检查线程是否已结束
                    if not thread.is_alive():
                        break
                    continue

                if event_type == "token":
                    accumulated = data
                    history[-1]["content"] = format_bubble("", accumulated, streaming=True)
                    self._save_history(convs, current, history)
                    yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(), gr_update()
                elif event_type == "append":
                    accumulated += data
                    history[-1]["content"] = format_bubble("", accumulated, streaming=True)
                    self._save_history(convs, current, history)
                    yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(), gr_update()
                elif event_type == "pause":
                    # 显示配对选择 UI，保存上下文，结束当前 generator
                    pairs = data.get("pairs", []) if isinstance(data, dict) else []
                    if pairs:
                        pair_text = self._format_pairs_text(pairs)
                        accumulated += f"\n\n📋 找到 {len(pairs)} 组影像配对：\n{pair_text}\n\n⏳ 请在下方选择配对后点击「确认选择」按钮继续...\n"
                        history[-1]["content"] = format_bubble("", accumulated, streaming=True)
                        self._save_history(convs, current, history)
                        # 保存上下文到 conv_state（供 resume_pair_select 使用）
                        conv_state["pending_pairs"] = pairs
                        conv_state["stream_accumulated"] = accumulated
                        # 构建 Radio 选项
                        choices = []
                        for i, p in enumerate(pairs):
                            s_date = p.get("sentinel2_date") or p.get("sentinel_date") or "?"
                            s_cov = p.get("sentinel2_coverage") or p.get("sentinel_coverage") or "?"
                            s_cnt = p.get("sentinel2_count") or p.get("sentinel_count") or "?"
                            label = (
                                f"{i+1}. Landsat {p.get('landsat_satellite', '?')} "
                                f"{p.get('landsat_date', '?')} ({p.get('landsat_count', '?')} 景, 覆盖 {p.get('landsat_coverage', '?')}%) + "
                                f"Sentinel {s_date} ({s_cnt} 景, 覆盖 {s_cov}%)"
                            )
                            choices.append((label, str(i)))
                        paused = True
                        # 显示选择卡片，结束 generator（不清理状态，agent 线程仍在 pause_event.wait 阻塞）
                        yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=True), gr_update(choices=choices, value="0", label=f"📋 找到 {len(pairs)} 组配对，请选择", interactive=True)
                        return
                    else:
                        # 无配对数据，自动恢复（让 agent 自己处理无配对情况）
                        self._pause_responses[cid] = None
                        pause_event.set()
                elif event_type == "workflow":
                    yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(), gr_update()
                elif event_type == "done":
                    break
                elif event_type == "error":
                    accumulated += f"\n\n⚠️ 执行出错：{data}"
                    history[-1]["content"] = format_bubble("", accumulated, streaming=False, elapsed=time.time() - start_time)
                    self._save_history(convs, current, history)
                    yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(), gr_update()
                    return

            # 完成
            elapsed = time.time() - start_time
            history[-1]["content"] = format_bubble("", accumulated, streaming=False, elapsed=elapsed)
            self._save_history(convs, current, history)
            yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
        except Exception as e:
            # 兜底：任何异常都显示到气泡，不让 generator 静默死亡
            accumulated += f"\n\n⚠️ 界面更新异常：{e}"
            history[-1]["content"] = format_bubble("", accumulated, streaming=False, elapsed=time.time() - start_time)
            try:
                self._save_history(convs, current, history)
            except Exception:
                pass
            yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
        finally:
            # paused=True 时跳过清理，保留 agent 线程/队列/事件供 resume 使用
            if not paused:
                self._stream_queues.pop(cid, None)
                self._pause_events.pop(cid, None)
                self._pause_responses.pop(cid, None)
                self._agent_threads.pop(cid, None)
                self._stream_starts.pop(cid, None)

    def resume_pair_select(self, history: list, convs: dict, current: dict, pair_radio):
        """用户选择配对后恢复 agent 执行

        generator：与 bot_respond 输出格式一致（6 元组）
        """
        cid = current.get("conv") if current else None
        # 无活动流，直接返回（隐藏选择框）
        if not cid or cid not in self._stream_queues:
            yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()
            return

        conv_state = self._get_conv_state(cid)
        pairs = conv_state.get("pending_pairs", [])

        # 解析用户选择
        try:
            idx = int(pair_radio) if pair_radio is not None else 0
        except (ValueError, TypeError):
            idx = 0
        if idx < 0 or idx >= len(pairs):
            idx = 0
        selected = pairs[idx] if pairs else None

        # 设置响应并恢复 agent 线程
        self._pause_responses[cid] = selected
        self._pause_events[cid].set()
        # 清除 pending_pairs
        conv_state.pop("pending_pairs", None)

        # 隐藏选择卡片
        start_time = self._stream_starts.get(cid, time.time())
        accumulated = conv_state.get("stream_accumulated", "")
        yield history, convs, self.get_workflow_status(current), self.get_accuracy_summary(current), gr_update(visible=False), gr_update()

        # 恢复消费队列
        yield from self._consume_stream(cid, history, convs, current, conv_state, start_time, accumulated)

    def _format_pairs_text(self, pairs: list) -> str:
        lines = []
        for i, p in enumerate(pairs):
            s_date = p.get("sentinel2_date") or p.get("sentinel_date") or "?"
            s_cov = p.get("sentinel2_coverage") or p.get("sentinel_coverage") or "?"
            s_cnt = p.get("sentinel2_count") or p.get("sentinel_count") or "?"
            lines.append(
                f"  {i+1}. Landsat {p.get('landsat_satellite', '?')} "
                f"{p.get('landsat_date', '?')} ({p.get('landsat_count', '?')} 景, 覆盖 {p.get('landsat_coverage', '?')}%) + "
                f"Sentinel {s_date} ({s_cnt} 景, 覆盖 {s_cov}%)"
            )
        return "\n".join(lines)

    def _save_history(self, convs: dict, current: dict, history: list):
        """保存历史到 convs_state 并持久化到磁盘"""
        pid = current.get("project") if current else None
        cid = current.get("conv") if current else None
        if not pid or not cid or pid not in convs or cid not in convs[pid]:
            return
        # 已删除对话：不写内存也不重建磁盘文件（彻底清除记忆）
        if cid in self._deleted_convs:
            return
        convs[pid][cid]["messages"] = history
        convs[pid][cid]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # 异步持久化（避免阻塞 UI）
        try:
            project_dir = convs[pid].get("__dir__", "")
            title = convs[pid][cid].get("title", "")
            self._persist_conversation(cid, pid, title, history, project_dir)
        except Exception:
            pass


# ── Gradio 辅助（轻量封装，避免在多处导入 gr） ──────────────────

def gr_update(**kwargs):
    """返回 gr.update() 等价物（延迟导入 gr）"""
    import gradio as gr
    return gr.update(**kwargs)


def gr_warning(msg: str):
    try:
        import gradio as gr
        gr.Warning(msg)
    except Exception:
        pass


def gr_info(msg: str):
    try:
        import gradio as gr
        gr.Info(msg)
    except Exception:
        pass


def _fmt_size(num: float) -> str:
    """字节数格式化为人类可读大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _dl_html(dst: str, rel_path: str = "", size: str = "") -> str:
    """生成下载链接 HTML：新标签页打开 + 可复制地址

    创空间把应用嵌在 iframe 里，直接点击下载常被沙箱拦截；
    新标签页（target=_blank）不受 iframe 沙箱限制，是可靠的下载方式。
    """
    if not dst:
        return "<span style='font-size:0.9em;color:#888;'>下载链接将在选择文件并点击下载后生成</span>"
    import urllib.parse
    href = f"gradio_api/file={urllib.parse.quote(dst)}"
    return (
        f'<div style="line-height:1.7;">'
        f'<a href="{href}" target="_blank" rel="noopener" '
        f'style="font-weight:bold;color:#2563eb;text-decoration:underline;font-size:1em;">'
        f"⬇️ 点击下载：{rel_path}（{size}）</a>"
        f'<div style="font-size:0.85em;color:#888;margin-top:6px;">'
        f"若点击没有反应（创空间 iframe 可能拦截下载），请复制下面地址，粘贴到浏览器新标签页打开："
        f'<br><code style="word-break:break-all;">{href}</code></div></div>'
    )

