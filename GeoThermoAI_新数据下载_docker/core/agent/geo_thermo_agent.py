"""
GeoThermoAI 核心智能体

理解用户自然语言指令，自动选择 Skill 并编排执行，
在关键节点进行异常检测、自动调参和用户交互。
"""

import json
import glob
import logging
import os
import pathlib
import time
from typing import Any, Dict, List, Optional, Callable

logger = logging.getLogger(__name__)

from ..ai_assistant import GeoThermoAI_Assistant
from ..skills.skill_registry import SkillRegistry
from ..skills.base_skill import SkillResult
from . import executor, plan_schema, presentation
from .executor import PAUSE_MARKER, MODEL_TRAIN_SKILLS as _MODEL_TRAIN_SKILLS
from .orchestrator import agent_config, approval as approval_proto
from .orchestrator.approval import Node, Option
from .orchestrator.exec_mode import normalize as _normalize_exec_mode
from .orchestrator.run_state import RunState
from .roles.base_role import extract_json
from .roles.slots import match_study_area

# 记忆系统不可用/未接入时的基础领域知识兜底（正常接入后由 RAG 检索注入，见 core/memory/）
_BASIC_KNOWLEDGE = """- TTRI（地形热响应指数）= a*DEM + b*Slope + c*cos(Aspect)
- TCR（热约束修正）用于修正地形对LST的影响
- 降尺度：Landsat 30m LST + Sentinel 10m多光谱 → 10m LST
- Landsat重访周期16天，Sentinel重访周期5天
- 影像配对要求：Landsat与Sentinel时间差≤2天
- 数据源：Microsoft Planetary Computer（STAC API）+ Copernicus Data Space（Sentinel-2/DEM 优先，国内更快），
  Landsat 8/9 L2、Sentinel-2 L2A、Copernicus GLO-30 DEM 均由系统自动搜索下载；不使用 Google Earth Engine（GEE）"""

# 各 Skill 的阶段说明（气泡中"阶段开始"时展示，帮助用户了解每一步在做什么）
# 单一来源在 core/agent/presentation.py，避免执行引擎与本文件两处中文名不一致
_STEP_DESCRIPTIONS = presentation.LEGACY_STEP_DESCRIPTIONS


_PROJECT_DIR_PROMPT = """## 项目目录（已由用户设置）
用户已选择项目目录：{0}
所有输出路径都在此目录下的 raw/、processed/、results/ 子目录中。
你必须直接生成JSON执行计划，不要询问输出目录路径。"""


class GeoThermoAgent:
    """GeoThermoAI 核心智能体 - 理解用户意图，选择Skill，编排执行"""

    def __init__(self, assistant: GeoThermoAI_Assistant, registry: SkillRegistry):
        self.assistant = assistant
        self.registry = registry

    # ── 公开接口 ─────────────────────────────────────────────────────

    def process_command(self, user_input: str, on_token=None, on_log=None, pause_callback=None, project_dir: str = "", workflow_callback=None, settings_path: str = "", study_areas_dir: str = "", conv_id: str = "", project_id: str = "", memory_manager=None, exec_mode: str = "", prior_messages=None, session_state=None, on_thinking=None) -> str:
        """处理用户自然语言指令

        流程：
        1. 获取当前软件状态上下文
        2. 获取所有已注册 Skill 的描述
        3. 构建 System Prompt（注入记忆：领域知识 + 项目历史经验）
        4. 调用 LLM 生成执行计划（JSON）
        5. 解析并执行计划（收尾自动写入实验记录）

        on_token: 可选回调，用于流式输出执行进度（气泡：阶段开始/完成摘要/最终结果）
        on_thinking: 可选回调，透传 LLM 思考过程（reasoning_content，升级点 15）
        on_log:   可选回调，用于输出过程日志（日志页：进度百分比/INFO/WARN/详细过程）
        pause_callback: 可选回调，当 Agent 需要用户输入时调用，
                        返回 {"paused": True, "data": {...}} 表示暂停，
                        返回 {"paused": False, "data": {...}} 表示已恢复
        settings_path:    每用户设置文件路径（多用户隔离；空则用全局 config/settings.json）
        study_areas_dir:  每用户研究区目录（多用户隔离；空则用全局 config/study_areas）
        conv_id:          当前对话 id（实验记录级联删除依据）
        project_id:       当前项目稳定 id（记忆按项目隔离）
        memory_manager:   MemoryManager 实例（None 则跳过记忆读写，向后兼容）
        exec_mode:        执行模式（approval / auto）；空则由角色编排取默认值
        prior_messages:   完整对话历史（修复「Agent 路径看不到上文」，技术方案 1.5(1)）
        session_state:    本对话已确认槽位（技术方案 8.2）

        以上三个参数均为可选，不传时行为与角色化改造前完全一致。
        """
        # 角色编排开关（技术方案第 12 章）：开启时走多角色路径，
        # 关闭时完全走下面的现有旧路径，行为与改造前一致
        if self._agent_settings(settings_path)["roles_enabled"]:
            return self.process_command_with_roles(
                user_input, on_token=on_token, on_log=on_log,
                pause_callback=pause_callback, project_dir=project_dir,
                workflow_callback=workflow_callback, settings_path=settings_path,
                study_areas_dir=study_areas_dir, conv_id=conv_id,
                project_id=project_id, memory_manager=memory_manager,
                exec_mode=exec_mode, prior_messages=prior_messages,
                session_state=session_state, on_thinking=on_thinking,
            )

        # 全局流式缓冲：process_command 与 _execute_plan 共用，
        # 保证气泡按"完整累积文本"展示整个中间过程（而不是被末尾覆盖）
        _stream_acc: List[str] = []
        def _emit(text):
            _stream_acc.append(text)
            if on_token:
                on_token("".join(_stream_acc))

        # 0. 提示已加载的研究区文件
        _study_areas_dir = (
            pathlib.Path(study_areas_dir) if study_areas_dir
            else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        )
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if uploaded:
            _emit(presentation.study_area_loaded(
                sorted(uploaded, key=lambda p: p.stat().st_mtime,
                       reverse=True)[0].name))

        # 检测是否为纯咨询类请求（例如参数推荐、原理解答、数据源问答）。
        # 这类请求不需要生成 JSON 执行计划，否则 LLM 可能返回类似
        # {"steps":[{"skill":"none",...}]} 的错误计划，导致 "未找到技能: none"。
        # 直接走 ask_stream 流式对话（注入记忆：领域知识 + 项目历史经验）。
        # 问句（含"什么"/以？结尾）默认视为咨询；含明确执行意图词时仍走计划。
        _is_advisory_request = (
            ("推荐" in user_input and "参数" in user_input)
            or ("原理" in user_input)
            or ("是什么" in user_input)
            or (
                (("什么" in user_input) or user_input.endswith("？") or user_input.endswith("?"))
                and not any(kw in user_input for kw in ["全流程", "一键", "跑完全流程", "执行全流程"])
            )
        )
        if _is_advisory_request:
            context = self._get_context(settings_path=settings_path, study_areas_dir=study_areas_dir)
            # 咨询路径同样注入记忆（让"上次 XX 效果如何"能答出历史数据），失败仅忽略
            if memory_manager is not None and project_id:
                try:
                    context["memory"] = memory_manager.enrich_prompt(project_id, user_input)
                except Exception:
                    pass
            return self.assistant.ask_stream(user_input, on_token, context=context,
                                             on_thinking=on_thinking)

        # 1. 获取当前软件状态
        context = self._get_context(settings_path=settings_path, study_areas_dir=study_areas_dir)

        # 2. 获取所有已注册 Skill 的描述
        tool_desc = self.registry.get_tool_descriptions_for_llm()

        # 3. 构建 System Prompt（注入记忆：领域知识 RAG + 项目历史经验；失败仅忽略，不影响主流程）
        memory_block = ""
        if memory_manager is not None and project_id:
            try:
                memory_block = memory_manager.enrich_prompt(project_id, user_input)
            except Exception as _e:
                memory_block = ""
        system_prompt = self._build_system_prompt(context, tool_desc, project_dir=project_dir, memory_block=memory_block)

        _emit(presentation.planning_started())

        # 4. 调用 LLM 生成执行计划（低温度；max_tokens 留足余量，
        #    若模型带推理，推理 token 也计入输出预算，太小的值会把 JSON 截断）
        response = self.assistant._call_api([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ], temperature=0.1, max_tokens=4096, on_thinking=on_thinking)

        # 5. 解析并执行计划（解析失败时用更严格的提示重试一次）
        plan = self._parse_plan(response)
        if plan is None:
            if response.startswith("API调用失败") or response.startswith("API流式调用失败"):
                return presentation.sanitize(response)
            _emit(presentation.planning_retry())
            response = self.assistant._call_api([
                {"role": "system", "content": system_prompt + "\n\n## 强制要求\n只输出一个JSON对象，不要任何解释文字、标题或代码块标记。"},
                {"role": "user", "content": user_input},
            ], temperature=0.0, max_tokens=4096, on_thinking=on_thinking)
            plan = self._parse_plan(response)
        if plan is None:
            # 全流程指令最终兜底：用内置完整计划，保证流程可继续执行
            if any(kw in user_input for kw in ["全流程", "一键", "跑完全流程", "执行全流程"]):
                _emit(presentation.planning_fallback())
                info = self._guess_region_from_input(user_input)
                plan = self._build_full_workflow_plan(info, study_areas_dir=study_areas_dir)
            else:
                # 其他指令解析失败时，返回原始响应方便用户排查
                return ("我没能把你的需求整理成可执行的步骤，请再说明一次"
                        "研究区、时间范围和要生成的产品。")

        _emit(presentation.plan_ready(len(plan.get("steps", []))))

        # 5.5 安全网：用户要求全流程时，确保计划完整且参数有效
        steps = plan.get("steps", [])
        skill_names = [s.get("skill", "") for s in steps]
        user_wants_workflow = any(kw in user_input for kw in ["全流程", "一键", "跑完全流程", "执行全流程"])

        # 检测是否需要强制修正：
        # 1. 只有 ai_assistant（LLM 不理解指令）
        # 2. 步骤数量不足（缺少关键 Skill）
        # 3. 关键参数为空（LLM 生成了错误的参数）
        required_skills = ["data_acquisition", "data_pipeline", "ttri_compute", "rf_model",
                           "tcr_compute", "lst_export", "accuracy_eval"]
        missing = [s for s in required_skills if s not in skill_names]

        has_empty_params = False
        for step in steps:
            params = step.get("params", {})
            if not params or all(v == "" or v is None for v in params.values()):
                has_empty_params = True
                break

        needs_fix = user_wants_workflow and (missing or has_empty_params
                                             or skill_names == ["ai_assistant"]
                                             or len(steps) == 0)

        if needs_fix:
            _emit(presentation.plan_completed_by_safety_net())
            info = self._guess_region_from_input(user_input)
            plan = self._build_full_workflow_plan(info, study_areas_dir=study_areas_dir)

        # 统一 LLM 生成的各步骤路径，避免不同步骤使用不一致的 output_dir
        self._normalize_plan_paths(plan, study_areas_dir=study_areas_dir)

        return self._execute_plan(plan, on_token=on_token, on_log=on_log, pause_callback=pause_callback, project_dir=project_dir, workflow_callback=workflow_callback, stream_acc=_stream_acc, settings_path=settings_path, study_areas_dir=study_areas_dir, conv_id=conv_id, project_id=project_id, memory_manager=memory_manager)

    # ── 角色编排入口（技术方案 2.1：Plan 交规划 Agent，Solve 由本类调度）────

    def _agent_settings(self, settings_path: str = "") -> Dict[str, Any]:
        """解析 settings.agent：每用户设置的 agent 段 > 全局 config/settings.json > 代码默认。"""
        user = self._load_config(settings_path)
        if isinstance(user.get("agent"), dict):
            return agent_config.resolve(user)
        return agent_config.resolve(self._load_config(""))

    def process_command_with_roles(self, user_input: str, on_token=None, on_log=None,
                                   pause_callback=None, project_dir: str = "",
                                   workflow_callback=None, settings_path: str = "",
                                   study_areas_dir: str = "", conv_id: str = "",
                                   project_id: str = "", memory_manager=None,
                                   exec_mode: str = "", prior_messages=None,
                                   session_state=None, on_thinking=None) -> str:
        """多角色路径（薄委托）：规划 Agent 出 plan，总调度按 plan 依次调用执行 Agent。

        实现在 `core/agent/orchestrator/role_flow.py`；与旧路径互不影响，
        `roles_enabled=False` 时永不进入这里。
        """
        from .orchestrator import role_flow

        return role_flow.run_with_roles(
            self, user_input, on_token=on_token, on_log=on_log,
            pause_callback=pause_callback, project_dir=project_dir,
            workflow_callback=workflow_callback, settings_path=settings_path,
            study_areas_dir=study_areas_dir, conv_id=conv_id, project_id=project_id,
            memory_manager=memory_manager, exec_mode=exec_mode,
            prior_messages=prior_messages, session_state=session_state,
            on_thinking=on_thinking,
        )

    def _resolved_study_areas_dir(self, study_areas_dir: str = "") -> str:
        path = (pathlib.Path(study_areas_dir) if study_areas_dir
                else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas")
        return str(path)

    def _list_study_areas(self, study_areas_dir: str = "") -> List[str]:
        base = pathlib.Path(self._resolved_study_areas_dir(study_areas_dir))
        if not base.exists():
            return []
        return [p.name for p in sorted(base.glob("*.geojson"),
                                       key=lambda p: p.stat().st_mtime, reverse=True)]

    def _guess_region_from_input(self, user_input: str) -> dict:
        """从用户输入中猜测研究区域和时间范围（仅旧路径兜底使用）"""
        region_map = {
            "武汉": "113.7,29.9,114.9,31.3",
            "北京": "115.4,39.4,117.5,41.1",
            "上海": "120.8,30.6,122.2,31.9",
            "广州": "112.8,22.8,114.0,23.8",
        }
        bbox = "113.7,29.9,114.9,31.3"  # 默认武汉
        for city, b in region_map.items():
            if city in user_input:
                bbox = b
                break

        # 尝试提取年月
        import re
        year_month = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', user_input)
        if year_month:
            year = int(year_month.group(1))
            month = int(year_month.group(2))
            start_date = f"{year}-{month:02d}-01"
            # 计算月份最后一天
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            end_date = f"{year}-{month:02d}-{last_day:02d}"
        else:
            start_date = "2024-07-01"
            end_date = "2024-07-31"

        return {"region": bbox, "start_date": start_date, "end_date": end_date}

    def _build_full_workflow_plan(self, info: dict, study_areas_dir: str = "") -> dict:
        """构建标准全流程执行计划

        Args:
            info: 包含 region, start_date, end_date 的字典
            study_areas_dir: 每用户研究区目录（多用户隔离；空则用全局 config/study_areas）
        """
        region = info.get("region", "113.7,29.9,114.9,31.3")
        start_date = info.get("start_date", "2024-07-01")
        end_date = info.get("end_date", "2024-07-31")

        # 优先使用已上传的研究区 GeoJSON 文件路径（用绝对路径，避免相对路径解析失败）
        _study_areas_dir = (
            pathlib.Path(study_areas_dir) if study_areas_dir
            else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        )
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if uploaded:
            study_file = str(sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0].resolve())
            region = study_file  # 用绝对路径代替 bbox

        return {
            "steps": [
                {
                    "skill": "data_acquisition",
                    "params": {"region": region, "start_date": start_date, "end_date": end_date,
                               "output_dir": "./output/raw"},
                    "reason": "下载遥感数据"
                },
                {
                    "skill": "data_pipeline",
                    "params": {"output_dir": "./output/processed"},
                    "reason": "数据预处理"
                },
                {
                    "skill": "ttri_compute",
                    "params": {"output_dir": "./output/processed"},
                    "reason": "计算TTRI"
                },
                {
                    "skill": "rf_model",
                    "params": {"output_dir": "./output/results"},
                    "reason": "训练RF模型"
                },
                {
                    "skill": "tcr_compute",
                    "params": {"output_dir": "./output/results"},
                    "reason": "计算TCR"
                },
                {
                    "skill": "lst_export",
                    "params": {"output_dir": "./output/results"},
                    "reason": "导出LST"
                },
                {
                    "skill": "accuracy_eval",
                    "params": {},
                    "reason": "精度评估"
                },
            ]
        }

    def _parse_plan(self, response: str) -> Optional[dict]:
        """从 LLM 响应中提取并解析 JSON 执行计划

        三级兜底（直接解析 → ```json``` 代码块 → 首尾大括号）的单一实现在
        `core/agent/roles/base_role.extract_json`，此处委托，避免两处解析策略漂移。
        """
        return extract_json(response)

    # ── 路径统一 ─────────────────────────────────────────────────────

    def _find_study_area_file(self, study_areas_dir: str = "",
                              preferred_name: str = "") -> Optional[str]:
        """查找研究区文件，返回绝对路径；未找到返回 None

        Args:
            study_areas_dir: 每用户研究区目录；空则用全局 config/study_areas
            preferred_name:  用户说的地名/文件名。给定时按「精确匹配 → 包含匹配」
                             找对应文件；都匹配不上再退回「取最新上传」。
                             不传时行为与改造前完全一致。
        """
        _study_areas_dir = (
            pathlib.Path(study_areas_dir) if study_areas_dir
            else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        )
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if not uploaded:
            return None
        # 用户手动指定了当前研究区（研究区面板「切换」写入 .current.txt 标记）→ 优先使用
        _mark = _study_areas_dir / ".current.txt"
        if _mark.exists():
            _cur = _mark.read_text(encoding="utf-8").strip()
            if _cur:
                _p = _study_areas_dir / _cur
                if _p.is_file():
                    return str(_p.resolve())
        if preferred_name:
            matched = match_study_area(uploaded, preferred_name)
            if matched is not None:
                return str(matched.resolve())
        latest = sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        return str(latest.resolve())

    def _load_config(self, settings_path: str = "") -> dict:
        """读取设置（每用户 settings_path 优先；空则全局 config/settings.json）"""
        path = (
            pathlib.Path(settings_path) if settings_path
            else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
        )
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def _looks_like_file_path(value: str) -> bool:
        """判断字符串是否像是文件路径"""
        if not value or not isinstance(value, str):
            return False
        lower = value.lower()
        return lower.endswith((".geojson", ".json", ".shp", ".kml", ".gpkg"))

    def _normalize_plan_paths(self, plan: dict, study_areas_dir: str = "") -> None:
        """统一执行计划中所有步骤的路径，确保 data_pipeline 等后续步骤
        使用与 data_acquisition 相同的基准目录。

        LLM 经常在不同步骤中生成不同的 output_dir 路径（如
        data_acquisition 用 './output/wuhan_lst_202407/raw' 而
        data_pipeline 用 './output/wuhan_202407/processed'），
        本方法会将所有路径归一化到 data_acquisition 的 output_dir 下。
        """
        steps = plan.get("steps", [])

        # 找到 data_acquisition 的 output_dir 作为基准
        base_dir = None
        region_dir = None
        for step in steps:
            if step.get("skill") == "data_acquisition":
                base_dir = step.get("params", {}).get("output_dir", "")
                break

        if not base_dir:
            # 无 data_acquisition 时，从其他步骤推断 region_dir
            for step in steps:
                od = step.get("params", {}).get("output_dir", "").replace("\\", "/").rstrip("/")
                if od and "./output/" in od:
                    od_parts = od.split("/")
                    for i, p in enumerate(od_parts):
                        if p == "output" and i + 1 < len(od_parts):
                            region_dir = "/".join(od_parts[:i + 2])
                            break
                    if region_dir:
                        break
            if not region_dir:
                return
        else:
            parts = base_dir.replace("\\", "/").rstrip("/").split("/")
            if parts[-1] in ("raw", "raw_data"):
                region_dir = "/".join(parts[:-1])
            else:
                region_dir = base_dir.rstrip("/")

        # ── 强制修正所有步骤路径（不依赖 LLM 生成的参数名）──
        # 预计算各步骤的 output_dir
        processed_dir = None
        results_dir = None
        for s in steps:
            sp = s.get("skill", "")
            sd = s.get("params", {}).get("output_dir", "").replace("\\", "/").rstrip("/")
            if sp == "data_pipeline":
                processed_dir = sd
            if sp == "rf_model":
                results_dir = sd

        # 从 raw 目录推断 processed/results 目录
        if not processed_dir:
            processed_dir = region_dir + "/processed"
        if not results_dir:
            results_dir = region_dir + "/results"

        # 验证 processed_dir 是否包含实际文件，否则搜索正确路径
        import glob as _glob
        if not os.path.isfile(processed_dir + "/30m_features_step2_meta.json"):
            # 搜索 output 目录下的 meta 文件，取最近修改的
            _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
            _output_root = os.path.normpath(_output_root)
            _found = _glob.glob(os.path.join(_output_root, "**/30m_features_step2_meta.json"), recursive=True)
            if _found:
                _found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                processed_dir = os.path.dirname(_found[0]).replace("\\", "/")

        # 预计算 rf_model 的实际 output_dir
        rf_output_dir = results_dir
        for s in steps:
            if s.get("skill") == "rf_model":
                rf_output_dir = s.get("params", {}).get("output_dir", "").replace("\\", "/").rstrip("/") or results_dir
                break

        # 修正每个步骤的所有路径
        for step in steps:
            params = step.get("params", {})
            skill = step.get("skill", "")

            # output_dir 修正
            if "output_dir" in params:
                old_dir = params["output_dir"].replace("\\", "/").rstrip("/")
                if old_dir == region_dir or old_dir == region_dir + "/":
                    pass
                elif old_dir.startswith(region_dir + "/"):
                    pass  # 已在 region_dir 下，不改
                elif old_dir.startswith("./output/"):
                    subdir = old_dir[len("./output/"):]
                    params["output_dir"] = region_dir + "/" + subdir

            # raw 波段文件路径
            for pk, fn in [("landsat_path", "landsat_lst.tif"), ("sentinel2_path", "sentinel2_bands.tif"),
                           ("qa_path", "landsat_qa_pixel.tif"), ("scl_path", "sentinel2_scl.tif"),
                           ("dem_path", "dem.tif")]:
                if pk in params:
                    params[pk] = region_dir + "/raw/" + fn

            # CSV 文件路径
            for pk, fn in [("train_csv", "train.csv"), ("val_csv", "validate.csv"),
                           ("test_csv", "test.csv"), ("full_30m_csv", "30m_features_step2.csv")]:
                if pk in params:
                    params[pk] = processed_dir + "/" + fn

            # meta JSON 路径
            for pk, fn in [("meta_30m_json", "30m_features_step2_meta.json"),
                           ("meta_10m_json", "10m_predict_features_meta.json")]:
                if pk in params:
                    params[pk] = processed_dir + "/" + fn

            # data_30m_csv / predict_10m_csv
            for pk, fn in [("data_30m_csv", "30m_features_step2.csv"),
                           ("predict_10m_csv", "10m_predict_features.csv")]:
                if pk in params:
                    params[pk] = processed_dir + "/" + fn

            # model_path（rf_model 在 output_dir/train/ 下输出模型）
            if "model_path" in params:
                import glob
                # 始终搜索整个 output 目录找最新的 pkl 模型
                _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
                _models = sorted(
                    glob.glob(os.path.join(os.path.normpath(_output_root), "**/*_model_*.pkl"), recursive=True),
                    key=lambda p: os.path.getmtime(p), reverse=True
                )
                if _models:
                    params["model_path"] = _models[0].replace("\\", "/")
                else:
                    params["model_path"] = rf_output_dir + "/train/rf_ttri_model.pkl"

            # TCR/LST 输出路径（使用 rf_model 的实际 output_dir）
            if skill == "tcr_compute" and "output_path" in params:
                params["output_path"] = self._find_latest(rf_output_dir, "tcr_result_*.csv",
                                                          rf_output_dir + "/tcr_result.csv")
            if skill == "lst_export":
                if "input_csv" in params:
                    # 搜索实际的 tcr_result（文件名带日期，升级点 4）
                    params["input_csv"] = self._find_latest(rf_output_dir, "tcr_result_*.csv",
                                                            rf_output_dir + "/tcr_result.csv")
            # accuracy_eval 的 predict_csv 指向 TCR 输出
            if skill == "accuracy_eval" and "predict_csv" in params:
                params["predict_csv"] = self._find_latest(rf_output_dir, "tcr_result_*.csv",
                                                          rf_output_dir + "/tcr_result.csv")

            # 兜底：如果关键 CSV 参数仍为空，搜索 output 目录
            _csv_search = {
                "data_30m_csv": "30m_features_step2.csv",
                "predict_10m_csv": "10m_predict_features.csv",
                "test_csv": "test.csv",
                "full_30m_csv": "30m_features_step2.csv",
                "predict_csv": "tcr_result.csv",
            }
            for pk, fn in _csv_search.items():
                if pk in params and not params[pk]:
                    _f = os.path.join(processed_dir, fn) if "30m" in fn or "10m" in fn or fn == "test.csv" else os.path.join(rf_output_dir, fn)
                    if not os.path.isfile(_f):
                        _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
                        _found = sorted(
                            glob.glob(os.path.join(os.path.normpath(_output_root), "**/" + fn), recursive=True),
                            key=lambda p: os.path.getmtime(p), reverse=True
                        )
                        if _found:
                            _f = _found[0]
                    params[pk] = _f

        # 强制 data_acquisition 的 region 使用已上传研究区文件的绝对路径：
        # 一旦存在已上传研究区，就以它为准，彻底屏蔽 LLM 生成城市名/bbox/错误路径
        # 等不一致输出导致 "could not convert string to float" 的解析崩溃。
        # plan 已由规划 Agent 解析出 region.study_area_file 时以它为准（技术方案 10.1）；
        # 旧格式 plan 无该字段，行为与改造前完全一致。
        planned_region = ""
        if isinstance(plan.get("region"), dict):
            planned_region = str(plan["region"].get("study_area_file") or "")
        study_area_file = planned_region or self._find_study_area_file(study_areas_dir)
        if study_area_file:
            for step in steps:
                if step.get("skill") == "data_acquisition":
                    step["params"]["region"] = study_area_file
                    break

    def _find_latest(self, directory: str, pattern: str, fallback: str) -> str:
        """在指定目录及全局 output 下找最新匹配文件（升级点 4：文件名带日期）。

        先精确目录，再兜底搜索 output 根；均未命中回退固定名。
        """
        import glob
        candidates = []
        if directory:
            candidates += glob.glob(os.path.join(directory, pattern))
        _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
        candidates += glob.glob(os.path.join(os.path.normpath(_output_root), "**", pattern),
                                recursive=True)
        if candidates:
            unique = list({os.path.abspath(p) for p in candidates})
            return max(unique, key=lambda p: os.path.getmtime(p)).replace("\\", "/")
        return fallback

    # ── 执行引擎 ─────────────────────────────────────────────────────

    def _execute_plan(self, plan: dict, on_token=None, on_log=None, pause_callback=None, project_dir: str = "", workflow_callback=None, stream_acc: Optional[list] = None, settings_path: str = "", study_areas_dir: str = "", conv_id: str = "", project_id: str = "", memory_manager=None, hooks=None, exec_mode: str = "", run_state=None) -> str:
        """遍历计划中的步骤，获取对应 Skill，执行并收集结果（薄委托）

        实现已平移到 `core/agent/executor.execute_plan`（技术方案 10.1/10.2）。
        本方法保留原有位置参数与关键字参数一字不改，是
        `tests/test_memory_synthetic.py` 的回归护栏；新增的 hooks / exec_mode /
        run_state 均为可选参数，不传时行为与平移前等价。

        on_token: 可选回调，用于流式输出气泡内容（阶段开始/完成摘要/最终结果）
        on_log:   可选回调，用于输出过程日志（进度百分比/INFO/WARN/详细过程）
        pause_callback: 可选回调，需要用户输入时调用
        hooks:    可选 StageHooks，角色编排的扩展点；None 时三个钩子全部短路
        """
        return executor.execute_plan(
            self, plan,
            on_token=on_token, on_log=on_log, pause_callback=pause_callback,
            project_dir=project_dir, workflow_callback=workflow_callback,
            stream_acc=stream_acc, settings_path=settings_path,
            study_areas_dir=study_areas_dir, conv_id=conv_id,
            project_id=project_id, memory_manager=memory_manager,
            hooks=hooks, exec_mode=exec_mode, run_state=run_state,
        )

    # ── 异常检测 ─────────────────────────────────────────────────────

    def _check_exceptions(self, skill_name: str, result: SkillResult,
                          pause_callback=None, _emit=None) -> bool:
        """检查 Skill 执行结果中的异常场景，必要时暂停询问用户

        返回 True 表示继续执行，False 表示已暂停等待用户输入
        """
        if _emit is None:
            _emit = lambda x: None

        if skill_name == "data_acquisition":
            # 配对选择已在执行循环中处理，这里只检查无配对情况
            pairs = result.data.get("image_pairs", [])
            if not pairs and "landsat_path" not in result.data:
                # 搜索模式无配对
                _emit("  没有找到符合条件的影像组合\n")
                _emit("  建议：扩大时间范围，或放宽云量要求\n")
            elif pairs and result.data.get("landsat_path"):
                # 下载模式下检查配对数量
                _emit(f"  已下载 {len(result.data.get('landsat_lst', [result.data.get('landsat_path')]))} 景影像\n")

        elif skill_name == "data_pipeline":
            valid_pixels = result.data.get("train_rows", 0)
            if valid_pixels < 10000:
                _emit(f"  有效训练样本仅 {valid_pixels:,} 个（建议不少于 10,000 个），模型精度可能偏低\n")

        elif skill_name == "rf_model":
            r2 = result.data.get("test_metrics", {}).get("R2", 1.0)
            if isinstance(r2, (int, float)) and r2 < 0.6:
                _emit(f"  测试集决定系数 {r2:.2f}，精度偏低。建议调整参数或更换时间段\n")

        elif skill_name == "tcr_compute":
            pass  # DEM标准差警告已移除

        return True

    # ── System Prompt ────────────────────────────────────────────────

    def _build_system_prompt(self, context: dict, tool_desc: str, project_dir: str = "", memory_block: str = "") -> str:
        """构建 Agent 的 System Prompt

        包含：可用 Skill 清单、领域知识（记忆注入；无记忆时基础兜底）、执行规则、当前软件状态
        """
        knowledge_section = memory_block if memory_block else f"## 领域知识\n{_BASIC_KNOWLEDGE}"
        return f"""你是GeoThermoAI的智能体，负责理解用户意图并编排Skill执行。

## 可用Skill清单
{tool_desc}

{knowledge_section}

## 执行规则
1. 用户指定具体算法（如"用XGBoost"）→ 选择对应Skill
2. 用户未指定 → 默认选择内置Skill（如rf_model）
3. 同组Skill可互相替换
4. tcr_compute、lst_export、accuracy_eval不依赖具体模型，正常执行
5. 发现异常时（无配对/云量高/样本不足/精度低），暂停执行并询问用户
6. **"全流程"/"一键"/"跑完全流程"** → 必须按顺序执行以下所有步骤：
   data_acquisition → data_pipeline → ttri_compute → rf_model → tcr_compute → lst_export → accuracy_eval
7. **禁止**在用户要求"全流程"时只选择 ai_assistant 或单个Skill

## 时间模糊处理
- 如果用户说"夏季"、"今年"、"最近"等模糊时间 → **必须**询问具体年份和月份
- 示例：用户说"处理武汉市夏季的数据" → 你应该回复"请确认具体年份和月份，例如：2024年7月"
- 不要在用户给出模糊时间时自行假设（如"夏季=6-8月"）
- 必须在用户给出明确的时间范围后才生成执行计划

## 输出格式（严格遵守）
你必须且只能返回一个JSON对象，不要输出任何其他内容（不要markdown标题、不要解释文字、不要代码块标记）。
格式如下：
{{"steps": [{{"skill": "skill_name", "params": {{}}, "reason": "选择该技能的原因"}}]}}

## 参数精简（重要，防止响应被截断）
1. params 中只放用户明确提到的关键信息：region（研究区/城市）、start_date、end_date
2. **禁止输出任何文件路径参数**（output_dir、xxx_path、model_path 等一律不要），系统会自动注入
3. 不要输出 cloud_threshold、dem_source 等已在配置里的参数
4. reason 用一句话概括，不超过30字
5. 整个响应必须尽量简短（一般300字以内），宁可少写不要多写

{_PROJECT_DIR_PROMPT.format(project_dir) if project_dir else ""}

当前软件状态: {json.dumps(context, ensure_ascii=False)}
"""

    # ── 上下文 ──────────────────────────────────────────────────────

    def _get_context(self, settings_path: str = "", study_areas_dir: str = "") -> dict:
        """返回当前软件状态上下文（多用户隔离：settings/研究区按用户）"""
        ctx = {"workflow_status": "idle", "config": {}}
        _study_areas_dir = (
            pathlib.Path(study_areas_dir) if study_areas_dir
            else pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        )
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if uploaded:
            ctx["study_area_file"] = str(sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0])

        # 读取用户配置，传给 LLM
        settings = self._load_config(settings_path)
        if settings:
            ctx["config"] = {
                "cloud_threshold": settings.get("data", {}).get("cloud_threshold", 30),
                "dem_source": settings.get("data", {}).get("dem_source", "copernicus"),
                "model_params": settings.get("model", {}),
                "processing": settings.get("processing", {}),
            }
        return ctx

    # ── 用户交互 ─────────────────────────────────────────────────────

    def _ask_user_to_select_pair(self, pairs: list, pause_callback, _emit,
                                  return_selected=False):
        """让用户选择影像配对

        Args:
            pairs: 配对列表
            pause_callback: 暂停回调
            _emit: 输出回调
            return_selected: 为 True 时返回选中的配对 dict，为 False 时返回 bool

        Returns:
            return_selected=True → 选中的配对 dict，或 None（等待用户）
            return_selected=False → True（继续）或 False（暂停）
        """
        if not pause_callback:
            _emit("  " + presentation.pairs_found(len(pairs)))
            _emit("  " + presentation.pair_auto_selected(1))
            selected = pairs[0] if pairs else None
            return selected if return_selected else bool(selected)

        pairs_info = []
        for i, pair in enumerate(pairs):
            info = {
                "index": i,
                "landsat_date": str(pair.get("landsat_date", "?")),
                "landsat_satellite": str(pair.get("landsat_satellite", "?")),
                "landsat_count": pair.get("landsat_count", 1),
                "landsat_scenes": pair.get("landsat_scenes", []),
                "landsat_cloud": pair.get("landsat_cloud_cover", "?"),
                "landsat_coverage": pair.get("landsat_coverage", "?"),
                "sentinel_date": str(pair.get("sentinel2_date", "?")),
                "sentinel_count": pair.get("sentinel2_count", 1),
                "sentinel_scenes": pair.get("sentinel2_scenes", []),
                "sentinel_cloud": pair.get("sentinel2_cloud_cover", "?"),
                "sentinel_coverage": pair.get("sentinel2_coverage", "?"),
            }
            # 数据 Agent 打过分时透传推荐标记（技术方案 3.3：只增加两个字段）；
            # 未打分时这两个字段不出现，前端旧逻辑完全不受影响
            if pair.get("recommended"):
                info["recommended"] = True
                info["recommend_reason"] = str(pair.get("recommend_reason", ""))
            if pair.get("quality_score") is not None:
                info["quality_score"] = pair.get("quality_score")
            pairs_info.append(info)

        # 「找到 N 组可用的影像组合」由执行引擎在调用本方法前统一输出，此处不再重复
        call_result = pause_callback({"type": "select_pair", "pairs": pairs_info})

        if call_result and call_result.get("paused"):
            return None if return_selected else False

        # 用户已选择，从回调返回值中获取选中的配对
        selected_data = None
        if call_result and not call_result.get("paused"):
            selected_key = call_result.get("data", {})
            selected_l_date = str(selected_key.get("landsat_date", ""))
            selected_s_date = str(selected_key.get("sentinel_date", ""))
            for pair in pairs:
                if (str(pair.get("landsat_date", "")) == selected_l_date
                        and str(pair.get("sentinel2_date", "")) == selected_s_date):
                    selected_data = pair
                    break
            if selected_data is None and pairs:
                selected_data = pairs[0]

        return selected_data if return_selected else True

    # ── 自动调参 ─────────────────────────────────────────────────────

    def _build_tuning_prompt(self, data_features: dict) -> str:
        """构建自动调参的 prompt

        包含数据特征（样本数、特征分布、地形复杂度等）和调参规则，
        要求 LLM 以 JSON 格式返回推荐的模型超参数。
        """
        return f"""你是GeoThermoAI的调参专家，请根据以下数据特征推荐随机森林超参数。

## 数据特征
- 训练样本数: {data_features.get('train_samples', 0):,}
- 验证样本数: {data_features.get('val_samples', 0):,}
- 测试样本数: {data_features.get('test_samples', 0):,}

## 特征分布
- NDVI均值: {data_features.get('ndvi_mean', 0):.2f} (标准差: {data_features.get('ndvi_std', 0):.2f})
  → 植被覆盖度: {"高" if data_features.get('ndvi_mean', 0) > 0.5 else "中" if data_features.get('ndvi_mean', 0) > 0.2 else "低"}
- DEM标准差: {data_features.get('dem_std', 0):.1f}m (范围: {data_features.get('dem_range', 0):.0f}m)
  → 地形复杂度: {"复杂" if data_features.get('dem_std', 0) > 100 else "中等" if data_features.get('dem_std', 0) > 30 else "平坦"}
- LST范围: {data_features.get('lst_range', 0):.1f}K (标准差: {data_features.get('lst_std', 0):.1f}K)
  → 温度变异性: {"大" if data_features.get('lst_std', 0) > 5 else "中" if data_features.get('lst_std', 0) > 2 else "小"}

## 调参规则
1. 样本数 > 50000 → n_estimators可以增大到200-500
2. 样本数 < 10000 → n_estimators减小到100-150，防止过拟合
3. 地形复杂（DEM标准差>100m）→ max_depth增大到30-40
4. 地形平坦（DEM标准差<30m）→ max_depth减小到15-20
5. 温度变异性大（LST标准差>5K）→ min_samples_leaf减小到5
6. 植被覆盖度高（NDVI>0.5）→ max_features可以增大到0.7

## 输出格式
返回JSON格式的参数推荐：
{{"n_estimators": 200, "max_depth": 25, "min_samples_split": 16, "min_samples_leaf": 8, "max_features": 0.5, "reason": "..."}}

只返回JSON，不要其他内容。"""

    # ── 数据特征收集 ─────────────────────────────────────────────────

    def _collect_data_features(self, pipeline_output: dict) -> dict:
        """从 data_pipeline 输出中收集数据特征

        计算训练集的特征统计（NDVI, NDWI, NDBI, DEM, LST），
        以及地形复杂度、植被覆盖度、温度范围等衍生指标。
        """
        import pandas as pd

        train_csv = pipeline_output.get("train_csv", "")
        if not train_csv:
            return {
                "train_samples": 0,
                "val_samples": 0,
                "test_samples": 0,
                "feature_stats": {},
                "dem_std": 0,
                "dem_range": 0,
                "ndvi_mean": 0,
                "ndvi_std": 0,
                "lst_range": 0,
                "lst_std": 0,
            }

        df_train = pd.read_csv(train_csv)

        # 计算特征统计
        feature_stats: Dict[str, Dict[str, float]] = {}
        for col in ["NDVI", "NDWI", "NDBI", "DEM", "LST"]:
            if col in df_train.columns:
                feature_stats[col] = {
                    "mean": float(df_train[col].mean()),
                    "std": float(df_train[col].std()),
                    "min": float(df_train[col].min()),
                    "max": float(df_train[col].max()),
                }

        # val / test 样本数
        val_csv = pipeline_output.get("val_csv", "")
        test_csv = pipeline_output.get("test_csv", "")
        val_samples = len(pd.read_csv(val_csv)) if val_csv else 0
        test_samples = len(pd.read_csv(test_csv)) if test_csv else 0

        return {
            "train_samples": len(df_train),
            "val_samples": val_samples,
            "test_samples": test_samples,
            "feature_stats": feature_stats,
            "dem_std": feature_stats.get("DEM", {}).get("std", 0),
            "dem_range": (
                feature_stats.get("DEM", {}).get("max", 0)
                - feature_stats.get("DEM", {}).get("min", 0)
            ),
            "ndvi_mean": feature_stats.get("NDVI", {}).get("mean", 0),
            "ndvi_std": feature_stats.get("NDVI", {}).get("std", 0),
            "lst_range": (
                feature_stats.get("LST", {}).get("max", 0)
                - feature_stats.get("LST", {}).get("min", 0)
            ),
            "lst_std": feature_stats.get("LST", {}).get("std", 0),
        }
