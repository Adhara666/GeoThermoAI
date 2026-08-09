"""
规划 Agent（技术方案第 4 章）

职责：判定意图 → 多轮补全槽位 → 反问 → 出结构化 plan → 轻反思。
边界：不执行任何 Skill，信息不全禁止放行。

拍板结论 2：**每条消息都过一次意图分类**，关键词表只作为 LLM 不可用时的兜底。
"""

import datetime
import json
import pathlib
from typing import Any, Dict, List, Optional

from .. import plan_schema
from ..orchestrator.agent_config import REPLAN_MAX
from ..reflection import planner_rules
from ..reflection.result import Action, ReflectionResult
from . import slots as slot_utils
from .base_role import RoleAgent
from .prompts import planner as planner_prompts

# 关键词兜底（LLM 不可用时判定「是否要跑流程」，与 server._AGENT_KEYWORDS 同源语义）
_FALLBACK_TASK_KEYWORDS = ("处理", "训练", "下载", "执行", "运行", "生成",
                           "全流程", "一键", "开始", "计算", "导出", "评估")

# 明确要求全流程的关键词（沿用现有安全网判据）
_FULL_WORKFLOW_KEYWORDS = ("全流程", "一键", "跑完全流程", "执行全流程")

# 结果后处理关键词（无空洞/填洞等：只对已有结果做后处理，不重跑生产流程）
_POSTPROCESS_KEYWORDS = ("无空洞", "填洞", "空洞填补", "结果后处理", "去除空洞", "去掉空洞")


class PlannerContext:
    """规划 Agent 的输入上下文（技术方案 4.2，必须全给）。"""

    def __init__(self, *, user_input: str, prior_messages: Optional[List[dict]] = None,
                 session_state: Optional[dict] = None, study_areas: Optional[List[str]] = None,
                 study_areas_dir: str = "", project_dir: str = "",
                 settings: Optional[dict] = None, skill_catalog: str = "",
                 replan_reason: str = "", replan_count: int = 0,
                 previous_plan: Optional[dict] = None, today: Optional[datetime.date] = None):
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

    # ── 第一步：意图分类 + 槽位抽取 ────────────────────────────────

    def classify_intent(self, ctx: PlannerContext) -> Dict[str, Any]:
        """低成本意图分类（temperature=0）。

        LLM 不可用时退回关键词兜底（拍板结论 2 的降级路径）。

        实现期修订 v1.2：`max_tokens` 从 600 提到 2048——带隐藏推理链的模型（如
        DeepSeek-V4-Flash）思考过程也计入输出预算，600 太小会把 JSON 答案截断，
        逼得每次都退回关键词兜底，参考旧路径 `max_tokens=4096` 的经验值放宽。
        """
        prompt = planner_prompts.intent_prompt(
            session_context=self._session_context_text(ctx),
            study_areas="、".join(ctx.study_areas) or "",
        )
        parsed = self.call_json(prompt, ctx.user_input, temperature=0.0, max_tokens=2048,
                               history=self._recent_history(ctx))
        if parsed is None:
            return self._keyword_fallback(ctx)

        intent = str(parsed.get("intent") or "").strip()
        if intent not in plan_schema.INTENTS:
            intent = self._keyword_fallback(ctx)["intent"]
        raw_slots = parsed.get("slots") if isinstance(parsed.get("slots"), dict) else {}
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
        """把地名解析成研究区文件绝对路径（技术方案 4.4 硬规则）。

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

        raw: Optional[dict] = None
        if region.get("study_area_file") and time_value and time_value[0]:
            raw = self.call_json(
                planner_prompts.plan_prompt(
                    region_name=region.get("name", ""),
                    time_range=slot_utils.describe_range(time_value[0], time_value[1]),
                    product=(merged.get("product") or {}).get("value", "lst_10m"),
                    constraints=json.dumps(constraints, ensure_ascii=False),
                    skill_catalog=ctx.skill_catalog,
                    memory_block=self.memory_block(ctx.user_input),
                ),
                self._plan_request_text(ctx, region, time_value),
                # 实现期修订 v1.2：与 4096 的旧路径经验值对齐，避免 7 步计划被截断
                temperature=0.1, max_tokens=4096,
            )

        base = raw if isinstance(raw, dict) else {}
        plan = plan_schema.parse({
            "intent": intent,
            "goal": str(base.get("goal") or goal),
            "region": {"name": region.get("name", ""),
                       "study_area_file": region.get("study_area_file", "")},
            "time_range": {"start": time_value[0] if time_value else "",
                           "end": time_value[1] if len(time_value) > 1 else ""},
            "constraints": {**constraints, **(base.get("constraints") or {})},
            "steps": base.get("steps") or self._builtin_steps(region, time_value),
            "memory_refs": base.get("memory_refs") or [],
        }, registry=self.registry)
        return plan

    def build_postprocess_plan(self, ctx: PlannerContext, intent: str) -> Dict[str, Any]:
        """后处理计划：只对已有 10m LST 结果做空洞填补，不重跑生产流程。

        region/time_range 留空（不需要新输入）；lst_gapfill 的输入路径由执行引擎
        从项目结果目录自动定位（executor 对 lst_gapfill 做动态查找）。
        """
        return plan_schema.parse({
            "intent": intent,
            "goal": "对已有十米地表温度产品做空洞填补，生成无空洞产品",
            "region": {"name": "", "study_area_file": ""},
            "time_range": {"start": "", "end": ""},
            "constraints": self._constraints(ctx, {}),
            "steps": [{"skill": "lst_gapfill", "params": {},
                       "reason": "对已有 10m LST 结果做空洞填补"}],
            "memory_refs": [],
        }, registry=self.registry)

    def _plan_request_text(self, ctx: PlannerContext, region: Dict[str, str],
                           time_value: List[str]) -> str:
        return (f"请为「{region.get('name', '')}」在 "
                f"{slot_utils.describe_range(time_value[0], time_value[1])} 的"
                f"十米地表温度产品生成完整执行计划。用户原话：{ctx.user_input}")

    def _builtin_steps(self, region: Dict[str, str], time_value: List[str]) -> List[dict]:
        """内置全流程步骤（LLM 不可用或输出不合法时的兜底）。"""
        start = time_value[0] if time_value else ""
        end = time_value[1] if len(time_value) > 1 else ""
        steps = []
        for name in plan_schema.WORKFLOW_STEPS:
            params: Dict[str, Any] = {}
            if name == "data_acquisition":
                params = {"region": region.get("study_area_file", ""),
                          "start_date": start, "end_date": end}
            steps.append({"skill": name, "params": params,
                          "reason": planner_rules._DEFAULT_REASONS.get(name, "")})
        return steps

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
        label = "十米地表温度产品" if product == "lst_10m" else product
        return f"生成{region_name or '所选研究区'} {when} 的{label}"

    # ── 第四步：轻反思（规则在后，永远覆盖 LLM） ──────────────────

    def reflect(self, ctx: PlannerContext, plan: Dict[str, Any],
                classified: Dict[str, Any]) -> ReflectionResult:
        llm_result = self._llm_reflect(ctx, plan, classified)
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
            ),
            # 实现期修订 v1.2：retry_once=False 只有一次机会，预算给足避免被截断
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

    # ── 对外入口 ──────────────────────────────────────────────────

    def run(self, ctx: PlannerContext) -> PlannerOutcome:
        """一次完整规划：分类 → 合并槽位 → 解析研究区 → 出 plan → 轻反思。"""
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
        if not slot_utils.is_executable(str(time_slot.get("precision") or "")):
            # 反问时间前补一段简短思考说明（升级点 15/16）：意图分类若走
            # 关键词兜底没有 LLM 思考，这里也让反问消息有思考块与用时可展示
            if not self._thinking_acc:
                region_name = str(region.get("name") or "") or "该研究区"
                self._push_thinking(
                    f"用户想生成{region_name}的地表温度产品，但还没说明影像的"
                    "时间范围，需要先反问确认具体月份，才能制定完整执行计划。"
                )
            return PlannerOutcome(PlannerOutcome.ASK,
                                  question=self._ask_time(time_slot, region),
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

    def replan(self, ctx: PlannerContext, adjusted_plan: Dict[str, Any]) -> PlannerOutcome:
        """带原因重新出 plan（技术方案 2.4 规则 5：必须体现针对性调整）。

        入参 `adjusted_plan` 已由总调度按子 Agent 建议做过确定性调整
        （放宽云量 / 扩大时间窗 / 换数据源），本方法据此重建步骤参数并跑一次反思。
        """
        time_range = adjusted_plan.get("time_range") or {}
        region = adjusted_plan.get("region") or {}
        steps = self._builtin_steps(
            {"study_area_file": region.get("study_area_file", "")},
            [time_range.get("start", ""), time_range.get("end", "")],
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
        """反问时间范围（实现期修订 v1.2：年份先做合理性校验，不合理时不能原样复述）。

        例如用户说「125年」，不能直接问「125 年范围比较大，请确认具体月份」——
        那是在不加甄别地复述一个明显异常的输入，得先指出年份本身有问题。
        """
        region_name = str((region or {}).get("name") or "").strip() or "该研究区"
        year = time_slot.get("year")
        month = time_slot.get("month")
        raw = str(time_slot.get("raw") or "").strip()
        if year and not slot_utils.year_plausible(year):
            hint = f"「{raw}」" if raw else f"「{year} 年」"
            return (f"{hint}看起来不像一个可以下载到影像的年份（系统数据从 "
                    f"{slot_utils.MIN_DATA_YEAR} 年前后才开始覆盖），你是想说哪一年？"
                    f"请确认具体年份和月份，例如 2025 年 7 月。")
        if year and not month:
            return f"{region_name}可以，不过 {year} 年范围比较大，请再确认具体到哪个月份，例如 {year} 年 7 月。"
        if month and not year:
            return f"要处理的是哪一年的 {month} 月？例如 2025 年 {month} 月。"
        return (f"好的，生成 {region_name} 的地表温度产品前，我还差一个信息："
                f"你想要哪个时间段（具体到月份）？例如 2025 年 7 月。")

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
        if ctx.replan_reason:
            parts.append(f"- 这是一次重新规划，原因：{ctx.replan_reason}")
        return "\n".join(parts)

    @staticmethod
    def _recent_history(ctx: PlannerContext, limit: int = 8) -> List[dict]:
        """只带最近若干轮，避免意图分类的 prompt 无限膨胀。"""
        return ctx.prior_messages[-limit:] if ctx.prior_messages else []


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
