"""
GeoThermoAI PyWebView API

暴露给前端 JS 调用的 Python 接口层，
负责 AI 对话、Skill 执行、配置管理、数据源连接测试等。
"""

import base64
import json
import os
import sys
import time
import threading
import uuid
import tempfile
from pathlib import Path

import webview

# 确保项目根目录在 sys.path 中，以便跨目录 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.ai_assistant import GeoThermoAI_Assistant
from core.skills.skill_registry import SkillRegistry
from core.agent.geo_thermo_agent import GeoThermoAgent


class GeoThermoAPI:
    """PyWebView API - 暴露给前端 JS 调用"""

    def __init__(self):
        # 从配置文件加载 API 设置
        settings = self._load_settings()
        api_config = settings.get("api", {})
        self.assistant = GeoThermoAI_Assistant(
            model_type=api_config.get("model_type", "deepseek"),
            api_key=api_config.get("api_key", ""),
            api_base_url=api_config.get("api_base_url", ""),
            model_id=api_config.get("model_id", ""),
            api_format=api_config.get("api_format", "openai"),
        )
        self.registry = SkillRegistry()
        self.agent = GeoThermoAgent(self.assistant, self.registry)
        self.current_workflow = None
        self.workflow_steps = [
            "data_acquisition",
            "data_pipeline",
            "ttri_compute",
            "rf_model",
            "tcr_compute",
            "lst_export",
            "accuracy_eval",
        ]

        # 对话管理
        self._conversations_dir = _ROOT / "data" / "conversations"
        self._conversations_dir.mkdir(parents=True, exist_ok=True)
        self._current_conversation_id = None
        self._conversation_messages = []

        # 注册内置 Skill
        self._register_builtin_skills()

        # 加载第三方 Skill
        self.registry.load_third_party_skills(str(_ROOT / "skills"))

        # 按对话隔离的状态：每个 conv_id 拥有独立的流式/暂停/工作流/项目目录/缩略图状态
        self._conversation_states: dict = {}

    def _get_conv_state(self, conv_id: str) -> dict:
        """获取（按需创建）指定对话的隔离状态"""
        if conv_id not in self._conversation_states:
            self._conversation_states[conv_id] = {
                "stream_state": {"content": "", "done": True, "error": None, "id": None},
                "paused_state": None,
                "pause_event": threading.Event(),
                "workflow_progress": {
                    "status": "idle",
                    "current_step": "",
                    "current_index": -1,
                    "steps": [],
                },
                "project_dir": "",
                "thumbnail_cache": {},
            }
        return self._conversation_states[conv_id]

    def _reset_conv_stream_state(self, conv_id: str, stream_id: str):
        """重置指定对话的流式状态，供发起新流时调用"""
        state = self._get_conv_state(conv_id)
        state["stream_state"].clear()
        state["stream_state"].update({"content": "", "done": False, "error": None, "id": stream_id})
        state["paused_state"] = None
        state["pause_event"].clear()

    # ── 内置 Skill 注册 ──────────────────────────────────────────

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

    # ── AI 对话 ──────────────────────────────────────────────────

    def chat(self, message: str, history: list = None) -> str:
        """AI 对话接口：判断是否是 Agent 指令，分发到不同处理路径"""
        project_dir = ""
        if self._current_conversation_id:
            project_dir = self._get_conv_state(self._current_conversation_id)["project_dir"]
        if self._is_agent_command(message):
            # 入口检查：研究区是否已上传
            is_workflow_cmd = any(kw in message for kw in ["全流程", "一键", "跑完全流程", "执行全流程", "处理", "下载", "获取"])
            if is_workflow_cmd:
                _study_areas_dir = _ROOT / "config" / "study_areas"
                uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
                if not uploaded:
                    return "⚠️ 请先上传研究区文件（Shapefile 或 GeoJSON），然后再发送指令。\n\n操作路径：**工作面板 → 数据设置 → 上传研究区域文件**"
                # 入口检查：工作流类命令必须已指定项目目录
                if not project_dir or not os.path.isdir(project_dir):
                    return "⚠️ 请先选择项目目录，然后再执行一键全流程。\n\n操作路径：在对话框下方输入项目文件夹完整路径，或点击右侧“选择文件夹”按钮。"
            return self.agent.process_command(message, project_dir=project_dir)
        else:
            context = self._get_context()
            return self.assistant.ask(message, context, prior_messages=history)

    def chat_stream_start(self, conv_id: str, message: str, history: list = None) -> bool:
        """开始流式对话（在后台线程中发送请求，前端轮询结果）"""
        if not conv_id:
            return False
        self._current_conversation_id = conv_id
        state = self._get_conv_state(conv_id)
        stream_id = uuid.uuid4().hex[:8]
        self._reset_conv_stream_state(conv_id, stream_id)

        # ── 入口检查：研究区是否已上传 ────────────────────────────
        is_workflow_cmd = any(kw in message for kw in ["全流程", "一键", "跑完全流程", "执行全流程", "处理", "下载", "获取"])
        if is_workflow_cmd and self._is_agent_command(message):
            _study_areas_dir = _ROOT / "config" / "study_areas"
            uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
            if not uploaded:
                state["stream_state"]["content"] = (
                    "⚠️ 请先上传研究区文件（Shapefile 或 GeoJSON），然后再发送指令。\n\n"
                    "操作路径：**工作面板 → 数据设置 → 上传研究区域文件**"
                )
                state["stream_state"]["done"] = True
                return True

            # 入口检查：工作流类命令必须已指定项目目录
            project_dir = state["project_dir"]
            if not project_dir or not os.path.isdir(project_dir):
                state["stream_state"]["content"] = (
                    "⚠️ 请先选择项目目录，然后再执行一键全流程。\n\n"
                    "操作路径：在对话框下方输入项目文件夹完整路径，或点击右侧“选择文件夹”按钮。"
                )
                state["stream_state"]["done"] = True
                return True

        import threading

        def _do_stream():
            try:
                # 如果该对话被新的 stream_start 覆盖，直接退出
                if state["stream_state"].get("id") != stream_id:
                    return

                context = self._get_context(conv_id)

                def on_token(content: str):
                    # 只有当前 stream 还是自己才更新
                    if state["stream_state"].get("id") == stream_id:
                        state["stream_state"]["content"] = content

                if self._is_agent_command(message):
                    # 创建 pause_callback：Agent 需要用户输入时同步阻塞等待
                    def pause_callback(pause_data):
                        if state["stream_state"].get("id") != stream_id:
                            return {"paused": False}
                        state["paused_state"] = {
                            "stream_id": stream_id,
                            "pause_data": pause_data,
                            "selected": None,
                        }
                        state["stream_state"]["waiting_for_input"] = pause_data
                        # 同步阻塞等待用户选择
                        state["pause_event"].clear()
                        state["pause_event"].wait()
                        # 被唤醒后检查是否有选择
                        if state["paused_state"] and state["paused_state"].get("selected") is not None:
                            return {"paused": False, "data": state["paused_state"]["selected"]}
                        return {"paused": True}

                    # 创建 workflow_callback：更新工作流进度条
                    def workflow_callback(skill_name, status, idx, total):
                        if state["stream_state"].get("id") != stream_id:
                            return
                        self._update_workflow_progress(state["workflow_progress"], skill_name, status)
                        # 用 workflow_progress 的 steps 字段方便前端渲染
                        step_names = self.workflow_steps
                        steps = []
                        for si, sn in enumerate(step_names):
                            if sn == skill_name:
                                steps.append({"name": sn, "status": status})
                            elif si < idx:
                                steps.append({"name": sn, "status": "completed"})
                            elif si > idx:
                                steps.append({"name": sn, "status": "pending"})
                            else:
                                steps.append({"name": sn, "status": status})
                        state["workflow_progress"] = {
                            "status": "running" if status != "completed" else (
                                "completed" if idx + 1 >= total else "running"
                            ),
                            "current_step": skill_name,
                            "current_index": idx,
                            "steps": steps,
                        }

                    result = self.agent.process_command(
                        message,
                        on_token=on_token,
                        pause_callback=pause_callback,
                        workflow_callback=workflow_callback,
                        project_dir=state["project_dir"],
                    )
                    # process_command 已通过 on_token 流式输出执行过程
                    # 但若返回错误信息，追加显示
                    if result and ("⚠️" in result or "失败" in result or "未找到" in result):
                        on_token("\n\n" + result)
                    # 流式输出结束后，预生成 LST 缩略图（如果 tif 存在）
                    # 这样用户切换到面板时缩略图已在磁盘缓存中，瞬间显示
                    if state["project_dir"]:
                        try:
                            self.get_lst_result(conv_id)
                        except Exception:
                            pass
                else:
                    self.assistant.ask_stream(
                        message, on_token, context=context, prior_messages=history
                    )
            except Exception as e:
                if state["stream_state"].get("id") == stream_id:
                    state["stream_state"]["error"] = str(e)
            finally:
                if state["stream_state"].get("id") == stream_id:
                    state["stream_state"]["done"] = True

        thread = threading.Thread(target=_do_stream, daemon=True)
        thread.start()
        return True

    def chat_stream_poll(self, conv_id: str) -> dict:
        """轮询指定对话的流式状态，返回 {"content": "...", "done": bool, "error": str|None, "waiting_for_input": ...}"""
        state = self._get_conv_state(conv_id)
        result = dict(state["stream_state"])
        # 如果处于等待输入状态，包含 pause_data
        if result.get("waiting_for_input"):
            result["waiting_for_input"] = state["paused_state"].get("pause_data") if state["paused_state"] else True
        return result

    def agent_select_pair(self, conv_id: str, pair_index: int) -> dict:
        """用户选择了影像配对，唤醒对应对话的 Agent 线程继续执行"""
        state = self._get_conv_state(conv_id)
        if state["paused_state"] and state["paused_state"].get("pause_data", {}).get("type") == "select_pair":
            pairs = state["paused_state"]["pause_data"].get("pairs", [])
            if 0 <= pair_index < len(pairs):
                selected = pairs[pair_index]
                # 保存选择结果
                state["paused_state"]["selected"] = selected
                # 从流式内容中追加选择信息
                state["stream_state"]["content"] += (
                    f"\n✅ 已选择第 {pair_index + 1} 对："
                    f"Landsat {selected.get('landsat_satellite', '')} {selected.get('landsat_date', '?')} "
                    f"({selected.get('landsat_count', 1)}景, 覆盖度 {selected.get('landsat_coverage', '?')}%), "
                    f"Sentinel {selected.get('sentinel_date', '?')} "
                    f"({selected.get('sentinel_count', 1)}景, 覆盖度 {selected.get('sentinel_coverage', '?')}%)"
                )
                # 清除等待状态
                state["stream_state"].pop("waiting_for_input", None)
                # 唤醒 Agent 线程
                state["pause_event"].set()
                return {"success": True, "selected": selected}
        return {"success": False, "message": "没有暂停的任务或选择无效"}

    # ── 对话管理 ──────────────────────────────────────────────────

    def list_conversations(self) -> list:
        """列出所有保存的对话，按更新时间倒序，星标优先"""
        conversations = []
        for f in self._conversations_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                conversations.append({
                    "id": data["id"],
                    "title": data.get("title", "未命名对话"),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "message_count": len(data.get("messages", [])),
                    "starred": data.get("starred", False),
                })
            except Exception:
                continue
        conversations.sort(key=lambda x: (not x.get("starred", False), x.get("updated_at", "")), reverse=False)
        conversations.sort(key=lambda x: x.get("starred", False), reverse=True)
        return conversations

    def create_conversation(self) -> dict:
        """创建新对话，返回对话 ID"""
        conv_id = uuid.uuid4().hex[:12]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conv = {
            "id": conv_id,
            "title": "新对话",
            "messages": [],
            "created_at": now,
            "updated_at": now,
            "starred": False,
        }
        path = self._conversations_dir / f"{conv_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(conv, f, ensure_ascii=False, indent=2)
        # 新对话使用全新的隔离状态
        self._conversation_states[conv_id] = {
            "stream_state": {"content": "", "done": True, "error": None, "id": None},
            "paused_state": None,
            "pause_event": threading.Event(),
            "workflow_progress": {
                "status": "idle",
                "current_step": "",
                "current_index": -1,
                "steps": [],
            },
            "project_dir": "",
            "thumbnail_cache": {},
        }
        self._current_conversation_id = conv_id
        self._conversation_messages = []
        return {"id": conv_id, "title": "新对话"}

    def load_conversation(self, conv_id: str) -> dict:
        """加载指定对话的消息列表"""
        path = self._conversations_dir / f"{conv_id}.json"
        if not path.exists():
            return {"success": False, "message": "对话不存在"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 恢复项目目录到该对话的隔离状态（即使为空也要更新，避免残留）
        saved_dir = data.get("project_dir", "")
        state = self._get_conv_state(conv_id)
        state["project_dir"] = saved_dir
        self._current_conversation_id = conv_id
        self._conversation_messages = data.get("messages", [])
        return {
            "success": True,
            "id": conv_id,
            "title": data.get("title", "未命名对话"),
            "messages": self._conversation_messages,
            "project_dir": saved_dir,
        }

    def save_conversation(self, conv_id: str, messages: list, title: str = None) -> bool:
        """保存/更新对话"""
        path = self._conversations_dir / f"{conv_id}.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"id": conv_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}

        data["messages"] = messages
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if title:
            data["title"] = title

        # 从隔离状态中读取该对话的项目目录并持久化
        state = self._get_conv_state(conv_id)
        if state["project_dir"]:
            data["project_dir"] = state["project_dir"]

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    def save_project_dir(self, conv_id: str, project_dir: str) -> bool:
        """仅保存项目目录到对话（不修改消息）"""
        path = self._conversations_dir / f"{conv_id}.json"
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["project_dir"] = project_dir
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 同步更新隔离状态
        state = self._get_conv_state(conv_id)
        state["project_dir"] = project_dir
        return True

    def delete_conversation(self, conv_id: str) -> bool:
        """删除指定对话"""
        path = self._conversations_dir / f"{conv_id}.json"
        if path.exists():
            os.remove(path)
        # 清理内存中的隔离状态
        self._conversation_states.pop(conv_id, None)
        if self._current_conversation_id == conv_id:
            self._current_conversation_id = None
            self._conversation_messages = []
        return True

    def rename_conversation(self, conv_id: str, title: str) -> bool:
        """重命名对话"""
        path = self._conversations_dir / f"{conv_id}.json"
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["title"] = title
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    def toggle_star(self, conv_id: str) -> dict:
        """切换对话星标状态，返回新的星标状态"""
        path = self._conversations_dir / f"{conv_id}.json"
        if not path.exists():
            return {"success": False}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["starred"] = not data.get("starred", False)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"success": True, "starred": data["starred"]}

    def load_third_party_skill(self, zip_base64: str) -> dict:
        """加载第三方 Skill（接收 base64 编码的 zip 文件）"""
        import zipfile
        import io
        import shutil
        import importlib
        from core.skills.base_skill import BaseSkill

        try:
            zip_data = base64.b64decode(zip_base64)
            zip_buffer = io.BytesIO(zip_data)

            # 解压到临时目录
            temp_dir = _ROOT / "data" / "temp_skills"
            temp_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                zf.extractall(temp_dir)

            # 查找 Skill 目录（包含 __init__.py 的目录）
            skill_dirs = [d for d in temp_dir.iterdir() if d.is_dir() and (d / "__init__.py").exists()]

            if not skill_dirs:
                return {"success": False, "message": "未在压缩包中找到有效的 Skill（缺少 __init__.py）"}

            results = []
            for skill_dir in skill_dirs:
                try:
                    # 添加到 sys.path
                    if str(skill_dir.parent) not in sys.path:
                        sys.path.insert(0, str(skill_dir.parent))

                    # 动态导入
                    module = importlib.import_module(skill_dir.name)

                    # 查找 BaseSkill 子类
                    loaded = False
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (isinstance(attr, type) and
                            issubclass(attr, BaseSkill) and attr != BaseSkill):
                            try:
                                skill_instance = attr()
                                self.registry.register(skill_instance)
                                results.append(f"✓ {skill_instance.name} ({skill_instance.group})")
                                loaded = True
                                break
                            except ValueError as e:
                                results.append(f"✗ {attr_name}: {e}")

                    if not loaded:
                        results.append(f"✗ {skill_dir.name}: 未找到 BaseSkill 子类")

                except Exception as e:
                    results.append(f"✗ {skill_dir.name}: {e}")

            # 清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)

            success_count = sum(1 for r in results if r.startswith("✓"))
            return {
                "success": success_count > 0,
                "message": f"成功加载 {success_count} 个 Skill",
                "details": results,
            }

        except Exception as e:
            return {"success": False, "message": f"加载失败: {e}"}

    def _is_agent_command(self, message: str) -> bool:
        """判断是否是 Agent 指令（包含执行类动词）"""
        agent_keywords = [
            "处理", "训练", "下载", "执行", "运行", "生成",
            "全流程", "一键", "开始", "计算", "导出", "评估",
        ]
        return any(kw in message for kw in agent_keywords)

    # ── Skill 执行 ───────────────────────────────────────────────

    def run_skill(self, skill_name: str, params: dict) -> dict:
        """执行指定 Skill，返回结构化结果"""
        skill = self.registry.get(skill_name)
        if not skill:
            return {"success": False, "message": f"Skill {skill_name} 不存在"}

        try:
            result = skill.execute(params)
            return {
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "artifacts": result.artifacts,
            }
        except Exception as e:
            return {"success": False, "message": f"Skill 执行异常: {str(e)}"}

    # ── 工作流状态 ───────────────────────────────────────────────

    def get_workflow_status(self, conv_id: str = None) -> dict:
        """获取指定对话的工作流执行状态；未指定时使用当前对话"""
        if conv_id is None:
            conv_id = self._current_conversation_id
        if conv_id:
            state = self._get_conv_state(conv_id)
            return state["workflow_progress"]
        return {
            "status": "idle",
            "current_step": "",
            "current_index": -1,
            "steps": [],
        }

    def get_workflow_step_labels(self) -> dict:
        """获取工作流各步骤的当前显示名称（根据加载的 Skill 动态更新）"""
        labels = {}
        for step_name in self.workflow_steps:
            skill = self.registry.get(step_name)
            if skill:
                labels[step_name] = skill.description
        return labels

    def get_model_skills(self) -> list:
        """获取所有可用的模型训练 Skill 及其超参数"""
        skills = self.registry.get_group("model_train_predict")
        result = []
        
        for skill in skills:
            hyperparams = []
            for hp in skill.hyperparameters:
                hyperparams.append({
                    "name": hp.name,
                    "label": hp.label,
                    "type": hp.type,
                    "default": hp.default,
                    "min": hp.min,
                    "max": hp.max,
                    "step": hp.step,
                    "options": hp.options,
                    "description": hp.description
                })
            
            result.append({
                "name": skill.name,
                "description": skill.description,
                "hyperparameters": hyperparams
            })
        
        return result

    def set_current_model(self, model_name: str) -> bool:
        """设置当前工作面板选择的模型，更新 AI 助手的系统提示词"""
        try:
            self.assistant.set_current_model(model_name)
            return True
        except Exception:
            return False

    def _update_workflow_progress(self, target: dict, step_name: str, status: str):
        """更新指定工作流进度对象（供内部调用）"""
        idx = self.workflow_steps.index(step_name) if step_name in self.workflow_steps else -1
        target.update({
            "status": status,
            "current_step": step_name,
            "current_index": idx,
            "steps": [
                {
                    "name": s,
                    "status": "completed" if i < idx else ("running" if i == idx else "pending"),
                }
                for i, s in enumerate(self.workflow_steps)
            ],
        })

    # ── 项目目录管理 ─────────────────────────────────────────────

    def get_project_dir(self, conv_id: str = None) -> str:
        """获取指定对话的项目目录；未指定时使用当前对话"""
        if conv_id is None:
            conv_id = self._current_conversation_id
        if not conv_id:
            return ""
        state = self._get_conv_state(conv_id)
        if state["project_dir"]:
            return state["project_dir"]
        # 启动时自动从对应对话文件恢复
        path = self._conversations_dir / f"{conv_id}.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = data.get("project_dir", "")
            if saved:
                state["project_dir"] = saved
                return saved
        return ""

    def get_accuracy_summary(self, conv_id: str = None) -> dict:
        """读取指定对话最新的精度评估结果，返回摘要供前端使用"""
        import os, json, glob
        if conv_id is None:
            conv_id = self._current_conversation_id
        if not conv_id:
            return {}
        state = self._get_conv_state(conv_id)
        if not state["project_dir"]:
            return {}
        # 搜索 spatial_consistency_*.json
        results_dir = os.path.join(state["project_dir"], "results")
        files = sorted(glob.glob(os.path.join(results_dir, "spatial_consistency_*.json")))
        if not files:
            return {}
        try:
            with open(files[-1], encoding="utf-8") as f:
                data = json.load(f)
            sc = data.get("spatial_consistency", {})
            vr = data.get("value_range", {})
            dev = vr.get("deviation", {})
            # 读取 rf_model 测试 R²
            test_r2 = 0
            test_rmse = 0
            metrics_files = sorted(glob.glob(os.path.join(results_dir, "test", "rf_ttri_predict_*.json")))
            if metrics_files:
                with open(metrics_files[-1], encoding="utf-8") as f2:
                    mdata = json.load(f2)
                m = mdata.get("metrics", {})
                test_r2 = m.get("R2", 0)
                test_rmse = m.get("RMSE", 0)
            return {
                "r2": test_r2,
                "test_rmse": test_rmse,
                "test_mae": m.get("MAE", 0),
                "n_test_samples": sc.get("n_test_samples", 0),
                "n_matched": sc.get("n_matched", 0),
                "mb": sc.get("metrics", {}).get("MB", 0),
                "mae": sc.get("metrics", {}).get("MAE", 0),
                "rmse": sc.get("metrics", {}).get("RMSE", 0),
                "dev_min": dev.get("test_10m_vs_test_30m_min", 0),
                "dev_max": dev.get("test_10m_vs_test_30m_max", 0),
                "max_abs_dev": dev.get("max_abs_deviation", 0),
                "passed": dev.get("passed", False),
            }
        except Exception:
            return {}

    def check_preprocessed_data(self, conv_id: str = None) -> bool:
        """检查指定对话的项目目录中是否存在推荐 RF 参数所需的预处理文件

        需要 train.csv 存在且非空才能特征数/样本数。
        当文件全部存在时返回 True，否则返回 False。
        """
        shape = self.get_csv_shape(conv_id)
        return shape["n_features"] > 0 and shape["n_samples"] > 0

    def get_csv_shape(self, conv_id: str = None) -> dict:
        """读取 processed/train.csv 的样本数，特征数固定为 9

        返回 {"n_features": 9, "n_samples": int}，文件不存在时返回全 0。
        """
        if conv_id is None:
            conv_id = self._current_conversation_id
        if not conv_id:
            return {"n_features": 0, "n_samples": 0}
        state = self._get_conv_state(conv_id)
        if not state["project_dir"]:
            return {"n_features": 0, "n_samples": 0}
        csv_path = os.path.join(state["project_dir"], "processed", "train.csv")
        if not os.path.isfile(csv_path):
            return {"n_features": 0, "n_samples": 0}
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                header = f.readline()
                if not header:
                    return {"n_features": 0, "n_samples": 0}
                n_samples = sum(1 for _ in f)
                return {"n_features": 9, "n_samples": n_samples}
        except Exception:
            return {"n_features": 0, "n_samples": 0}

    def set_project_dir(self, path: str) -> bool:
        """设置当前对话的项目目录"""
        import os
        if path and not os.path.isdir(path):
            return False
        normalized = path.replace("\\", "/") if path else ""
        if self._current_conversation_id:
            state = self._get_conv_state(self._current_conversation_id)
            state["project_dir"] = normalized
        return True

    def select_project_dir_dialog(self) -> str:
        """打开系统原生文件夹选择对话框，返回选择的路径"""
        icon_ico = getattr(self, '_icon_path', None) or str(_ROOT / "ui" / "assets" / "logo.ico")
        # 优先使用视觉设计稿中的高清 logo（圆角版）
        icon_png = str(_ROOT.parent / "视觉设计" / "GeoThermoAI_logo_gpt初版圆角.png")
        if not os.path.exists(icon_png):
            icon_png = str(_ROOT / "ui" / "assets" / "logo.png")
        # 统一使用 tkinter（图标可控，且与软件 logo 一致）
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            root.title("选择项目目录")
            # 设置软件图标：优先用高分辨率 PNG（避免 ico 模糊）
            icon_set = False
            try:
                if icon_png and os.path.exists(icon_png):
                    from PIL import Image, ImageTk
                    img = Image.open(icon_png)
                    # 限制最大尺寸，避免占用过多内存
                    img.thumbnail((256, 256), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    # default=True 让窗口所有位置都用此图标
                    root.iconphoto(True, photo)
                    # 保持引用避免被 GC
                    root._icon_photo = photo
                    icon_set = True
            except Exception:
                pass
            if not icon_set:
                try:
                    if icon_ico and os.path.exists(icon_ico):
                        root.iconbitmap(default=icon_ico)
                except Exception:
                    pass
            path = filedialog.askdirectory(title="选择项目目录")
            root.destroy()
            if path:
                normalized = path.replace("\\", "/")
                if self._current_conversation_id:
                    state = self._get_conv_state(self._current_conversation_id)
                    state["project_dir"] = normalized
                return normalized
        except Exception:
            # 退化到 pywebview 原生对话框（已通过 window icon 设置）
            if hasattr(self, '_window') and self._window is not None:
                try:
                    result = self._window.create_file_dialog(
                        1,  # FOLDER_DIALOG
                        allow_multiple=False
                    )
                    if result:
                        if isinstance(result, (list, tuple)):
                            result = result[0]
                        normalized = str(result).replace("\\", "/")
                        if self._current_conversation_id:
                            state = self._get_conv_state(self._current_conversation_id)
                            state["project_dir"] = normalized
                        return normalized
                except Exception:
                    pass
        return ""

    # ── 配置管理 ─────────────────────────────────────────────────

    def get_config(self) -> dict:
        """获取当前配置"""
        settings = self._load_settings()
        api_config = settings.get("api", {})
        return {
            "api_key": self.assistant.api_key,
            "model_type": self.assistant.model_type,
            "api_base_url": api_config.get("api_base_url", ""),
            "model_id": api_config.get("model_id", ""),
            "api_format": api_config.get("api_format", "openai"),
            "display_name": api_config.get("display_name", ""),
            "model_series": api_config.get("model_series", "default"),
            "context_input": api_config.get("context_input", 128000),
            "context_output": api_config.get("context_output", 16000),
            "data_source": settings.get("data_source", {"type": "planetary_computer", "status": "public"}),
            "model": settings.get("model", {}),
            "processing": settings.get("processing", {}),
            "data": settings.get("data", {}),
        }

    def update_config(self, key: str, value) -> bool:
        """更新运行时配置并持久化到 settings.json"""
        # 更新运行时状态
        if key == "api_key":
            self.assistant.api_key = value
        elif key == "model_type":
            self.assistant.model_type = value
        elif key == "api_base_url":
            self.assistant.api_base_url = value
        elif key == "model_id":
            self.assistant.model_id = value

        # 持久化到 settings.json
        settings = self._load_settings()
        # data 相关配置存到 data 分组
        if key in ("cloud_threshold", "dem_source"):
            if "data" not in settings:
                settings["data"] = {}
            settings["data"][key] = value
        # 模型参数存到 model 分组
        elif key in ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf", "max_features", "random_state"):
            if "model" not in settings:
                settings["model"] = {}
            settings["model"][key] = value
        else:
            if "api" not in settings:
                settings["api"] = {}
            settings["api"][key] = value
        self._save_settings(settings)
        return True

    def test_data_source_connection(self) -> dict:
        """测试数据源 (Planetary Computer) 连接"""
        try:
            from pystac_client import Client
            catalog = Client.open(
                "https://planetarycomputer.microsoft.com/api/stac/v1",
                headers={"Accept": "application/json"},
            )
            # 简单搜索验证
            search = catalog.search(collections=["landsat-c2-l2"], max_items=1)
            _ = list(search.items())
            return {"success": True, "message": "Planetary Computer 连接成功"}
        except ImportError:
            return {"success": False, "message": "未安装 pystac-client，请运行: pip install pystac-client"}
        except Exception as e:
            return {"success": False, "message": f"Planetary Computer 连接失败: {e}"}

    # ── 研究区域上传 ─────────────────────────────────────────────

    def upload_study_area(self, filename: str, content_base64: str, extra_files: dict = None) -> dict:
        """上传研究区域文件（支持 .geojson 和 .shp）

        对于 Shapefile，需要同时上传 .dbf、.shx、.prj 等伴随文件。
        extra_files 格式: {"file.dbf": "base64_content", "file.shx": "base64_content", ...}
        """
        try:
            save_dir = _ROOT / "config" / "study_areas"
            save_dir.mkdir(parents=True, exist_ok=True)

            ext = os.path.splitext(filename)[1].lower()

            if ext == ".geojson":
                # 直接保存 GeoJSON
                dest = save_dir / filename
                with open(dest, "wb") as f:
                    f.write(base64.b64decode(content_base64))
                return {"success": True, "path": str(dest), "format": "geojson"}

            elif ext == ".shp":
                # 保存所有 shapefile 组件
                shp_dir = save_dir / os.path.splitext(filename)[0]
                shp_dir.mkdir(parents=True, exist_ok=True)

                # 保存 .shp 主文件
                shp_path = shp_dir / filename
                with open(shp_path, "wb") as f:
                    f.write(base64.b64decode(content_base64))

                # 保存伴随文件（.dbf, .shx, .prj 等）
                if extra_files:
                    for fname, fcontent in extra_files.items():
                        extra_path = shp_dir / fname
                        with open(extra_path, "wb") as f:
                            f.write(base64.b64decode(fcontent))

                # 转换为 GeoJSON
                geojson_path = shp_dir.parent / f"{os.path.splitext(filename)[0]}.geojson"
                result = self._shp_to_geojson(str(shp_path), str(geojson_path))
                if result["success"]:
                    return {"success": True, "path": result["path"], "format": "geojson"}
                else:
                    return result

            else:
                return {"success": False, "message": f"不支持的文件格式: {ext}，仅支持 .shp 和 .geojson"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _shp_to_geojson(self, shp_path: str, geojson_path: str) -> dict:
        """将 Shapefile 转换为 GeoJSON"""
        try:
            import shapefile

            reader = shapefile.Reader(shp_path)
            fields = reader.fields[1:]  # 跳过 DeletionFlag

            features = []
            for sr in reader.iterShapeRecords():
                geom = sr.shape.__geo_interface__
                props = {}
                for i, field in enumerate(fields):
                    field_name = field[0]
                    value = sr.record[i]
                    # 处理不可 JSON 序列化的类型
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    props[field_name] = value

                features.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": props,
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features,
            }

            with open(geojson_path, "w", encoding="utf-8") as f:
                json.dump(geojson, f, ensure_ascii=False, indent=2)

            return {"success": True, "path": geojson_path}
        except ImportError:
            return {"success": False, "message": "未安装 pyshp，请运行: pip install pyshp"}
        except Exception as e:
            return {"success": False, "message": f"Shapefile 转换失败: {e}"}

    # ── 结果图片 ─────────────────────────────────────────────────

    def get_result_image(self, artifact_path: str) -> str:
        """获取结果图片的 Base64 编码（用于前端显示）"""
        if not os.path.exists(artifact_path):
            return ""
        with open(artifact_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(artifact_path)[1].lower()
        mime = "image/png" if ext == ".png" else "image/tiff" if ext in (".tif", ".tiff") else "image/png"
        return f"data:{mime};base64,{img_data}"

    def get_lst_result(self, conv_id: str = None) -> dict:
        """获取指定对话项目目录下的最终 LST 结果（GeoTIFF 缩略图 + 统计信息）

        缩略图会缓存到磁盘 (results/.lst_thumbnail.jpg)，重启后直接读取，
        仅当 tif 文件比缓存更新时才重新生成。
        """
        if conv_id is None:
            conv_id = self._current_conversation_id
        if not conv_id:
            return {"found": False}
        state = self._get_conv_state(conv_id)
        project_dir = state["project_dir"]
        if not project_dir:
            return {"found": False}
        import os, glob
        results_dir = os.path.join(project_dir, "results")
        tif_files = sorted(glob.glob(os.path.join(results_dir, "rf_10m_lst_final.tif")))
        if not tif_files:
            tif_files = sorted(glob.glob(os.path.join(results_dir, "*10m_lst*.tif")))
            if not tif_files:
                tif_files = sorted(glob.glob(os.path.join(results_dir, "*.tif")))
        if not tif_files:
            return {"found": False}
        tif_path = tif_files[-1]
        try:
            current_mtime = os.path.getmtime(tif_path)
        except OSError:
            current_mtime = 0

        # 1) 内存缓存
        cached = state["thumbnail_cache"].get(tif_path)
        if cached and cached[0] == current_mtime:
            return {"found": True, "path": tif_path, "thumbnail": cached[1]}

        # 2) 磁盘缓存（.lst_thumbnail.jpg）—— 重启后直接读取，无需 rasterio
        thumb_path = os.path.join(results_dir, ".lst_thumbnail.jpg")
        try:
            thumb_mtime = os.path.getmtime(thumb_path)
        except OSError:
            thumb_mtime = 0
        if thumb_mtime > current_mtime and os.path.isfile(thumb_path):
            # 磁盘缓存有效，直接读取 JPEG
            try:
                with open(thumb_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                data_uri = f"data:image/jpeg;base64,{b64}"
                state["thumbnail_cache"][tif_path] = (current_mtime, data_uri)
                return {"found": True, "path": tif_path, "thumbnail": data_uri}
            except Exception:
                pass

        # 3) 首次生成：用 rasterio 读取低分辨率数据
        try:
            import rasterio
            import numpy as np
            from PIL import Image
            with rasterio.open(tif_path) as src:
                h_orig, w_orig = src.height, src.width
                pixel_w = abs(src.transform.a)
                pixel_h = abs(src.transform.e)
                if w_orig == 0 or pixel_w == 0:
                    return {"found": True, "path": tif_path, "thumbnail": None}
                # 目标尺寸：宽度 300px
                target_w = min(300, w_orig)
                aspect = (h_orig * pixel_h) / (w_orig * pixel_w)
                target_h = max(1, int(target_w * aspect))
                step_w = max(1, int(w_orig / target_w))
                step_h = max(1, int(h_orig / target_h))
                win_w = (w_orig + step_w - 1) // step_w
                win_h = (h_orig + step_h - 1) // step_h
                band = src.read(1, out_shape=(win_h, win_w))
                nodata = src.nodata
                if nodata is not None:
                    band = np.where(band == nodata, np.nan, band.astype(np.float32))
                else:
                    band = band.astype(np.float32)
                valid = band[~np.isnan(band)]
                if valid.size == 0:
                    return {"found": True, "path": tif_path, "thumbnail": None}
                # 用 min/max 代替 nanpercentile（快很多）
                vmin = float(np.nanmin(valid))
                vmax = float(np.nanmax(valid))
                if vmax <= vmin:
                    vmax = vmin + 1
                normalized = np.clip((band - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)
                # 直接用灰度 JPEG（不转 RGB，体积更小、编码更快）
                img = Image.fromarray(normalized, mode='L')
                # 只编码一次 JPEG，保存到磁盘
                img.save(thumb_path, format="JPEG", quality=75)
            # 从磁盘文件直接读 base64（避免重复编码）
            with open(thumb_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            data_uri = f"data:image/jpeg;base64,{b64}"
            state["thumbnail_cache"][tif_path] = (current_mtime, data_uri)
            return {"found": True, "path": tif_path, "thumbnail": data_uri}
        except Exception:
            pass
        return {"found": True, "path": tif_path, "thumbnail": None}

    def check_result_exists(self, result_path: str) -> bool:
        """检查结果文件是否仍然存在"""
        return bool(result_path) and os.path.isfile(result_path)

    # ── 内部辅助 ─────────────────────────────────────────────────

    def _get_context(self, conv_id: str = None) -> dict:
        """获取指定对话的软件状态上下文；未指定时使用当前对话"""
        return {
            "workflow_status": self.get_workflow_status(conv_id or self._current_conversation_id),
            "config": self.get_config(),
        }

    def get_settings(self) -> dict:
        """获取完整 settings（供前端读取已有配置）"""
        return self._load_settings()

    def _load_settings(self) -> dict:
        """从 config/settings.json 加载配置"""
        settings_path = _ROOT / "config" / "settings.json"
        if settings_path.exists():
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_settings(self, settings: dict):
        """保存配置到 config/settings.json"""
        settings_path = _ROOT / "config" / "settings.json"
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)


# ── 启动 PyWebView ───────────────────────────────────────────────

def start_ui():
    """启动 GeoThermoAI PyWebView 应用"""
    api = GeoThermoAPI()

    html_path = Path(__file__).resolve().parent / "index.html"
    # 软件图标（用于主窗口及原生文件选择对话框）
    icon_path = Path(__file__).resolve().parent / "assets" / "logo.ico"

    window = webview.create_window(
        "GeoThermoAI - 基于跨尺度热响应一致性的高分辨率地表温度智能重建系统",
        url=str(html_path),
        js_api=api,
        width=1280,
        height=860,
        resizable=True,
        min_size=(960, 640),
        icon=str(icon_path) if icon_path.exists() else None,
    )
    api._window = window
    api._icon_path = str(icon_path) if icon_path.exists() else None
    webview.start(debug=False)


if __name__ == "__main__":
    start_ui()
