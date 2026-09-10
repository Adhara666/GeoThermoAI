"""按节点调用原生产实现。文件交接显式引用，算法可写输入先私有复制。"""
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from . import acquisition
from .worker import write_json


def _params(spec):
    return acquisition.values(spec["snapshot"])


def _private_workspace(context, output, node_type):
    """只读文件使用符号链接，所有原地写入目标使用私有副本。

    第五阶段：把输入映射清单写入暂存根（inputs.json），供产物发布时
    区分「输入别名/只读副本」与「本节点真实输出」（§10.1：提交清单
    只包含本节点真实输出，不把上游输入再作为新输出复制一次）。
    ttri 的可写副本（原地改写后的表）标记为 writable_copy——它们是
    本节点的真实输出，发布时保留。
    """
    manifest = {}
    writable = {"for_train/train.parquet", "for_train/validate.parquet", "for_train/test.parquet", "30m_constraint_grid.parquet"} if node_type == "ttri" else set()
    for rel, source in context.get("files", {}).items():
        target = output / rel
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("输入相对路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel in writable:
            shutil.copyfile(source, target)
            manifest[rel] = {"kind": "writable_copy"}
        elif rel == "run_manifest.json":
            shutil.copyfile(source, target)
            manifest[rel] = {"kind": "compat_alias"}
        else:
            try:
                target.symlink_to(Path(source).resolve())
                manifest[rel] = {"kind": "symlink"}
            except OSError:
                shutil.copyfile(source, target)
                manifest[rel] = {"kind": "copy"}
    try:
        (output.parent / "inputs.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
    except OSError:
        # 清单写失败：发布侧仍有内建排除（符号链接、run_manifest.json、
        # .writing 临时文件），仅普通输入副本无法区分的极端情况不发布
        pass
    return manifest


def _files(root):
    return {p.relative_to(root).as_posix(): str(p.resolve()) for p in root.rglob("*") if p.is_file() and not p.name.endswith((".lock", ".writing"))}


def _scale(context):
    """文件元数据探针在子进程预算内完成，不向主服务传数组。"""
    result = dict(context.get("scale", {}))
    files = context.get("files", {})
    result["file_bytes"] = sum(Path(p).stat().st_size for p in files.values())
    table_bytes = 0
    sample_bytes = 0
    constraint_bytes = 0
    for rel, path in files.items():
        if not rel.endswith(".parquet"):
            continue
        import pyarrow.parquet as pq
        metadata = pq.read_metadata(path)
        size = sum(metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups))
        table_bytes += size
        if rel.startswith("for_train/"):
            sample_bytes += size
        if rel == "for_train/train.parquet":
            result["train_rows"] = metadata.num_rows
        if rel == "30m_constraint_grid.parquet":
            constraint_bytes = size
    if table_bytes:
        result.update(table_bytes=table_bytes, sample_bytes=sample_bytes, constraint_bytes=constraint_bytes)
    for key in ("sentinel2_path", "landsat_path"):
        raw = context.get("raw_paths", {}).get(key)
        if raw:
            from osgeo import gdal
            ds = gdal.Open(raw)
            if ds:
                result.update(width=ds.RasterXSize, height=ds.RasterYSize, pixels=ds.RasterXSize * ds.RasterYSize, bands=ds.RasterCount)
                ds = None
                break
    return result


def _raw_check(context):
    from osgeo import gdal
    paths = context.get("raw_paths", {})
    if context.get("input_type") != "download_subset":
        missing = {"landsat_path", "qa_path", "sentinel2_path", "scl_path", "dem_path"} - paths.keys()
        if missing:
            raise ValueError(f"原始包缺少 {sorted(missing)}")
    facts = {}
    for key, path in paths.items():
        if not Path(path).is_file() or Path(path).stat().st_size == 0:
            raise ValueError(f"输入文件缺失 {key}")
        ds = gdal.Open(path)
        if ds is None or not ds.GetProjection() or min(ds.RasterXSize, ds.RasterYSize) <= 0:
            raise ValueError(f"栅格空间契约不合格 {key}")
        if key == "sentinel2_path" and ds.RasterCount != 5:
            raise ValueError("Sentinel 2 必须包含原有五个波段")
        ds.GetRasterBand(1).ReadRaster(0, 0, min(32, ds.RasterXSize), min(32, ds.RasterYSize))
        facts[key] = {"width": ds.RasterXSize, "height": ds.RasterYSize, "bands": ds.RasterCount}
        ds = None
    return facts


def _train_decision(spec, context, report):
    from core.agent.roles.train_agent import TrainAgent
    from core.agent.reflection import train_rules
    from core.agent.orchestrator.hooks import StepDecision
    from core.agent.orchestrator.exec_mode import normalize
    from core.skills.skill_registry import SkillRegistry
    from core.skills.builtin.rf_model import RFModelSkill
    from core.ai_assistant import GeoThermoAI_Assistant
    params = _params(spec)
    execution = spec["snapshot"].get("execution", {})
    settings_path = execution.get("settings_path")
    api = json.loads(Path(settings_path).read_text(encoding="utf-8")).get("api", {}) if settings_path else {}
    assistant = GeoThermoAI_Assistant(**{k: api[k] for k in ("model_type", "api_key", "api_base_url", "model_id", "api_format") if k in api}) if api else None
    registry = SkillRegistry()
    registry.register(RFModelSkill())

    class ManagedTrain(TrainAgent):
        def _finalize(self, ctx, record):
            # 保留原选优口径，只登记引用；不执行复制、mtime 调整或目录清理。
            self.finalized = True
            self.selected = record or self._best()

        def _llm_decision(self, ctx, record):
            return super()._llm_decision(ctx, record) if assistant is not None else None

    train = ManagedTrain(assistant, registry, on_log=report, max_rounds=params.get("tuning_max_rounds", 5))
    state = context.get("train_state", {})
    for key in ("tuning_started", "decided_no_tuning", "manual_params", "ai_rounds", "_round_kind"):
        if key in state:
            setattr(train, key, state[key])
    train.rounds = list(context.get("rounds", []))
    node_params = json.loads(spec["node"]["params"])
    answer = node_params.get("execution_answer")
    hooks = SimpleNamespace(exec_mode=normalize(params.get("exec_mode")), run_state=None, ranked_pairs=[],
                            ask=lambda payload: answer)
    # TrainAgent._ask 调用 hooks._ask，返回 None 就立刻落持久问题，绝不阻塞。
    hooks._ask = lambda payload: answer
    ctx = SimpleNamespace(project_dir="", results_dir=context.get("workspace", ""), exp_state={},
                          data_features=context.get("data_features", {}), emit=report)
    result = train.on_trained(SimpleNamespace(data=context["rf_data"]), ctx, hooks)
    train.rounds[-1]["output_dir"] = context["rf_directory"]
    persisted = {key: getattr(train, key) for key in ("tuning_started", "decided_no_tuning", "manual_params", "ai_rounds", "_round_kind")}
    if result.action == StepDecision.PAUSE:
        return {"question": {"prompt": result.reason, "candidates": result.payload.get("options", []), "payload": result.payload}}
    if result.action in (StepDecision.ABORT, StepDecision.REPLAN):
        raise ValueError(result.reason or "本任务需要修改后建立替代运行")
    context.update(rounds=train.rounds, train_state=persisted)
    if result.action == StepDecision.RETRY:
        params_new = {k: v for k, v in result.new_params.items() if k in ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf", "max_features", "random_state")}
        decision = {"action": "adjust", "new_params": params_new, "reason": result.reason}
    else:
        best = getattr(train, "selected", None) or train_rules.best_round(train.rounds)
        if not best:
            raise ValueError("没有可以采用的有效模型轮次")
        context["best_round"] = best
        decision = {"action": "accept", "reason": result.reason}
    return {"context": context, "decision": decision}


def execute_node(spec, report, cancel):
    from .transfer import configure
    configure(spec["claim"]["connections"] or 1, spec["claim"]["disk"], spec["disk_margin"], cancel)
    node_type = spec["node"]["node_type"]
    context = dict(spec["context"])
    stage = Path(spec["staging_dir"])
    params = _params(spec)
    if node_type == "rebuild":
        rebuild_skill = json.loads(spec["node"]["params"]).get("rebuild_skill")
        node_type = {"data_pipeline": "preprocess_split", "ttri_compute": "ttri",
                     "rf_model": "rf_round", "tcr_compute": "tcr",
                     "lst_export": "export", "accuracy_eval": "closure_eval"}.get(rebuild_skill, "")
        if not node_type:
            raise ValueError("重建节点技能不在允许的科学步骤内")
    execution = spec["snapshot"].get("execution", {})
    # 已由程序绑定的冻结原始包，既可用于合法本地输入，也用于同输入科学回归。
    bound_raw = execution.get("raw_paths")
    if bound_raw and node_type in ("search_scene", "select_scene", "acquire_asset", "prepare_local"):
        for key, path in bound_raw.items():
            if not Path(path).is_file():
                raise ValueError(f"冻结输入缺失 {key}")
        if node_type == "acquire_asset":
            from .transfer import file_hash
            expected = execution.get("raw_hashes", {})
            for key, path in bound_raw.items():
                if expected.get(key) and file_hash(path) != expected[key]:
                    raise ValueError(f"冻结输入内容不符 {key}")
        context.update(raw_paths=bound_raw, input_type="complete_lst", raw_dir=str(Path(next(iter(bound_raw.values()))).parent))
        context["scale"] = _scale(context)
        result = {"context": context, "message": "使用已绑定的本地冻结输入"}
        if node_type == "select_scene":
            result["selection"] = {"source": "frozen_local", "hashes": execution.get("raw_hashes", {}), "scene_ids": execution.get("scene_ids", {})}
        return result
    if node_type == "search_scene":
        return acquisition.search(spec, report)
    if node_type == "select_scene":
        return acquisition.select(spec)
    if node_type == "acquire_asset":
        return acquisition.acquire(spec, report)
    if node_type == "prepare_local":
        result = acquisition.prepare(spec, report)
        result["context"]["scale"] = _scale(result["context"])
        return result
    if node_type == "data_check":
        if not context.get("raw_paths") and context.get("assets_path"):
            from osgeo import gdal
            package = json.loads(Path(context["assets_path"]).read_text())
            for a in package["assets"]:
                if a["required"] and gdal.Open(a["path"]) is None:
                    raise ValueError("下载子集有不可读的必需资产")
            return {"context": {**context, "input_type": "download_subset"}, "message": "指定下载子集通过验证"}
        facts = _raw_check(context)
        context["scale"] = _scale(context)
        return {"context": context, "data_facts": facts}
    if node_type == "train_decision":
        return _train_decision(spec, context, report)
    if node_type == "promote_best":
        best = context.get("best_round")
        if not best or not Path(best["raw"]["model_path"]).is_file():
            raise ValueError("最佳轮模型缺失，禁止使用最近模型兜底")
        context["model_path"] = best["raw"]["model_path"]
        context["rf_data"] = best["raw"]
        return {"context": context, "message": "已明确绑定最佳轮模型引用，文件仍在暂存区"}
    output = stage / "work"
    output.mkdir()
    _private_workspace(context, output, node_type)
    from core.pipeline import EasyLSTPipeline
    pipeline = EasyLSTPipeline()
    pipeline.configure(**{k: params[k] for k in ("train_ratio", "val_ratio", "test_ratio", "seed", "block_size_px", "guard_buffer_m", "batch_size", "chunk_size", "step2_min_valid_samples", "tcr_mode") if k in params},
                       output_dir=str(output), rf_params=params.get("rf_params"), execution_budget=spec["claim"],
                       **context.get("raw_paths", {}))
    if context.get("model_path"):
        pipeline.configure(_model_path=context["model_path"])
    pipeline.configure(_tcr_mode_used=params.get("tcr_mode", "block_constant"))
    groups = {"preprocess_split": ("preprocessing", "split_dataset"),
              "ttri": ("ttri_train", "ttri_predict"), "rf_round": ("train_rf", "predict_test"),
              "tcr": ("tcr", "lst_final"), "export": ("export_geotiff",), "closure_eval": ("evaluate_closure",)}
    if node_type in groups:
        if node_type == "rf_round":
            patch = json.loads(spec["node"]["params"]).get("rf_params", {})
            pipeline.configure(rf_params={**(params.get("rf_params") or {}), **patch})
            # 每轮输出独立，绝不让前一轮的模型或 metrics 路径参与覆盖。
            for rel in list(context.get("files", {})):
                if rel.startswith(("results/train/", "results/test/")):
                    (output / rel).unlink()
        completed = {}
        for step in groups[node_type]:
            report("开始节点内步骤", step)
            completed[step] = pipeline.run_step(step, report, report)
        if node_type == "rf_round":
            train, test = completed["train_rf"], completed["predict_test"]
            context.update(model_path=train["model_path"], rf_directory=str(output / "results"), rf_data={
                "model_path": train["model_path"], "metrics_path": train["metrics_path"],
                "train_metrics": train["metrics"], "test_metrics": test["metrics"],
                "params": train["params"], "features": train["features"], "feature_importance": train["feature_importance"]})
        if node_type == "preprocess_split":
            context["pipeline_data"] = completed["preprocessing"]
        context["step_results"] = {**context.get("step_results", {}), **completed}
    elif node_type in ("prep_check", "ttri_check"):
        from core.agent.reflection import data_rules
        import pyarrow.parquet as pq
        split = json.loads((output / "for_train/split_info.json").read_text())
        data = dict(context["pipeline_data"])
        data["split_stats"] = {k: {"count": v} for k, v in split["counts"].items()}
        problems = data_rules._check_d3(data) + data_rules._check_d4(data) + data_rules._check_d5(data)
        problems += data_rules._check_d7(data, str(output), data_rules.default_meta_probe)
        if node_type == "ttri_check":
            problems += data_rules._check_d6(str(output / "for_train"), data_rules.default_csv_probe)
        if problems:
            raise ValueError("；".join(problems))
        for name in ("train", "validate", "test"):
            if pq.read_metadata(output / "for_train" / f"{name}.parquet").num_rows <= 0:
                raise ValueError("划分结果为空")
    elif node_type == "gapfill":
        from core.gapfill import gapfill_lst
        source = context.get("main_tif") or execution.get("main_tif")
        if not source:
            raise ValueError("填洞缺少明确绑定的主产品")
        target = output / "lst_filled.tif"
        gapfill_lst(source, str(target), str(output / "gapfill_mask.tif"), max_level=params.get("max_level", 6), region_geojson=acquisition.region_path(spec["snapshot"]), progress_callback=report)
        context["filled_tif"] = str(target)
    else:
        raise ValueError(f"未知执行节点 {node_type}")
    context.update(files=_files(output), workspace=str(output))
    context["scale"] = _scale(context)
    if node_type == "export":
        context["main_tif"] = pipeline.get_default_paths()["lst_final_tif"]
    # 在工作进程内验证返回文件仍可打开。控制器不读取科学数组。
    for rel, path in context["files"].items():
        if not Path(path).is_file():
            raise ValueError(f"节点输出缺失 {rel}")
    return {"context": context, "message": f"{node_type} 已在独立尝试暂存区完成"}
