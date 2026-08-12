"""
执行引擎

负责遍历计划中的步骤、获取对应 Skill 并执行、收集结果，并向气泡输出进度。
`GeoThermoAgent._execute_plan` 保留为签名一字不改的薄委托（`tests/test_memory_synthetic.py`
直接调用它，是回归护栏）。

钩子机制（`StepDecision` / `StageHooks`）供角色编排介入
「改参数 / 暂停审批 / 重跑本步 / 中止 / 交回 replan」。
"""

import glob
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from . import presentation
from .orchestrator.hooks import StageHooks, StepDecision
from ..memtrim import release_rss_memory
from ..skills.base_skill import SkillResult

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
    """实验记录累加器（固定键位；新增键为空时不进入记录）。"""
    return {
        "acq_params": None,       # data_acquisition 步骤参数（region/日期）
        "pair": None,             # 用户选中的影像配对
        "data_features": None,    # data_pipeline 数据特征
        "rf_data": None,          # rf_model 结果 data
        "acc_data": None,         # accuracy_eval 结果 data
        "step_success": {},       # skill_name -> bool
        "failed": None,           # (skill_name, message) 首个失败步骤
        "paused": False,          # 是否因等待用户输入而暂停
        # ── 角色编排新增（工作流经验的数据来源）──
        "pair_candidates": [],    # 候选配对及其得分
        "pair_selected_by": "",   # user / auto
        "tuning_trace": [],       # 调优轨迹
        "final_params": {},       # 最终生效超参
        "exec_mode": "",
        "approval_choices": {},
        "eval_verdict": "",
    }


def build_skill_paths(raw_dir: str, processed_dir: str, results_dir: str,
                      dates: Optional[dict] = None, project_dir: str = "") -> Dict[str, dict]:
    """各 Skill 的输入/输出路径硬编码映射。

    dates 携带当前配对的影像日期（YYYYMMDD），用于生成带日期的文件名
    （本地 LST/Sentinel-2 文件名必须带日期；DEM 不带日期）。
    project_dir 注入给 data_acquisition，供「已下载配对去重 / 重复影像复制 / DEM 复用」使用。
    """
    ldate = str((dates or {}).get("landsat") or "").strip()
    sdate = str((dates or {}).get("sentinel2") or "").strip()

    def _landsat(name: str) -> str:
        return f"{name}_{ldate}.tif" if ldate else f"{name}.tif"

    def _sentinel(name: str) -> str:
        return f"{name}_{sdate}.tif" if sdate else f"{name}.tif"

    landsat_file = _landsat("landsat_lst")
    qa_file = _landsat("landsat_qa_pixel")
    s2_file = _sentinel("sentinel2_bands")
    scl_file = _sentinel("sentinel2_scl")
    tcr_csv = f"tcr_result_{sdate}.parquet" if sdate else "tcr_result.parquet"
    lst_tif = f"rf_10m_lst_final_{sdate}.tif" if sdate else "rf_10m_lst_final.tif"
    lst_csv = f"rf_10m_predict_{sdate}.parquet" if sdate else "rf_10m_predict.parquet"
    # 结果后处理（空洞填补）产物：命名与原始产品区分，
    # 前缀 _filled 保证与 lst_export 的 glob（rf_10m_lst_final_[0-9]*）互不串扰
    filled_tif = f"rf_10m_lst_final_filled_{sdate}.tif" if sdate else "rf_10m_lst_final_filled.tif"
    filled_mask = f"rf_10m_lst_final_filled_{sdate}_cloud_mask.tif" if sdate else "rf_10m_lst_final_filled_cloud_mask.tif"

    return {
        "data_acquisition": {
            "output_dir": raw_dir,
            "project_dir": project_dir,  # 供已下载对跳过/重复影像复制/DEM 复用
        },
        "data_pipeline": {
            "output_dir": processed_dir,
            "landsat_path": raw_dir + "/" + landsat_file,
            "qa_path": raw_dir + "/" + qa_file,
            "sentinel2_path": raw_dir + "/" + s2_file,
            "scl_path": raw_dir + "/" + scl_file,
            "dem_path": raw_dir + "/dem.tif",
        },
        "ttri_compute": {
            "output_dir": processed_dir,
            "data_30m_csv": processed_dir + "/30m_features_step2.parquet",
            "predict_10m_csv": processed_dir + "/10m_predict_features.parquet",
            "train_csv": processed_dir + "/train.parquet",
            "val_csv": processed_dir + "/validate.parquet",
            "test_csv": processed_dir + "/test.parquet",
        },
        "rf_model": {
            "output_dir": results_dir,
            "train_csv": processed_dir + "/train.parquet",
            "val_csv": processed_dir + "/validate.parquet",
            "test_csv": processed_dir + "/test.parquet",
        },
        "tcr_compute": {
            "output_dir": results_dir,
            "output_path": results_dir + "/" + tcr_csv,
            "data_30m_csv": processed_dir + "/30m_features_step2.parquet",
            "meta_30m_json": processed_dir + "/30m_features_step2_meta.json",
            "predict_10m_csv": processed_dir + "/10m_predict_features.parquet",
            "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
            "model_path": None,  # 动态查找
        },
        "lst_export": {
            "output_dir": results_dir,
            "input_csv": results_dir + "/" + tcr_csv,
            "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
            "output_tif": results_dir + "/" + lst_tif,
            "lst_final_csv": results_dir + "/" + lst_csv,
        },
        "accuracy_eval": {
            "output_dir": results_dir,
            "test_csv": processed_dir + "/test.parquet",
            "full_30m_csv": processed_dir + "/30m_features_step2.parquet",
            "predict_csv": results_dir + "/" + tcr_csv,
            "meta_30m_json": processed_dir + "/30m_features_step2_meta.json",
            "meta_10m_json": processed_dir + "/10m_predict_features_meta.json",
        },
        "lst_gapfill": {
            "output_dir": results_dir,
            "input_tif": results_dir + "/" + lst_tif,
            "output_tif": results_dir + "/" + filled_tif,
            "output_mask": results_dir + "/" + filled_mask,
        },
    }


def pair_dirs(project_dir: str, selected_pair: Optional[dict]) -> tuple:
    """按当前选中的影像对计算独立目录（每对影像及其后续文件目录独立）。

    目录布局：{project_dir}/pairs/L{landsat_date}_S{sentinel2_date}/{raw,processed,results}
    未选择配对（搜索阶段）时回退项目级 {project_dir}/{raw,processed,results}。
    """
    if not project_dir:
        return "", "", ""
    pair = selected_pair if isinstance(selected_pair, dict) else None
    l = str((pair or {}).get("landsat_date") or "").replace("-", "")
    s = str((pair or {}).get("sentinel2_date") or "").replace("-", "")
    if l and s:
        base = f"{project_dir}/pairs/L{l}_S{s}".replace("\\", "/")
        return f"{base}/raw", f"{base}/processed", f"{base}/results"
    return (f"{project_dir}/raw", f"{project_dir}/processed", f"{project_dir}/results")


def pair_dates(selected_pair: Optional[dict]) -> dict:
    """从选中的配对提取 YYYYMMDD 日期；无配对返回空。"""
    pair = selected_pair if isinstance(selected_pair, dict) else None
    l = str((pair or {}).get("landsat_date") or "").replace("-", "")
    s = str((pair or {}).get("sentinel2_date") or "").replace("-", "")
    return {"landsat": l, "sentinel2": s} if (l and s) else {}


def build_experiment_record(exp_state: dict, conv_id: str, project_id: str,
                            status: str, failure_stage: str = "",
                            failure_message: str = "") -> dict:
    """把累加器组装为结构化实验记录（新增字段仅在非空时写入）。"""
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
    # 影像获取方式：月度合成 / 配对模式（月度模式的伪配对带 composite="monthly"）。
    # 显式顶层字段，与旧记录（pair.composite）语义一致，查询与展示直接使用。
    _pair = record["pair"]
    record["acquisition_mode"] = (
        "monthly" if str(_pair.get("composite") or "") == "monthly" else "pair"
    )
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

    特殊处理：
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
        # 气泡文案统一数字两侧空格（如「第1轮」→「第 1 轮」），幂等
        _emit_accumulator.append(presentation.normalize_number_spacing(text))
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

    i = 0
    while i < total:
        step = steps[i]
        skill_name = step["skill"]
        skill = agent.registry.get(skill_name)
        ctx.step_index = i

        # ── 每对影像独立目录：按当前已选配对动态解析路径 ──
        # 搜索阶段无配对时用项目级目录；用户选择配对后（params.selected_pair 已注入）
        # 自动切换为该配对的独立目录，同一计划内的所有后续步骤随之生效。
        _sel_pair = None
        for _s in steps:
            if _s.get("skill") == "data_acquisition":
                _sp = _s.get("params") or {}
                if _sp.get("selected_pair"):
                    _sel_pair = _sp["selected_pair"]
                break
        if _sel_pair:
            raw_dir, processed_dir, results_dir = pair_dirs(project_dir, _sel_pair)
            ctx.raw_dir, ctx.processed_dir, ctx.results_dir = raw_dir, processed_dir, results_dir
        SKILL_PATHS = build_skill_paths(raw_dir, processed_dir, results_dir,
                                        dates=pair_dates(_sel_pair), project_dir=project_dir)

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

        # lst_gapfill：输入必须是已存在的 10m LST 产品。SKILL_PATHS 里的默认名
        # （不带日期）通常不存在，从项目目录递归找最新的 rf_10m_lst_final_*.tif
        # （排除已填补的 _filled 产物），输出放到同目录并带日期命名。
        if skill_name == "lst_gapfill" and project_dir:
            _in = step.get("params", {}).get("input_tif", "")
            if not _in or not os.path.isfile(_in):
                _cands = sorted(
                    glob.glob(os.path.join(project_dir, "**", "rf_10m_lst_final_*.tif"),
                              recursive=True),
                    key=lambda p: os.path.getmtime(p), reverse=True)
                _cands = [c for c in _cands if "_filled" not in os.path.basename(c)]
                if _cands:
                    _src = _cands[0].replace("\\", "/")
                    _dir = os.path.dirname(_src)
                    _base = os.path.basename(_src)
                    params = step.setdefault("params", {})
                    params["input_tif"] = _src
                    params["output_dir"] = _dir
                    params["output_tif"] = os.path.join(
                        _dir, _base.replace("_final_", "_final_filled_")).replace("\\", "/")
                    params["output_mask"] = os.path.join(
                        _dir, _base.replace("_final_", "_final_filled_")
                        .replace(".tif", "_cloud_mask.tif")).replace("\\", "/")
                    # 只填研究区矢量范围内的空洞：优先复用该影像对生成时记录的研究区
                    #（region_study_area.json，保证与产品同一区域），再回退项目根记录，
                    # 最后回退计划里的研究区
                    _rg = ""
                    for _cand in (os.path.join(os.path.dirname(_dir),
                                               "region_study_area.json"),
                                  os.path.join(project_dir, "region_study_area.json")):
                        try:
                            with open(_cand, encoding="utf-8") as _f:
                                _rg = str(json.load(_f).get("study_area_file") or "")
                            if _rg and os.path.isfile(_rg):
                                break
                            _rg = ""
                        except Exception:
                            _rg = ""
                            continue
                    if not _rg and isinstance(plan.get("region"), dict):
                        _rg = str(plan["region"].get("study_area_file") or "")
                    if _rg and os.path.isfile(_rg):
                        params["region_geojson"] = _rg
                else:
                    # 项目里确实还没有 10m LST 结果：保留原参数，让 skill 报"缺少输入"
                    _emit(f"  未找到已有的 10m 地表温度产品（{skill_name} 需要先导出 LST）\n")

        # 注入用户配置参数（不覆盖 LLM 已指定的值）
        if skill_name == "data_acquisition":
            _cfg = agent._load_config(settings_path).get("data", {})
            if "cloud_threshold" not in step.get("params", {}):
                step.setdefault("params", {})["cloud_threshold"] = _cfg.get("cloud_threshold", 30)
            if "dem_source" not in step.get("params", {}):
                step.setdefault("params", {})["dem_source"] = _cfg.get("dem_source", "copernicus")

        # data_pipeline：注入研究区 GeoJSON，让像元占比统计按研究区多边形口径计算
        # （无研究区文件时跳过，预处理回退 bbox 口径）
        if skill_name == "data_pipeline":
            _region_file = ""
            if isinstance(plan.get("region"), dict):
                _region_file = str(plan["region"].get("study_area_file") or "")
            if not (_region_file and os.path.isfile(_region_file)):
                _region_file = agent._find_study_area_file(
                    study_areas_dir,
                    preferred_name=str((plan.get("region") or {}).get("name") or "")) or ""
            if _region_file and not step.get("params", {}).get("study_area_geojson"):
                step.setdefault("params", {})["study_area_geojson"] = _region_file
            # 记录本影像对实际使用的研究区文件（region_study_area.json）：
            # 后续「单步结果后处理（空洞填补）」必须复用同一研究区，否则按「最新上传」
            # 取研究区会把别的区域多边形（如鄂州）套到本产品（如武汉）上，导致填洞
            # 只在错误区域内进行、真实像元全部被置 NoData。单步结果后处理本身不写。
            try:
                if str(plan.get("intent") or "") != "postprocess":
                    _pair_root = os.path.dirname(results_dir) if results_dir else project_dir
                    if _pair_root:
                        os.makedirs(_pair_root, exist_ok=True)
                        with open(os.path.join(_pair_root, "region_study_area.json"),
                                  "w", encoding="utf-8") as _f:
                            json.dump({
                                "study_area_file": _region_file,
                                "name": str((plan.get("region") or {}).get("name") or ""),
                            }, _f, ensure_ascii=False)
            except Exception:
                pass

        if not skill:
            results.append(f"未找到技能: {skill_name}")
            _emit(presentation.skill_missing(skill_name))
            i += 1
            continue

        # 强制 data_acquisition 的 region 使用已上传研究区文件（执行期兜底：
        # 即使 _normalize_plan_paths 的替换未生效，也保证 region 是 GeoJSON 绝对路径，
        # 屏蔽 LLM 生成的纯城市名/bbox 导致的解析崩溃）。
        # plan 带 region.study_area_file 时以它为准。
        if skill_name == "data_acquisition":
            _planned = ""
            if isinstance(plan.get("region"), dict):
                _planned = str(plan["region"].get("study_area_file") or "")
            _sa = _planned or agent._find_study_area_file(study_areas_dir)
            if _sa:
                step.setdefault("params", {})["region"] = _sa

        # 步骤之间用留白（不画分割线）：分割线只用于区分**步骤内部**的内容块
        # （如调优各轮之间），步骤之间用比段落略大的垂直留白隔开
        _emit(presentation.step_gap())
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
            # 上一步/上一轮释放但被 glibc malloc 保留的空闲堆先归还 OS：
            # 大研究区长流程（预处理 → TTRI → RF 调优 → TCR）若不清，RSS 会
            # 停在峰值附近，在 WSL2 配额（20GB）下触发 OOM 杀进程（exit 137）。
            release_rss_memory()
            # ── 执行 Skill（data_acquisition 特殊处理：搜索→选择→下载）─
            try:
                def _log(tag, msg):
                    # 日志行统一格式：`[INFO]`/`[WARN]` 后固定 1 空格；
                    # 消息若带 2 空格前缀视为子信息，统一保留为 2 空格层级缩进。
                    # 异常消息里的换行折叠为空格（如 CDSE 504 的整段 HTML），
                    # 保证每条日志只占一行、不破坏面板排版。
                    text = re.sub(r"\s*\n\s*", " ", str(msg or "")).strip()
                    if text.startswith("  "):
                        text = "  " + text[2:].lstrip()
                    _emit(f"  [{tag}] {text}\n", to_log=True)

                def _progress(name, pct, msg):
                    _emit(f"  {name} {int(pct*100)}%: {msg}\n", to_log=True)

                # data_acquisition: 先搜索返回配对，用户选择后再下载
                if skill_name == "data_acquisition":
                    exp_state["acq_params"] = step.get("params", {})
                    if step.get("params", {}).get("selected_pair"):
                        # 下载模式：selected_pair 已注入（月度伪配对 continue 重入，
                        # 或配对模式用户选择后的二次调用），直接执行下载。
                        # 月度模式在 data_acquisition 内部走 _download_monthly 合成分支。
                        result = skill.execute(
                            step.get("params", {}),
                            progress_callback=_progress,
                            log_callback=_log,
                        )
                    else:
                        # 第一次：搜索模式
                        result = skill.execute(
                            step.get("params", {}),
                            progress_callback=_progress,
                            log_callback=_log,
                        )
                        result_data = result.data if isinstance(result.data, dict) else {}
                        # 复用第一次搜索的目录结果：下载模式（月度 continue 重入、
                        # 配对选择后的二次调用）直接消费，避免下载前重复 STAC 查询
                        _cached_search = {
                            "landsat": result_data.get("landsat_items", []),
                            "sentinel": result_data.get("sentinel2_items", []),
                        }
                        if _cached_search["landsat"] or _cached_search["sentinel"]:
                            step["params"]["cached_search"] = _cached_search
                        # 月度合成模式：跳过配对选择，用该月全部景直接进入下载（合成）。
                        # 用一个「代表日（月末日）」伪配对确定产物目录/文件名，与下游同构。
                        if str(step.get("params", {}).get("composite") or "") == "monthly":
                            _rep_date = str(step.get("params", {}).get("end_date") or "")[:10]
                            if _rep_date:
                                _pseudo = {"landsat_date": _rep_date, "sentinel2_date": _rep_date,
                                           "composite": "monthly"}
                                step["params"]["selected_pair"] = _pseudo
                                exp_state["pair"] = _pseudo
                                raw_dir, processed_dir, results_dir = pair_dirs(project_dir, _pseudo)
                                ctx.raw_dir, ctx.processed_dir, ctx.results_dir = (
                                    raw_dir, processed_dir, results_dir)
                                SKILL_PATHS = build_skill_paths(
                                    raw_dir, processed_dir, results_dir,
                                    dates=pair_dates(_pseudo), project_dir=project_dir)
                                for _k, _v in SKILL_PATHS.get("data_acquisition", {}).items():
                                    step["params"][_k] = _v
                                _emit("  " + presentation.monthly_composite_started(
                                    result_data.get("landsat_count", 0),
                                    result_data.get("sentinel_count", 0),
                                    month=presentation.month_label(
                                        step.get("params", {}).get("end_date"))))
                                continue  # 重新执行（下载模式 + composite=monthly）
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
                            # 注入选择，重新执行下载（切换为该配对的独立目录）
                            step["params"]["selected_pair"] = selected
                            exp_state["pair"] = selected
                            raw_dir, processed_dir, results_dir = pair_dirs(project_dir, selected)
                            ctx.raw_dir, ctx.processed_dir, ctx.results_dir = (
                                raw_dir, processed_dir, results_dir)
                            SKILL_PATHS = build_skill_paths(
                                raw_dir, processed_dir, results_dir,
                                dates=pair_dates(selected), project_dir=project_dir)
                            for _k, _v in SKILL_PATHS.get("data_acquisition", {}).items():
                                step["params"][_k] = _v
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
                elif skill_name == "lst_gapfill" and hooks is not None \
                        and getattr(hooks, "eval_agent", None) is not None:
                    # 结果后处理：执行交给结果 Agent（EvalAgent）——气泡的开始/完成提示、
                    # 填洞进度日志与研究区矢量范围限定都由它负责；输入/输出路径由
                    # 上方的动态查找分支解析进 step.params。
                    hooks.eval_agent._run_gapfill(ctx, params=step.get("params", {}))
                    _gf_ok = bool((ctx.exp_state.get("step_success") or {})
                                  .get("lst_gapfill"))
                    result = SkillResult(
                        _gf_ok, "结果后处理完成" if _gf_ok else "结果后处理未完成")
                else:
                    # 普通执行或其他 Skill
                    result = skill.execute(
                        step.get("params", {}),
                        progress_callback=_progress,
                        log_callback=_log,
                    )

                results.append(f"{skill_name}: {result.message}")
                # 摘要用项目符号与步骤标题/说明形成列表结构。
                # 调优轮（rf_model 非首轮）与结果后处理（lst_gapfill）不再重复输出
                # 摘要：分别由训练 Agent / 结果 Agent 输出进度与完成提示。
                # 步骤之间不再输出分割线（改用 step_gap 留白，见步骤头部），
                # 分割线只用于调优各轮之间（见下方 RETRY 分支）。
                if skill_name == "rf_model" and hooks is not None \
                        and exp_state.get("rf_data") is not None:
                    pass
                elif skill_name == "lst_gapfill" and hooks is not None:
                    pass
                else:
                    _emit("  - " + presentation.summarize(
                        skill_name, result, pair=exp_state.get("pair")) + "\n")
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
                        # 与训练 Agent 的调优公告/规则说明同一层级：项目符号对齐
                        # 「- 模型训练（第N轮）完成：…」；轮间分割线由训练 Agent
                        # 在每轮「开始调优训练」公告前输出（见 train_agent._emit_block）
                        _emit("  - " + decision.reason + "\n")
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

    # ── 收尾：判定整体状态并写入实验记录（聚合判定）──
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
