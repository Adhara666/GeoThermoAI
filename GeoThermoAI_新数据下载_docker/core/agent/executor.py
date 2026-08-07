"""
执行引擎（技术方案 10.2）

由 `GeoThermoAgent._execute_plan` 平移而来，`GeoThermoAgent._execute_plan` 保留为
签名一字不改的薄委托（`tests/test_memory_synthetic.py` 直接调用它，是回归护栏）。

平移原则（P0）：`hooks is None` 时执行路径与气泡文案与平移前**完全等价**；
气泡文案的中文化在 P6 统一进行（届时改为调用 `presentation` 的渲染函数）。

钩子机制（`StepDecision` / `StageHooks`）供角色编排（P1 起）介入
「改参数 / 暂停审批 / 重跑本步 / 中止 / 交回 replan」。
"""

import glob
import logging
import os
import time
from typing import Any, Dict, List, Optional

from . import presentation
from .orchestrator.hooks import StageHooks, StepDecision

logger = logging.getLogger(__name__)

# 单步 RETRY 硬上限（防止死循环，必须大于调优轮数上限）
MAX_RETRY_PER_STEP = 10

# 特殊标记：Agent 需要用户输入才能继续（与 geo_thermo_agent.PAUSE_MARKER 同值）
PAUSE_MARKER = "__AGENT_PAUSE__"

# model_train_predict 组中所有 Skill 名称，用于模型超参注入
MODEL_TRAIN_SKILLS = {"rf_model", "xgboost_model"}


class ExecContext:
    """一次执行的运行时上下文（钩子读写它来协作）。"""

    def __init__(self, *, agent, plan: dict, project_dir: str, raw_dir: str,
                 processed_dir: str, results_dir: str, settings_path: str,
                 study_areas_dir: str, conv_id: str, project_id: str,
                 memory_manager, exec_mode: str, run_state, exp_state: dict,
                 emit, pause_callback, total: int):
        self.agent = agent
        self.registry = agent.registry
        self.plan = plan
        self.project_dir = project_dir
        self.raw_dir = raw_dir
        self.processed_dir = processed_dir
        self.results_dir = results_dir
        self.settings_path = settings_path
        self.study_areas_dir = study_areas_dir
        self.conv_id = conv_id
        self.project_id = project_id
        self.memory_manager = memory_manager
        self.exec_mode = exec_mode
        self.run_state = run_state
        self.exp_state = exp_state
        self.emit = emit                    # emit(text, to_log=False)
        self.pause_callback = pause_callback
        self.total = total
        # 运行中累积
        self.step_index = 0
        self.data_features: Optional[dict] = None
        self.last_pairs: List[dict] = []
        self.retry_count = 0
        self.replan_reason = ""
        self.replan_payload: Dict[str, Any] = {}
        self.notes: List[str] = []


def new_exp_state() -> dict:
    """实验记录累加器（前 8 个键与平移前一致；新增键为空时不进入记录）。"""
    return {
        "acq_params": None,       # data_acquisition 步骤参数（region/日期）
        "pair": None,             # 用户选中的影像配对
        "data_features": None,    # data_pipeline 数据特征
        "rf_data": None,          # rf_model 结果 data
        "acc_data": None,         # accuracy_eval 结果 data
        "step_success": {},       # skill_name -> bool
        "failed": None,           # (skill_name, message) 首个失败步骤
        "paused": False,          # 是否因等待用户输入而暂停
        # ── 角色编排新增（技术方案 8.3 工作流经验的数据来源）──
        "pair_candidates": [],    # 候选配对及其得分
        "pair_selected_by": "",   # user / auto
        "tuning_trace": [],       # 调优轨迹
        "final_params": {},       # 最终生效超参
        "exec_mode": "",
        "approval_choices": {},
        "eval_verdict": "",
    }


def build_skill_paths(raw_dir: str, processed_dir: str, results_dir: str) -> Dict[str, dict]:
    """各 Skill 的输入/输出路径硬编码映射（与平移前一致）。"""
    return {
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


def build_experiment_record(exp_state: dict, conv_id: str, project_id: str,
                            status: str, failure_stage: str = "",
                            failure_message: str = "") -> dict:
    """把累加器组装为结构化实验记录（与平移前一致；新增字段仅在非空时写入）。"""
    acq = exp_state.get("acq_params") or {}
    region = acq.get("region", "")
    if isinstance(region, str) and region.lower().endswith(".geojson"):
        region = os.path.basename(region)
    record = {
        "schema_version": 1,
        "experiment_id": f"exp_{project_id[:8]}_{int(time.time())}" if project_id else "",
        "conv_id": conv_id,
        "project_id": project_id,
        "region": region,
        "date_range": [acq.get("start_date", ""), acq.get("end_date", "")],
        "pair": exp_state.get("pair") or {},
        "status": status,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    df = exp_state.get("data_features")
    if df:
        record["data_features"] = df
    rf = exp_state.get("rf_data") or {}
    if rf:
        metrics = {}
        if rf.get("train_metrics"):
            metrics["train"] = rf["train_metrics"]
        if rf.get("test_metrics"):
            metrics["test"] = rf["test_metrics"]
        record["model"] = "rf"
        record["params"] = rf.get("params", {})
        record["metrics"] = metrics
        record["feature_importance"] = rf.get("feature_importance", [])
        record["independent_prediction"] = rf.get("independent_prediction", {})
        record["train_time_seconds"] = rf.get("train_time_seconds", 0)
    acc = exp_state.get("acc_data") or {}
    acc_full = acc.get("closure_metrics") or {}
    if acc_full:
        closure = dict(acc_full.get("closure", {}))
        closure["value_range"] = acc_full.get("value_range", {})
        record["closure"] = closure
    for key in ("pair_candidates", "tuning_trace", "final_params",
                "approval_choices", "exec_mode", "pair_selected_by"):
        value = exp_state.get(key)
        if value:
            record[key] = value
    if status == "failed":
        record["failure_stage"] = failure_stage
        record["failure_message"] = failure_message
    return record


def execute_plan(agent, plan: dict, on_token=None, on_log=None, pause_callback=None,
                 project_dir: str = "", workflow_callback=None,
                 stream_acc: Optional[list] = None, settings_path: str = "",
                 study_areas_dir: str = "", conv_id: str = "", project_id: str = "",
                 memory_manager=None, hooks: Optional[StageHooks] = None,
                 exec_mode: str = "", run_state=None) -> str:
    """遍历计划中的步骤，获取对应 Skill，执行并收集结果。

    特殊处理（与平移前一致）：
    - data_acquisition 走「搜索 → 让用户选配对 → 注入 selected_pair → 下载」
    - data_pipeline 完成后收集数据特征
    - 按 project_dir 强制注入各 Skill 的输入输出路径
    """
    results: List[str] = []
    data_features: Optional[dict] = None
    _emit_accumulator = stream_acc if stream_acc is not None else []

    exp_state = new_exp_state()
    if exec_mode:
        exp_state["exec_mode"] = exec_mode

    def _finalize_experiment(status: str, failure_stage: str = "", failure_message: str = ""):
        """收尾写实验记录：缺记忆上下文或写入失败仅告警，绝不影响主流程。"""
        if memory_manager is None or not project_id or not conv_id:
            return
        try:
            record = build_experiment_record(exp_state, conv_id, project_id,
                                             status, failure_stage, failure_message)
            memory_manager.auto_save_experiment(project_id, record)
        except Exception as e:
            logger.warning(f"[memory] 实验记录写入失败（已忽略）: {e}")

    def _record_step(skill_name: str, ok: bool, message: str = ""):
        exp_state["step_success"][skill_name] = ok
        if not ok and exp_state["failed"] is None:
            exp_state["failed"] = (skill_name, message)

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

    ctx = ExecContext(
        agent=agent, plan=plan, project_dir=project_dir, raw_dir=raw_dir,
        processed_dir=processed_dir, results_dir=results_dir,
        settings_path=settings_path, study_areas_dir=study_areas_dir,
        conv_id=conv_id, project_id=project_id, memory_manager=memory_manager,
        exec_mode=exec_mode, run_state=run_state, exp_state=exp_state,
        emit=_emit, pause_callback=pause_callback, total=total,
    )

    SKILL_PATHS = build_skill_paths(raw_dir, processed_dir, results_dir)

    i = 0
    while i < total:
        step = steps[i]
        skill_name = step["skill"]
        skill = agent.registry.get(skill_name)
        ctx.step_index = i

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
            _cfg = agent._load_config(settings_path).get("data", {})
            if "cloud_threshold" not in step.get("params", {}):
                step.setdefault("params", {})["cloud_threshold"] = _cfg.get("cloud_threshold", 30)
            if "dem_source" not in step.get("params", {}):
                step.setdefault("params", {})["dem_source"] = _cfg.get("dem_source", "copernicus")

        if not skill:
            results.append(f"未找到技能: {skill_name}")
            _emit(presentation.skill_missing(skill_name))
            i += 1
            continue

        # 强制 data_acquisition 的 region 使用已上传研究区文件（执行期兜底：
        # 即使 _normalize_plan_paths 的替换未生效，也保证 region 是 GeoJSON 绝对路径，
        # 屏蔽 LLM 生成的纯城市名/bbox 导致的解析崩溃）。
        # plan 带 region.study_area_file 时以它为准（技术方案 10.1）。
        if skill_name == "data_acquisition":
            _planned = ""
            if isinstance(plan.get("region"), dict):
                _planned = str(plan["region"].get("study_area_file") or "")
            _sa = _planned or agent._find_study_area_file(study_areas_dir)
            if _sa:
                step.setdefault("params", {})["region"] = _sa

        _emit(presentation.step_header(i + 1, total, skill_name))
        # 更新工作流进度（running）
        if workflow_callback:
            workflow_callback(skill_name, "running", i, total)

        # ── 自动调参：model_train_predict 组 Skill 执行前 ─────────────
        if skill_name in MODEL_TRAIN_SKILLS:
            # 从用户设置注入模型参数（settings_path 为空时回退全局配置）
            _model_cfg = agent._load_config(settings_path).get("model", {})
            for k in ["n_estimators", "max_depth", "min_samples_split",
                      "min_samples_leaf", "max_features"]:
                v = _model_cfg.get(k)
                if v is not None:
                    step["params"][k] = v

        # ── 钩子①：执行前（hooks 为 None 时短路）────────────────────
        if hooks is not None:
            pre = hooks.before_step(skill_name, step, ctx)
            if pre is not None and pre.action != StepDecision.CONTINUE:
                outcome = _handle_control_decision(
                    pre, ctx, results, _emit, _finalize_experiment,
                    workflow_callback, skill_name, i, total)
                if outcome is not None:
                    return outcome

        attempt = 0
        while True:
            # ── 执行 Skill（data_acquisition 特殊处理：搜索→选择→下载）─
            try:
                def _log(tag, msg):
                    _emit(f"  [{tag}] {msg}\n", to_log=True)

                def _progress(name, pct, msg):
                    _emit(f"  {name} {int(pct*100)}%: {msg}\n", to_log=True)

                # data_acquisition: 先搜索返回配对，用户选择后再下载
                if skill_name == "data_acquisition":
                    exp_state["acq_params"] = step.get("params", {})
                    if not step.get("params", {}).get("selected_pair"):
                        # 第一次：搜索模式
                        result = skill.execute(
                            step.get("params", {}),
                            progress_callback=_progress,
                            log_callback=_log,
                        )
                        result_data = result.data if isinstance(result.data, dict) else {}
                        pairs = result_data.get("image_pairs", [])
                        if pairs:
                            # 钩子②：排序与推荐标记（不介入时保持原顺序）
                            if hooks is not None:
                                ranked = hooks.rank_pairs(pairs, ctx)
                                if ranked:
                                    pairs = ranked
                            ctx.last_pairs = list(pairs)
                            _emit("  " + presentation.pairs_found(len(pairs)))
                            # 总是让用户确认选择，即使只有一对
                            selected = None
                            if hooks is not None:
                                selected = hooks.select_pair(pairs, ctx)
                                if selected is not None:
                                    exp_state["pair_selected_by"] = "auto"
                            if selected is None and pause_callback:
                                selected = agent._ask_user_to_select_pair(
                                    pairs, pause_callback, _emit, return_selected=True)
                                if selected is None:
                                    exp_state["paused"] = True
                                    _emit(presentation.waiting_for_user())
                                    _finalize_experiment("paused")
                                    return "\n".join(results) + f"\n{PAUSE_MARKER}"
                                exp_state["pair_selected_by"] = "user"
                            elif selected is None:
                                selected = pairs[0]
                                exp_state["pair_selected_by"] = "auto"
                                _emit("  " + presentation.pair_auto_selected(1))
                            # 注入选择，重新执行下载
                            step["params"]["selected_pair"] = selected
                            exp_state["pair"] = selected
                            _emit("  " + presentation.download_started())
                            result = skill.execute(
                                step.get("params", {}),
                                progress_callback=_progress,
                                log_callback=_log,
                            )
                        else:
                            # 执行失败：透出真实错误，不要伪装成"未找到配对"
                            if not result.success:
                                _msg = (result.message or f"{skill_name} 执行失败").strip()
                                _emit("  " + presentation.sanitize(_msg) + "\n")
                                results.append(f"{skill_name}: {_msg}")
                                if workflow_callback:
                                    workflow_callback(skill_name, "failed", i, total)
                                _record_step(skill_name, False, _msg)
                                _finalize_experiment("failed", skill_name, _msg)
                                return "\n".join(results)
                            _lc = result_data.get("landsat_count", 0)
                            _sc = result_data.get("sentinel_count", 0)
                            # 钩子：无合格配对的处置（不介入时沿用原有终止逻辑）
                            if hooks is not None:
                                _detail = {
                                    "landsat_count": _lc,
                                    "sentinel_count": _sc,
                                    "cloud_threshold": step.get("params", {}).get("cloud_threshold"),
                                }
                                np_decision = hooks.on_no_pair(_detail, ctx)
                                if np_decision is not None:
                                    _record_step(skill_name, False, "未找到影像配对")
                                    outcome = _handle_control_decision(
                                        np_decision, ctx, results, _emit,
                                        _finalize_experiment, workflow_callback,
                                        skill_name, i, total)
                                    if outcome is not None:
                                        return outcome
                            _emit(presentation.no_pair_reason({
                                "landsat_count": _lc, "sentinel_count": _sc,
                                "cloud_threshold": step.get("params", {}).get(
                                    "cloud_threshold"),
                            }))
                            results.append(
                                f"{skill_name}: 未找到符合条件的影像配对"
                                f"（Landsat {_lc} 景 / Sentinel {_sc} 景）"
                            )
                            _record_step(skill_name, False, "未找到影像配对")
                            _finalize_experiment("failed", skill_name, "未找到影像配对")
                            return "\n".join(results)
                else:
                    # 普通执行或其他 Skill
                    result = skill.execute(
                        step.get("params", {}),
                        progress_callback=_progress,
                        log_callback=_log,
                    )

                results.append(f"{skill_name}: {result.message}")
                _emit("  " + presentation.summarize(skill_name, result) + "\n")
                # 更新工作流进度（completed）
                if workflow_callback:
                    workflow_callback(skill_name, "completed", i, total)

                # 统一记录本步骤成败（供实验记录聚合判定）
                _record_step(skill_name, result.success, result.message)

                # data_pipeline 完成后收集数据特征
                if skill_name == "data_pipeline" and result.success:
                    data_features = agent._collect_data_features(
                        result.data if isinstance(result.data, dict) else {})
                    ctx.data_features = data_features
                    exp_state["data_features"] = data_features

                # rf_model 成功后缓存模型结果（供实验记录）
                if skill_name == "rf_model" and result.success:
                    exp_state["rf_data"] = dict(result.data) if isinstance(result.data, dict) else {}

                # accuracy_eval 成功后缓存闭合指标（供实验记录）
                if skill_name == "accuracy_eval" and result.success:
                    exp_state["acc_data"] = dict(result.data) if isinstance(result.data, dict) else {}

                # ── 钩子③：执行后（hooks 为 None 时走原有异常检测）──────
                decision = hooks.after_step(skill_name, result, ctx) if hooks is not None else None
                if decision is None or decision.action == StepDecision.CONTINUE:
                    if hooks is None:
                        # 检查异常场景（原逻辑：只提示不拦截）
                        should_continue = agent._check_exceptions(
                            skill_name, result, pause_callback, _emit)
                        if not should_continue:
                            # Agent 暂停等待用户输入，中止后续步骤
                            exp_state["paused"] = True
                            _emit(presentation.waiting_for_user())
                            _finalize_experiment("paused")
                            return "\n".join(results) + f"\n{PAUSE_MARKER}"
                    break

                if decision.action == StepDecision.RETRY:
                    attempt += 1
                    if attempt > MAX_RETRY_PER_STEP:
                        _emit(f"  {presentation.stage_label(skill_name)}重试次数已达上限，"
                              f"按当前结果继续\n")
                        break
                    if decision.new_params:
                        step["params"] = {**step.get("params", {}), **decision.new_params}
                    if decision.reason:
                        _emit(f"  {decision.reason}\n")
                    ctx.retry_count = attempt
                    continue

                outcome = _handle_control_decision(
                    decision, ctx, results, _emit, _finalize_experiment,
                    workflow_callback, skill_name, i, total)
                if outcome is not None:
                    return outcome
                break
            except Exception as e:
                results.append(f"{skill_name} 失败: {e}")
                _emit(f"  {presentation.stage_label(skill_name)}未通过："
                      f"{presentation.sanitize(str(e))}\n")
                if workflow_callback:
                    workflow_callback(skill_name, "failed", i, total)
                _record_step(skill_name, False, str(e))
                break

        i += 1

    # ── 收尾：判定整体状态并写入实验记录（聚合判定，见 Schema 第三节）──
    if exp_state["paused"]:
        _finalize_experiment("paused")
    else:
        failed = exp_state["failed"]
        if failed is not None:
            _finalize_experiment("failed", failed[0], failed[1])
        else:
            _finalize_experiment("success")
    return "\n".join(results)


def _handle_control_decision(decision: StepDecision, ctx: ExecContext, results: List[str],
                             emit, finalize, workflow_callback, skill_name: str,
                             index: int, total: int) -> Optional[str]:
    """处理 PAUSE / ABORT / REPLAN 三类控制决策。

    返回非 None 表示执行引擎应立即返回该字符串；返回 None 表示继续往下执行。
    """
    if decision.action == StepDecision.PAUSE:
        ctx.exp_state["paused"] = True
        emit(presentation.waiting_for_user())
        finalize("paused")
        return "\n".join(results) + f"\n{PAUSE_MARKER}"

    if decision.action == StepDecision.ABORT:
        if decision.message:
            emit(decision.message if decision.message.endswith("\n") else decision.message + "\n")
        if workflow_callback:
            workflow_callback(skill_name, "failed", index, total)
        reason = decision.reason or "流程已停止"
        results.append(f"{skill_name}: {reason}")
        finalize("failed", skill_name, reason)
        return "\n".join(results)

    if decision.action == StepDecision.REPLAN:
        ctx.replan_reason = decision.reason or "需要重新规划"
        ctx.replan_payload = dict(decision.payload or {})
        if decision.message:
            emit(decision.message if decision.message.endswith("\n") else decision.message + "\n")
        results.append(f"{skill_name}: {ctx.replan_reason}")
        finalize("failed", skill_name, ctx.replan_reason)
        return "\n".join(results)

    return None
