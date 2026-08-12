"""
角色编排的总调度流程

`GeoThermoAgent.process_command_with_roles` 的实现落在这里，`GeoThermoAgent` 侧只保留
薄委托。这样做的原因有两个：
1. `geo_thermo_agent.py` 已承载队友的旧路径（关键词分流、路径归一化、内置全流程兜底），
   把新增的编排逻辑塞进去会让单文件继续膨胀，违反「多个小文件」的编码规范；
2. 「Plan 交规划 Agent、Solve 由总调度按 plan 依次调用执行 Agent」是编排层的职责，
   与 `orchestrator/` 下的执行模式、审批协议、状态机天然同层。

replan 只能由总调度发起、只能由规划 Agent 产出新 plan（规则 1），
这条规则的落点就是本文件的 `solve_with_replan`。
"""

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

from .. import plan_schema, presentation
from ..executor import PAUSE_MARKER
from . import agent_config, approval as approval_proto
from .approval import Node, Option
from .exec_mode import normalize as _normalize_exec_mode
from .run_state import RunState

logger = logging.getLogger(__name__)


def run_with_roles(agent, user_input: str, on_token=None, on_log=None,
                   pause_callback=None, project_dir: str = "", workflow_callback=None,
                   settings_path: str = "", study_areas_dir: str = "", conv_id: str = "",
                   project_id: str = "", memory_manager=None, exec_mode: str = "",
                   prior_messages=None, session_state=None, on_thinking=None) -> str:
    """多角色路径：规划 Agent 出 plan，本方法按 plan 依次调用执行 Agent。

    与 `process_command` 的旧路径互不影响；`roles_enabled=False` 时永不进入这里。
    """
    from ..roles.planner_agent import PlannerAgent, PlannerContext, PlannerOutcome

    cfg = agent._agent_settings(settings_path)
    mode = _normalize_exec_mode(exec_mode, cfg["default_exec_mode"])
    settings = agent._load_config(settings_path)

    stream_acc: List[str] = []

    def _emit(text: str, to_log: bool = False):
        if to_log:
            if on_log:
                on_log(text)
            return
        # 气泡文案统一数字两侧空格（幂等）
        stream_acc.append(presentation.normalize_number_spacing(text))
        if on_token:
            on_token("".join(stream_acc))

    session = session_store(memory_manager, conv_id, project_id)
    state_data = session.load() if session is not None else dict(session_state or {})

    planner = PlannerAgent(agent.assistant, agent.registry,
                           memory_manager=memory_manager, project_id=project_id,
                           on_log=on_log, replan_max=cfg["replan_max"],
                           on_thinking=on_thinking)
    run_state = RunState(exec_mode=mode, replan_max=cfg["replan_max"],
                         conv_id=conv_id, project_id=project_id)

    ctx = PlannerContext(
        user_input=user_input,
        prior_messages=prior_messages or [],
        session_state=state_data,
        study_areas=agent._list_study_areas(study_areas_dir),
        study_areas_dir=agent._resolved_study_areas_dir(study_areas_dir),
        project_dir=project_dir,
        settings=settings,
        skill_catalog=agent.registry.get_tool_descriptions_for_llm(),
        replan_count=int(state_data.get("replan_count") or 0),
    )

    outcome = planner.run(ctx)
    persist_slots(session, outcome, state_data)

    if outcome.action == PlannerOutcome.CHAT:
        context = agent._get_context(settings_path=settings_path,
                                   study_areas_dir=study_areas_dir)
        if memory_manager is not None and project_id:
            try:
                context["memory"] = memory_manager.enrich_prompt(project_id, user_input)
            except Exception:
                pass
        return agent.assistant.ask_stream(user_input, on_token, context=context,
                                         prior_messages=prior_messages or [],
                                         on_thinking=on_thinking)

    if outcome.action == PlannerOutcome.ASK:
        _emit(outcome.question)
        return outcome.question

    plan = outcome.plan or {}
    run_state.plan_id = plan.get("plan_id", "")
    # 研究区未确定（如单步结果后处理）时不出「已载入研究区：」空行
    _region_name = (plan.get("region") or {}).get("name", "")
    if _region_name:
        _emit(presentation.study_area_loaded(_region_name))
    _emit(presentation.plan_ready(len(plan.get("steps") or [])))

    if not project_dir:
        message = "还没有设置项目目录，请先在左侧选择或创建项目，我再开始执行。"
        _emit(message)
        return message

    # 审批节点 plan_confirm（由我批准模式停下确认；完全执行模式直接开跑）
    if approval_proto.should_pause(Node.PLAN_CONFIRM, mode) and pause_callback:
        payload = approval_proto.build_plan_confirm(
            plan.get("goal", ""), summary=plan_summary(plan))
        response = pause_callback(payload)
        if not isinstance(response, dict) or response.get("paused"):
            _emit(presentation.waiting_for_user())
            return "\n".join(stream_acc) + f"\n{PAUSE_MARKER}"
        choice = (response.get("data") or {}).get("option_id", Option.START)
        run_state.record_approval(Node.PLAN_CONFIRM, choice)
        if choice != Option.START:
            message = "好的，请说明要调整的研究区、时间范围或产品要求。"
            _emit(message)
            if session is not None:
                session.update(pending_question=message)
            return message

    # 月度合成模式选择（时间范围为整月时，无论「完全执行」还是「由我批准」，
    # 都必须用弹窗询问用户是「配对模式」还是「月度合成模式」；
    # 用户指令里已明确提到「月度合成/配对模式」等说法时跳过弹窗直接进入对应模式）
    _pause_flag = ask_acquisition_mode_if_month(plan, pause_callback, run_state, _emit,
                                                user_input=user_input)
    if _pause_flag:
        return "\n".join(stream_acc) + f"\n{_pause_flag}"

    return solve_with_replan(agent, 
        plan, planner=planner, ctx=ctx, run_state=run_state, session=session,
        emit=_emit, mode=mode, cfg=cfg,
        on_token=on_token, on_log=on_log, pause_callback=pause_callback,
        project_dir=project_dir, workflow_callback=workflow_callback,
        stream_acc=stream_acc, settings_path=settings_path,
        study_areas_dir=study_areas_dir, conv_id=conv_id, project_id=project_id,
        memory_manager=memory_manager, on_thinking=on_thinking,
    )


def _detect_acquisition_mode_hint(text: str) -> str:
    """用户指令里是否已明确指定影像获取方式。

    「月度合成/月合成/按月合成/月度模式/合成模式」→ 'monthly'；
    「配对模式/影像配对/逐对/按对/配对」→ 'pair'；
    都没提到 → 返回空串（照旧弹窗询问）。月度词优先于配对词。
    """
    text = text or ""
    if any(w in text for w in ("月度合成", "月合成", "按月合成", "月度模式", "合成模式")):
        return "monthly"
    if any(w in text for w in ("配对模式", "影像配对", "逐对", "按对", "配对")):
        return "pair"
    return ""


def ask_acquisition_mode_if_month(plan: dict, pause_callback, run_state, emit,
                                  user_input: str = "") -> str:
    """时间范围为整月时，弹窗询问「配对模式 / 月度合成模式」。

    无论执行模式（完全执行 / 由我批准）都会触发；用户选择写入 plan 的
    data_acquisition 步骤 params.composite（'monthly' / 'pair'，执行引擎与
    下载模块的约定值，与前端选项值 monthly_mode/pair_mode 区分开）。
    返回非空表示流程应暂停（PAUSE_MARKER）；返回空字符串表示继续执行。
    时间范围不是整月（或未提供）时不弹窗。

    用户指令已明确指定获取方式（如「做月度合成」「用配对模式」）时跳过弹窗，
    直接按指定方式写入 composite（不依赖时间范围是否整月）。
    """
    # 用户指令直接指定获取方式：跳过弹窗（并记录该「选择」供断点恢复）
    _mode_hint = _detect_acquisition_mode_hint(user_input)
    if _mode_hint:
        run_state.record_approval(
            approval_proto.Node.ACQUISITION_MODE,
            (approval_proto.Option.MONTHLY_MODE if _mode_hint == "monthly"
             else approval_proto.Option.PAIR_MODE))
        for _s in plan.get("steps", []):
            if _s.get("skill") == "data_acquisition":
                _s.setdefault("params", {})["composite"] = _mode_hint
                break
        return ""

    _tr = plan.get("time_range") or {}
    _start, _end = str(_tr.get("start") or ""), str(_tr.get("end") or "")
    _is_month = bool(_start[:7]) and _start[:7] == _end[:7]
    if not (_is_month and pause_callback):
        return ""
    _month_label = f"{_start[:4]} 年 {int(_start[5:7])} 月"
    _mode_payload = approval_proto.build(
        approval_proto.Node.ACQUISITION_MODE,
        "选择影像获取方式",
        f"你的时间范围是{_month_label}（整月），请选择影像获取方式：",
        [
            approval_proto.option(approval_proto.Option.PAIR_MODE, "配对模式",
                                  hint="逐对 Landsat/Sentinel-2 影像处理，保留单日细节"),
            approval_proto.option(approval_proto.Option.MONTHLY_MODE, "月度合成模式",
                                  hint="将该月全部符合云量阈值的影像合成为一张月度产品后再处理"),
        ],
    )
    _mode_resp = pause_callback(_mode_payload)
    if not isinstance(_mode_resp, dict) or _mode_resp.get("paused"):
        emit(presentation.waiting_for_user())
        return PAUSE_MARKER
    _mode_choice = ((_mode_resp.get("data") or {}).get("option_id")
                    or approval_proto.Option.PAIR_MODE)
    run_state.record_approval(approval_proto.Node.ACQUISITION_MODE, _mode_choice)
    # 规范化：前端选项值（monthly_mode/pair_mode）→ 执行引擎/下载模块约定值（monthly/pair）
    _composite = "monthly" if _mode_choice == approval_proto.Option.MONTHLY_MODE else "pair"
    for _s in plan.get("steps", []):
        if _s.get("skill") == "data_acquisition":
            _s.setdefault("params", {})["composite"] = _composite
            break
    return ""

def solve_with_replan(agent, plan: dict, *, planner, ctx, run_state, session, emit,
                      mode: str, cfg: dict, on_token=None, on_log=None,
                      pause_callback=None, project_dir: str = "", workflow_callback=None,
                      stream_acc=None, settings_path: str = "", study_areas_dir: str = "",
                      conv_id: str = "", project_id: str = "", memory_manager=None,
                      on_thinking=None) -> str:
    """Solve 阶段：按 plan 执行；子 Agent 请求 replan 时由本方法（总调度）发起。

    replan 只能由总调度发起、只能由规划 Agent 产出新 plan（规则 1）。
    """
    from .role_hooks import RoleHooks
    from ..roles.data_agent import DataAgent
    from ..roles.eval_agent import EvalAgent
    from ..roles.train_agent import TrainAgent

    results: List[str] = []
    current = plan

    while True:
        hooks = RoleHooks(
            exec_mode=mode, run_state=run_state, pause_callback=pause_callback,
            data_agent=DataAgent(agent.assistant, memory_manager=memory_manager,
                                 project_id=project_id, on_log=on_log,
                                 on_thinking=on_thinking),
            train_agent=TrainAgent(agent.assistant, agent.registry,
                                   memory_manager=memory_manager,
                                   project_id=project_id, on_log=on_log,
                                   max_rounds=cfg["tuning_max_rounds"],
                                   on_thinking=on_thinking),
            eval_agent=EvalAgent(agent.assistant, memory_manager=memory_manager,
                                 project_id=project_id, on_log=on_log,
                                 on_thinking=on_thinking),
            agent_cfg=cfg, on_log=on_log,
        )
        agent._normalize_plan_paths(current, study_areas_dir=study_areas_dir)
        output = agent._execute_plan(
            current, on_token=on_token, on_log=on_log, pause_callback=pause_callback,
            project_dir=project_dir, workflow_callback=workflow_callback,
            stream_acc=stream_acc, settings_path=settings_path,
            study_areas_dir=study_areas_dir, conv_id=conv_id, project_id=project_id,
            memory_manager=memory_manager, exec_mode=mode, run_state=run_state,
            hooks=hooks,
        )
        results.append(output)

        request = hooks.replan_request
        if request is None or PAUSE_MARKER in output:
            return "\n".join(r for r in results if r)

        # 「重新选择影像组合」是阶段内回退，不是重新规划：
        # 不调用规划 Agent、不计入 replan 次数，原样复用当前 plan 重新执行（自然回到
        # data_acquisition 重新搜索并弹出配对选择），避免多余的 LLM 调用和被规划 Agent
        # 改写整条计划的风险。
        if (request.get("payload") or {}).get("reselect_pair"):
            emit("好的，回到影像组合选择，重新来一次。\n")
            # 必须清掉上一轮写入 data_acquisition 步骤 params 的 selected_pair，
            # 否则执行引擎会认为「已经选好配对」直接跳过搜索——而 data_acquisition
            # 分支在这种情况下并没有走任何一条会给 result 赋值的代码路径，会在
            # 拼装结果消息时抛出「result 未定义」的异常（步骤 params 字典是被就地
            # 修改的，原样复用同一个 plan 对象会带着上一轮的选择残留）。
            current = clear_selected_pair(current)
            continue

        if not run_state.can_replan():
            message = ("已经自动重新规划多次仍未成功。请告诉我下一步怎么调整："
                       "换时间段、换研究区，还是放宽云量要求？")
            emit(message + "\n")
            if session is not None:
                session.update(pending_question=message)
            return "\n".join(r for r in results if r) + "\n" + message

        emit(f"正在按新的条件重新规划：{request['reason']}\n")
        new_plan = replan(agent, planner, ctx, current, request, run_state)
        if new_plan is None:
            message = ("重新规划没能给出有效的新方案。请告诉我要改什么："
                       "换时间段、换研究区，还是放宽云量要求？")
            emit(message + "\n")
            if session is not None:
                session.update(pending_question=message)
            return "\n".join(r for r in results if r) + "\n" + message
        current = new_plan

def replan(agent, planner, ctx, previous_plan: dict, request: dict,
            run_state) -> Optional[dict]:
    """带原因交规划 Agent 重新出 plan，并要求新方案有实质差异（规则 P5/P7）。"""
    from ..roles.planner_agent import PlannerContext, PlannerOutcome

    adjusted = adjust_for_replan(previous_plan, request.get("payload") or {})
    new_ctx = PlannerContext(
        user_input=ctx.user_input,
        prior_messages=ctx.prior_messages,
        session_state=ctx.session_state,
        study_areas=ctx.study_areas,
        study_areas_dir=ctx.study_areas_dir,
        project_dir=ctx.project_dir,
        settings=ctx.settings,
        skill_catalog=ctx.skill_catalog,
        replan_reason=request.get("reason", ""),
        replan_count=run_state.replan_count,
        previous_plan=previous_plan,
        today=ctx.today,
    )
    outcome = planner.replan(new_ctx, adjusted)
    if outcome.action != PlannerOutcome.PLAN or not outcome.plan:
        return None
    return outcome.plan

def clear_selected_pair(plan: dict) -> dict:
    """返回清掉了 `data_acquisition.params.selected_pair` 的新 plan（不可变，不改入参）。

    重新选择影像组合时原样复用 plan 重新执行，必须先清掉上一轮写入的选择，否则
    执行引擎会把「params 里带着 selected_pair」误判为「已经选好了」而跳过搜索。
    """
    steps = []
    for step in plan.get("steps") or []:
        if step.get("skill") == "data_acquisition" and "selected_pair" in (step.get("params") or {}):
            params = dict(step["params"])
            params.pop("selected_pair", None)
            step = {**step, "params": params}
        steps.append(step)
    return {**plan, "steps": steps}


def adjust_for_replan(plan: dict, payload: dict) -> dict:
    """按子 Agent 的建议做确定性调整，保证新 plan 与上一版有实质差异（规则 P7）。"""
    import datetime as _dt

    constraints = dict(plan.get("constraints") or {})
    time_range = dict(plan.get("time_range") or {})

    if payload.get("relax_cloud"):
        current = constraints.get("cloud_threshold")
        try:
            current = int(current)
        except (TypeError, ValueError):
            current = 30
        constraints["cloud_threshold"] = min(90, current + 20)

    if payload.get("widen_time"):
        try:
            start = _dt.date.fromisoformat(time_range.get("start", ""))
            end = _dt.date.fromisoformat(time_range.get("end", ""))
            time_range["start"] = (start - _dt.timedelta(days=30)).isoformat()
            time_range["end"] = min(_dt.date.today(),
                                    end + _dt.timedelta(days=30)).isoformat()
        except (TypeError, ValueError):
            pass

    if payload.get("change_source"):
        constraints["dem_source"] = ("planetary"
                                     if constraints.get("dem_source") == "copernicus"
                                     else "copernicus")

    return {**plan, "constraints": constraints, "time_range": time_range}

# ── 角色路径的小工具 ─────────────────────────────────────────────

def session_store(memory_manager, conv_id: str, project_id: str):
    if memory_manager is None or not conv_id:
        return None
    try:
        return memory_manager.session_state(conv_id, project_id)
    except Exception as e:
        logger.warning(f"[memory] 会话状态不可用（已忽略）: {e}")
        return None

def persist_slots(session, outcome, state_data: dict) -> None:
    """把本轮槽位与待补问题落盘；写失败仅告警（记忆系统现有约定）。"""
    if session is None:
        return
    from ..roles.planner_agent import PlannerOutcome

    slots = dict(outcome.slots or {})
    if outcome.action == PlannerOutcome.CHAT:
        # 非任务轮提到的地名降级为 mentioned，供下一轮延续上下文
        region = slots.get("region_name")
        if isinstance(region, dict) and region.get("source") == "user":
            slots["region_name"] = {**region, "source": "mentioned"}
    pending = outcome.question if outcome.action == PlannerOutcome.ASK else ""
    try:
        session.set_slots(slots, intent=outcome.intent, pending_question=pending,
                          plan_id=(outcome.plan or {}).get("plan_id", "")
                          if outcome.plan else "")
    except Exception as e:
        logger.warning(f"[memory] 会话槽位写入失败（已忽略）: {e}")
def plan_summary(plan: dict) -> str:
    """给 plan_confirm 用的中文摘要（气泡红线：不出现英文技能名与路径）。"""
    from ..roles.slots import describe_range

    region = (plan.get("region") or {}).get("name", "") or "所选研究区"
    tr = plan.get("time_range") or {}
    when = describe_range(tr.get("start", ""), tr.get("end", ""))
    stages = "、".join(presentation.stage_label(s) for s in plan_schema.skill_names(plan))
    constraints = plan.get("constraints") or {}
    cloud = constraints.get("cloud_threshold")
    cloud_text = f"，云量上限 {cloud}" if cloud is not None else ""
    # 研究区/时间/云量与步骤数分两行显示，前后两句都不带句号
    return (f"研究区 {region}，时间 {when}{cloud_text}\n"
            f"共 {len(plan.get('steps') or [])} 步：{stages}")
