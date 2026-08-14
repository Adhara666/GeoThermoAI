"""
规划 Agent

职责：判定意图 → 多轮补全槽位 → 反问 → 出结构化 plan → 轻反思。
边界：不执行任何 Skill，信息不全禁止放行。
规则：每条消息都过一次意图分类，关键词表只作为 LLM 不可用时的兜底。
"""

import datetime
import json
import os
import pathlib
import re
from typing import Any, Dict, List, Optional

from .. import plan_schema
from ..orchestrator.agent_config import REPLAN_MAX
from ..reflection import planner_rules
from ..reflection.result import Action, ReflectionResult
from . import slots as slot_utils
from .base_role import RoleAgent
from .prompts import planner as planner_prompts

# 关键词兜底（LLM 不可用时判定「是否要跑流程」，与 server._AGENT_KEYWORDS 同源语义）
# 关键词统一来源（I1）：从 orchestrator/keywords.py 导入，不再各自定义
from ..orchestrator.keywords import (
    is_postprocess_request as _is_postprocess_request,
    is_postprocess_soft_request as _is_postprocess_soft_request,
    is_partial_request as _is_partial_request,
    is_continuation_request as _is_continuation_request,
    is_correction_request as _is_correction_request,
    negation_slot_terms as _negation_slot_terms,
    POSTPROCESS_KEYWORDS as _POSTPROCESS_KEYWORDS,
    POSTPROCESS_SOFT_KEYWORDS as _POSTPROCESS_SOFT_KEYWORDS,
    FULLWORKFLOW_MARKERS as _FULLWORKFLOW_MARKERS,
    FALLBACK_TASK_KEYWORDS as _FALLBACK_TASK_KEYWORDS,
    TASK_PRODUCT_MARKERS,
)

# 沿用旧名（代码中多处引用）
_FULL_WORKFLOW_KEYWORDS = _FULLWORKFLOW_MARKERS


class PlannerContext:
    """规划 Agent 的输入上下文（必须全给）。"""

    def __init__(self, *, user_input: str, prior_messages: Optional[List[dict]] = None,
                 session_state: Optional[dict] = None, study_areas: Optional[List[str]] = None,
                 study_areas_dir: str = "", project_dir: str = "",
                 settings: Optional[dict] = None, skill_catalog: str = "",
                 replan_reason: str = "", replan_count: int = 0,
                 previous_plan: Optional[dict] = None, today: Optional[datetime.date] = None,
                 replan_payload: Optional[dict] = None,
                 adjust_history: Optional[List[str]] = None):
        self.user_input = user_input or ""
        self.prior_messages = list(prior_messages or [])
        self.session_state = dict(session_state or {})
        self.study_areas = list(study_areas or [])
        self.study_areas_dir = study_areas_dir
        self.project_dir = project_dir
        self.settings = dict(settings or {})
        self.skill_catalog = skill_catalog
        self.replan_reason = replan_reason
        self.replan_count = int(replan_count or 0)
        self.previous_plan = previous_plan
        self.today = today or datetime.date.today()
        # replan 失败诊断（子 Agent 上报的 detail / violations / suggestions）
        self.replan_payload = dict(replan_payload or {})
        # 已尝试的调整历史（供 LLM 避免重复相同调整）
        self.adjust_history = list(adjust_history or [])

    def study_area_paths(self) -> List[pathlib.Path]:
        base = pathlib.Path(self.study_areas_dir) if self.study_areas_dir else None
        if base is None or not base.exists():
            return []
        return sorted(base.glob("*.geojson"), key=lambda p: p.stat().st_mtime, reverse=True)


class PlannerOutcome:
    """规划结果：总调度按 action 决定下一步。"""

    CHAT = "chat"     # 转流式对话（纯聊天或领域问答）
    ASK = "ask"       # 反问用户，本轮不跑任何 Skill
    PLAN = "plan"     # 出了合法 plan，可以进入 solve

    def __init__(self, action: str, plan: Optional[dict] = None, question: str = "",
                 intent: str = "", slots: Optional[dict] = None, note: str = "",
                 reflection: Optional[ReflectionResult] = None):
        self.action = action
        self.plan = plan
        self.question = question
        self.intent = intent
        self.slots = dict(slots or {})
        self.note = note
        self.reflection = reflection


class PlannerAgent(RoleAgent):
    role = "planner"
    role_name = "规划"

    def __init__(self, assistant, registry, memory_manager=None, project_id: str = "",
                 on_log=None, replan_max: int = REPLAN_MAX, on_thinking=None):
        super().__init__(assistant, memory_manager=memory_manager,
                         project_id=project_id, on_log=on_log,
                         on_thinking=on_thinking)
        self.registry = registry
        self.replan_max = replan_max
        self._current_user_input = ""  # 供 _builtin_steps 推断 partial 步骤用

    # ── 第一步：意图分类 + 槽位抽取 ────────────────────────────────

    def classify_intent(self, ctx: PlannerContext) -> Dict[str, Any]:
        """低成本意图分类（temperature=0）。

        LLM 不可用时退回关键词兜底。

        `max_tokens` 提到 2048：带隐藏推理链的模型（如 DeepSeek-V4-Flash）思考过程
        也计入输出预算，太小会把 JSON 答案截断而退回关键词兜底。
        """
        prompt = planner_prompts.intent_prompt(
            session_context=self._session_context_text(ctx),
            study_areas="、".join(ctx.study_areas) or "",
            existing_products=self._existing_products_text(ctx),
        )
        parsed = self.call_json(prompt, ctx.user_input, temperature=0.0, max_tokens=2048,
                               history=self._recent_history(ctx))
        if parsed is None:
            return self._keyword_fallback(ctx)

        intent = str(parsed.get("intent") or "").strip()
        if intent not in plan_schema.INTENTS:
            intent = self._keyword_fallback(ctx)["intent"]
        # 关键词预判（软性）→ 二次判定：用户单独提出「无空洞/填洞/结果后处理」而 LLM
        # 判成其他时，追加一次低成本判定让 LLM 拍板——是对已有结果的后处理还是新任务。
        # 主路径是 LLM 智能判断（prompt 已喂入「已有产物」上下文，见 _existing_products_text）；
        # 关键词规则做最后防线：只要用户明确说了「结果后处理 / 无空洞 / 填洞」等关键词，
        # 无论 LLM 二次判定如何、无论 _existing_products_text 是否扫描到结果，都改判 postprocess。
        # 原因：_existing_products_text 可能因路径编码、project_dir 未恢复等原因返回空，
        # 导致 LLM 失去「已有结果」上下文而误判 task 并反问时间范围——这比让 executor
        # 在找不到文件时报「未找到已生成的 10m LST 结果」更不智能。
        if intent != "postprocess" and _is_postprocess_request(ctx.user_input):
            if self._confirm_postprocess(ctx):
                self.log(f"意图修正：LLM 判定 {intent}，二次判定为 postprocess，已改判")
                intent = "postprocess"
                parsed["question"] = ""
            else:
                # LLM 二次判定坚持新任务：关键词命中时直接改判 postprocess，
                # 不依赖 _existing_products_text（文件扫描可能因编码/路径问题返回空）。
                # 如果确实没有结果，executor 的 lst_gapfill 会给出明确错误。
                _has_results = bool(self._existing_products_text(ctx))
                self.log(f"意图修正：LLM 二次判定判新任务，但关键词命中 postprocess"
                         f"（已有结果: {_has_results}），强制改判 postprocess")
                intent = "postprocess"
                parsed["question"] = ""
        # 模糊关键词预判（上下文感知）：用户说「继续」「处理一下」「补全」等模糊词，
        # 且工作目录有已有结果时，也改判 postprocess。这覆盖了用户跑完全流程后
        # 用自然语言（而非明确关键词）要求后处理的场景，让 Agent 更智能。
        if intent != "postprocess" and _is_postprocess_soft_request(ctx.user_input):
            _has_results = bool(self._existing_products_text(ctx))
            if _has_results:
                self.log(f"意图修正：模糊关键词命中 + 已有结果，改判 postprocess")
                intent = "postprocess"
                parsed["question"] = ""
            else:
                self.log(f"模糊关键词命中但无已有结果，维持 LLM 判定 {intent}")
        # partial 关键词预判：LLM 误判 task/modify/unclear/qa/chat 时，如果用户只说了下载/搜索/预处理等
        # 关键词（没提全流程/产品/降尺度，且不是疑问句），改判 partial，避免把部分请求
        # 当全流程跑7步或当成问答/闲聊。is_partial_request 已排除疑问句和全流程标记，
        # 不会误伤真正的 qa（如"数据源是什么？"）或闲聊（如"你叫什么"）。
        # modify 也纳入：LLM 有时把"搜索影像并下载"判为 modify（修改需求），不转换会走7步全流程。
        if intent in ("task", "modify", "unclear", "qa", "chat") and _is_partial_request(ctx.user_input):
            self.log(f"意图修正：LLM 判定 {intent}，但关键词判定为 partial，已改判")
            intent = "partial"
            parsed["question"] = ""
        # partial 完成后续接：用户说"继续后续流程"等，且最近对话中有 partial 关键词，
        # 且当前没有 10m LST 结果（否则应该是 postprocess），判为 partial 续接。
        # 必须在 postprocess soft keyword 检查之后、返回之前执行——
        # "继续"在 POSTPROCESS_SOFT_KEYWORDS 里，但没有 10m LST 结果时不会改判 postprocess，
        # 只是维持 LLM 判定。这里把"继续后续流程"从 task 改判为 partial 续接。
        if intent in ("task", "modify", "unclear") and self._is_continuation_after_partial(ctx):
            self.log("意图修正：检测到 partial 完成后的续接请求，改判 partial")
            intent = "partial"
            parsed["question"] = ""
        # 多轮对话延续：用户在回答反问时（pending_question 存在），如果最近几轮对话
        # 中出现过 partial 关键词（如"下载""搜索影像"），且当前回答不含全流程/产品标记，
        # 延续 partial 意图——覆盖"用户回答 Landsat/Sentinel-2/DEM 都需要"这类不含
        # partial 关键词但上下文明确的场景。
        if intent in ("task", "modify", "unclear") and self._is_partial_context(ctx):
            self.log(f"意图修正：多轮对话上下文延续 partial（LLM 判定 {intent}）")
            intent = "partial"
            parsed["question"] = ""
        # partial 意图最终保障：无论 LLM 返回什么 question，partial 不应携带反问
        # （partial 的步骤由关键词推断，不需要 LLM 反问）
        if intent == "partial":
            parsed["question"] = ""
        # 纠错/修正：用户说「不是武汉/改成9月/不对」等，是对上一轮已确认信息的修正。
        # 负向项（不是X/不要X 里的 X）必须从槽位中剥离，避免被当成新槽位
        # （如「不是武汉」里的武汉不能成为研究区）；intent 若被误判为 unclear，
        # 延续为 task（结合已确认槽位修正执行），而不是反问打断。
        _correction = _is_correction_request(ctx.user_input)
        if _correction:
            _neg = _negation_slot_terms(ctx.user_input)
            if _neg:
                self.log(f"意图修正：纠错请求，负向槽位排除 {_neg}")
            if intent == "unclear":
                intent = "task"
                parsed["question"] = ""
        raw_slots = parsed.get("slots") if isinstance(parsed.get("slots"), dict) else {}
        for _term in _negation_slot_terms(ctx.user_input):
            if _term and _term in str(raw_slots.get("region_name") or ""):
                raw_slots["region_name"] = ""
            if _term and _term in str(raw_slots.get("time_expression") or ""):
                raw_slots["time_expression"] = ""
        return {
            "intent": intent,
            "intent_confidence": _as_float(parsed.get("intent_confidence"), 0.5),
            "reason": str(parsed.get("reason") or ""),
            "slots": {
                "region_name": _as_text(raw_slots.get("region_name")),
                "time_expression": _as_text(raw_slots.get("time_expression")),
                "product": _as_text(raw_slots.get("product")) or "lst_10m",
                "model": _as_text(raw_slots.get("model")),
            },
            # model 有默认值（项目偏好 → rf），缺失不是阻塞项：从 missing 中剔除，
            # 防止轻反思复查阶段把"没提模型"当成必须反问的缺失项
            "missing": [str(x) for x in (parsed.get("missing") or []) if str(x) != "model"],
            "question": _as_text(parsed.get("question")),
            "source": "llm",
            "correction": _correction,
        }

    def _keyword_fallback(self, ctx: PlannerContext) -> Dict[str, Any]:
        """LLM 不可用时的关键词兜底路由。"""
        text = ctx.user_input
        pending = str(ctx.session_state.get("pending_question") or "")
        if any(kw in text for kw in _POSTPROCESS_KEYWORDS):
            self.log("意图分类退回关键词兜底：intent=postprocess")
            return {
                "intent": "postprocess",
                "intent_confidence": 0.6,
                "reason": "用户要求对已有结果做后处理（关键词兜底判定）",
                "slots": {"region_name": "", "time_expression": text,
                          "product": "lst_10m", "model": ""},
                "missing": [],
                "question": "",
                "source": "keyword",
            }
        # partial 关键词兜底：用户只说下载/搜索/预处理等，没提全流程/产品
        if _is_partial_request(text):
            self.log("意图分类退回关键词兜底：intent=partial")
            return {
                "intent": "partial",
                "intent_confidence": 0.5,
                "reason": "用户只要求执行部分流程（关键词兜底判定）",
                "slots": {"region_name": "", "time_expression": text,
                          "product": "lst_10m", "model": ""},
                "missing": [],
                "question": "",
                "source": "keyword",
            }
        is_task = any(kw in text for kw in _FALLBACK_TASK_KEYWORDS) or bool(pending)
        self.log(f"意图分类退回关键词兜底：intent={'task' if is_task else 'chat'}")
        return {
            "intent": "task" if is_task else "chat",
            "intent_confidence": 0.3,
            "reason": "大模型不可用，按关键词兜底判定",
            "slots": {"region_name": "", "time_expression": text,
                      "product": "lst_10m", "model": ""},
            "missing": [],
            "question": "",
            "source": "keyword",
        }

    # ── 第二步：槽位合并与解析 ────────────────────────────────────

    def merge_slots(self, ctx: PlannerContext, classified: Dict[str, Any]) -> Dict[str, Any]:
        """把本轮抽取的槽位与会话已确认槽位合并。

        优先级：本轮用户明确说的 > 会话中 source=user 的 > 其它来源。
        """
        stored = ctx.session_state.get("slots") or {}
        fresh = classified.get("slots") or {}
        merged: Dict[str, Any] = {}

        for key in ("region_name", "product", "model"):
            value = fresh.get(key) or ""
            if value:
                merged[key] = {"value": value, "source": "user"}
            elif isinstance(stored.get(key), dict) and stored[key].get("value"):
                merged[key] = dict(stored[key])

        # 时间：本轮表达与历史年/月分别合并，支持「25 年」→「7 月」两轮补全
        stored_time = stored.get("time_range") if isinstance(stored.get("time_range"), dict) else {}
        year = stored_time.get("year")
        month = stored_time.get("month")
        raw_parts = [p for p in [stored_time.get("raw"), fresh.get("time_expression")] if p]

        if fresh.get("time_expression"):
            parsed = slot_utils.parse_time_expression(fresh["time_expression"], today=ctx.today)
            if parsed.get("year"):
                year = parsed["year"]
            if parsed.get("month"):
                month = parsed["month"]
            if slot_utils.is_executable(parsed["precision"]):
                merged["time_range"] = {"value": [parsed["start"], parsed["end"]],
                                        "raw": " ".join(raw_parts), "source": "user",
                                        "year": year, "month": month,
                                        "precision": parsed["precision"]}
                return self._apply_preferences(ctx, merged)

        # 本轮无新时间表达时，优先沿用已存储的精确时间范围（value + precision）。
        # 否则 merge_time_parts(year, month) 会把"最近15天"（day 精度，2026-07-29~08-13）
        # 重建为整月范围（month 精度，2026-07-01~07-31），导致时间被篡改且触发
        # 不必要的月度合成模式弹窗。
        if not fresh.get("time_expression"):
            _stored_precision = str(stored_time.get("precision") or "")
            _stored_value = stored_time.get("value") or []
            if slot_utils.is_executable(_stored_precision) and len(_stored_value) == 2 and _stored_value[0]:
                merged["time_range"] = dict(stored_time)
                return self._apply_preferences(ctx, merged)

        combined = slot_utils.merge_time_parts(year, month)
        if slot_utils.is_executable(combined["precision"]):
            merged["time_range"] = {"value": [combined["start"], combined["end"]],
                                    "raw": " ".join(raw_parts), "source": "user",
                                    "year": year, "month": month,
                                    "precision": combined["precision"]}
        elif year or month:
            merged["time_range"] = {"value": [], "raw": " ".join(raw_parts),
                                    "source": "user", "year": year, "month": month,
                                    "precision": combined["precision"]}
        return self._apply_preferences(ctx, merged)

    def _apply_preferences(self, ctx: PlannerContext, merged: Dict[str, Any]) -> Dict[str, Any]:
        """未指定的槽位按「项目偏好 → settings → 内置默认」补齐。"""
        if "product" not in merged:
            merged["product"] = {"value": "lst_10m", "source": "default"}
        if "model" not in merged:
            preferred = self._preference("preferred_model", "")
            merged["model"] = ({"value": preferred, "source": "preference"} if preferred
                               else {"value": "rf", "source": "default"})
        return merged

    def _preference(self, key: str, default: Any) -> Any:
        if self.memory is None or not self.project_id:
            return default
        try:
            return self.memory.get_preference(self.project_id, key, default)
        except Exception:
            return default

    def resolve_region(self, ctx: PlannerContext,
                       merged: Dict[str, Any]) -> Dict[str, Any]:
        """把地名解析成研究区文件绝对路径（硬规则）。

        返回 `{"name", "study_area_file", "question"}`；question 非空表示需要反问。
        """
        paths = ctx.study_area_paths()
        slot = merged.get("region_name") or {}
        name = str(slot.get("value") or "")

        if not paths:
            return {"name": name, "study_area_file": "",
                    "question": "还没有看到你上传的研究区文件，请先上传研究区"
                                "（GeoJSON 或 Shapefile），我再安排流程。"}

        if not name:
            # 兼容性保护：只有一个研究区且用户没说地名时沿用现状取该文件
            if len(paths) == 1:
                return {"name": paths[0].stem, "study_area_file": str(paths[0].resolve()),
                        "question": ""}
            listed = "、".join(p.stem for p in paths[:6])
            return {"name": "", "study_area_file": "",
                    "question": f"你已上传的研究区有：{listed}。这次要处理哪一个？"}

        matched = slot_utils.match_study_area(paths, name)
        if matched is not None:
            return {"name": matched.stem, "study_area_file": str(matched.resolve()),
                    "question": ""}

        candidates = slot_utils.match_candidates(paths, name)
        if len(candidates) > 1:
            listed = "、".join(p.stem for p in candidates[:6])
            return {"name": name, "study_area_file": "",
                    "question": f"「{name}」匹配到多个研究区：{listed}，你要处理哪一个？"}

        listed = "、".join(p.stem for p in paths[:6])
        return {"name": name, "study_area_file": "",
                "question": f"没有找到名为「{name}」的研究区。已上传的有：{listed}，"
                            f"要用哪一个？"}

    # ── 第三步：出 plan ───────────────────────────────────────────

    def build_plan(self, ctx: PlannerContext, intent: str, merged: Dict[str, Any],
                   region: Dict[str, str]) -> Dict[str, Any]:
        """生成结构化 plan：先问 LLM，拿不到就用内置全流程兜底。"""
        time_slot = merged.get("time_range") or {}
        time_value = time_slot.get("value") or ["", ""]
        constraints = self._constraints(ctx, merged)
        goal = self._goal(region.get("name", ""), time_value, merged)

        # 方案E：主动查同区域历史工作流经验，把 cloud_threshold 建议注入 prompt
        # 比 RAG 模糊检索更精确——直接按区域名查找最佳历史记录
        region_name = region.get("name", "")
        history_hint = self._history_cloud_hint(region_name)
        mem_block = self.memory_block(ctx.user_input)
        if history_hint:
            mem_block = (mem_block + "\n" + history_hint).strip() if mem_block else history_hint

        raw: Optional[dict] = None
        # 续接请求跳过 LLM：LLM 不知道哪些步骤已完成，会返回全部7步。
        # 直接用 _builtin_steps 根据工作目录检测结果生成未完成的步骤。
        _skip_llm = (intent == "partial" and _is_continuation_request(ctx.user_input))
        if not _skip_llm and region.get("study_area_file") and time_value and time_value[0]:
            raw = self.call_json(
                planner_prompts.plan_prompt(
                    region_name=region_name,
                    time_range=slot_utils.describe_range(time_value[0], time_value[1]),
                    product=(merged.get("product") or {}).get("value", "lst_10m"),
                    constraints=json.dumps(constraints, ensure_ascii=False),
                    skill_catalog=ctx.skill_catalog,
                    memory_block=mem_block,
                ),
                self._plan_request_text(ctx, region, time_value),
                # 与全流程路径经验值对齐，避免 7 步计划被截断
                temperature=0.1, max_tokens=4096,
            )

        base = raw if isinstance(raw, dict) else {}
        # partial 意图强制用 _builtin_steps：LLM 不知道 partial 规则，会返回7步。
        # LLM 的 constraints/goal 仍然可用，只是 steps 用关键词推断的子集。
        _partial_force_builtin = (intent == "partial")
        _steps = (self._builtin_steps(region, time_value, intent, project_dir=ctx.project_dir)
                  if _partial_force_builtin
                  else (base.get("steps") or self._builtin_steps(region, time_value, intent,
                                                                  project_dir=ctx.project_dir)))
        plan = plan_schema.parse({
            "intent": intent,
            "goal": str(base.get("goal") or goal),
            "region": {"name": region.get("name", ""),
                       "study_area_file": region.get("study_area_file", "")},
            "time_range": {"start": time_value[0] if time_value else "",
                           "end": time_value[1] if len(time_value) > 1 else ""},
            "constraints": {**constraints, **(base.get("constraints") or {})},
            "steps": _steps,
            "memory_refs": base.get("memory_refs") or [],
        }, registry=self.registry)
        return plan

    def build_postprocess_plan(self, ctx: PlannerContext, intent: str) -> Dict[str, Any]:
        """后处理计划：只对已有 10m LST 结果做空洞填补，不重跑生产流程。

        region/time_range 留空（不需要新输入）；lst_gapfill 的输入路径由执行引擎
        从项目结果目录自动定位（executor 对 lst_gapfill 做动态查找）。
        研究区 GeoJSON 优先读影像对目录里记录的生成时研究区（region_study_area.json），
        回退到当前研究区目录最新上传文件——填洞只作用于研究区矢量范围内
        （区外保持 NoData，见结果后处理需求），且必须与产品同一区域。
        """
        record = self._latest_pair_region_record(ctx)
        _sa = []
        _name = ""
        if record.get("study_area_file") and \
                pathlib.Path(str(record["study_area_file"])).is_file():
            _sa = [pathlib.Path(str(record["study_area_file"]))]
            _name = str(record.get("name") or "") or _sa[0].stem
        else:
            _sa = ctx.study_area_paths()
            if _sa:
                _n = pathlib.Path(str(_sa[0])).name
                for _ext in (".geojson", ".json", ".shp", ".kml", ".gpkg"):
                    if _n.lower().endswith(_ext):
                        _n = _n[: -len(_ext)]
                        break
                _name = _n
        return plan_schema.parse({
            "intent": intent,
            "goal": "对已有10m地表温度产品做空洞填补，生成无空洞产品",
            "region": {"name": _name, "study_area_file": str(_sa[0]) if _sa else ""},
            "time_range": {"start": "", "end": ""},
            "constraints": self._constraints(ctx, {}),
            "steps": [{"skill": "lst_gapfill", "params": {},
                       "reason": "对已有 10m LST 结果做空洞填补"}],
            "memory_refs": [],
        }, registry=self.registry)

    def _latest_pair_region_record(self, ctx: PlannerContext) -> Dict[str, str]:
        """影像对目录里 mtime 最新的 region_study_area.json（生成产品时记录的研究区）。

        保证「单步结果后处理」复用生成产品时的同一研究区，而不是最新上传的文件
        （否则可能把别的区域多边形套到本产品上，导致填洞区域错误）。
        找不到/解析失败返回空 dict。
        """
        base = pathlib.Path(ctx.project_dir) if ctx.project_dir else None
        if base is None or not base.is_dir():
            return {}
        try:
            cands = sorted(base.glob("pairs/*/region_study_area.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return {}
        if not cands:
            return {}
        try:
            with open(cands[0], encoding="utf-8") as f:
                data = json.load(f)
            return {"study_area_file": str(data.get("study_area_file") or ""),
                    "name": str(data.get("name") or "")}
        except Exception:
            return {}

    def _plan_request_text(self, ctx: PlannerContext, region: Dict[str, str],
                           time_value: List[str]) -> str:
        return (f"请为「{region.get('name', '')}」在 "
                f"{slot_utils.describe_range(time_value[0], time_value[1])} 的"
                f"10m地表温度产品生成完整执行计划。用户原话：{ctx.user_input}")

    def _builtin_steps(self, region: Dict[str, str], time_value: List[str],
                       intent: str = "task", project_dir: str = "") -> List[dict]:
        """内置步骤兜底（LLM 不可用或输出不合法时）。

        partial 意图只生成用户要求的部分步骤（如只下载）；task/modify 生成完整7步。
        续接请求（"继续后续流程"）跳过已完成的步骤。
        """
        start = time_value[0] if time_value else ""
        end = time_value[1] if len(time_value) > 1 else ""
        region_file = region.get("study_area_file", "")

        if intent == "partial":
            # 部分流程：根据用户输入推断要执行哪些步骤
            text = self._current_user_input or ""
            steps_to_run = self._infer_partial_steps(text, project_dir=project_dir)
            steps = []
            for name in steps_to_run:
                params: Dict[str, Any] = {}
                if name == "data_acquisition":
                    params = {"region": region_file, "start_date": start, "end_date": end}
                steps.append({"skill": name, "params": params,
                              "reason": planner_rules._DEFAULT_REASONS.get(name, "")})
            return steps

        # 完整流程：全部7步
        steps = []
        for name in plan_schema.WORKFLOW_STEPS:
            params: Dict[str, Any] = {}
            if name == "data_acquisition":
                params = {"region": region_file, "start_date": start, "end_date": end}
            steps.append({"skill": name, "params": params,
                          "reason": planner_rules._DEFAULT_REASONS.get(name, "")})
        return steps

    @staticmethod
    def _infer_partial_steps(text: str, project_dir: str = "") -> List[str]:
        """从用户输入推断 partial 要执行的步骤。

        按关键词匹配，返回 WORKFLOW_STEPS 的子集（保持顺序）。
        无法推断时默认只执行 data_acquisition（最常见的部分请求）。

        续接请求（"继续后续流程"）：检查工作目录确定已完成步骤，返回未完成的步骤。
        """
        text = text or ""

        # 续接请求：检查工作目录确定已完成步骤，生成未完成的步骤
        if _is_continuation_request(text):
            completed = PlannerAgent._detect_completed_steps(project_dir)
            remaining = [s for s in plan_schema.WORKFLOW_STEPS if s not in completed]
            if remaining:
                return remaining
            # 全部完成时兜底返回空列表（build_plan 会走 LLM 路径）
            return []

        wanted = set()
        # 下载/搜索/找数据 → data_acquisition
        if any(kw in text for kw in ("下载", "搜索", "找", "获取", "拉取", "爬取")):
            wanted.add("data_acquisition")
        # 预处理 → data_pipeline
        if any(kw in text for kw in ("预处理", "清洗", "裁剪", "配准", "分割")):
            wanted.add("data_pipeline")
        # TTRI → ttri_compute
        if any(kw in text for kw in ("TTRI", "ttri", "地形热响应", "地形指数")):
            wanted.add("ttri_compute")
        # 训练 → rf_model
        if any(kw in text for kw in ("训练", "建模", "拟合", "调参", "调优")):
            wanted.add("rf_model")
        # TCR → tcr_compute
        if any(kw in text for kw in ("TCR", "tcr", "热约束", "残差")):
            wanted.add("tcr_compute")
        # 导出 → lst_export
        if any(kw in text for kw in ("导出", "输出", "生成产品", "导出产品")):
            wanted.add("lst_export")
        # 评估 → accuracy_eval
        if any(kw in text for kw in ("评估", "精度", "验证", "评价")):
            wanted.add("accuracy_eval")

        if not wanted:
            wanted.add("data_acquisition")  # 默认：只下载
        # 按 WORKFLOW_STEPS 顺序返回
        return [s for s in plan_schema.WORKFLOW_STEPS if s in wanted]

    def _constraints(self, ctx: PlannerContext, merged: Dict[str, Any]) -> Dict[str, Any]:
        data_cfg = (ctx.settings or {}).get("data", {}) if isinstance(ctx.settings, dict) else {}
        cloud = self._preference("cloud_threshold", data_cfg.get("cloud_threshold", 30))
        return {
            "cloud_threshold": cloud,
            "dem_source": data_cfg.get("dem_source", "copernicus"),
            "model": (merged.get("model") or {}).get("value", "rf"),
        }

    @staticmethod
    def _goal(region_name: str, time_value: List[str], merged: Dict[str, Any]) -> str:
        when = slot_utils.describe_range(time_value[0] if time_value else "",
                                        time_value[1] if len(time_value) > 1 else "")
        product = (merged.get("product") or {}).get("value", "lst_10m")
        label = "10m地表温度产品" if product == "lst_10m" else product
        return f"生成{region_name or '所选研究区'} {when} 的{label}"

    def _history_cloud_hint(self, region_name: str) -> str:
        """方案E：查同区域历史工作流经验，返回 cloud_threshold 建议文本。

        从 memory_manager.best_workflow 精确查询同区域 R² 最高的成功流程，
        提取 cloud_threshold 和 R² 作为显式上下文注入 plan prompt，让 LLM 沿用。
        找不到返回空串（不影响主流程）。
        """
        if not region_name or self.memory is None or not self.project_id:
            return ""
        try:
            record = self.memory.best_workflow(self.project_id, region_name)
            if not record:
                return ""
            metrics = record.get("metrics") or {}
            r2 = metrics.get("test_r2")
            params = record.get("final_params") or {}
            cloud = params.get("cloud_threshold") or (record.get("approval_choices") or {}).get("cloud_threshold")
            date_range = record.get("date_range") or ["", ""]
            parts = [f"[历史经验] {region_name} 上次成功流程（{date_range[0]}~{date_range[1]}）"]
            if cloud:
                parts.append(f"cloud_threshold={cloud}")
            if r2:
                parts.append(f"测试集 R²={r2}")
            parts.append("建议沿用相同 cloud_threshold")
            return "，".join(parts)
        except Exception:
            return ""

    # ── 第四步：轻反思（规则在后，永远覆盖 LLM） ──────────────────

    def reflect(self, ctx: PlannerContext, plan: Dict[str, Any],
                classified: Dict[str, Any]) -> ReflectionResult:
        # partial 意图跳过 LLM 反思：步骤由关键词推断、规则反思足够；LLM 看到空
        # time_range 反而可能反问多余问题打断流程。
        # postprocess 走 LLM 反思——反思 prompt 已注入「当前对话已有产物」上下文
        # （见 _llm_reflect），LLM 能理解填洞不需要时间范围；若仍反问时间/研究区，
        # 由下方安全网忽略（LLM 主判 + 规则兜底纠偏）。
        llm_result = None if plan.get("intent") == "partial" \
            else self._llm_reflect(ctx, plan, classified)
        # partial 意图不强制7步；只有 task/modify 才强制完整流程
        wants_full = plan.get("intent") in ("task", "modify")
        plan_after, rule_result = planner_rules.check(
            plan,
            registry=self.registry,
            study_areas_dir=ctx.study_areas_dir,
            study_areas=ctx.study_areas,
            wants_full_workflow=wants_full,
            replan_count=ctx.replan_count,
            replan_max=self.replan_max,
            previous_plan=ctx.previous_plan if ctx.replan_reason else None,
            today=ctx.today,
        )
        # 规则未通过 → 规则结论直接生效（覆盖 LLM）
        if rule_result.action != Action.PROCEED:
            return ReflectionResult(
                ok=rule_result.ok, action=rule_result.action,
                question=rule_result.question, note=rule_result.note,
                violations=rule_result.violations, suggestions=rule_result.suggestions,
                rule_hits=rule_result.rule_hits, data={"plan": plan_after})
        # 规则通过时才采纳 LLM 的 ask / chat_only
        if llm_result is not None and llm_result.action != Action.PROCEED:
            # 安全网：postprocess（填洞）不需要时间范围/新研究区，LLM 若仍反问
            # 这些（如「请提供时间范围」），直接忽略按 proceed 放行——与反思 prompt
            # 中「不要反问时间范围」的说明互为兜底，确保流程不被无谓打断。
            if plan.get("intent") == "postprocess" \
                    and llm_result.action == Action.ASK \
                    and self._is_postprocess_irrelevant_question(llm_result.question):
                return ReflectionResult.passed(note=llm_result.note,
                                               rule_hits=rule_result.rule_hits,
                                               data={"plan": plan_after})
            return ReflectionResult(
                ok=llm_result.ok, action=llm_result.action, question=llm_result.question,
                note=llm_result.note, violations=llm_result.violations,
                suggestions=llm_result.suggestions, rule_hits=rule_result.rule_hits,
                data={"plan": plan_after})
        return ReflectionResult.passed(note=rule_result.note,
                                       rule_hits=rule_result.rule_hits,
                                       data={"plan": plan_after})

    def _llm_reflect(self, ctx: PlannerContext, plan: Dict[str, Any],
                     classified: Dict[str, Any]) -> Optional[ReflectionResult]:
        parsed = self.call_json(
            planner_prompts.reflect_prompt(
                user_input=ctx.user_input,
                intent=plan.get("intent", ""),
                confidence=classified.get("intent_confidence", ""),
                slots=json.dumps(plan.get("region", {}), ensure_ascii=False)
                + json.dumps(plan.get("time_range", {}), ensure_ascii=False),
                step_names="、".join(plan_schema.skill_names(plan)),
                missing="、".join(classified.get("missing") or []),
                # 注入「当前对话已有产物」：postprocess 的反思 LLM 看到已有 10m 结果，
                # 才能理解空 time_range 是填洞的正常设计，而不是误判缺信息反问。
                existing_products=self._existing_products_text(ctx),
            ),
            # retry_once=False 只有一次机会，预算给足避免被截断
            "请开始复查。", temperature=0.0, max_tokens=1024, retry_once=False,
        )
        if parsed is None:
            return None
        action = str(parsed.get("action") or Action.PROCEED)
        if action not in (Action.PROCEED, Action.ASK, Action.CHAT_ONLY):
            action = Action.PROCEED
        note = str(parsed.get("note") or "")
        if action == Action.ASK:
            question = str(parsed.get("question") or "").strip()
            if not question:
                return None
            return ReflectionResult.ask(question, note=note)
        if action == Action.CHAT_ONLY:
            return ReflectionResult.chat_only(note=note)
        return ReflectionResult.passed(note=note)

    @staticmethod
    def _is_postprocess_irrelevant_question(question: str) -> bool:
        """postprocess 不需要时间范围/新研究区：LLM 反问这些时视为无效追问。

        填洞只作用于已有的 10m 产品，时间范围/研究区留空是正常设计
        （build_postprocess_plan 自动定位已有结果与生成时研究区）。关键词命中任一
        即判为「与后处理执行无关的追问」，由 reflect 忽略按 proceed 放行。
        仅限 postprocess 意图下启用，不影响 task/partial 的正常反问。
        """
        q = str(question or "")
        return any(kw in q for kw in (
            "时间范围", "时间", "年-月", "年月", "月份", "日期",
            "时间段", "几年", "几月", "哪个时间", "研究区", "地区",
        ))

    # ── 对外入口 ──────────────────────────────────────────────────

    def run(self, ctx: PlannerContext) -> PlannerOutcome:
        """一次完整规划：分类 → 合并槽位 → 解析研究区 → 出 plan → 轻反思。"""
        self._current_user_input = ctx.user_input  # 供 _builtin_steps 推断 partial 步骤
        classified = self.classify_intent(ctx)
        intent = classified["intent"]
        merged = self.merge_slots(ctx, classified)

        if intent in ("chat", "qa"):
            return PlannerOutcome(PlannerOutcome.CHAT, intent=intent, slots=merged,
                                  note=classified.get("reason", ""))
        if intent == "unclear":
            question = classified.get("question") or \
                "我还不确定你想做什么。你是想生成某个研究区、某个月份的地表温度产品吗？"
            return PlannerOutcome(PlannerOutcome.ASK, question=question, intent=intent,
                                  slots=merged, note=classified.get("reason", ""))

        if intent == "postprocess":
            # 结果后处理：对已有 10m LST 结果做空洞填补，不需要新研究区/时间，
            # 直接出单步 plan（只 lst_gapfill，不重跑生产流程）。
            if not self._thinking_acc:
                self._push_thinking(
                    "用户要求对已有的地表温度结果做后处理（无空洞），"
                    "不需要重新下载数据或训练模型，只对已导出的 10m 产品做空洞填补即可。"
                )
            plan = self.build_postprocess_plan(ctx, intent)
            reflection = self.reflect(ctx, plan, classified)
            plan = reflection.data.get("plan") or plan
            if reflection.action == Action.CHAT_ONLY:
                return PlannerOutcome(PlannerOutcome.CHAT, intent="qa", slots=merged,
                                      note=reflection.note, reflection=reflection)
            if reflection.action == Action.ASK:
                return PlannerOutcome(PlannerOutcome.ASK, question=reflection.question,
                                      intent=intent, slots=merged, note=reflection.note,
                                      reflection=reflection)
            return PlannerOutcome(PlannerOutcome.PLAN, plan=plan, intent=intent,
                                  slots=merged, note=reflection.note,
                                  reflection=reflection)

        # 低置信度确认（意图识别防误判）：LLM 判 task/modify 但置信度很低（<0.5）、
        # 且会话已有确认过的槽位/计划（不是全新对话）、且不是纠错、也不是在回答反问时，
        # 轻量反问确认意图，避免把含糊表达（如"帮我做一下"）直接跑全流程。
        if (classified.get("source") == "llm"
                and not classified.get("correction")
                and not ctx.session_state.get("pending_question")
                and intent in ("task", "modify")
                and _as_float(classified.get("intent_confidence"), 1.0) < 0.5
                and (bool(ctx.session_state.get("slots")) or bool(ctx.previous_plan))):
            if not self._thinking_acc:
                self._push_thinking(
                    "用户这次表达比较模糊，LLM 判定置信度较低。为避免理解偏差"
                    "直接跑完整流程，先向用户轻量确认一下意图。"
                )
            return PlannerOutcome(PlannerOutcome.ASK,
                                  question=self._confirm_question(ctx, merged),
                                  intent=intent, slots=merged,
                                  note="意图置信度低，先确认")

        region = self.resolve_region(ctx, merged)
        if region.get("question"):
            if not self._thinking_acc:
                self._push_thinking(
                    "还没确定用户要处理的研究区，需要先向用户确认具体位置，"
                    "才能继续检索影像并制定执行计划。"
                )
            return PlannerOutcome(PlannerOutcome.ASK, question=region["question"],
                                  intent=intent, slots=merged, note="研究区未确定")

        time_slot = merged.get("time_range") or {}
        # partial 意图：不含 data_acquisition（如只做预处理或续接）时不需要时间范围
        _partial_no_download = (intent == "partial"
                                and "data_acquisition" not in self._infer_partial_steps(
                                    ctx.user_input, project_dir=ctx.project_dir))
        # LLM 误判时可能没提取 time_expression，导致时间槽位为空。
        # 从用户输入中补提取时间（如"最近15天"），避免不必要的反问。
        if not _partial_no_download and not slot_utils.is_executable(str(time_slot.get("precision") or "")):
            _parsed = slot_utils.parse_time_expression(ctx.user_input, today=ctx.today)
            if _parsed and slot_utils.is_executable(_parsed.get("precision", "")):
                merged["time_range"] = {
                    "value": [_parsed["start"], _parsed["end"]],
                    "raw": ctx.user_input, "source": "user",
                    "year": _parsed.get("year"), "month": _parsed.get("month"),
                    "precision": _parsed["precision"]}
                time_slot = merged["time_range"]
        if not _partial_no_download and not slot_utils.is_executable(str(time_slot.get("precision") or "")):
            # 反问时间前补一段简短思考说明：意图分类若走
            # 关键词兜底没有 LLM 思考，这里也让反问消息有思考块与用时可展示
            if not self._thinking_acc:
                region_name = str(region.get("name") or "") or "该研究区"
                self._push_thinking(
                    f"用户想生成{region_name}的地表温度产品，但还没说明影像的"
                    "时间范围，需要先反问确认具体月份，才能制定完整执行计划。"
                )
            # 优先用 LLM 生成自然反问（带上当前月份等上下文），失败回退模板
            _question = self._ask_time_natural(ctx, time_slot, region)
            return PlannerOutcome(PlannerOutcome.ASK,
                                  question=_question,
                                  intent=intent, slots=merged, note="时间范围未确定")

        plan = self.build_plan(ctx, intent, merged, region)
        reflection = self.reflect(ctx, plan, classified)
        plan = reflection.data.get("plan") or plan

        if reflection.action == Action.CHAT_ONLY:
            return PlannerOutcome(PlannerOutcome.CHAT, intent="qa", slots=merged,
                                  note=reflection.note, reflection=reflection)
        if reflection.action == Action.ASK:
            return PlannerOutcome(PlannerOutcome.ASK, question=reflection.question,
                                  intent=intent, slots=merged, note=reflection.note,
                                  reflection=reflection)
        return PlannerOutcome(PlannerOutcome.PLAN, plan=plan, intent=intent, slots=merged,
                              note=reflection.note, reflection=reflection)

    def llm_replan_adjust(self, ctx: PlannerContext,
                          previous_plan: Dict[str, Any]) -> tuple:
        """LLM 看失败原因自拟 replan 调整方案（升级 replan 的核心）。

        返回 (调整后的 plan, 中文调整描述)；LLM 不可用 / 解析失败 / 方案无效时
        返回 (None, "")，由总调度回退到固定规则 `adjust_for_replan`。
        方案限定在 P7 校验字段内（time_range / cloud_threshold / dem_source），
        避免产出"只改 goal"的无差异方案。
        """
        plan = dict(previous_plan or {})
        payload = ctx.replan_payload or {}
        detail = payload.get("detail") or {}
        cloud_stats = detail.get("cloud_stats") or {}

        # 组装失败诊断（真实数据：云量分布 + 违规项 + 建议）
        diagnosis_parts: List[str] = []
        _digest = {k: v for k, v in detail.items() if k != "cloud_stats"}
        if _digest:
            diagnosis_parts.append(json.dumps(_digest, ensure_ascii=False))
        if isinstance(cloud_stats, dict) and (cloud_stats.get("landsat") or cloud_stats.get("sentinel")):
            diagnosis_parts.append("云量分布: " + json.dumps(cloud_stats, ensure_ascii=False))
        violations = payload.get("violations") or []
        if violations:
            diagnosis_parts.append("违规项: " + "；".join(str(v) for v in list(violations)[:6]))
        suggestions = payload.get("suggestions") or []
        if suggestions:
            diagnosis_parts.append("建议: " + "；".join(str(s) for s in list(suggestions)[:4]))

        adjust = self.call_json(
            planner_prompts.replan_adjust_prompt(
                reason=ctx.replan_reason,
                diagnosis="\n".join(diagnosis_parts) or "（无诊断数据）",
                region=json.dumps(plan.get("region") or {}, ensure_ascii=False),
                time_range=json.dumps(plan.get("time_range") or {}, ensure_ascii=False),
                constraints=json.dumps(plan.get("constraints") or {}, ensure_ascii=False),
                adjust_history="；".join(ctx.adjust_history) if ctx.adjust_history else "",
            ),
            "请分析失败原因并给出调整方案。", temperature=0.0, max_tokens=1024,
            retry_once=False,
        )
        if not isinstance(adjust, dict):
            self.log("LLM replan 方案缺失，回退固定规则")
            return None, ""
        body = adjust.get("adjust")
        if not isinstance(body, dict):
            self.log("LLM replan 方案格式错误，回退固定规则")
            return None, ""
        adjusted, desc = self._apply_llm_adjust(plan, body)
        if not desc:
            self.log("LLM replan 方案无有效调整，回退固定规则")
            return None, ""
        return adjusted, desc

    def _apply_llm_adjust(self, plan: Dict[str, Any], body: Dict[str, Any]) -> tuple:
        """把 LLM 的调整建议应用到 plan 副本（带边界钳制），返回 (新 plan, 描述)。

        body 形如 {"cloud_threshold": 45, "widen_days": 15, "change_source": false}。
        至少修改 time_range / cloud_threshold / dem_source 之一才返回非空描述，
        保证 P7（实质差异）通过；越界值截断，非法值忽略。
        """
        import datetime as _dt

        constraints = dict(plan.get("constraints") or {})
        time_range = dict(plan.get("time_range") or {})
        desc_parts: List[str] = []
        today = _dt.date.today()

        # 云量阈值（30~90，整数）
        try:
            new_cloud = int(body.get("cloud_threshold"))
            old_cloud = constraints.get("cloud_threshold")
            try:
                old_cloud = int(old_cloud)
            except (TypeError, ValueError):
                old_cloud = 30
            new_cloud = max(30, min(90, new_cloud))
            if new_cloud != old_cloud:
                constraints["cloud_threshold"] = new_cloud
                desc_parts.append(f"云量阈值从 {old_cloud} 放宽到 {new_cloud}")
        except (TypeError, ValueError):
            pass

        # 时间扩展（前后各扩 widen_days 天，1~60）
        try:
            widen = int(body.get("widen_days"))
            start, end = time_range.get("start", ""), time_range.get("end", "")
            old_start, old_end = _dt.date.fromisoformat(str(start)), _dt.date.fromisoformat(str(end))
            widen = max(1, min(60, widen))
            new_start = (old_start - _dt.timedelta(days=widen)).isoformat()
            new_end = min(today, old_end + _dt.timedelta(days=widen)).isoformat()
            time_range["start"], time_range["end"] = new_start, new_end
            desc_parts.append(f"时间范围扩大到 {new_start} ~ {new_end}")
        except (TypeError, ValueError):
            pass

        # DEM 数据源切换
        if body.get("change_source") is True:
            old_src = constraints.get("dem_source", "copernicus")
            constraints["dem_source"] = ("planetary" if old_src == "copernicus"
                                         else "copernicus")
            desc_parts.append(f"数据源从 {old_src} 切换到 {constraints['dem_source']}")

        if not desc_parts:
            return plan, ""
        return {**plan, "constraints": constraints, "time_range": time_range,
                "_adjust_desc": "，".join(desc_parts)}, "，".join(desc_parts)

    def replan(self, ctx: PlannerContext, adjusted_plan: Dict[str, Any]) -> PlannerOutcome:
        """带原因重新出 plan（规则 5：必须体现针对性调整）。

        入参 `adjusted_plan` 已由总调度确定（LLM 自拟方案优先，固定规则兜底），
        本方法据此重建步骤参数并跑一次反思。
        """
        self._current_user_input = ctx.user_input  # partial 步骤推断需要
        time_range = adjusted_plan.get("time_range") or {}
        region = adjusted_plan.get("region") or {}
        replan_intent = str(adjusted_plan.get("intent") or "task")
        steps = self._builtin_steps(
            {"study_area_file": region.get("study_area_file", "")},
            [time_range.get("start", ""), time_range.get("end", "")],
            intent=replan_intent,
        )
        plan = plan_schema.parse({**adjusted_plan, "steps": steps},
                                 registry=self.registry)
        plan = {**plan, "plan_id": plan_schema.new_plan_id(),
                "goal": adjusted_plan.get("goal", ""),
                "reflection": {"info_complete": True, "risks": [],
                               "note": ctx.replan_reason}}

        classified = {"intent": plan.get("intent", "task"), "intent_confidence": 1.0,
                      "missing": []}
        reflection = self.reflect(ctx, plan, classified)
        plan = reflection.data.get("plan") or plan
        if reflection.action == Action.PROCEED:
            return PlannerOutcome(PlannerOutcome.PLAN, plan=plan,
                                  intent=plan.get("intent", "task"),
                                  note=reflection.note, reflection=reflection)
        return PlannerOutcome(PlannerOutcome.ASK, question=reflection.question,
                              intent=plan.get("intent", "task"), note=reflection.note,
                              reflection=reflection)

    @staticmethod
    def _ask_time(time_slot: Dict[str, Any], region: Optional[Dict[str, Any]] = None) -> str:
        """反问时间范围（年份先做合理性校验，不合理时不能原样复述）。

        例如用户说「125年」，不能直接问「125 年范围比较大，请确认具体月份」——
        那是在不加甄别地复述一个明显异常的输入，得先指出年份本身有问题。

        支持的时间表达不限于月份：用户可以说"2025年7月""上个月""最近30天"等。
        """
        region_name = str((region or {}).get("name") or "").strip() or "该研究区"
        year = time_slot.get("year")
        month = time_slot.get("month")
        raw = str(time_slot.get("raw") or "").strip()
        if year and not slot_utils.year_plausible(year):
            hint = f"「{raw}」" if raw else f"「{year} 年」"
            return (f"{hint}看起来不像一个可以下载到影像的年份（系统数据从 "
                    f"{slot_utils.MIN_DATA_YEAR} 年前后才开始覆盖），你是想说哪一年？"
                    f"请确认具体时间，例如 2025 年 7 月。")
        if year and not month:
            return (f"{region_name}可以，不过 {year} 年范围比较大，"
                    f"请再确认具体时间，例如 {year} 年 7 月、{year} 年最近 30 天等。")
        if month and not year:
            return f"要处理的是哪一年的 {month} 月？例如 2025 年 {month} 月。"
        return (f"好的，生成 {region_name} 的地表温度产品前，我还差一个信息："
                f"你想要哪个时间段？可以说具体月份（如 2025 年 7 月），"
                f"也可以说相对时间（如上个月、最近 15 天）。")

    def _ask_time_natural(self, ctx: PlannerContext, time_slot: Dict[str, Any],
                          region: Optional[Dict[str, Any]] = None) -> str:
        """用 LLM 生成自然的反问，带上当前月份等上下文；失败回退到 _ask_time 模板。

        比 _ask_time 的固定模板更自然：
        - 提到当前月份，让用户知道"现在能下载到最近哪些数据"
        - 结合研究区名，不生硬
        - 提示用户可以说相对时间（上个月、最近N天等），不局限于月份
        - 年份不合理时仍走 _ask_time（需要明确指出错误）
        """
        # 年份不合理时仍走模板（需要明确指出错误，不适合 LLM 自由发挥）
        year = time_slot.get("year")
        if year and not slot_utils.year_plausible(year):
            return self._ask_time(time_slot, region)

        region_name = str((region or {}).get("name") or "").strip() or "该研究区"
        month = time_slot.get("month")
        raw = str(time_slot.get("raw") or "").strip()
        today = ctx.today
        current_month_str = f"{today.year} 年 {today.month} 月"

        # 构造 LLM prompt
        _parts = [f"研究区：{region_name}"]
        if year and not month:
            _parts.append(f"用户已说年份：{year} 年，但没说月份")
        elif month and not year:
            _parts.append(f"用户已说月份：{month} 月，但没说年份")
        elif raw:
            _parts.append(f"用户说了模糊时间：{raw}")
        else:
            _parts.append("用户完全没说时间")
        _parts.append(f"当前时间：{current_month_str}")
        _parts.append(f"系统数据从 {slot_utils.MIN_DATA_YEAR} 年开始覆盖")
        _parts.append("系统支持的时间表达：具体年月（2025年7月）、相对时间（上个月、最近15天、3个月前等）")

        prompt = (
            "你是 GeoThermoAI 的对话助手。用户想生成地表温度产品，但时间信息不完整。\n"
            f"上下文：\n{chr(10).join('- ' + p for p in _parts)}\n\n"
            "请用一句自然的话反问用户确认时间范围。\n"
            "要求：\n"
            "- 语气自然，不要太机械\n"
            "- 提到当前月份，让用户知道现在能下载到最近哪些数据\n"
            "- 提示用户可以说具体月份，也可以说相对时间（如上个月、最近15天）\n"
            "- 不超过 60 字\n"
            "- 只输出反问的话，不要输出任何 JSON 或代码块\n"
        )
        try:
            resp = self.call_text(
                "你是 GeoThermoAI 的对话助手，负责在信息不完整时反问用户确认。",
                prompt,
                temperature=0.7,
                max_tokens=150,
                thinking={"type": "disabled"},
            )
            question = (resp or "").strip().strip('"').strip('"').strip('"')
            # 简单校验：不能太长、不能是空、不能含 JSON、不能是错误信息
            if question and len(question) <= 120 and not question.startswith("{") \
                    and "API调用失败" not in question and "未检测到LLM" not in question:
                return question
        except Exception:
            pass
        # LLM 失败时回退到固定模板
        return self._ask_time(time_slot, region)

    def _confirm_question(self, ctx: PlannerContext, merged: Dict[str, Any]) -> str:
        """低置信度时的轻量意图确认问题。

        用已确认/已合并的槽位生成一句确认话，让用户回答「对/不对」或补充信息。
        LLM 不可用或校验不通过时回退固定模板，保证一定有反问。
        """
        region = str((merged.get("region_name") or {}).get("value") or "")
        time_slot = merged.get("time_range") or {}
        time_value = time_slot.get("value") or []
        when = slot_utils.describe_range(time_value[0] if time_value else "",
                                         time_value[1] if len(time_value) > 1 else "")
        target = f"{region or '所选研究区'}" + (f" {when}" if when else "")
        prompt = (
            "你是 GeoThermoAI 的对话助手。用户上一句表达比较模糊，需要你生成一句"
            "轻量的确认问句，让用户用「对/不对」或补充信息来确认。\n"
            f"基于已有信息推测用户要做的事：{target} 的地表温度产品。\n"
            "要求：一句自然的中文问句，不超过 40 字，不要解释。"
        )
        try:
            resp = self.call_text(
                "你是 GeoThermoAI 的对话助手，负责在意图模糊时确认用户意图。",
                prompt, temperature=0.3, max_tokens=120,
                thinking={"type": "disabled"})
            q = (resp or "").strip().strip('"').strip('"')
            if q and 6 <= len(q) <= 60 and not q.startswith("{") \
                    and "API调用失败" not in q \
                    and ("吗" in q or "？" in q or "?" in q):
                return q
        except Exception:
            pass
        return f"你是想生成{target}的地表温度产品吗？"

    # ── 内部工具 ──────────────────────────────────────────────────

    def _session_context_text(self, ctx: PlannerContext) -> str:
        state = ctx.session_state or {}
        parts: List[str] = []
        stored = state.get("slots") or {}
        for key, label in (("region_name", "研究区"), ("time_range", "时间范围"),
                           ("product", "产品类型"), ("model", "模型")):
            item = stored.get(key)
            if isinstance(item, dict) and item.get("value"):
                parts.append(f"- {label}：{item['value']}（来源：{item.get('source', '未知')}）")
        if state.get("pending_question"):
            parts.append(f"- 你上一轮问了用户：{state['pending_question']}")
        # 上一轮确认的计划摘要：用户说「就它了/还是那个地方/继续之前那个」等
        # 指代词时，LLM 据此沿用上轮设置，而不是因为没提新地名/新时间就反问
        if ctx.previous_plan:
            _goal = str(ctx.previous_plan.get("goal") or "")
            if _goal:
                parts.append(f"- 上一轮确认的计划：{_goal}")
        if ctx.replan_reason:
            parts.append(f"- 这是一次重新规划，原因：{ctx.replan_reason}")
        return "\n".join(parts)

    def _existing_products_text(self, ctx: PlannerContext) -> str:
        """当前对话工作区里已有的 10m LST 结果（排除 _filled 填洞产物）。

        意图分类的关键信息：LLM 看到「已有结果」，才能把「继续生成无空洞的结果」
        判为 postprocess 而不是全流程 task（后者会导致反问时间范围）。返回空串时
        调用方按「暂无结果」兜底。目录：{project_dir}/pairs/L*_S*/results/。

        扫描策略：先精确 glob pairs/*/results/，失败时回退递归 **/ 确保不漏
        （project_dir 路径编码异常或目录结构变化时精确 glob 可能返回空）。
        """
        base = pathlib.Path(ctx.project_dir) if ctx.project_dir else None
        if base is None or not base.is_dir():
            return ""
        try:
            files = sorted(base.glob("pairs/*/results/rf_10m_lst_final_*.tif"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        # 精确 glob 为空时递归扫描兜底（防止目录结构变化或路径问题导致漏扫）
        if not files:
            try:
                files = sorted(base.rglob("rf_10m_lst_final_*.tif"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return ""
        files = [p for p in files if "_filled" not in p.name and "_cloud_mask" not in p.name]
        if not files:
            return ""
        # 用自然语言摘要强调「已有结果」这一上下文，而不是甩给 LLM 一堆文件路径：
        # 让 LLM 明确意识到本对话已完成全流程、手上有带空洞的 10m 产品，
        # 从而把「继续生成无空洞」理解成只需一步结果后处理。
        lines = [
            f"本对话已生成 {len(files)} 份 10m 地表温度结果（含空洞）。",
            "用户若说「无空洞 / 继续生成无空洞 / 填洞 / 空洞填补 / 结果后处理」"
            "就是对这 N 份已有结果做空洞填补，只需要一步 lst_gapfill，"
            "不需要重新下载数据或训练模型。最近的结果如下：",
        ]
        for p in files[:5]:
            m = re.search(r"_final_(\d{8})\.tif$", p.name)
            date = m.group(1) if m else p.stem
            lines.append(f"- 影像日期 {date}：{p}")
        return "\n".join(lines)

    def _confirm_postprocess(self, ctx: PlannerContext) -> bool:
        """二次判定：LLM 拍板「这条消息是对已有结果的后处理，还是重新跑完整流程」。

        返回 True=按 postprocess 处理（对已有结果填洞），False=尊重 LLM 原判定（新任务）。
        主路径仍是 LLM 决策：只有 LLM 二次判定明确判「新任务」且高置信（≥0.8）才尊重；
        判 postprocess / 拿不准 / LLM 不可用（解析失败）都按关键词命中兜底为 postprocess。
        """
        prompt = planner_prompts.confirm_postprocess_prompt(
            user_input=ctx.user_input,
            existing_products=self._existing_products_text(ctx),
        )
        parsed = self.call_json(prompt, ctx.user_input, temperature=0.0, max_tokens=512,
                                thinking={"type": "disabled"})
        if parsed is None:
            self.log("二次判定 LLM 不可用，按关键词命中视为 postprocess")
            return True
        intent = str(parsed.get("intent") or "").strip()
        confidence = _as_float(parsed.get("confidence"), 0.0)
        self.log(f"二次判定：LLM 认为 intent={intent}（置信度 {confidence}）")
        if intent == "postprocess":
            return True
        if intent in ("task", "new_task"):
            # 明确判新任务且高置信才尊重 LLM；否则按关键词兜底 postprocess
            return confidence < 0.8
        return True

    @staticmethod
    def _recent_history(ctx: PlannerContext, limit: int = 8) -> List[dict]:
        """只带最近若干轮，避免意图分类的 prompt 无限膨胀。"""
        return ctx.prior_messages[-limit:] if ctx.prior_messages else []

    @staticmethod
    def _is_partial_context(ctx: PlannerContext) -> bool:
        """多轮对话上下文是否表明用户在做 partial 请求。

        条件：
        1. 用户正在回答反问（session_state 有 pending_question）
        2. 最近几轮对话中出现过 partial 关键词（下载/搜索影像/预处理等）
        3. 当前输入不含全流程/产品标记（全流程/降尺度/产品等）
        4. 当前输入不含 postprocess 关键词

        覆盖场景：用户说"搜索影像并下载" → 系统反问 → 用户回答"只需要下载影像"
        → 系统又反问 → 用户回答"Landsat、Sentinel-2和DEM都需要"
        → 第三轮回答不含 partial 关键词，但上下文明确是 partial。
        """
        # 必须在回答反问时才启用（否则会把新对话误判为 partial）
        if not ctx.session_state.get("pending_question"):
            return False
        # 当前输入含全流程/产品标记时不延续 partial
        if any(m in ctx.user_input for m in TASK_PRODUCT_MARKERS):
            return False
        # 当前输入含 postprocess 关键词时不延续 partial
        if _is_postprocess_request(ctx.user_input):
            return False
        # 检查最近几轮对话是否包含 partial 关键词
        recent = ctx.prior_messages[-6:] if ctx.prior_messages else []
        combined = " ".join(
            str(m.get("content") or m.get("text") or "")
            for m in recent if isinstance(m, dict)
        )
        return _is_partial_request(combined)

    def _is_continuation_after_partial(self, ctx: PlannerContext) -> bool:
        """检测 partial 完成后的续接请求（"继续后续流程"等）。

        条件：
        1. 用户说了续接词（"继续后续流程""接着往下跑"等）
        2. 最近对话中出现过 partial 关键词（说明之前做过 partial）
        3. 当前输入不含全流程/产品标记
        4. 当前输入不含 postprocess 关键词
        5. 没有已有的 10m LST 结果（否则应该是 postprocess 而不是续接）

        覆盖场景：用户说"搜索影像并下载" → 1步完成 → 系统提示"如需继续，
        可以说「数据预处理」或「继续后续流程」" → 用户说"继续后续流程"
        → 应判 partial 续接（跳过已完成的 data_acquisition），不是全新 task。
        """
        # 必须含续接词
        if not _is_continuation_request(ctx.user_input):
            return False
        # 含全流程/产品标记时不延续（用户可能改主意要全流程）
        if any(m in ctx.user_input for m in TASK_PRODUCT_MARKERS):
            return False
        # 含 postprocess 关键词时不延续
        if _is_postprocess_request(ctx.user_input):
            return False
        # 检查最近对话是否包含 partial 关键词（确认之前做过 partial）
        recent = ctx.prior_messages[-8:] if ctx.prior_messages else []
        combined = " ".join(
            str(m.get("content") or m.get("text") or "")
            for m in recent if isinstance(m, dict)
        )
        if not _is_partial_request(combined):
            return False
        # 没有已有的 10m LST 结果（否则应该是 postprocess）
        if self._existing_products_text(ctx):
            return False
        return True

    @staticmethod
    def _detect_completed_steps(project_dir: str) -> set:
        """检查工作目录，返回已完成的步骤集合。

        通过检查各步骤的产物文件是否存在来判断（7步全覆盖）：
        - data_acquisition: pairs/ 目录存在且有子目录（或 raw/ 有文件）
        - data_pipeline: pairs/*/processed/ 有 .tif 文件
        - ttri_compute: pairs/*/processed/ttri_coefficients.json 存在
        - rf_model: pairs/*/results/train/ 有 .pkl 文件
        - tcr_compute: pairs/*/results/ 有 tcr_result*.parquet
        - lst_export: pairs/*/results/ 有 rf_10m_lst_final_*.tif（排除 _filled）
        - accuracy_eval: pairs/*/results/ 有 coarse_constraint_closure.json
        """
        import glob as _glob
        completed = set()
        if not project_dir or not os.path.isdir(project_dir):
            return completed

        # data_acquisition: pairs/ 目录存在且有子目录
        pairs_dir = os.path.join(project_dir, "pairs")
        if not os.path.isdir(pairs_dir):
            # 也检查 raw/ 目录（非配对模式）
            raw_dir = os.path.join(project_dir, "raw")
            if os.path.isdir(raw_dir) and any(os.scandir(raw_dir)):
                completed.add("data_acquisition")
            return completed

        pair_subdirs = [d for d in os.listdir(pairs_dir)
                        if os.path.isdir(os.path.join(pairs_dir, d))]
        if not pair_subdirs:
            return completed
        completed.add("data_acquisition")

        for d in pair_subdirs:
            processed = os.path.join(pairs_dir, d, "processed")
            results_dir = os.path.join(pairs_dir, d, "results")

            # data_pipeline: processed/ 有 .tif
            if os.path.isdir(processed) and _glob.glob(os.path.join(processed, "*.tif")):
                completed.add("data_pipeline")

            # ttri_compute: processed/ttri_coefficients.json
            if os.path.isfile(os.path.join(processed, "ttri_coefficients.json")):
                completed.add("ttri_compute")

            # rf_model: results/train/ 有 .pkl
            train_dir = os.path.join(results_dir, "train")
            if os.path.isdir(train_dir) and _glob.glob(os.path.join(train_dir, "*.pkl")):
                completed.add("rf_model")

            # tcr_compute: results/ 有 tcr_result*.parquet
            if os.path.isdir(results_dir) and \
                    _glob.glob(os.path.join(results_dir, "tcr_result*.parquet")):
                completed.add("tcr_compute")

            # lst_export: results/ 有 rf_10m_lst_final_*.tif（排除 _filled）
            if os.path.isdir(results_dir):
                tifs = _glob.glob(os.path.join(results_dir, "rf_10m_lst_final_*.tif"))
                tifs = [t for t in tifs if "_filled" not in os.path.basename(t)]
                if tifs:
                    completed.add("lst_export")

            # accuracy_eval: results/coarse_constraint_closure.json
            if os.path.isfile(os.path.join(results_dir, "coarse_constraint_closure.json")):
                completed.add("accuracy_eval")

        return completed


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("null", "none", "") else text


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def wants_full_workflow(user_input: str) -> bool:
    """用户是否明确要求全流程（沿用现有安全网关键词判据）。"""
    return any(kw in (user_input or "") for kw in _FULL_WORKFLOW_KEYWORDS)
