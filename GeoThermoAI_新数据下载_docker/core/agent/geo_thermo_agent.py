"""
GeoThermoAI 核心智能体

理解用户自然语言指令，自动选择 Skill 并编排执行，
在关键节点进行异常检测、自动调参和用户交互。
"""

import json
import glob
import os
import pathlib
from typing import Any, Dict, List, Optional, Callable

from ..ai_assistant import GeoThermoAI_Assistant
from ..skills.skill_registry import SkillRegistry
from ..skills.base_skill import SkillResult


# model_train_predict 组中所有 Skill 名称，用于自动调参判断
_MODEL_TRAIN_SKILLS = {"rf_model", "xgboost_model"}

# 特殊标记：Agent 需要用户输入才能继续
PAUSE_MARKER = "__AGENT_PAUSE__"

# 各 Skill 的阶段说明（气泡中"阶段开始"时展示，帮助用户了解每一步在做什么）
_STEP_DESCRIPTIONS = {
    "data_acquisition": "下载 Landsat 8/9、Sentinel-2 L2A 与 DEM 影像",
    "data_pipeline": "预处理并划分数据集：生成 30m 训练数据、完整约束层与 10m 预测数据",
    "ttri_compute": "拟合地形校正（TTRI）系数并空间化到 30m/10m 网格",
    "rf_model": "训练随机森林降尺度模型并输出独立精度评价",
    "tcr_compute": "计算地形校正残差（TCR）",
    "lst_export": "计算最终 10m 地表温度并导出 GeoTIFF",
    "accuracy_eval": "粗尺度闭合精度评估",
}


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

    def process_command(self, user_input: str, on_token=None, on_log=None, pause_callback=None, project_dir: str = "", workflow_callback=None) -> str:
        """处理用户自然语言指令

        流程：
        1. 获取当前软件状态上下文
        2. 获取所有已注册 Skill 的描述
        3. 构建 System Prompt
        4. 调用 LLM 生成执行计划（JSON）
        5. 解析并执行计划

        on_token: 可选回调，用于流式输出执行进度（气泡：阶段开始/完成摘要/最终结果）
        on_log:   可选回调，用于输出过程日志（日志页：进度百分比/INFO/WARN/详细过程）
        pause_callback: 可选回调，当 Agent 需要用户输入时调用，
                        返回 {"paused": True, "data": {...}} 表示暂停，
                        返回 {"paused": False, "data": {...}} 表示已恢复
        """
        # 全局流式缓冲：process_command 与 _execute_plan 共用，
        # 保证气泡按"完整累积文本"展示整个中间过程（而不是被末尾覆盖）
        _stream_acc: List[str] = []
        def _emit(text):
            _stream_acc.append(text)
            if on_token:
                on_token("".join(_stream_acc))

        # 0. 提示已加载的研究区文件
        _study_areas_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if uploaded:
            _emit(f"📁 已加载研究区文件：{sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0].name}\n")

        # 检测是否为纯咨询类请求（例如参数推荐、原理解答）。
        # 这类请求不需要生成 JSON 执行计划，否则 LLM 可能返回类似
        # {"steps":[{"skill":"none",...}]} 的错误计划，导致 "未找到技能: none"。
        # 直接走 ask_stream 流式对话。
        _is_advisory_request = (
            ("推荐" in user_input and "参数" in user_input)
            or ("原理" in user_input)
            or ("是什么" in user_input)
        )
        if _is_advisory_request:
            context = self._get_context()
            return self.assistant.ask_stream(user_input, on_token, context=context)

        # 1. 获取当前软件状态
        context = self._get_context()

        # 2. 获取所有已注册 Skill 的描述
        tool_desc = self.registry.get_tool_descriptions_for_llm()

        # 3. 构建 System Prompt（传入 project_dir 让 LLM 知道项目目录已设置）
        system_prompt = self._build_system_prompt(context, tool_desc, project_dir=project_dir)

        _emit("正在调用 LLM 生成执行计划...\n")

        # 4. 调用 LLM 生成执行计划（低温度；max_tokens 留足余量，
        #    若模型带推理，推理 token 也计入输出预算，太小的值会把 JSON 截断）
        response = self.assistant._call_api([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ], temperature=0.1, max_tokens=4096)

        # 5. 解析并执行计划（解析失败时用更严格的提示重试一次）
        plan = self._parse_plan(response)
        if plan is None:
            if response.startswith("API调用失败") or response.startswith("API流式调用失败"):
                return f"⚠️ {response}"
            _emit("⚠️ 执行计划解析失败，重试一次...\n")
            response = self.assistant._call_api([
                {"role": "system", "content": system_prompt + "\n\n## 强制要求\n只输出一个JSON对象，不要任何解释文字、标题或代码块标记。"},
                {"role": "user", "content": user_input},
            ], temperature=0.0, max_tokens=4096)
            plan = self._parse_plan(response)
        if plan is None:
            # 全流程指令最终兜底：用内置完整计划，保证流程可继续执行
            if any(kw in user_input for kw in ["全流程", "一键", "跑完全流程", "执行全流程"]):
                _emit("⚠️ LLM 计划解析失败，改用内置完整工作流计划继续执行...\n")
                info = self._guess_region_from_input(user_input)
                plan = self._build_full_workflow_plan(info)
            else:
                # 其他指令解析失败时，返回原始响应方便用户排查
                return f"⚠️ 执行计划解析失败，返回内容如下（可截图反馈）：\n{response[:500]}"

        _emit("执行计划已生成\n")

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
            _emit("⚠️ LLM 返回的执行计划不完整，自动修正为完整工作流...\n")
            info = self._guess_region_from_input(user_input)
            plan = self._build_full_workflow_plan(info)

        # 统一 LLM 生成的各步骤路径，避免不同步骤使用不一致的 output_dir
        self._normalize_plan_paths(plan)

        return self._execute_plan(plan, on_token=on_token, on_log=on_log, pause_callback=pause_callback, project_dir=project_dir, workflow_callback=workflow_callback, stream_acc=_stream_acc)

    def _guess_region_from_input(self, user_input: str) -> dict:
        """从用户输入中猜测研究区域和时间范围"""
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

    def _build_full_workflow_plan(self, info: dict) -> dict:
        """构建标准全流程执行计划

        Args:
            info: 包含 region, start_date, end_date 的字典
        """
        region = info.get("region", "113.7,29.9,114.9,31.3")
        start_date = info.get("start_date", "2024-07-01")
        end_date = info.get("end_date", "2024-07-31")

        # 优先使用已上传的研究区 GeoJSON 文件路径（用绝对路径，避免相对路径解析失败）
        _study_areas_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
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
        """从 LLM 响应中提取并解析 JSON 执行计划"""
        text = response.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从 ```json ... ``` 代码块中提取
        import re
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试找到第一个 { 和最后一个 } 之间的内容
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                pass

        return None

    # ── 路径统一 ─────────────────────────────────────────────────────

    @staticmethod
    def _find_study_area_file() -> Optional[str]:
        """查找最新上传的研究区文件，返回绝对路径；未找到返回 None"""
        _study_areas_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if not uploaded:
            return None
        latest = sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        return str(latest.resolve())

    @staticmethod
    def _looks_like_file_path(value: str) -> bool:
        """判断字符串是否像是文件路径"""
        if not value or not isinstance(value, str):
            return False
        lower = value.lower()
        return lower.endswith((".geojson", ".json", ".shp", ".kml", ".gpkg"))

    def _normalize_plan_paths(self, plan: dict) -> None:
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
                params["output_path"] = rf_output_dir + "/tcr_result.csv"
            if skill == "lst_export":
                if "input_csv" in params:
                    # 搜索实际的 tcr_result.csv
                    _tcr_path = rf_output_dir + "/tcr_result.csv"
                    if not os.path.isfile(_tcr_path):
                        _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
                        _tcr_files = sorted(
                            glob.glob(os.path.join(os.path.normpath(_output_root), "**/tcr_result.csv"), recursive=True),
                            key=lambda p: os.path.getmtime(p), reverse=True
                        )
                        if _tcr_files:
                            _tcr_path = _tcr_files[0]
                    params["input_csv"] = _tcr_path
            # accuracy_eval 的 predict_csv 指向 TCR 输出
            if skill == "accuracy_eval" and "predict_csv" in params:
                _tcr_path = rf_output_dir + "/tcr_result.csv"
                if not os.path.isfile(_tcr_path):
                    _output_root = os.path.join(os.path.dirname(__file__), "..", "..", "output")
                    _tcr_files = sorted(
                        glob.glob(os.path.join(os.path.normpath(_output_root), "**/tcr_result.csv"), recursive=True),
                        key=lambda p: os.path.getmtime(p), reverse=True
                    )
                    if _tcr_files:
                        _tcr_path = _tcr_files[0]
                params["predict_csv"] = _tcr_path

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
        study_area_file = self._find_study_area_file()
        if study_area_file:
            for step in steps:
                if step.get("skill") == "data_acquisition":
                    step["params"]["region"] = study_area_file
                    break

    # ── 执行引擎 ─────────────────────────────────────────────────────

    def _execute_plan(self, plan: dict, on_token=None, on_log=None, pause_callback=None, project_dir: str = "", workflow_callback=None, stream_acc: Optional[list] = None) -> str:
        """遍历计划中的步骤，获取对应 Skill，执行并收集结果

        特殊处理：
        - data_acquisition 完成后，将其实际输出路径注入后续步骤
        - data_pipeline 完成后收集数据特征
        - rf_model 执行前若用户未手动指定参数，自动调用 LLM 推荐超参数
        - 自动统一所有步骤的 output_dir 路径
        - 当 data_acquisition 找到多组影像配对时，通过 pause_callback 询问用户

        on_token: 可选回调，用于流式输出气泡内容（阶段开始/完成摘要/最终结果）
        on_log:   可选回调，用于输出过程日志（进度百分比/INFO/WARN/详细过程）
        pause_callback: 可选回调，需要用户输入时调用
        """
        results: List[str] = []
        data_features: Optional[dict] = None  # 缓存 data_pipeline 输出的数据特征
        _emit_accumulator = stream_acc if stream_acc is not None else []  # 与 process_command 共用缓冲

        # 解析项目目录：所有路径写死相对于 project_dir
        raw_dir = (project_dir + "/raw").replace("\\", "/") if project_dir else ""
        processed_dir = (project_dir + "/processed").replace("\\", "/") if project_dir else ""
        results_dir = (project_dir + "/results").replace("\\", "/") if project_dir else ""

        steps = plan.get("steps", [])
        total = len(steps)

        def _emit(text, to_log=False):
            # to_log=True 的过程日志只进日志页（on_log），不进气泡/对话历史；
            # 其余内容进气泡累加器，经 on_token 推全文给前端气泡
            if to_log:
                if on_log:
                    on_log(text)
                return
            _emit_accumulator.append(text)
            full_text = "".join(_emit_accumulator)
            if on_token:
                on_token(full_text)

        # 硬编码路径映射（每个 skill 的输入/输出路径）
        SKILL_PATHS = {
            "data_acquisition": {
                "output_dir": raw_dir,
            },
            "data_pipeline": {
                "output_dir": processed_dir,
                "landsat_path": raw_dir + "/landsat_lst.tif",
                "qa_path": raw_dir + "/landsat_qa_pixel.tif",
                "sentinel2_path": raw_dir + "/sentinel2_bands.tif",
                "scl_path": raw_dir + "/sentinel2_scl.tif",
                "dem_path": raw_dir + "/dem.tif",
            },
            "ttri_compute": {
                "output_dir": processed_dir,
                "data_30m_csv": processed_dir + "/30m_features_step2.csv",
                "predict_10m_csv": processed_dir + "/10m_predict_features.csv",
                "train_csv": processed_dir + "/train.csv",
                "val_csv": processed_dir + "/validate.csv",
                "test_csv": processed_dir + "/test.csv",
            },
            "rf_model": {
                "output_dir": results_dir,
                "train_csv": processed_dir + "/train.csv",
                "val_csv": processed_dir + "/validate.csv",
                "test_csv": processed_dir + "/test.csv",
            },
            "tcr_compute": {
                 "output_dir": results_dir,
                 "output_path": results_dir + "/tcr_result.csv",
                 "data_30m_csv": processed_dir + "/30m_features_step2.csv",
                 "meta_30m_json": processed_dir + "/30m_features_step2_meta.json",
                 "predict_10m_csv": processed_dir + "/10m_predict_features.csv",
                 "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
                 "model_path": None,  # 动态查找
             },
            "lst_export": {
                 "output_dir": results_dir,
                 "input_csv": results_dir + "/tcr_result.csv",
                 "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
             },
            "accuracy_eval": {
                "output_dir": results_dir,
                "test_csv": processed_dir + "/test.csv",
                "full_30m_csv": processed_dir + "/30m_features_step2.csv",
                "predict_csv": results_dir + "/tcr_result.csv",
                "meta_30m_json": processed_dir + "/30m_features_step2_meta.json",
                "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
            },
        }

        for i, step in enumerate(steps):
            skill_name = step["skill"]
            skill = self.registry.get(skill_name)

            # 注入硬编码路径
            if project_dir and skill_name in SKILL_PATHS:
                params = step.get("params", {})
                if not params:
                    params = {}
                for k, v in SKILL_PATHS[skill_name].items():
                    if v is None:
                        # 动态查找：model_path 等
                        if k == "model_path" and results_dir:
                            _mdir = results_dir + "/train"
                            if os.path.isdir(_mdir):
                                _pkls = sorted(
                                    glob.glob(os.path.join(_mdir, "*.pkl")),
                                    key=lambda p: os.path.getmtime(p), reverse=True
                                )
                                if _pkls:
                                    params[k] = _pkls[0].replace("\\", "/")
                    else:
                        params[k] = v
                step["params"] = params

            # 注入用户配置参数（不覆盖 LLM 已指定的值）
            if skill_name == "data_acquisition":
                _cfg = {}
                _sp = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
                if _sp.is_file():
                    try:
                        with open(_sp, "r", encoding="utf-8") as _f:
                            _cfg = json.load(_f).get("data", {})
                    except Exception:
                        pass
                if "cloud_threshold" not in step.get("params", {}):
                    step.setdefault("params", {})["cloud_threshold"] = _cfg.get("cloud_threshold", 30)
                if "dem_source" not in step.get("params", {}):
                    step.setdefault("params", {})["dem_source"] = _cfg.get("dem_source", "copernicus")

            if not skill:
                results.append(f"未找到技能: {skill_name}")
                _emit(f"  ❌ 未找到技能: {skill_name}\n")
                continue

            # 强制 data_acquisition 的 region 使用已上传研究区文件（执行期兜底：
            # 即使 _normalize_plan_paths 的替换未生效，也保证 region 是 GeoJSON 绝对路径，
            # 屏蔽 LLM 生成的纯城市名/bbox 导致的解析崩溃）
            if skill_name == "data_acquisition":
                _sa = self._find_study_area_file()
                if _sa:
                    step.setdefault("params", {})["region"] = _sa

            _desc = _STEP_DESCRIPTIONS.get(skill_name, "")
            _emit(f"**Step {i+1}/{total}**: {skill_name}" + (f" — {_desc}" if _desc else "") + "\n")
            # 更新工作流进度（running）
            if workflow_callback:
                workflow_callback(skill_name, "running", i, total)

            # ── 自动调参：model_train_predict 组 Skill 执行前 ─────────────
            if skill_name in _MODEL_TRAIN_SKILLS:
                # 从 settings.json 注入用户设置的模型参数
                _settings_path = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
                if _settings_path.exists():
                    try:
                        with open(_settings_path, "r", encoding="utf-8") as _f:
                            _cfg = json.load(_f)
                        _model_cfg = _cfg.get("model", {})
                        for k in ["n_estimators", "max_depth", "min_samples_split", "min_samples_leaf", "max_features"]:
                            v = _model_cfg.get(k)
                            if v is not None:
                                step["params"][k] = v
                    except Exception:
                        pass

                user_specified = bool(step.get("params"))
                if data_features is not None and not user_specified:
                    _emit("正在调用 LLM 推荐模型参数...\n")
                    tuning_prompt = self._build_tuning_prompt(data_features)
                    tuning_response = self.assistant._call_api([
                        {"role": "system", "content": tuning_prompt},
                    ])
                    try:
                        recommended_params = json.loads(tuning_response)
                        step["params"] = recommended_params
                        results.append(
                            f"[自动调参] 根据数据特征推荐参数: "
                            f"{json.dumps(recommended_params, ensure_ascii=False)}"
                        )
                    except json.JSONDecodeError:
                        results.append(
                            "[自动调参] LLM 返回的参数无法解析，将使用 Skill 默认参数"
                        )

            # ── 执行 Skill（data_acquisition 特殊处理：搜索→选择→下载）─
            try:
                def _log(tag, msg):
                    _emit(f"  [{tag}] {msg}\n", to_log=True)
                def _progress(name, pct, msg):
                    _emit(f"  {name} {int(pct*100)}%: {msg}\n", to_log=True)

                # data_acquisition: 先搜索返回配对，用户选择后再下载
                if skill_name == "data_acquisition" and not step.get("params", {}).get("selected_pair"):
                    # 第一次：搜索模式
                    result = skill.execute(
                        step.get("params", {}),
                        progress_callback=_progress,
                        log_callback=_log,
                    )
                    result_data = result.data if isinstance(result.data, dict) else {}
                    pairs = result_data.get("image_pairs", [])
                    if pairs:
                        _emit(f"  📋 找到 {len(pairs)} 组影像配对\n")
                        # 总是让用户确认选择，即使只有一对
                        selected = None
                        if pause_callback:
                            selected = self._ask_user_to_select_pair(
                                pairs, pause_callback, _emit, return_selected=True)
                            if selected is None:
                                _emit(f"⏸️ 等待用户选择...\n")
                                return "\n".join(results) + f"\n{PAUSE_MARKER}"
                        else:
                            selected = pairs[0]
                            _emit(f"  ✅ 自动选择第 1 对\n")
                        # 注入选择，重新执行下载
                        step["params"]["selected_pair"] = selected
                        _emit(f"  **开始下载所选配对数据**\n")
                        result = skill.execute(
                            step.get("params", {}),
                            progress_callback=_progress,
                            log_callback=_log,
                        )
                    else:
                        # 执行失败：透出真实错误，不要伪装成"未找到配对"
                        if not result.success:
                            _msg = (result.message or f"{skill_name} 执行失败").strip()
                            _emit(f"  ❌ {_msg}\n")
                            results.append(f"{skill_name}: {_msg}")
                            if workflow_callback:
                                workflow_callback(skill_name, "failed", i, total)
                            return "\n".join(results)
                        _lc = result_data.get("landsat_count", 0)
                        _sc = result_data.get("sentinel_count", 0)
                        _emit(f"  ⚠️ 未找到影像配对（Landsat {_lc} 景 / Sentinel {_sc} 景），后续步骤终止\n")
                        results.append(
                            f"{skill_name}: 未找到符合条件的影像配对"
                            f"（Landsat {_lc} 景 / Sentinel {_sc} 景）"
                        )
                        return "\n".join(results)
                else:
                    # 普通执行或其他 Skill
                    result = skill.execute(
                        step.get("params", {}),
                        progress_callback=_progress,
                        log_callback=_log,
                    )

                results.append(f"{skill_name}: {result.message}")
                _emit(f"  {skill_name}: {result.message}\n")
                # 更新工作流进度（completed）
                if workflow_callback:
                    workflow_callback(skill_name, "completed", i, total)

                # data_acquisition 完成后，缓存实际输出路径
                if skill_name == "data_acquisition" and result.success:
                    acquisition_outputs = dict(result.data) if isinstance(result.data, dict) else {}
                    # 确保 output_dir 存在
                    if "output_dir" not in acquisition_outputs:
                        acquisition_outputs["output_dir"] = step.get("params", {}).get("output_dir", "")

                # data_pipeline 完成后收集数据特征
                if skill_name == "data_pipeline" and result.success:
                    data_features = self._collect_data_features(
                        result.data if isinstance(result.data, dict) else {})

                # 检查异常场景
                should_continue = self._check_exceptions(skill_name, result, pause_callback, _emit)
                if not should_continue:
                    # Agent 暂停等待用户输入，中止后续步骤
                    _emit(f"⏸️ 等待用户选择...\n")
                    return "\n".join(results) + f"\n{PAUSE_MARKER}"
            except Exception as e:
                results.append(f"{skill_name} 失败: {e}")
                _emit(f"  {skill_name} 失败: {e}\n")
                if workflow_callback:
                    workflow_callback(skill_name, "failed", i, total)

        return "\n".join(results)

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
                _emit("  ⚠️ 未找到符合条件的影像配对\n")
                _emit("  建议：①扩大时间范围 ②放宽云量阈值\n")
            elif pairs and result.data.get("landsat_path"):
                # 下载模式下检查配对数量
                _emit(f"  ✅ 已下载 {len(result.data.get('landsat_lst', [result.data.get('landsat_path')]))} 景影像\n")

        elif skill_name == "data_pipeline":
            valid_pixels = result.data.get("train_rows", 0)
            if valid_pixels < 10000:
                _emit(f"  ⚠️ 有效训练样本仅 {valid_pixels:,} 个（建议>10,000），模型精度可能较低\n")

        elif skill_name == "rf_model":
            r2 = result.data.get("test_metrics", {}).get("R2", 1.0)
            if isinstance(r2, (int, float)) and r2 < 0.6:
                _emit(f"  ⚠️ 模型 R²={r2:.2f}，精度较低。建议：调整参数或选择其他时间\n")

        elif skill_name == "tcr_compute":
            pass  # DEM标准差警告已移除

        return True

    # ── System Prompt ────────────────────────────────────────────────

    def _build_system_prompt(self, context: dict, tool_desc: str, project_dir: str = "") -> str:
        """构建 Agent 的 System Prompt

        包含：可用 Skill 清单、领域知识、执行规则、当前软件状态
        """
        return f"""你是GeoThermoAI的智能体，负责理解用户意图并编排Skill执行。

## 可用Skill清单
{tool_desc}

## 领域知识
- TTRI（地形热响应指数）= a*DEM + b*Slope + c*cos(Aspect)
- TCR（热约束修正）用于修正地形对LST的影响
- 降尺度：Landsat 30m LST + Sentinel 10m多光谱 → 10m LST
- Landsat重访周期16天，Sentinel重访周期5天
- 影像配对要求：Landsat与Sentinel时间差≤2天

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

    def _get_context(self) -> dict:
        """返回当前软件状态上下文"""
        ctx = {"workflow_status": "idle", "config": {}}
        _study_areas_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "study_areas"
        uploaded = list(_study_areas_dir.glob("*.geojson")) if _study_areas_dir.exists() else []
        if uploaded:
            ctx["study_area_file"] = str(sorted(uploaded, key=lambda p: p.stat().st_mtime, reverse=True)[0])

        # 读取用户配置，传给 LLM
        _settings_path = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
        if _settings_path.is_file():
            try:
                with open(_settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                ctx["config"] = {
                    "cloud_threshold": settings.get("data", {}).get("cloud_threshold", 30),
                    "dem_source": settings.get("data", {}).get("dem_source", "copernicus"),
                    "model_params": settings.get("model", {}),
                    "processing": settings.get("processing", {}),
                }
            except Exception:
                pass
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
            _emit(f"  📋 找到 {len(pairs)} 组影像配对，自动选择第 1 对\n")
            selected = pairs[0] if pairs else None
            return selected if return_selected else bool(selected)

        pairs_info = []
        for i, pair in enumerate(pairs):
            pairs_info.append({
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
            })

        _emit(f"  📋 找到 {len(pairs)} 组影像配对，请选择：\n")

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
1. 样本数 > 50000 → n_estimators可以增大到300-500
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
